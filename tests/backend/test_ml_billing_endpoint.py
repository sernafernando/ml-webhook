"""Acceso de lectura a la facturacion de ML para conciliar la liquidacion.

La orden no alcanza para el neto: trae taxes.amount=null, payments[].taxes_amount=0
y fee_details=null. Las retenciones e impuestos solo aparecen en facturacion.

La API de facturacion de ML es distinta del resto: tiene rate limit propio de 5
requests por minuto POR CUENTA, y la doc dice explicitamente que no se use en
batch, que el uso sea secuencial y que alcanza una consulta diaria porque el
dato es estatico durante el dia. Un consumidor que la llame por orden no solo se
rompe a si mismo: se come el limite de la cuenta y deja sin facturacion a todo
lo demas. Por eso esta ruta se throttlea del lado del proxy y no confia en que
el consumidor se porte bien.
"""
import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"results": []}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def sin_throttle_previo(monkeypatch):
    """Cada test arranca con la ventana de throttle limpia."""
    monkeypatch.setattr(app_module, "_billing_last_call", 0.0)


@pytest.fixture
def ml_calls(monkeypatch):
    calls = []

    def _fake_get(url, headers=None, params=None, **kwargs):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(app_module, "ml_api_get", _fake_get)
    return calls


PERIODOS = "/billing/integration/monthly/periods?group=ML&document_type=BILL"
DETALLE = "/billing/integration/periods/key/2026-09-01/group/ML/details?document_type=BILL&limit=50"


@pytest.mark.parametrize("resource", [PERIODOS, DETALLE,
                                      DETALLE.replace("/group/ML/", "/group/MP/")])
def test_acepta_los_recursos_de_facturacion(client, ml_calls, resource):
    res = client.get("/api/ml/billing", query_string={"resource": resource})

    assert res.status_code == 200
    assert ml_calls[0] == f"https://api.mercadolibre.com{resource}"


@pytest.mark.parametrize("resource", [
    "/payments/176106034911",       # API de MP, el token de ML da 404
    "/collections/176106034911",    # existe, pero expone nombre y DNI del comprador
    "/orders/2000018265495500",     # eso va por /api/ml/orders
    "/billing/integration/periods/key/2026-09-01/group/XX/details",
    "/billing/../users/123",
    "/billing/integration/periods/key/2026-09-01/group/ML/details/../../x",
])
def test_rechaza_lo_que_no_es_facturacion(client, monkeypatch, resource):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    res = client.get("/api/ml/billing", query_string={"resource": resource})

    assert res.status_code == 400
    assert res.is_json


def test_el_throttle_protege_el_limite_de_la_cuenta(client, ml_calls):
    """La segunda llamada dentro de la ventana se rechaza en el proxy, sin salir
    a ML: si saliera, gastaria uno de los 5 tokens por minuto de la cuenta."""
    assert client.get("/api/ml/billing",
                      query_string={"resource": DETALLE}).status_code == 200

    res = client.get("/api/ml/billing", query_string={"resource": DETALLE})

    assert res.status_code == 429
    assert res.headers.get("Retry-After")
    assert len(ml_calls) == 1, "la segunda no debe salir a ML"


def test_el_429_es_json_y_reintentable(client, ml_calls):
    client.get("/api/ml/billing", query_string={"resource": DETALLE})
    res = client.get("/api/ml/billing", query_string={"resource": DETALLE})

    assert res.is_json
    assert res.get_json()["error"]
    assert int(res.headers["Retry-After"]) > 0


def test_no_acepta_post(client):
    assert client.post("/api/ml/billing",
                       query_string={"resource": DETALLE}).status_code == 405


# =====================================================================
# summary/details — los totales del periodo
# =====================================================================
# No trae las retenciones impositivas (no viven en facturacion de ML), pero si
# total_amount, total_perception y payment_collected, que son los totales contra
# los que se concilia lo que suma el detalle.

SUMMARY = "/billing/integration/periods/key/2026-09-01/summary/details?group=ML&document_type=BILL"


def test_acepta_el_resumen_del_periodo(client, ml_calls):
    res = client.get("/api/ml/billing", query_string={"resource": SUMMARY})

    assert res.status_code == 200
    assert ml_calls[0] == f"https://api.mercadolibre.com{SUMMARY}"


def test_el_resumen_tambien_esta_throttleado(client, ml_calls):
    """Comparte el limite de 5/min de la cuenta con el resto de facturacion."""
    assert client.get("/api/ml/billing",
                      query_string={"resource": SUMMARY}).status_code == 200

    assert client.get("/api/ml/billing",
                      query_string={"resource": SUMMARY}).status_code == 429
    assert len(ml_calls) == 1


@pytest.mark.parametrize("resource", [
    "/billing/integration/periods/key/2026-09-01/summary",          # sin /details
    "/billing/integration/periods/key/2026-09-01/summary/details/x",
    "/v1/account/settlement_report/list",   # API de Mercado Pago, otro host
])
def test_el_resumen_no_abre_recursos_vecinos(client, monkeypatch, resource):
    monkeypatch.setattr(app_module, "ml_api_get",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert client.get("/api/ml/billing",
                      query_string={"resource": resource}).status_code == 400
