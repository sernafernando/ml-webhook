"""Webhook de candidate: tiene que disparar el reconcile completo, con precios.

Antes el candidate se procesaba en linea con un upsert PARCIAL que solo tocaba
status y updated_at. La fila quedaba con el precio viejo (y el payload viejo,
donde viven los % de meli y del vendedor) pero con updated_at recien puesto:
un precio stale que parecia fresco. Un operador vio 1121999 "actualizado
recien" cuando ML tenia 976653.

Ahora el candidate va por el mismo camino que los offers: el MLA entra al set
de sucios y worker_promos corre reconcile_item_promotions, que persiste con
precios y cierra el set.
"""
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


CANDIDATE = "/seller-promotions/candidates/CANDIDATE-MLA1807356285-71690560920"


class _FakeRedis:
    def __init__(self):
        self.sets = {}

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)


@pytest.fixture
def entorno(monkeypatch):
    """Redis falso + DB y API de ML que registran cualquier uso sincrono."""
    redis = _FakeRedis()
    usos = {"sql": [], "ml": []}

    @contextmanager
    def fake_db_cursor():
        class _C:
            rowcount = 0
            def execute(self, sql, *a, **k): usos["sql"].append(sql)
            def fetchone(self): return None
            def fetchall(self): return []
        yield _C()

    class _Resp:
        status_code = 200
        def json(self):
            return {"item_id": "MLA1807356285", "promotion_id": "P-1",
                    "type": "DEAL", "status": "candidate"}

    def fake_api_get(resource, extra_params=None):
        usos["ml"].append(resource)
        return _Resp()

    monkeypatch.setattr(app_module, "_redis_client", redis)
    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "_promos_api_get", fake_api_get)
    monkeypatch.setattr(app_module, "PROMOS_WEBHOOK_ENABLED", True)
    return redis, usos


def _sucios(redis):
    return redis.sets.get(app_module.PROMOS_DIRTY_SET_KEY, set())


def test_candidate_encola_el_mla_para_el_reconcile_completo(entorno):
    redis, _ = entorno

    app_module._process_promotion_webhook(CANDIDATE)

    assert _sucios(redis) == {"MLA1807356285"}


def test_candidate_no_toca_la_tabla_ni_llama_a_ml_en_linea(entorno):
    """El upsert parcial en linea es justamente lo que dejaba el precio stale."""
    _, usos = entorno

    app_module._process_promotion_webhook(CANDIDATE)

    assert usos["sql"] == []
    assert usos["ml"] == []


@pytest.mark.parametrize("resource", [
    "/seller-promotions/candidates/",
    "/seller-promotions/candidates/CANDIDATE-sin-item",
    "/seller-promotions/candidates/CANDIDATE-XYZ-71690560920",
])
def test_candidate_sin_mla_no_encola_nada_y_no_rompe(entorno, resource):
    redis, usos = entorno

    app_module._process_promotion_webhook(resource)

    assert _sucios(redis) == set()
    assert usos["sql"] == []


def test_offer_sigue_encolando_el_mla(entorno):
    redis, _ = entorno

    app_module._process_promotion_webhook(
        "/seller-promotions/offers/OFFER-MLA1807356285-11627676498")

    assert _sucios(redis) == {"MLA1807356285"}


@pytest.mark.parametrize("resource", [
    CANDIDATE,
    "/seller-promotions/offers/OFFER-MLA1807356285-11627676498",
])
def test_sin_redis_reconcilia_en_linea_en_vez_de_perder_el_aviso(entorno, monkeypatch, resource):
    """Sin redis no hay set de sucios: el aviso no se puede perder en silencio.
    Se corre el reconcile completo (con precios) en linea."""
    reconciliados = []
    monkeypatch.setattr(app_module, "_redis_client", None)
    monkeypatch.setattr(app_module, "reconcile_item_promotions", reconciliados.append)

    app_module._process_promotion_webhook(resource)

    assert reconciliados == ["MLA1807356285"]


def test_promo_resource_mla_entiende_candidates():
    assert app_module._promo_resource_mla(CANDIDATE) == "MLA1807356285"
