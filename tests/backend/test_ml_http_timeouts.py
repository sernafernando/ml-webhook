"""Las llamadas salientes a ML no pueden colgarse para siempre.

Un proxy que cuelga es peor que uno que falla: el consumidor puede reintentar un
error, pero un cuelgue le come el worker. requests sin timeout espera para
siempre, asi que un socket colgado del otro lado se lleva puesto al worker.
"""
import ast
import threading
import time

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


# =====================================================================
# Estructural: ninguna salida HTTP queda sin timeout
# =====================================================================

def test_ninguna_llamada_saliente_queda_sin_timeout():
    """Escanea app.py entero. Es el test que impide la regresion: alcanza con
    que alguien agregue un requests.get sin timeout para que vuelva el cuelgue."""
    with open(app_module.__file__.replace(".pyc", ".py"), encoding="utf-8") as fh:
        arbol = ast.parse(fh.read())

    sin_timeout = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        fn = nodo.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in ("get", "post")):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "requests"):
            continue
        if not any(kw.arg == "timeout" for kw in nodo.keywords):
            sin_timeout.append(nodo.lineno)

    assert not sin_timeout, f"requests sin timeout en las lineas {sin_timeout}"


# =====================================================================
# ml_api_get
# =====================================================================

def test_ml_api_get_manda_timeout(monkeypatch):
    visto = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

    def _fake_get(url, headers=None, params=None, timeout=None, **kwargs):
        visto["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(app_module.requests, "get", _fake_get)
    app_module.ml_api_get("https://api.mercadolibre.com/users/me")

    assert visto["timeout"] is not None
    conectar, leer = visto["timeout"]
    assert 0 < conectar <= 15
    assert 0 < leer <= 60


def test_ml_api_get_propaga_el_timeout_como_excepcion(monkeypatch):
    """Un timeout debe subir como error, no convertirse en un None silencioso:
    la ruta que llama lo traduce a 502 y el consumidor puede reintentar."""
    import requests as requests_mod

    def _timeout(*args, **kwargs):
        raise requests_mod.exceptions.Timeout("read timed out")

    monkeypatch.setattr(app_module.requests, "get", _timeout)

    with pytest.raises(requests_mod.exceptions.Timeout):
        app_module.ml_api_get("https://api.mercadolibre.com/users/me")


# =====================================================================
# refresh_token
# =====================================================================

@pytest.fixture
def token_vencido(monkeypatch):
    """Deja el proceso como recien arrancado: sin token en memoria ni en DB."""
    monkeypatch.setattr(app_module, "ACCESS_TOKEN", None)
    monkeypatch.setattr(app_module, "EXPIRATION", 0)
    monkeypatch.setattr(app_module, "load_token_from_db",
                        lambda: {"refresh_token": "REFRESH-VIEJO"})
    monkeypatch.setattr(app_module, "save_token_to_db", lambda data: None)


def test_refresh_token_manda_timeout(monkeypatch, token_vencido):
    visto = {}

    class _Resp:
        def json(self):
            return {"access_token": "NUEVO", "expires_in": 21600}

    def _fake_post(url, data=None, timeout=None, **kwargs):
        visto["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(app_module.requests, "post", _fake_post)
    app_module.refresh_token()

    assert visto["timeout"] is not None


def test_refresh_token_no_se_estampida_al_arrancar(monkeypatch, token_vencido):
    """Al reiniciar el proceso, cada worker tiene ACCESS_TOKEN=None y sale a
    refrescar a la vez. ML rota el refresh_token, asi que la estampida no solo
    es trafico de mas: invalida los refresh de las demas. Debe salir UNA sola.
    """
    posts = []
    arranquen = threading.Event()

    class _Resp:
        def json(self):
            return {"access_token": "NUEVO", "expires_in": 21600}

    def _fake_post(url, data=None, timeout=None, **kwargs):
        posts.append(data)
        time.sleep(0.05)  # ventana para que las otras lleguen si no hay lock
        return _Resp()

    monkeypatch.setattr(app_module.requests, "post", _fake_post)

    def _worker():
        arranquen.wait()
        app_module.get_token()

    hilos = [threading.Thread(target=_worker) for _ in range(8)]
    for h in hilos:
        h.start()
    arranquen.set()
    for h in hilos:
        h.join(timeout=10)

    assert len(posts) == 1, f"salieron {len(posts)} refresh en paralelo"
