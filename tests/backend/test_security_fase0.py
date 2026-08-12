"""Fase 0 de seguridad (docs/plan-autenticacion.md).

Cubre los arreglos que no requieren credenciales ni coordinar con consumidores:
exfiltracion del token via 'resource', allowlist en modo observacion, validacion
del state de OAuth y serializacion del sweep.
"""
import json
import time
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


@pytest.fixture(autouse=True)
def client_secret_fijo(monkeypatch):
    """ML_CLIENT_SECRET sale de .env, que esta gitignoreado. Sin fijarlo aca los
    tests del state pasarian o fallarian segun la maquina."""
    monkeypatch.setattr(app_module, "ML_CLIENT_SECRET", "secreto-de-test")


class _Resp:
    def __init__(self, status_code=200, payload=None, content=b"{}", text="{}", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text
        self.headers = headers if headers is not None else {"content-type": "application/json"}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


@pytest.fixture
def no_network(monkeypatch):
    """Registra toda salida HTTP. Los tests asertan que la lista quede vacia."""
    calls = []

    def _forbidden_get(url, *args, **kwargs):
        calls.append(("GET", url))
        raise AssertionError(f"No debe salir ninguna request: GET {url}")

    def _forbidden_post(url, *args, **kwargs):
        calls.append(("POST", url))
        raise AssertionError(f"No debe salir ninguna request: POST {url}")

    monkeypatch.setattr(app_module.requests, "get", _forbidden_get)
    monkeypatch.setattr(app_module.requests, "post", _forbidden_post)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    return calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN-DEL-VENDEDOR")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# =====================================================================
# build_ml_api_url — la defensa primaria, a nivel unitario
# =====================================================================

# El payload de la izquierda es el resource crudo; a la derecha, el hostname que
# urlsplit resuelve de verdad al concatenarlo contra api.mercadolibre.com.
@pytest.mark.parametrize("resource, host_real", [
    ("@atacante.tld/x", "atacante.tld"),
    (".atacante.tld/x", "api.mercadolibre.com.atacante.tld"),
])
def test_build_ml_api_url_rechaza_hosts_que_el_parser_resuelve_afuera(resource, host_real):
    from urllib.parse import urlsplit

    # Primero: confirmar que el payload realmente desvia el host. Si ML cambiara
    # de dominio o Python cambiara el parser, este assert avisa antes que el otro.
    assert urlsplit(f"https://api.mercadolibre.com{resource}").hostname == host_real

    url, motivo = app_module.build_ml_api_url(resource)
    assert url is None
    assert motivo


@pytest.mark.parametrize("resource", [
    "",
    None,
    "items/MLA123",           # sin '/' inicial
    "https://atacante.tld/x",
    "\t@atacante.tld/x",      # el tab lo come urlsplit y el host queda afuera
    "/items/\nMLA123",        # caracteres de control: parser y cliente pueden diferir
])
def test_build_ml_api_url_rechaza_entradas_invalidas(resource):
    url, motivo = app_module.build_ml_api_url(resource)
    assert url is None
    assert motivo


@pytest.mark.parametrize("resource", [
    "/items/MLA123",
    "/items/MLA123/price_to_win?version=v2",
    "/orders/2000012345",
    "/shipments/44556677",
    "/seller-promotions/offers/OFFER-MLA1-1",
])
def test_build_ml_api_url_acepta_recursos_legitimos(resource):
    url, motivo = app_module.build_ml_api_url(resource)
    assert motivo is None
    assert url == f"https://api.mercadolibre.com{resource}"


def test_allowlist_cubre_los_recursos_que_la_app_consume():
    for resource in [
        "/items/MLA123",
        "/orders/2000012345",
        "/shipments/44556677",
        "/post-purchase/v1/claims/5000/detail",
        "/seller-promotions/users/123",
        "/users/123/items/search",
        "/products/MLA999/items",
        "/sites/MLA/search?q=779",
    ]:
        assert app_module.ml_resource_in_allowlist(resource), resource

    # /oauth/token queda deliberadamente afuera.
    assert not app_module.ml_resource_in_allowlist("/oauth/token")


# =====================================================================
# 1 y 2 — /api/ml/render no filtra el token
# =====================================================================

@pytest.mark.parametrize("resource", [
    "@atacante.tld/x",
    ".atacante.tld/x",
    "items/MLA123",  # no empieza con '/'
])
def test_render_rechaza_resource_hostil_sin_salir_a_la_red(client, no_network, resource):
    res = client.get("/api/ml/render", query_string={"resource": resource})

    assert res.status_code == 400
    # Lo que importa no es solo el 400: es que el header Authorization con el
    # token del vendedor no haya salido a ningun lado.
    assert no_network == []


def test_render_rechaza_inyeccion_via_price_to_win(client, no_network):
    """La validacion corre DESPUES de agregar version=v2.

    Si corriera antes, el append seria una via para tocar un resource ya aprobado.
    """
    res = client.get("/api/ml/render", query_string={"resource": "@atacante.tld/price_to_win"})

    assert res.status_code == 400
    assert no_network == []


def test_render_deja_pasar_un_resource_legitimo(client, monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, headers))
        return _Resp(payload={"id": "MLA123", "title": "Producto"})

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    res = client.get("/api/ml/render", query_string={"resource": "/items/MLA123", "format": "json"})

    assert res.status_code == 200
    assert calls[0][0] == "https://api.mercadolibre.com/items/MLA123"
    assert calls[0][1]["Authorization"] == "Bearer TOKEN-DEL-VENDEDOR"
    assert json.loads(res.data)["id"] == "MLA123"


