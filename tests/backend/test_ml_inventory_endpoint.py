"""/api/ml/inventory — lectura acotada de stock y reposicion para meli-full-report.

El consumidor necesita stock y ventas de Full pero no puede tener el token de ML
(el refresh_token es de un solo uso y es de ml-webhook). Mismo contrato que
/api/ml/orders: allowlist de patrones completos, JSON siempre, status de ML
preservado y el resource crudo solo al log.
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


SELLER_ID = 413658225


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    monkeypatch.setattr(app_module, "_promos_seller_id", lambda: SELLER_ID)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def ml_calls(monkeypatch):
    """Captura las llamadas salientes y devuelve un JSON fijo."""
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append({"url": url, "headers": headers or {}, "params": params})
        return _Resp(payload={"ok": True})

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


@pytest.fixture
def no_ml(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))


ITEMS_20 = "/items?ids=" + ",".join(f"MLA{1000 + i}" for i in range(20))
ITEMS_21 = "/items?ids=" + ",".join(f"MLA{1000 + i}" for i in range(21))
STOCK = "/user-products/MLAU1234567890/stock"
REPLENISHMENT = "/marketplace/fbm/user-products/MLAU1234567890/replenishment?country=AR"


# =====================================================================
# Los recursos que el reporte de Full necesita
# =====================================================================

@pytest.mark.parametrize("resource", [
    "/items?ids=MLA123",
    "/items?ids=MLA123,MLA456",
    ITEMS_20,
    "/items?ids=MLA123,MLA456&attributes=id,available_quantity,sold_quantity",
    STOCK,
    REPLENISHMENT,
])
def test_acepta_los_recursos_de_inventario(client, ml_calls, resource):
    res = client.get("/api/ml/inventory", query_string={"resource": resource})

    assert res.status_code == 200
    assert res.is_json
    assert res.get_json() == {"ok": True}
    assert len(ml_calls) == 1
    assert ml_calls[0]["url"] == f"https://api.mercadolibre.com{resource}"
    assert ml_calls[0]["headers"]["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"


# =====================================================================
# Todo lo demas se rechaza sin salir a la red
# =====================================================================

@pytest.mark.parametrize("resource", [
    "/items/MLA123",                                  # legitimo para render, no aca
    "/items/MLA123/description",
    "/items?ids=MLA123/../../users/1",
    "/items?ids=MLA123&ids=MLA456",
    "/items?ids=MLA123,",
    "/items?ids=MLB123",                              # otro sitio
    "/items?ids=MLA123&attributes=",
    "/items?ids=MLA123&attributes=id&x=1",
    "/items?ids=MLA123&access_token=robado",
    "/items?ids=MLA١٢٣",               # digitos unicode, no ASCII
    ITEMS_21,                                         # multiget topea en 20
    "/items",
    "/user-products/MLA123/stock",                    # id que no es MLAU
    "/user-products/MLAUabc/stock",
    "/user-products/MLAU123/stock/extra",
    "/user-products/MLAU123",
    "/user-products/MLAU123/stock?x=1",
    "/marketplace/fbm/user-products/MLAU123/replenishment",
    "/marketplace/fbm/user-products/MLAU123/replenishment?country=BR",
    "/marketplace/fbm/user-products/MLAU123/replenishment?country=AR&x=1",
    "/marketplace/fbm/user-products/MLAU123/replenishment?country=AR#x",
    "/marketplace/fbm/user-products/MLA123/replenishment?country=AR",
    "/users/123",
    "/orders/2000012345",
    "@evil.tld/items?ids=MLA123",                     # el host real seria evil.tld
    ".evil.tld/items?ids=MLA123",
    "//evil.tld/items?ids=MLA123",
    "%40evil.tld/items?ids=MLA123",                   # encodings que no se decodifican
    "/items%3Fids=MLA123",
    "/user-products/MLAU123%2Fstock",
    "/items?ids=MLA123\n",                            # newline al final
    "items?ids=MLA123",                               # sin '/' inicial
    " /items?ids=MLA123",
])
def test_rechaza_todo_lo_que_no_sea_inventario(client, no_ml, resource):
    res = client.get("/api/ml/inventory", query_string={"resource": resource})

    assert res.status_code == 400
    assert res.is_json
    assert res.get_json()["error"]


def test_sin_resource_devuelve_400_json(client, no_ml):
    res = client.get("/api/ml/inventory")

    assert res.status_code == 400
    assert res.is_json
    assert res.get_json()["error"]


def test_el_resource_no_se_filtra_en_la_respuesta_de_error(client, no_ml):
    """Reflejarlo seria un XSS si el consumidor lo pinta, y le confirma al
    atacante que su payload llego."""
    hostil = "/items?ids=MLA1<script>alert(1)</script>"

    res = client.get("/api/ml/inventory", query_string={"resource": hostil})

    assert res.status_code == 400
    assert "<script>" not in res.get_data(as_text=True)
    assert "MLA1" not in res.get_data(as_text=True)


def test_no_acepta_post(client):
    assert client.post("/api/ml/inventory",
                       query_string={"resource": STOCK}).status_code == 405


# =====================================================================
# Headers de caller: solo para reposicion
# =====================================================================

def test_replenishment_agrega_los_headers_de_caller(client, ml_calls):
    client.get("/api/ml/inventory", query_string={"resource": REPLENISHMENT})

    h = ml_calls[0]["headers"]
    assert h["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"
    assert h["x-caller-id"] == str(SELLER_ID)
    assert h["x-caller-siteId"] == "MLA"


@pytest.mark.parametrize("resource", ["/items?ids=MLA123", STOCK])
def test_los_demas_recursos_no_llevan_headers_de_caller(client, ml_calls, resource):
    client.get("/api/ml/inventory", query_string={"resource": resource})

    h = ml_calls[0]["headers"]
    assert set(h) == {"Authorization"}


def test_los_headers_del_consumidor_no_llegan_a_ml(client, ml_calls):
    """El consumidor no elige el caller: si manda x-caller-id se ignora."""
    client.get("/api/ml/inventory", query_string={"resource": REPLENISHMENT},
               headers={"x-caller-id": "999", "Authorization": "Bearer otro"})

    h = ml_calls[0]["headers"]
    assert h["x-caller-id"] == str(SELLER_ID)
    assert h["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"


def test_replenishment_sin_seller_id_no_sale_a_ml(client, monkeypatch, no_ml):
    monkeypatch.setattr(app_module, "_promos_seller_id", lambda: None)

    res = client.get("/api/ml/inventory", query_string={"resource": REPLENISHMENT})

    assert res.status_code == 500
    assert res.is_json
    assert res.get_json()["error"]


def test_replenishment_con_la_db_caida_responde_json(client, monkeypatch, no_ml):
    def _boom():
        raise RuntimeError("db caida")

    monkeypatch.setattr(app_module, "_promos_seller_id", _boom)

    res = client.get("/api/ml/inventory", query_string={"resource": REPLENISHMENT})

    assert res.status_code == 500
    assert res.is_json
    assert "db caida" not in res.get_data(as_text=True)


def test_falla_del_token_se_traduce_a_502_json(client, monkeypatch, no_ml):
    def _boom():
        raise RuntimeError("refresh fallido")

    monkeypatch.setattr(app_module, "get_token", _boom)

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 502
    assert res.is_json


def test_stock_no_depende_del_seller_id(client, monkeypatch, ml_calls):
    """Solo reposicion lo necesita: que falle la lectura del seller no debe
    tirar el resto."""
    def _boom():
        raise RuntimeError("db caida")

    monkeypatch.setattr(app_module, "_promos_seller_id", _boom)

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 200


# =====================================================================
# Contrato de respuesta: siempre JSON, status de ML preservado
# =====================================================================

def test_preserva_206_y_reenvia_x_content_missing(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get", lambda *a, **k: _Resp(
        status_code=206, payload={"available_quantity": 3},
        headers={"content-type": "application/json", "x-content-missing": "locations"}))

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 206
    assert res.get_json() == {"available_quantity": 3}
    assert res.headers.get("x-content-missing") == "locations"


def test_sin_x_content_missing_no_inventa_el_header(client, ml_calls):
    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert "x-content-missing" not in res.headers


def test_multiget_pasa_los_codigos_por_item_tal_cual(client, monkeypatch):
    cuerpo = [
        {"code": 200, "body": {"id": "MLA123", "available_quantity": 5}},
        {"code": 404, "body": {"message": "Item with id MLA456 not found"}},
    ]
    monkeypatch.setattr(app_module, "ml_api_get", lambda *a, **k: _Resp(payload=cuerpo))

    res = client.get("/api/ml/inventory", query_string={"resource": "/items?ids=MLA123,MLA456"})

    assert res.status_code == 200
    assert res.get_json() == cuerpo


@pytest.mark.parametrize("status", [404, 429])
def test_preserva_el_status_de_ml(client, monkeypatch, status):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(status_code=status, payload={"error": "x"}))

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == status
    assert res.get_json() == {"error": "x"}


def test_respuesta_no_json_de_ml_se_traduce_a_json(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get", lambda *a, **k: _Resp(
        status_code=503, payload=None, content=b"<html>down</html>",
        headers={"content-type": "text/html"}))

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 503
    assert res.is_json
    assert res.get_json() == {"error": "respuesta no-JSON de ML", "ml_status": 503}


def test_respuesta_no_json_con_200_se_traduce_a_502(client, monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: _Resp(status_code=200, payload=None, content=b""))

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 502
    assert res.get_json()["ml_status"] == 200


def test_falla_de_red_se_traduce_a_502(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("se cayo la red")

    monkeypatch.setattr(app_module, "ml_api_get", _boom)

    res = client.get("/api/ml/inventory", query_string={"resource": STOCK})

    assert res.status_code == 502
    assert res.is_json
    assert res.get_json()["error"]
