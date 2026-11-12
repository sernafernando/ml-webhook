"""Seller shipping cost per price bracket (ml_seller_shipping_costs).

ML charges the seller by price bracket, so quoting with item_id alone prices the
item at its list price. Fixtures are verbatim ML responses captured on 2026-11-12.
"""
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")

REAL = json.loads((Path(__file__).parent / "fixtures" / "shipping_brackets_real.json").read_text())


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _ml_router(calls, sale_price_status=200, free_status=200):
    """Serve the captured ML responses: /items/{id}/sale_price and shipping_options/free by item_price."""

    def fake(url, headers=None, params=None, max_retries=3):
        params = dict(params or {})
        calls.append((url, params))
        if url.endswith("/sale_price"):
            mla = url.split("/items/")[1].split("/")[0]
            return _Resp(sale_price_status, REAL[mla]["sale_price"])
        if "/shipping_options/free" in url:
            if free_status != 200:
                return _Resp(free_status, {})
            by_price = REAL[params["item_id"]]["free_by_item_price"]
            key = "none" if "item_price" not in params else str(params["item_price"])
            return _Resp(200, by_price[key])
        raise AssertionError(f"unexpected ML call {url}")

    return fake


def _free_calls(calls):
    return [p for u, p in calls if "/shipping_options/free" in u]


@pytest.fixture(autouse=True)
def _fresh_webhook_memo(monkeypatch):
    """The webhook memo is process-wide: each test starts without cached brackets."""
    if app_module is not None:
        monkeypatch.setattr(app_module, "_WEBHOOK_BRACKET_MEMO", app_module._TtlMemo(ttl_seconds=3600))


@pytest.fixture
def captured_upserts(monkeypatch):
    rows = []

    class _Cur:
        def execute(self, sql, params=None):
            rows.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield _Cur()

    monkeypatch.setattr(app_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(app_module, "get_token", lambda: "TOKEN")
    return rows


def test_real_case_mla1173065146_quotes_at_sale_price_6790_not_7290(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router(calls))
    item = REAL["MLA1173065146"]["item"]

    quote = app_module._quote_seller_shipping("MLA1173065146", item, {"Authorization": "x"})

    assert quote["sale_price"] == 41999.0
    assert quote["list_cost"] == 6790
    assert quote["billable_weight"] == 396
    assert quote["cost_by_bracket"] == [
        {"min_price": 0, "list_cost": 0},
        {"min_price": 33000, "list_cost": 6790},
        {"min_price": 50000, "list_cost": 7290},
    ]
    # every quote carries item_id + item_price: without item_price ML prices the list price
    assert all("item_price" in p and p["item_id"] == "MLA1173065146" for p in _free_calls(calls))


def test_upsert_stores_sale_price_cost_and_brackets(monkeypatch, captured_upserts):
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router([]))

    app_module._upsert_seller_shipping_cost("MLA1173065146", REAL["MLA1173065146"]["item"], source="webhook")

    sql, params = captured_upserts[-1]
    assert "cost_by_bracket" in sql and "sale_price" in sql
    assert params[0] == "MLA1173065146"
    assert params[2] == 6790  # list_cost
    stored_brackets = params[params.index(41999.0) + 1].adapted
    assert stored_brackets == [
        {"min_price": 0, "list_cost": 0},
        {"min_price": 33000, "list_cost": 6790},
        {"min_price": 50000, "list_cost": 7290},
    ]


def test_memo_reuses_brackets_for_same_weight_listing_and_logistic(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router(calls))
    memo = {}
    item = REAL["MLA1173065146"]["item"]

    app_module._quote_seller_shipping("MLA1173065146", item, {}, memo=memo)
    first = len(_free_calls(calls))
    again = app_module._quote_seller_shipping("MLA1173065146", item, {}, memo=memo)

    assert first == 3
    assert len(_free_calls(calls)) == first + 1  # only the probe that reveals billable_weight
    assert again["list_cost"] == 6790


def test_other_product_crossing_the_bracket_mla4153290772(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router([]))

    quote = app_module._quote_seller_shipping("MLA4153290772", REAL["MLA4153290772"]["item"], {})

    assert quote["sale_price"] == 40390.0
    assert quote["list_cost"] == 6790


def test_sale_price_unavailable_falls_back_to_item_price(monkeypatch):
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router([], sale_price_status=404))

    quote = app_module._quote_seller_shipping("MLA1173065146", REAL["MLA1173065146"]["item"], {})

    assert quote["sale_price"] == 65472.0
    assert quote["list_cost"] == 7290


def test_failed_quote_stores_nothing(monkeypatch, captured_upserts):
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router([], free_status=500))

    app_module._upsert_seller_shipping_cost("MLA1173065146", REAL["MLA1173065146"]["item"], source="webhook")

    assert captured_upserts == []


def test_bracket_floors_come_from_env(monkeypatch):
    assert app_module._parse_bracket_floors("0,33000,50000") == (0, 33000, 50000)
    assert app_module._parse_bracket_floors(" 50000 , 0,33000 ") == (0, 33000, 50000)
    assert app_module._parse_bracket_floors("") == (0, 33000, 50000)
    assert app_module._parse_bracket_floors("abc") == (0, 33000, 50000)


