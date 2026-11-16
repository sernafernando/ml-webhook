"""El SQL de ml_item_promotions corrido contra un Postgres real.

Opcional: corre solo si ML_WEBHOOK_TEST_PG_URL apunta a una base descartable.
Todo pasa en una tabla TEMP (pg_temp va primero en el search_path, asi que tapa
a cualquier ml_item_promotions real) dentro de una transaccion que se descarta.

Prueba lo que los tests con cursor falso no pueden: que el upsert mueve
updated_at solo si cambia status o price y devuelve si cambio, y que el cierre
del backfill respeta las filas vistas y las tocadas durante la corrida.
"""
import os
from datetime import timedelta

import pytest

PG_URL = os.getenv("ML_WEBHOOK_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="ML_WEBHOOK_TEST_PG_URL no configurada")

try:
    import psycopg2
    import app as app_module
    import backfill_promotions
except Exception as exc:  # pragma: no cover - entorno sin dependencias
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar: {exc}")


DDL = """
CREATE TEMP TABLE ml_item_promotions (
    mla                        TEXT NOT NULL,
    promotion_id               TEXT NOT NULL,
    promotion_type             TEXT,
    sub_type                   TEXT,
    status                     TEXT,
    original_price             NUMERIC(18, 2),
    price                      NUMERIC(18, 2),
    min_discounted_price       NUMERIC(18, 2),
    max_discounted_price       NUMERIC(18, 2),
    suggested_discounted_price NUMERIC(18, 2),
    payload                    JSONB NOT NULL,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mla, promotion_id)
) ON COMMIT DROP
"""


@pytest.fixture
def cur():
    conn = psycopg2.connect(PG_URL)
    try:
        with conn.cursor() as c:
            c.execute(DDL)
            yield c
    finally:
        conn.rollback()
        conn.close()


def _updated_at(cur, mla, pid):
    cur.execute("SELECT updated_at FROM ml_item_promotions WHERE mla=%s AND promotion_id=%s", (mla, pid))
    return cur.fetchone()[0]


def _envejecer(cur):
    cur.execute("UPDATE ml_item_promotions SET updated_at = NOW() - interval '1 day'")


def _upsert(cur, **entry):
    return app_module._upsert_item_promo(cur, "MLA1", "P-1", "DEAL", None, entry)


def test_una_fila_nueva_cuenta_como_cambio(cur):
    assert _upsert(cur, status="started", price=100) is True


def test_sin_cambios_updated_at_queda_quieto(cur):
    _upsert(cur, status="started", price=100)
    _envejecer(cur)
    antes = _updated_at(cur, "MLA1", "P-1")

    assert _upsert(cur, status="started", price=100.0, min_discounted_price=50) is False
    assert _updated_at(cur, "MLA1", "P-1") == antes


@pytest.mark.parametrize("cambio", [{"status": "finished", "price": 100}, {"status": "started", "price": 90}])
def test_cambio_de_status_o_price_mueve_updated_at(cur, cambio):
    _upsert(cur, status="started", price=100)
    _envejecer(cur)
    antes = _updated_at(cur, "MLA1", "P-1")

    assert _upsert(cur, **cambio) is True
    assert _updated_at(cur, "MLA1", "P-1") > antes


def test_price_de_null_a_valor_es_un_cambio(cur):
    _upsert(cur, status="candidate", price=None)
    _envejecer(cur)

    assert _upsert(cur, status="candidate", price=10) is True


class _CursorCtx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def test_el_cierre_del_backfill_respeta_vistas_y_tocadas_durante_la_corrida(cur, monkeypatch):
    for mla in ("MLA1", "MLA2", "MLA3"):
        app_module._upsert_item_promo(cur, mla, "C-1", "DEAL", None, {"status": "started", "price": 10})
    app_module._upsert_item_promo(cur, "MLA4", "C-2", "DEAL", None, {"status": "started", "price": 10})
    _envejecer(cur)
    cur.execute("SELECT NOW() - interval '1 hour'")
    inicio = cur.fetchone()[0]
    # MLA3: un webhook la toco durante la corrida (updated_at posterior al inicio).
    cur.execute("UPDATE ml_item_promotions SET updated_at = %s WHERE mla = 'MLA3'", (inicio + timedelta(minutes=1),))
    monkeypatch.setattr(backfill_promotions, "db_cursor", lambda: _CursorCtx(cur))

    cerradas = backfill_promotions._finish_stale_started(["C-1"], {("MLA1", "C-1")}, inicio)

    cur.execute("SELECT mla, status FROM ml_item_promotions ORDER BY mla")
    assert cur.fetchall() == [("MLA1", "started"), ("MLA2", "finished"), ("MLA3", "started"), ("MLA4", "started")]
    assert cerradas == 1


def test_el_cierre_sin_vistas_cierra_todo_lo_viejo_de_la_promo(cur, monkeypatch):
    app_module._upsert_item_promo(cur, "MLA1", "C-1", "DEAL", None, {"status": "started", "price": 10})
    _envejecer(cur)
    cur.execute("SELECT NOW() - interval '1 hour'")
    inicio = cur.fetchone()[0]
    monkeypatch.setattr(backfill_promotions, "db_cursor", lambda: _CursorCtx(cur))

    assert backfill_promotions._finish_stale_started(["C-1"], set(), inicio) == 1
