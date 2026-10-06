"""reconcile_item_promotions cierra el set COMPLETO de promos de un MLA.

Lo que ML ya no devuelve para el item (candidate/pending/started) pasa a
'finished'. Antes solo se bajaban las 'started' y solo si la lista no venia
vacia: candidatas viejas quedaban vivas semanas despues de su fecha de fin.
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
        self._payload = payload

    def json(self):
        return self._payload


MLA = "MLA1"
OTRO = "MLA2"


@pytest.fixture
def db(monkeypatch):
    """Tabla en memoria {(mla, promotion_id): status}; emula solo el UPDATE de cierre."""
    rows = {
        (MLA, "C-ABSENT"): "candidate",
        (MLA, "P-ABSENT"): "pending",
        (MLA, "S-ABSENT"): "started",
        (MLA, "F-OLD"): "finished",
        (MLA, "C-PRESENT"): "candidate",
        (MLA, "S-PRESENT"): "started",
        (OTRO, "C-OTHER"): "candidate",
    }

    class _Cur:
        def execute(self, sql, params=None):
            if "SET status='finished'" not in sql:
                return  # upserts de _persist_item_promos: las filas ya existen
            mla, statuses, current = params
            for (m, k), st in list(rows.items()):
                if m == mla and st in statuses and k not in current:
                    rows[(m, k)] = "finished"

    @contextmanager
    def fake_db_cursor():
        yield _Cur()

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "_persist_item_promos", lambda mla, data: None)
    return rows


def _ml(monkeypatch, resp):
    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: resp)


ML_LIST = [
    {"id": "C-PRESENT", "type": "DEAL", "status": "candidate"},
    {"id": "S-PRESENT", "type": "DEAL", "status": "started"},
    {"type": "PRICE_DISCOUNT", "status": "started"},  # sin id: la key es el type
]


def test_candidate_pending_started_ausentes_pasan_a_finished(db, monkeypatch):
    _ml(monkeypatch, _Resp(200, ML_LIST))
    assert app_module.reconcile_item_promotions(MLA) is True
    assert db[(MLA, "C-ABSENT")] == "finished"
    assert db[(MLA, "P-ABSENT")] == "finished"
    assert db[(MLA, "S-ABSENT")] == "finished"


def test_las_presentes_y_otros_mla_no_se_tocan(db, monkeypatch):
    _ml(monkeypatch, _Resp(200, ML_LIST))
    app_module.reconcile_item_promotions(MLA)
    assert db[(MLA, "C-PRESENT")] == "candidate"
    assert db[(MLA, "S-PRESENT")] == "started"
    assert db[(OTRO, "C-OTHER")] == "candidate"


def test_key_sin_id_usa_el_type_como_lo_persiste_el_upsert(db, monkeypatch):
    db[(MLA, "PRICE_DISCOUNT")] = "started"
    _ml(monkeypatch, _Resp(200, ML_LIST))
    app_module.reconcile_item_promotions(MLA)
    assert db[(MLA, "PRICE_DISCOUNT")] == "started"


def test_lista_vacia_cierra_todo_lo_activo(db, monkeypatch):
    _ml(monkeypatch, _Resp(200, []))
    assert app_module.reconcile_item_promotions(MLA) is True
    assert db[(MLA, "C-PRESENT")] == "finished"
    assert db[(MLA, "S-PRESENT")] == "finished"
    assert db[(MLA, "C-ABSENT")] == "finished"
    assert db[(OTRO, "C-OTHER")] == "candidate"


@pytest.mark.parametrize("body", [{"message": "x"}, None, "oops"])
def test_cuerpo_que_no_es_lista_no_cierra_nada(db, monkeypatch, body):
    antes = dict(db)
    _ml(monkeypatch, _Resp(200, body))
    app_module.reconcile_item_promotions(MLA)
    assert db == antes


def test_non_200_no_cierra_nada(db, monkeypatch):
    antes = dict(db)
    _ml(monkeypatch, _Resp(500, {"message": "boom"}))
    assert app_module.reconcile_item_promotions(MLA) is False
    assert db == antes
