import json

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Resp:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload
        self.content = b"{}"
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    monkeypatch.setattr(app_module, "_ml_pxq_throttle", lambda: None)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# -------------------- lectura de tramos --------------------

def test_tiers_for_item_manda_el_header_que_destapa_los_tramos(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["headers"] = headers
        return _Resp(payload={"prices": [
            {"id": "0", "amount": 3000, "currency_id": "ARS"},
            {"id": "2", "amount": 2850, "currency_id": "ARS",
             "conditions": {"min_purchase_unit": 10}},
        ]})

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    monkeypatch.setattr(app_module, "_ml_pxq_throttle", lambda: None)

    tiers, err = app_module._pxq_tiers_for_item("MLA1")

    assert err is None
    # sin este header ML omite los nodos con min_purchase_unit
    assert seen["headers"]["show-all-prices"] == "true"
    assert tiers == [{"id": "2", "quantity": 10, "amount": 2850, "currency_id": "ARS"}]


@pytest.mark.parametrize("resp", [
    _Resp(status_code=500),
    _Resp(payload={"algo": "otro"}),
])
def test_tiers_for_item_devuelve_none_no_lista_vacia_ante_fallo(monkeypatch, resp):
    """None != []: [] afirmaria 'no tiene tramos' y borraria tramos reales."""
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **k: resp)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    monkeypatch.setattr(app_module, "_ml_pxq_throttle", lambda: None)

    tiers, err = app_module._pxq_tiers_for_item("MLA1")

    assert tiers is None
    assert err


# -------------------- worker --------------------

class _FakeCursor:
    def __init__(self, log, fresh):
        self.log = log
        self.fresh = fresh
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append(("execute", " ".join(sql.split())[:60], params))
        self._rows = [(m,) for m in self.fresh] if "ml_pxq_tier_scans" in sql else []

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, fresh=()):
        self.log = []
        self.fresh = fresh
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.log, self.fresh)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _prepare(monkeypatch, mlas, tiers_by_mla, fresh=()):
    conn = _FakeConn(fresh)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    monkeypatch.setattr(app_module, "psycopg2", app_module.psycopg2)
    monkeypatch.setattr(app_module.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(app_module, "_enumerate_active_mlas",
                        lambda *a, **k: list(mlas))
    monkeypatch.setattr(app_module, "_pxq_tiers_for_item",
                        lambda mla: tiers_by_mla[mla])

    inserted = []
    monkeypatch.setattr(app_module, "execute_values",
                        lambda cur, sql, rows: inserted.append((sql.split()[2], rows)))

    class _Ctx:
        def __enter__(self):
            class C:
                def execute(self, *a, **k):
                    pass

                def fetchone(self):
                    return (111,)
            return C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(app_module, "db_cursor", lambda: _Ctx())
    app_module._pxq_sweep_state.update({
        "processed": 0, "skipped": 0, "errors": 0, "with_tiers": 0,
        "total_enumerated": 0, "running": True,
    })
    return conn, inserted


def test_sweep_persiste_tramos_y_cuenta_las_publicaciones_con_tramos(monkeypatch):
    tiers = {
        "MLA1": ([{"id": "2", "quantity": 10, "amount": 2850, "currency_id": "ARS"}], None),
        "MLA2": ([], None),
    }
    conn, inserted = _prepare(monkeypatch, ["MLA1", "MLA2"], tiers)

    app_module._sweep_pxq_tiers(None, False, 0)

    st = app_module._pxq_sweep_state
    assert st["processed"] == 2
    assert st["with_tiers"] == 1
    assert st["errors"] == 0
    tables = [t for t, _ in inserted]
    assert "ml_pxq_price_tiers" in tables and "ml_pxq_tier_scans" in tables
    # MLA2 no aporta tramos pero SI queda registrada en el scan
    scan_rows = [rows for t, rows in inserted if t == "ml_pxq_tier_scans"][0]
    assert {r[0] for r in scan_rows} == {"MLA1", "MLA2"}


def test_sweep_no_escribe_para_una_mla_cuya_lectura_fallo(monkeypatch):
    tiers = {
        "MLA1": (None, "status=500"),
        "MLA2": ([{"id": "2", "quantity": 5, "amount": 900, "currency_id": "ARS"}], None),
    }
    conn, inserted = _prepare(monkeypatch, ["MLA1", "MLA2"], tiers)

    app_module._sweep_pxq_tiers(None, False, 0)

    st = app_module._pxq_sweep_state
    assert st["errors"] == 1
    assert st["processed"] == 1
    scan_rows = [rows for t, rows in inserted if t == "ml_pxq_tier_scans"][0]
    assert {r[0] for r in scan_rows} == {"MLA2"}  # MLA1 no se toca


def test_sweep_dry_run_no_escribe(monkeypatch):
    tiers = {"MLA1": ([{"id": "2", "quantity": 5, "amount": 900}], None)}
    conn, inserted = _prepare(monkeypatch, ["MLA1"], tiers)

    app_module._sweep_pxq_tiers(None, True, 0)

    assert inserted == []
    assert app_module._pxq_sweep_state["processed"] == 1


def test_sweep_saltea_las_mlas_frescas(monkeypatch):
    tiers = {"MLA2": ([], None)}
    conn, inserted = _prepare(monkeypatch, ["MLA1", "MLA2"], tiers, fresh=["MLA1"])

    app_module._sweep_pxq_tiers(None, False, 24)

    assert app_module._pxq_sweep_state["skipped"] == 1
    assert app_module._pxq_sweep_state["processed"] == 1


def test_sweep_aborta_si_la_enumeracion_falla(monkeypatch):
    conn, inserted = _prepare(monkeypatch, [], {})
    monkeypatch.setattr(app_module, "_enumerate_active_mlas", lambda *a, **k: None)

    app_module._sweep_pxq_tiers(None, False, 0)

    assert app_module._pxq_sweep_state["errors"] == 1
    assert inserted == []


# -------------------- ruta admin --------------------

def test_status_no_dispara_el_barrido(client, monkeypatch):
    called = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: called.append(1))
    res = client.get("/admin/sweep-pxq-tiers?status=1")
    assert res.status_code == 200
    assert called == []


def test_dispara_en_background_y_responde_202(client, monkeypatch):
    class _T:
        def __init__(self, *a, **k):
            self.kwargs = k

        def start(self):
            pass

    app_module._pxq_sweep_state["running"] = False
    monkeypatch.setattr(app_module.threading, "Thread", _T)

    res = client.get("/admin/sweep-pxq-tiers?dry_run=1&limit=5&min_age_hours=12")

    assert res.status_code == 202
    body = json.loads(res.data)
    assert body["dry_run"] is True and body["limit"] == 5
    assert app_module._pxq_sweep_state["running"] is True
    app_module._pxq_sweep_state["running"] = False


def test_rechaza_un_segundo_barrido_concurrente(client, monkeypatch):
    monkeypatch.setattr(app_module.threading, "Thread", lambda *a, **k: None)
    app_module._pxq_sweep_state["running"] = True
    res = client.get("/admin/sweep-pxq-tiers")
    assert res.status_code == 409
    app_module._pxq_sweep_state["running"] = False


def test_limit_invalido_da_400(client):
    res = client.get("/admin/sweep-pxq-tiers?limit=abc")
    assert res.status_code == 400
