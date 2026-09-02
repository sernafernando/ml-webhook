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
    "/shipments/44556677/items",
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
