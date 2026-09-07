"""/api/ml/orders — lectura acotada de ordenes y envios para la ingesta.

Es la ruta pensada para consumidores de ingesta (pricing-app). A diferencia de
/api/ml/render, no renderiza HTML, no acepta recursos arbitrarios y no depende
de la allowlist ancha que render usa en modo observacion.
"""
import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None, content=b"{}", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers if headers is not None else {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def ml_calls(monkeypatch):
    """Captura las llamadas salientes y devuelve un JSON fijo."""
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append({"url": url, "headers": headers or {}})
        return _Resp(payload={"ok": True})

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


# =====================================================================
# Los tres recursos que la ingesta necesita
# =====================================================================

@pytest.mark.parametrize("resource", [
    "/orders/search?seller=123&order.date_last_updated.from=2026-01-01T00:00:00.000-00:00&offset=50",
    "/orders/2000012345",
    "/shipments/44556677",
])
def test_acepta_los_recursos_de_la_ingesta(client, ml_calls, resource):
    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 200
    assert res.is_json
    assert res.get_json() == {"ok": True}
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"
    assert ml_calls[0]["headers"]["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"


# =====================================================================
# Todo lo demas se rechaza sin salir a la red
# =====================================================================

@pytest.mark.parametrize("resource", [
    "/items/MLA123",                      # legitimo para render, fuera de alcance aca
    "/users/123",
    "/orders/search/../../users/123",
    "/orders/2000012345/feedback",
    "/orders/abc",                        # id no numerico
    "/shipments/44556677/history",
    "/oauth/token",
    "@atacante.tld/orders/search",        # el host real seria atacante.tld
    "/orders/\n2000012345",               # caracteres de control
    "orders/search",                      # sin '/' inicial
    "",
])
def test_rechaza_todo_lo_que_no_sea_la_ingesta(client, monkeypatch, resource):
    def _forbidden(*args, **kwargs):
        raise AssertionError("No debe salir ninguna request")

    monkeypatch.setattr(app_module, "ml_api_get", _forbidden)

    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 400
    assert res.is_json
    assert res.get_json()["error"]


def test_sin_resource_devuelve_400_json(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    res = client.get("/api/ml/orders")

    assert res.status_code == 400
    assert res.is_json
    assert res.get_json()["error"]


def test_el_resource_no_se_filtra_en_la_respuesta_de_error(client, monkeypatch):
    """El error no debe reflejar el resource crudo: seria un XSS reflejado si un
    consumidor lo pinta, y le confirma al atacante que su payload llego."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    res = client.get("/api/ml/orders",
                     query_string={"resource": "/orders/<script>alert(1)</script>"})

    assert res.status_code == 400
    assert "<script>" not in res.get_data(as_text=True)


# =====================================================================
# Contrato de respuesta: siempre JSON, y el status de ML se preserva
# =====================================================================

def test_preserva_el_status_de_ml(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(status_code=404, payload={"error": "not_found"}))

    res = client.get("/api/ml/orders", query_string={"resource": "/orders/2000012345"})

    assert res.status_code == 404
    assert res.get_json() == {"error": "not_found"}


def test_respuesta_no_json_de_ml_se_traduce_a_json(client, monkeypatch):
    """La ingesta parsea JSON siempre. Un HTML de error de ML no debe llegarle crudo."""
    monkeypatch.setattr(app_module, "ml_api_get", lambda *a, **k: _Resp(
        status_code=502, payload=None, content=b"<html>bad gateway</html>",
        headers={"content-type": "text/html"}))

    res = client.get("/api/ml/orders", query_string={"resource": "/orders/2000012345"})

    assert res.status_code == 502
    assert res.is_json
    assert res.get_json()["error"]


def test_error_interno_se_traduce_a_json(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("se cayo la red")

    monkeypatch.setattr(app_module, "ml_api_get", _boom)

    res = client.get("/api/ml/orders", query_string={"resource": "/orders/2000012345"})

    assert res.status_code == 502
    assert res.is_json
    assert res.get_json()["error"]


# =====================================================================
# No es un proxy de escritura
# =====================================================================

def test_no_acepta_post(client):
    assert client.post("/api/ml/orders",
                       query_string={"resource": "/orders/2000012345"}).status_code == 405


# =====================================================================
# /shipments/<id>/costs — el desglose con la parte del vendedor separada
# =====================================================================
# La orden trae shipping_cost=null y el shipment trae base_cost, que es el costo
# total. La parte que ML le cobra al vendedor sale de senders[].cost. Inferirla
# como "la mitad de base_cost" es asumir una proporcion que ML puede cambiar.

def test_acepta_el_desglose_de_costos_del_envio(client, ml_calls):
    resource = "/shipments/47925243368/costs"

    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 200
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"


@pytest.mark.parametrize("resource", [
    "/shipments/47925243368/history",   # otros subrecursos siguen afuera
    "/shipments/47925243368/costs/x",
    "/shipments/abc/costs",
    "/shipments//costs",
])
def test_costs_no_abre_el_resto_de_los_subrecursos(client, monkeypatch, resource):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert client.get("/api/ml/orders",
                      query_string={"resource": resource}).status_code == 400


# =====================================================================
# packs, items del envio y descuentos de la orden
# =====================================================================

def test_acepta_el_pack(client, ml_calls):
    """Un pack se reconstruia cruzando pack_id entre las ordenes barridas, asi
    que quedaba incompleto si una hermana caia fuera de la ventana: el costo
    daba mas bajo que el real y sin sintoma. /packs/<id> trae orders[] completo,
    que es lo que distingue 'pack de una orden' de 'pack incompleto'."""
    resource = "/packs/2000014906212865"

    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 200
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"


def test_acepta_los_items_del_envio(client, ml_calls):
    """Trae order_id por item: es la relacion envio-orden autoritativa, en vez
    de inferirla desde las ordenes y arriesgar un doble conteo."""
    resource = "/shipments/47925243368/items"

    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 200
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"


def test_acepta_los_descuentos_de_la_orden(client, ml_calls):
    """Trae supplier.funding_mode y amounts.seller, o sea de que bolsillo sale
    cada descuento."""
    resource = "/orders/2000018265495500/discounts"

    res = client.get("/api/ml/orders", query_string={"resource": resource})

    assert res.status_code == 200
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"


@pytest.mark.parametrize("resource", [
    "/packs/abc",
    "/packs/2000014906212865/orders",
    "/packs/",
    "/shipments/47925243368/items/1",
    "/orders/2000018265495500/discounts/1",
    "/orders/2000018265495500/feedback",   # sigue afuera
    "/orders/2000018265495500/billing_info",
])
def test_los_recursos_nuevos_no_abren_vecinos(client, monkeypatch, resource):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert client.get("/api/ml/orders",
                      query_string={"resource": resource}).status_code == 400


# =====================================================================
# El pack no reparte al comprador
# =====================================================================
# /packs/<id> es el unico recurso de esta ruta cuyo cuerpo trae una clave
# dedicada al comprador. Se saca antes de responder: el mismo criterio que dejo
# players[] fuera de los reclamos. Que un dato no se persista depende de que el
# consumidor se acuerde; que no llegue, no depende de nadie.

PACK_CRUDO = {
    "id": 2000014906212865,
    "status": "cancelled",
    "status_detail": "buyer",
    "orders": [{"id": 2000018325540962, "static_tags": []}],
    "shipment": {"id": 47952444508},
    "family_pack_id": None,
    "trash_pack_id": None,
    "date_created": "2026-09-07T09:16:54.000-0400",
    "last_updated": "2026-09-07T09:21:24.000-0400",
    "buyer": {"id": 2564836509},
}


def test_el_pack_no_devuelve_al_comprador(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PACK_CRUDO))

    res = client.get("/api/ml/orders",
                     query_string={"resource": "/packs/2000014906212865"})
    d = res.get_json()

    assert "buyer" not in d
    assert "2564836509" not in res.get_data(as_text=True)


def test_el_pack_conserva_todo_lo_demas(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=PACK_CRUDO))

    d = client.get("/api/ml/orders",
                   query_string={"resource": "/packs/2000014906212865"}).get_json()

    assert d["orders"] == [{"id": 2000018325540962, "static_tags": []}]
    assert d["shipment"] == {"id": 47952444508}
    # status_detail "buyer" describe QUIEN cancelo, no identifica a nadie.
    assert d["status_detail"] == "buyer"
    assert d["id"] == 2000014906212865


def test_los_demas_recursos_siguen_tal_cual(client, monkeypatch):
    """El filtro es solo para packs: el resto sigue devolviendo el cuerpo de ML
    sin tocar."""
    cuerpo = {"id": 1, "buyer": {"id": 99}}
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=cuerpo))

    d = client.get("/api/ml/orders",
                   query_string={"resource": "/orders/2000018265495500"}).get_json()

    assert d == cuerpo


# =====================================================================
# El desglose de costos tampoco reparte al comprador
# =====================================================================
# /shipments/<id>/costs trae receiver.user_id, que es el comprador. Se saca solo
# ese campo: receiver.cost queda, porque es parte del desglose y hay consumidores
# que lo leen.

COSTS_CRUDO = {
    "gross_amount": 56330,
    "base_exchange": None,
    "receiver": {
        "user_id": 657269857,
        "cost": 0,
        "compensation": 0,
        "discounts": [{"promoted_amount": 25950, "rate": 1, "type": "ratio"}],
        "save": 25950,
    },
    "senders": [{
        "user_id": 413658225,
        "cost": 15190,
        "discounts": [{"promoted_amount": 15190, "rate": 0.5, "type": "mandatory"}],
        "save": 15190,
    }],
}


def test_costs_no_devuelve_el_user_id_del_comprador(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=COSTS_CRUDO))

    res = client.get("/api/ml/orders",
                     query_string={"resource": "/shipments/47925243368/costs"})

    assert "657269857" not in res.get_data(as_text=True)
    assert "user_id" not in res.get_json()["receiver"]


def test_costs_conserva_el_desglose_entero(client, monkeypatch):
    """receiver.cost y senders[].cost son lo que se consume: no se tocan."""
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(payload=COSTS_CRUDO))

    d = client.get("/api/ml/orders",
                   query_string={"resource": "/shipments/47925243368/costs"}).get_json()

    assert d["receiver"]["cost"] == 0
    assert d["receiver"]["save"] == 25950
    assert d["senders"][0]["cost"] == 15190
    assert d["gross_amount"] == 56330
    # El sender somos nosotros: ese user_id no es dato de nadie mas.
    assert d["senders"][0]["user_id"] == 413658225
