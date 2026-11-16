"""Aviso a pricing-app cuando cambia el espejo de promos de un MLA.

Cuando reconcile_item_promotions cambia de verdad el estado o el precio de una
promo del MLA (o cierra alguna), se avisa a pricing-app
(POST /api/promociones/recompute-precios) para que recalcule ya las columnas de
precio del item, en vez de esperar al cron de 3 veces por dia. Best-effort: si
pricing no contesta solo se registra; el cron de pricing lo levanta igual.

Ademas updated_at solo se mueve cuando cambia status o price: si no, cada
pasada parecia una novedad para pricing.
"""
from contextlib import contextmanager

import pytest

try:
    import app as app_module
    import backfill_promotions
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


MLA = "MLA1"
URL = "https://pricing.example/api/promociones/recompute-precios"
TOKEN = "tok-secreto-123"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def pricing(monkeypatch):
    """Destino configurado; registra cada POST a pricing."""
    llamadas = []
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module.requests, "post",
                        lambda url, **kw: llamadas.append((url, kw)) or _Resp(202, {}))
    return llamadas


@pytest.fixture
def espejo(monkeypatch):
    """ML devuelve una lista; el upsert y el cierre se controlan desde el test."""
    estado = {"upsert_cambio": False, "cerradas": 0}

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            if "SET status='finished'" in sql:
                self.rowcount = estado["cerradas"]

    @contextmanager
    def fake_db_cursor():
        yield _Cur()

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "_persist_item_promos", lambda mla, data: estado["upsert_cambio"])
    monkeypatch.setattr(app_module, "_promos_api_get",
                        lambda r, **k: _Resp(200, [{"id": "P-1", "type": "DEAL", "status": "started"}]))
    return estado


# =====================================================================
# Cuando se avisa
# =====================================================================

def test_un_cambio_de_promo_avisa_a_pricing_con_el_mla(espejo, pricing):
    espejo["upsert_cambio"] = True

    assert app_module.reconcile_item_promotions(MLA) is True

    assert len(pricing) == 1
    url, kw = pricing[0]
    assert url == URL
    assert kw["json"] == {"mlas": [MLA]}
    assert kw["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    # Timeout obligatorio: pricing colgado no puede quedarse con el worker.
    assert kw.get("timeout") is not None


def test_cerrar_una_promo_tambien_avisa(espejo, pricing):
    espejo["cerradas"] = 1

    app_module.reconcile_item_promotions(MLA)

    assert [kw["json"] for _, kw in pricing] == [{"mlas": [MLA]}]


def test_sin_cambios_no_avisa(espejo, pricing):
    assert app_module.reconcile_item_promotions(MLA) is True
    assert pricing == []


def test_si_ml_falla_no_avisa(espejo, pricing, monkeypatch):
    espejo["upsert_cambio"] = True
    monkeypatch.setattr(app_module, "_promos_api_get", lambda r, **k: _Resp(500, {"message": "boom"}))

    assert app_module.reconcile_item_promotions(MLA) is False
    assert pricing == []


# =====================================================================
# Best-effort
# =====================================================================

def test_pricing_caido_no_rompe_el_reconcile(espejo, monkeypatch):
    espejo["upsert_cambio"] = True
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", TOKEN)

    def _cae(url, **kw):
        raise app_module.requests.ConnectionError(f"no route to {url} Bearer {TOKEN}")

    monkeypatch.setattr(app_module.requests, "post", _cae)

    assert app_module.reconcile_item_promotions(MLA) is True
    ultimo = app_module._ultimo_recompute
    assert ultimo["mla"] == MLA and ultimo["status"] is None
    # El token nunca queda en el diagnostico.
    assert TOKEN not in ultimo["error"]


def test_un_rechazo_de_pricing_queda_registrado(espejo, monkeypatch):
    espejo["upsert_cambio"] = True
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", TOKEN)
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: _Resp(403, {}))

    assert app_module.reconcile_item_promotions(MLA) is True
    assert app_module._ultimo_recompute["status"] == 403


# =====================================================================
# Destino
# =====================================================================

def test_sin_destino_no_intenta_nada(espejo, monkeypatch):
    espejo["upsert_cambio"] = True
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", None)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", None)
    monkeypatch.setattr(app_module.requests, "post",
                        lambda *a, **k: pytest.fail("No debe salir ninguna request"))

    assert app_module.reconcile_item_promotions(MLA) is True


def test_sin_url_propia_usa_el_host_de_pricing_del_ping(monkeypatch):
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", None)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://pricing.example:8443/api/ml-ventas-ops/activity/ping")

    assert app_module._promo_recompute_url() == "https://pricing.example:8443/api/promociones/recompute-precios"