def test_sweep_stores_brackets_with_one_quote_per_repeated_shape(monkeypatch, captured_upserts):
    """Two MLAs with the same seller/weight/listing/logistic: the second reuses the brackets."""
    calls = []
    route = _ml_router(calls)
    item = REAL["MLA1173065146"]["item"]

    def fake_get(url, headers=None, params=None, max_retries=3):
        if url.endswith("/items/search"):
            calls.append((url, dict(params or {})))
            return _Resp(200, {"results": ["MLA1173065146", "MLA1173065146"], "scroll_id": None})
        if url.endswith("/items"):
            calls.append((url, dict(params or {})))
            return _Resp(200, [{"code": 200, "body": item}])
        return route(url, headers, params, max_retries)

    class _ConnCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

    class _Conn:
        autocommit = True

        def cursor(self):
            return _ConnCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    stored = []

    def fake_execute_values(cur, sql, rows, template=None, page_size=None):
        stored.append((sql, template, list(rows)))

    class _SellerCur:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (413658225,)

    @contextmanager
    def seller_cursor():
        yield _SellerCur()

    monkeypatch.setattr(app_module, "db_cursor", seller_cursor)
    monkeypatch.setattr(app_module, "ml_api_get", fake_get)
    monkeypatch.setattr(app_module.psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(app_module, "execute_values", fake_execute_values)
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql://fake")

    app_module._sweep_seller_shipping_costs(limit=None, dry_run=False, min_age_hours=0)

    sql, template, rows = stored[-1]
    assert "cost_by_bracket" in sql and template.count("%s") == 11
    assert [r[2] for r in rows] == [6790, 6790]  # list_cost at the 41999 sale price, both rows
    assert rows[0][9] == 41999.0
    assert rows[0][10].adapted[1] == {"min_price": 33000, "list_cost": 6790}
    assert rows[0][5] == "fulfillment"
    assert len(_free_calls(calls)) == 3 + 1  # full bracket set once, then only the weight probe


def test_memo_is_not_shared_without_listing_type(monkeypatch):
    """A failed /items lookup leaves listing_type_id unknown: never reuse another MLA's brackets then."""
    calls = []
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router(calls))
    memo = {}
    unknown = {"seller_id": 413658225, "shipping": {"logistic_type": "fulfillment"}}

    app_module._quote_seller_shipping("MLA1173065146", unknown, {}, memo=memo)
    app_module._quote_seller_shipping("MLA1173065146", unknown, {}, memo=memo)

    assert len(_free_calls(calls)) == 6  # both MLAs quoted every bracket
    assert memo == {}


def test_memo_key_includes_the_shipping_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router(calls))
    memo = {}
    item = REAL["MLA1173065146"]["item"]
    other_mode = {**item, "shipping": {**item["shipping"], "mode": "me1"}}

    app_module._quote_seller_shipping("MLA1173065146", item, {}, memo=memo)
    app_module._quote_seller_shipping("MLA1173065146", other_mode, {}, memo=memo)

    assert len(_free_calls(calls)) == 6


def _failing_floor(route, floor, response):
    def fake(url, headers=None, params=None, max_retries=3):
        if "/shipping_options/free" in url and (params or {}).get("item_price") == floor:
            return response
        return route(url, headers, params, max_retries)

    return fake


def test_a_lower_bracket_failing_after_the_top_stores_nothing(monkeypatch, captured_upserts):
    monkeypatch.setattr(app_module, "ml_api_get", _failing_floor(_ml_router([]), 33000, _Resp(500, {})))

    app_module._upsert_seller_shipping_cost("MLA1173065146", REAL["MLA1173065146"]["item"], source="webhook")

    assert captured_upserts == []


def test_a_200_without_coverage_is_a_failed_quote(monkeypatch, captured_upserts):
    monkeypatch.setattr(app_module, "ml_api_get", _failing_floor(_ml_router([]), 33000, _Resp(200, {})))

    app_module._upsert_seller_shipping_cost("MLA1173065146", REAL["MLA1173065146"]["item"], source="webhook")

    assert captured_upserts == []


def test_webhook_path_reuses_brackets_across_notifications(monkeypatch, captured_upserts):
    """Items webhooks of MLAs with the same shape: the full bracket set is quoted once per TTL, then 2 calls."""
    calls = []
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router(calls))
    monkeypatch.setattr(app_module, "_WEBHOOK_BRACKET_MEMO", app_module._TtlMemo(ttl_seconds=3600))
    item = REAL["MLA1173065146"]["item"]

    app_module._upsert_seller_shipping_cost("MLA1173065146", item, source="webhook")
    first = len(calls)
    app_module._upsert_seller_shipping_cost("MLA1173065146", item, source="webhook")

    assert first == 4  # 3 brackets + sale_price
    assert len(calls) - first == 2  # weight probe + sale_price
    assert captured_upserts[-1][1][2] == 6790


def test_webhook_memo_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: now[0])
    memo = app_module._TtlMemo(ttl_seconds=60)

    memo["k"] = {"cached": True}
    assert memo.get("k") == {"cached": True}
    now[0] += 61
    assert memo.get("k") is None


def test_a_rate_limited_quote_is_logged_as_429(monkeypatch, capsys):
    monkeypatch.setattr(app_module, "ml_api_get", _ml_router([], free_status=429))

    quote = app_module._quote_seller_shipping("MLA1173065146", REAL["MLA1173065146"]["item"], {})

    assert quote is None
    assert "429" in capsys.readouterr().out
