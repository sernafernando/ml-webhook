"""Refresh de promociones de un item: distinguir "el item no se puede" de "fallo el proxy".

Un item cerrado hace que ML devuelva 400 "Item status is not allowed (closed)".
Eso no es un bad gateway: es una condicion del item, permanente, y el consumidor
tiene que poder decir POR QUE no se actualizo en vez de mostrar un cartel
generico. Ademas un 5xx del origen se lo come Cloudflare, que reemplaza el
cuerpo por su propia pagina: el consumidor ni siquiera ve el JSON que mandamos.
"""
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def fake_db_cursor():
        class _C:
            rowcount = 0
            def execute(self, *a, **k): pass
            def fetchone(self): return (0,)
            def fetchall(self): return []
        yield _C()

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


CERRADO = {"cause": [], "error": "bad_request",
           "message": "Item status is not allowed (closed)", "status": 400}


def test_un_item_cerrado_no_es_un_bad_gateway(client, monkeypatch):
    """El consumidor tiene que poder distinguirlo de "el proxy se cayo"."""
    monkeypatch.setattr(app_module, "_promos_api_get",
                        lambda r, **k: _Resp(400, CERRADO))

    res = client.post("/api/promociones/item/MLA2063558261/refresh")

    assert res.status_code != 502
    assert res.status_code < 500


def test_dice_por_que_no_se_pudo(client, monkeypatch):
    """Un cartel generico obliga al operador a adivinar. El motivo viene de ML."""
    monkeypatch.setattr(app_module, "_promos_api_get",
                        lambda r, **k: _Resp(400, CERRADO))

    d = client.post("/api/promociones/item/MLA2063558261/refresh").get_json()

    assert d["refreshed"] is False
    assert d["mla"] == "MLA2063558261"
    assert "closed" in d["reason"]
    assert d["ml_status"] == 400


def test_un_fallo_real_del_upstream_sigue_siendo_502(client, monkeypatch):
    """Un 5xx de ML si es un problema de infraestructura y hay que reintentarlo.
    Confundirlo con una condicion del item haria que el consumidor deje de
    reintentar algo que se iba a arreglar solo."""
    monkeypatch.setattr(app_module, "_promos_api_get",
                        lambda r, **k: _Resp(503, {"error": "service unavailable"}))

    res = client.post("/api/promociones/item/MLA1/refresh")

    assert res.status_code == 502
    assert res.get_json()["refreshed"] is False


def test_un_item_sin_promociones_se_refresca_bien(client, monkeypatch):
    """La lista vacia es un caso legitimo, no un error: el item existe y no tiene
    promos. Cerrar el set con una lista vacia no debe romper nada."""
    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: _Resp(200, []))

    res = client.post("/api/promociones/item/MLA3/refresh")

    assert res.status_code == 200
    assert res.get_json()["refreshed"] is True


def test_un_item_con_promociones_se_refresca_bien(client, monkeypatch):
    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: _Resp(
        200, [{"id": "P-MLA1", "type": "SMART", "status": "candidate"}]))

    res = client.post("/api/promociones/item/MLA4/refresh")

    assert res.status_code == 200
    assert res.get_json()["refreshed"] is True


def test_el_worker_sigue_viendo_un_booleano(monkeypatch):
    """worker_promos.py hace `if reconcile_item_promotions(mla)`. El contrato
    viejo no se rompe."""
    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: _Resp(200, []))
    monkeypatch.setattr(app_module, "_persist_item_promos", lambda m, d: None)

    assert app_module.reconcile_item_promotions("MLA5") is True

    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: _Resp(400, CERRADO))
    assert app_module.reconcile_item_promotions("MLA6") is False
