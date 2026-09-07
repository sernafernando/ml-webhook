"""Reclamos y devoluciones, para saber POR QUE se cancelo una venta.

Sin esto, un comprador que se arrepintio antes de despachar y una devolucion con
el producto ya en la calle se ven identicos, y para el neto no son lo mismo.

Se proyecta: el reclamo trae players[] con el user_id del comprador, y para
saber el motivo y el estado no hace falta saber quien reclamo.
"""
import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


RECLAMO_CRUDO = {
    "id": 5569852589,
    "type": "returns",
    "stage": "claim",
    "status": "opened",
    "reason_id": "PDD9939",
    "resolution": None,
    "resource": "order",
    "resource_id": 2000018128435014,
    "date_created": "2026-09-01T17:51:28.000-04:00",
    "last_updated": "2026-09-01T17:53:02.000-04:00",
    "fulfilled": True,
    "quantity_type": "total",
    "claimed_quantity": 1,
    "parent_id": None,
    "site_id": "MLA",
    # Esto no debe salir: identifica al comprador y no hace falta.
    "players": [
        {"role": "complainant", "type": "buyer", "user_id": 204690943,
         "available_actions": []},
        {"role": "respondent", "type": "seller", "user_id": 413658225,
         "available_actions": [{"action": "refund"}]},
    ],
    "related_entities": ["order"],
}


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def busqueda(monkeypatch):
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append(url)
        return _Resp(payload={"data": [RECLAMO_CRUDO],
                              "paging": {"total": 1, "limit": 50, "offset": 0}})

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


@pytest.fixture
def detalle(monkeypatch):
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append(url)
        return _Resp(payload=RECLAMO_CRUDO)

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


BUSQUEDA = "/post-purchase/v1/claims/search?status=opened&limit=50"
DETALLE = "/post-purchase/v1/claims/5569852589"


def test_acepta_la_busqueda(client, busqueda):
    res = client.get("/api/ml/claims", query_string={"resource": BUSQUEDA})

    assert res.status_code == 200
    assert busqueda[0] == f"https://api.mercadolibre.com{BUSQUEDA}"
    assert res.get_json()["paging"]["total"] == 1


def test_acepta_el_detalle(client, detalle):
    res = client.get("/api/ml/claims", query_string={"resource": DETALLE})

    assert res.status_code == 200
    assert detalle[0] == f"https://api.mercadolibre.com{DETALLE}"


def test_proyecta_motivo_estado_y_a_que_orden_aplica(client, detalle):
    d = client.get("/api/ml/claims", query_string={"resource": DETALLE}).get_json()

    assert d["type"] == "returns"
    assert d["stage"] == "claim"
    assert d["status"] == "opened"
    assert d["reason_id"] == "PDD9939"
    assert d["resource"] == "order"
    assert d["resource_id"] == 2000018128435014
    assert d["claimed_quantity"] == 1
    assert d["date_created"].startswith("2026-09-01")


def test_no_filtra_al_comprador(client, detalle):
    crudo = client.get("/api/ml/claims",
                       query_string={"resource": DETALLE}).get_data(as_text=True)

    assert "204690943" not in crudo
    assert "players" not in crudo
    assert "complainant" not in crudo


def test_la_busqueda_tambien_proyecta_cada_reclamo(client, busqueda):
    crudo = client.get("/api/ml/claims",
                       query_string={"resource": BUSQUEDA}).get_data(as_text=True)

    assert "204690943" not in crudo
    d = client.get("/api/ml/claims", query_string={"resource": BUSQUEDA}).get_json()
    assert d["data"][0]["reason_id"] == "PDD9939"
    assert "players" not in d["data"][0]


@pytest.mark.parametrize("resource", [
    "/post-purchase/v1/claims/5569852589/messages",   # mensajes con el comprador
    "/post-purchase/v1/claims/5569852589/attachments",
    "/post-purchase/v1/claims/abc",
    "/post-purchase/v1/claims",
    "/orders/2000018128435014",
    "/users/413658225",
])
def test_rechaza_lo_que_no_es_un_reclamo(client, monkeypatch, resource):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert client.get("/api/ml/claims",
                      query_string={"resource": resource}).status_code == 400


def test_no_acepta_post(client):
    assert client.post("/api/ml/claims",
                       query_string={"resource": DETALLE}).status_code == 405