def test_render_price_to_win_sigue_agregando_version_v2(client, monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        return _Resp(payload={"item_id": "MLA123"})

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    res = client.get(
        "/api/ml/render",
        query_string={"resource": "/items/MLA123/price_to_win", "format": "json"},
    )

    assert res.status_code == 200
    assert calls == ["https://api.mercadolibre.com/items/MLA123/price_to_win?version=v2"]


# =====================================================================
# 3 — allowlist en MODO OBSERVACION: loguea pero no bloquea
# =====================================================================

def test_render_fuera_de_allowlist_loguea_pero_no_bloquea(client, monkeypatch, capsys):
    """Modo observacion: todavia no cortamos porque no tenemos el inventario
    de consumidores (Fase 1). Solo dejamos rastro."""
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(payload={"ok": True}),
    )

    res = client.get(
        "/api/ml/render",
        query_string={"resource": "/marketplace/algo-desconocido", "format": "json"},
    )

    assert res.status_code == 200  # NO bloquea
    assert "RENDER FUERA DE ALLOWLIST" in capsys.readouterr().out


def test_render_dentro_de_allowlist_no_loguea_la_advertencia(client, monkeypatch, capsys):
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda *a, **k: _Resp(payload={"ok": True}),
    )

    client.get("/api/ml/render", query_string={"resource": "/items/MLA123", "format": "json"})

    assert "RENDER FUERA DE ALLOWLIST" not in capsys.readouterr().out


# =====================================================================
# 2 — la segunda puerta: POST /webhook -> fetch_and_store_preview
# =====================================================================

class _Cursor:
    def __init__(self):
        self.rowcount = 1

    def execute(self, query, params=None):
        self.rowcount = 1

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


@pytest.fixture
def webhook_client(monkeypatch):
    @contextmanager
    def fake_db_cursor():
        yield _Cursor()

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    # Sincrono: queremos que fetch_and_store_preview corra dentro del request.
    monkeypatch.setattr(app_module, "WEBHOOK_PREVIEW_ASYNC", False)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.mark.parametrize("resource", [
    "@atacante.tld/x",
    ".atacante.tld/x",
    "items/MLA123",
])
def test_webhook_con_resource_hostil_no_dispara_request_saliente(webhook_client, no_network, resource):
    payload = {
        "_id": "00000000-0000-0000-0000-0000000f0001",
        "topic": "items",
        "user_id": 123,
        "resource": resource,
    }

    res = webhook_client.post("/webhook", data=json.dumps(payload), content_type="application/json")

    # El webhook siempre acusa recibo: si devolviera error, ML reintenta igual.
    assert res.status_code == 200
    assert no_network == []


def test_fetch_and_store_preview_corta_antes_de_pedir_el_token(monkeypatch):
    """El resource se valida ANTES de get_token(): un resource hostil no llega
    nunca a tener la credencial cerca."""
    def _explota():
        raise AssertionError("get_token no debe llamarse con un resource invalido")

    monkeypatch.setattr(app_module, "get_token", _explota)

    assert app_module.fetch_and_store_preview("@atacante.tld/x") is None