def test_la_url_propia_gana(monkeypatch):
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_URL", "https://otro.example/api/ml-ventas-ops/activity/ping")

    assert app_module._promo_recompute_url() == URL


# =====================================================================
# updated_at solo se mueve con un cambio real
# =====================================================================

class _UpsertCur:
    def __init__(self, devoluciones):
        self.sqls = []
        self._devoluciones = list(devoluciones)

    def execute(self, sql, params=None):
        self.sqls.append(sql)

    def fetchone(self):
        return (self._devoluciones.pop(0),)


def test_el_upsert_solo_mueve_updated_at_si_cambia_status_o_price():
    cur = _UpsertCur([False])

    cambio = app_module._upsert_item_promo(cur, MLA, "P-1", "DEAL", None, {"status": "started", "price": 10})

    sql = " ".join(cur.sqls[0].split())
    assert "payload = EXCLUDED.payload, updated_at = NOW()" not in sql
    assert ("CASE WHEN ml_item_promotions.status IS DISTINCT FROM EXCLUDED.status "
            "OR ml_item_promotions.price IS DISTINCT FROM EXCLUDED.price "
            "THEN NOW() ELSE ml_item_promotions.updated_at END") in sql
    assert "RETURNING updated_at = NOW()" in sql
    assert cambio is False


def test_persist_item_promos_dice_si_alguna_fila_cambio(monkeypatch):
    cur = _UpsertCur([False, True])

    @contextmanager
    def fake_db_cursor():
        yield cur

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    data = [{"id": "P-1", "type": "DEAL"}, {"id": "P-2", "type": "DEAL"}]

    assert app_module._persist_item_promos(MLA, data) is True


def test_persist_item_promos_sin_cambios(monkeypatch):
    cur = _UpsertCur([False])

    @contextmanager
    def fake_db_cursor():
        yield cur

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)

    assert app_module._persist_item_promos(MLA, [{"id": "P-1", "type": "DEAL"}]) is False


# =====================================================================
# El backfill ya no infiere "visto" por updated_at
# =====================================================================

def test_el_backfill_no_cierra_una_started_vista_aunque_su_updated_at_sea_viejo(monkeypatch):
    """Con updated_at quieto en las filas sin cambios, "updated_at < inicio de la
    corrida" ya no significa "ML no la reporto": se cierra solo lo no visto."""
    rows = {
        ("MLA1", "C-1"): "started",  # vista en esta corrida, sin cambios
        ("MLA2", "C-1"): "started",  # ML ya no la reporta
        ("MLA3", "C-2"): "started",  # promo que no se completo: no se toca
    }

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            done, mlas, pids, _inicio = params
            vistos = set(zip(mlas, pids))
            for (m, p), st in list(rows.items()):
                # Todas estas filas tienen updated_at anterior al inicio de la corrida.
                if st == "started" and p in done and (m, p) not in vistos:
                    rows[(m, p)] = "finished"
                    self.rowcount += 1

    @contextmanager
    def fake_db_cursor():
        yield _Cur()

    monkeypatch.setattr(backfill_promotions, "db_cursor", fake_db_cursor)

    cerradas = backfill_promotions._finish_stale_started(["C-1"], {("MLA1", "C-1")}, object())

    assert cerradas == 1
    assert rows == {("MLA1", "C-1"): "started", ("MLA2", "C-1"): "finished", ("MLA3", "C-2"): "started"}


# =====================================================================
# Observaciones de la review
# =====================================================================

def test_dos_cambios_del_mismo_mla_son_un_solo_aviso(espejo, pricing):
    espejo["upsert_cambio"] = True
    espejo["cerradas"] = 2

    app_module.reconcile_item_promotions(MLA)

    assert len(pricing) == 1


def test_el_token_no_aparece_en_el_log(espejo, monkeypatch, capsys):
    espejo["upsert_cambio"] = True
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "ACTIVITY_PING_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)

    def _cae(url, **kw):
        raise app_module.requests.ConnectionError(f"Bearer {TOKEN}")

    monkeypatch.setattr(app_module.requests, "post", _cae)

    app_module.reconcile_item_promotions(MLA)

    assert TOKEN not in capsys.readouterr().out


