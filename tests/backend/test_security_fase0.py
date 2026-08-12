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


# =====================================================================
# 5 y 6 — /callback y el state firmado
# =====================================================================

@pytest.fixture
def callback_client(monkeypatch):
    """Registra escrituras en ml_tokens en vez de hacerlas."""
    saved = []
    monkeypatch.setattr(app_module, "save_token_to_db", lambda token_data: saved.append(token_data))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, saved


def _state_con_firma_alterada():
    state = app_module.generate_oauth_state()
    payload_b64, firma = state.split(".")
    # Cambiar un solo caracter de la firma alcanza.
    alterada = ("B" if firma[0] != "B" else "C") + firma[1:]
    return f"{payload_b64}.{alterada}"


def _state_con_payload_alterado():
    """Payload nuevo (timestamp fresco) pegado a una firma vieja y valida."""
    state = app_module.generate_oauth_state()
    _payload_viejo, firma = state.split(".")
    nuevo = app_module._b64url_encode(f"{int(time.time())}:falsificado".encode("utf-8"))
    return f"{nuevo}.{firma}"


def test_callback_sin_state_rechaza_y_no_escribe_ml_tokens(callback_client, no_network):
    c, saved = callback_client

    res = c.get("/callback", query_string={"code": "CODE-DEL-ATACANTE"})

    assert res.status_code == 400
    assert saved == []
    # Ni siquiera se canjea el code: el state se valida antes.
    assert no_network == []


@pytest.mark.parametrize("state_factory, caso", [
    (lambda: "no-es-un-state", "formato invalido"),
    (lambda: "", "vacio"),
    (lambda: "abc.def.ghi", "demasiadas partes"),
    (_state_con_firma_alterada, "firma alterada"),
    (_state_con_payload_alterado, "payload alterado con firma vieja"),
    (lambda: app_module.generate_oauth_state(now=time.time() - 601), "vencido"),
])
def test_callback_rechaza_state_invalido_y_no_escribe_ml_tokens(
    callback_client, no_network, state_factory, caso
):
    c, saved = callback_client

    res = c.get("/callback", query_string={"code": "CODE-DEL-ATACANTE", "state": state_factory()})

    assert res.status_code == 400, caso
    assert saved == [], caso
    assert no_network == [], caso


def test_callback_con_state_valido_sigue_el_flujo(callback_client, monkeypatch):
    c, saved = callback_client

    monkeypatch.setattr(
        app_module.requests, "post",
        lambda *a, **k: _Resp(payload={
            "access_token": "APP_USR-nuevo",
            "refresh_token": "TG-nuevo",
            "expires_in": 21600,
            "user_id": 123,
        }),
    )

    res = c.get("/callback", query_string={"code": "CODE-LEGITIMO", "state": app_module.generate_oauth_state()})

    assert res.status_code == 200
    assert len(saved) == 1
    assert saved[0]["access_token"] == "APP_USR-nuevo"


def test_callback_no_loguea_el_access_token(callback_client, monkeypatch, capsys):
    c, _saved = callback_client

    monkeypatch.setattr(
        app_module.requests, "post",
        lambda *a, **k: _Resp(payload={
            "access_token": "APP_USR-SECRETO-NO-DEBE-APARECER",
            "refresh_token": "TG-SECRETO-NO-DEBE-APARECER",
            "expires_in": 21600,
            "user_id": 123,
        }),
    )

    c.get("/callback", query_string={"code": "CODE", "state": app_module.generate_oauth_state()})

    salida = capsys.readouterr().out
    assert "APP_USR-SECRETO-NO-DEBE-APARECER" not in salida
    assert "TG-SECRETO-NO-DEBE-APARECER" not in salida


def test_auth_redirige_con_un_state_que_callback_acepta(client):
    res = client.get("/auth")

    assert res.status_code == 302
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(res.headers["Location"]).query)["state"][0]
    ok, motivo = app_module.verify_oauth_state(state)
    assert ok, motivo


def test_state_es_stateless_no_depende_del_proceso_que_lo_genero():
    """Si el state viviera en un dict en memoria o en la session de Flask, el
    login se rompe de forma intermitente con varios workers: lo genera un
    proceso y lo verifica otro. Verificar solo depende de ML_CLIENT_SECRET."""
    state = app_module.generate_oauth_state()

    ok, _ = app_module.verify_oauth_state(state)
    assert ok

    # Dos states seguidos son distintos (nonce), y los dos validan.
    otro = app_module.generate_oauth_state()
    assert otro != state
    assert app_module.verify_oauth_state(otro)[0]


def test_verify_oauth_state_falla_cerrado_sin_client_secret(monkeypatch):
    """Sin secreto, la firma seria forjable por cualquiera que conozca el
    esquema. Preferimos romper el login antes que aceptar un state sin firma
    real: el canje contra ML tampoco funcionaria sin client_secret."""
    monkeypatch.setattr(app_module, "ML_CLIENT_SECRET", "")

    ok, motivo = app_module.verify_oauth_state(app_module.generate_oauth_state())

    assert not ok
    assert motivo


# =====================================================================
# 4 — sweep: el thread arranca DENTRO del lock
# =====================================================================

def test_sweep_arranca_el_thread_dentro_del_lock(client, monkeypatch):
    """Con el t.start() afuera del lock, el guard de 'ya esta corriendo' no
    serializaba nada: dos requests concurrentes pasaban el chequeo."""
    observado = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            observado["lock_al_crear"] = app_module._sweep_lock.locked()

        def start(self):
            observado["lock_al_arrancar"] = app_module._sweep_lock.locked()

    monkeypatch.setattr(app_module.threading, "Thread", _FakeThread)
    monkeypatch.setitem(app_module._sweep_state, "running", False)

    res = client.get("/admin/sweep-shipping-costs")

    assert res.status_code == 202
    assert observado["lock_al_crear"] is True
    assert observado["lock_al_arrancar"] is True

    # Dejar el estado limpio para no contaminar otros tests.
    app_module._sweep_state["running"] = False


def test_sweep_status_no_dispara_el_job(client, monkeypatch):
    def _explota(*a, **k):
        raise AssertionError("?status=1 es solo lectura, no debe arrancar nada")

    monkeypatch.setattr(app_module.threading, "Thread", _explota)

    res = client.get("/admin/sweep-shipping-costs", query_string={"status": "1"})

    assert res.status_code == 200
    assert "running" in res.get_json()