def test_tras_un_fallo_de_pricing_se_pausan_los_avisos(espejo, monkeypatch):
    """Con pricing caido, cada aviso podia sumarle segundos al worker. Tras un
    fallo se deja de avisar un rato: el cron de pricing cubre el hueco."""
    espejo["upsert_cambio"] = True
    llamadas = []
    reloj = [1000.0]
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module.time, "monotonic", lambda: reloj[0])

    def _cae(url, **kw):
        llamadas.append(url)
        raise app_module.requests.ConnectionError("caido")

    monkeypatch.setattr(app_module.requests, "post", _cae)

    app_module.reconcile_item_promotions(MLA)
    app_module.reconcile_item_promotions("MLA2")
    assert len(llamadas) == 1

    reloj[0] += app_module.PROMO_RECOMPUTE_PAUSA_TRAS_FALLO + 1
    app_module.reconcile_item_promotions("MLA3")
    assert len(llamadas) == 2


def test_un_5xx_de_pricing_tambien_pausa(espejo, monkeypatch):
    espejo["upsert_cambio"] = True
    llamadas = []
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: llamadas.append(url) or _Resp(503, {}))

    app_module.reconcile_item_promotions(MLA)
    app_module.reconcile_item_promotions("MLA2")

    assert len(llamadas) == 1


def test_un_rechazo_4xx_no_pausa(espejo, monkeypatch):
    """Un 4xx es un problema de ese aviso (permiso, formato), no de pricing caido."""
    espejo["upsert_cambio"] = True
    llamadas = []
    monkeypatch.setattr(app_module, "PROMO_RECOMPUTE_URL", URL)
    monkeypatch.setattr(app_module, "_recompute_pausa_hasta", 0.0)
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: llamadas.append(url) or _Resp(422, {}))

    app_module.reconcile_item_promotions(MLA)
    app_module.reconcile_item_promotions("MLA2")

    assert len(llamadas) == 2


def test_una_promo_repetida_en_la_respuesta_se_upsertea_una_vez(monkeypatch):
    """Dos entradas con la misma key en una transaccion: la segunda veia el
    updated_at = NOW() de la primera y reportaba un cambio que no hubo."""
    cur = _UpsertCur([False])

    @contextmanager
    def fake_db_cursor():
        yield cur

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    data = [{"type": "PRICE_DISCOUNT", "price": 1}, {"type": "PRICE_DISCOUNT", "price": 2}]

    assert app_module._persist_item_promos(MLA, data) is False
    assert len(cur.sqls) == 1


def test_los_caminos_masivos_no_avisan(monkeypatch, pricing):
    cur = _UpsertCur([True])

    @contextmanager
    def fake_db_cursor():
        yield cur

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)

    app_module._persist_promo_items("C-1", "DEAL", {"results": [{"id": MLA, "status": "started"}]})

    assert pricing == []


def test_el_backfill_junta_las_claves_que_persiste(monkeypatch):
    """Las claves vistas son las mismas (mla, promotion_id) que guarda
    _persist_promo_items: el id de cada resultado es el MLA."""
    paginas = [
        _Resp(200, {"results": [{"id": "MLA1"}, {"id": "MLA2"}], "paging": {"searchAfter": "x"}}),
        _Resp(200, {"results": [{"id": "MLA3"}], "paging": {}}),
    ]
    persistidas = []
    monkeypatch.setattr(backfill_promotions, "_promos_api_get", lambda r, p: paginas.pop(0))
    monkeypatch.setattr(backfill_promotions, "_persist_promo_items",
                        lambda pid, t, data: persistidas.extend((e["id"], pid) for e in data["results"]))
    vistos = set()

    vistos_n, completa = backfill_promotions._backfill_promo({"id": "C-1", "type": "DEAL"}, vistos)

    assert (vistos_n, completa) == (3, True)
    assert vistos == set(persistidas) == {("MLA1", "C-1"), ("MLA2", "C-1"), ("MLA3", "C-1")}


def test_el_backfill_no_cierra_lo_que_otro_camino_toco_durante_la_corrida(monkeypatch):
    """Una fila que un webhook paso a 'started' mientras corria el backfill (despues
    de su pagina) no esta en las vistas, pero su updated_at es de la corrida."""
    ejecutados = []

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            ejecutados.append((" ".join(sql.split()), params))

    @contextmanager
    def fake_db_cursor():
        yield _Cur()

    monkeypatch.setattr(backfill_promotions, "db_cursor", fake_db_cursor)
    inicio = object()

    backfill_promotions._finish_stale_started(["C-1"], {("MLA1", "C-1")}, inicio)

    sql, params = ejecutados[0]
    assert "AND updated_at < %s" in sql
    assert "NOT EXISTS" in sql and "NOT IN" not in sql
    assert params[-1] is inicio
