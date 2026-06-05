from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _RecordingCursor:
    """Routes each INSERT into a shared sink keyed by target table."""

    def __init__(self, sink):
        self._sink = sink

    def execute(self, query, params=None):
        if "INSERT INTO ml_cancelled_orders" in query:
            self._sink["cancelled_params"] = params
        elif "INSERT INTO ml_previews" in query:
            self._sink["preview_params"] = params

    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture
def patched(monkeypatch):
    sink = {}

    @contextmanager
    def fake_db_cursor():
        yield _RecordingCursor(sink)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "get_token", lambda: "fake-token")
    monkeypatch.setattr(app_module, "sse_notify", lambda *a, **k: None, raising=False)
    return sink


def _patch_order(monkeypatch, order_payload):
    monkeypatch.setattr(
        app_module, "ml_api_get", lambda url, **kw: _FakeResponse(order_payload)
    )


# ml_previews INSERT column order: resource, title, price, currency_id,
# thumbnail, winner, winner_price, status, brand, extra_data
PV_TITLE = 1
PV_STATUS = 7

# ml_cancelled_orders INSERT column order: order_id, pack_id, status,
# status_detail, cancelled_by, date_created, date_closed, total_amount,
# currency_id, buyer_id, buyer_nickname, seller_id, items, payload
CO_ORDER_ID = 0
CO_STATUS = 2
CO_STATUS_DETAIL = 3
CO_CANCELLED_BY = 4
CO_TOTAL = 7
CO_ITEMS = 12


def test_cancelled_order_is_written_to_ml_cancelled_orders(patched, monkeypatch):
    order_payload = {
        "id": 2000003508897476,
        "status": "cancelled",
        "date_created": "2026-06-01T10:00:00.000-03:00",
        "date_closed": "2026-06-02T11:00:00.000-03:00",
        "total_amount": 15999.0,
        "currency_id": "ARS",
        "order_items": [
            {
                "item": {
                    "id": "MLA123",
                    "title": "Auricular Bluetooth XYZ",
                    "seller_sku": "SKU-XYZ-1",
                },
                "quantity": 1,
                "unit_price": 15999.0,
            }
        ],
        "buyer": {"id": 55, "nickname": "COMPRADOR123"},
        "seller": {"id": 99},
        "cancel_detail": {
            "code": "Cancelled by seller",
            "description": "El vendedor canceló la venta",
            "requested_by": "respondent",
        },
    }
    _patch_order(monkeypatch, order_payload)

    result = app_module.fetch_and_store_preview("/orders/2000003508897476")

    # The cancellation row must be persisted for pricing-app to query
    co = patched["cancelled_params"]
    assert co is not None, "cancelled order was not written to ml_cancelled_orders"
    assert co[CO_ORDER_ID] == 2000003508897476
    assert co[CO_STATUS] == "cancelled"
    assert co[CO_STATUS_DETAIL] == "El vendedor canceló la venta"
    assert co[CO_CANCELLED_BY] == "respondent"
    assert co[CO_TOTAL] == 15999.0

    items = co[CO_ITEMS].adapted  # psycopg2 Json wraps the list in .adapted
    assert items[0]["item_id"] == "MLA123"
    assert items[0]["seller_sku"] == "SKU-XYZ-1"
    assert items[0]["quantity"] == 1

    # Preview row is still produced with a usable title + status
    assert result["status"] == "cancelled"
    pv = patched["preview_params"]
    assert pv[PV_TITLE]
    assert pv[PV_STATUS] == "cancelled"


def test_paid_order_is_not_written_to_ml_cancelled_orders(patched, monkeypatch):
    order_payload = {
        "id": 999,
        "status": "paid",
        "currency_id": "ARS",
        "total_amount": 100.0,
        "order_items": [
            {"item": {"id": "MLA9", "title": "Producto Pago"}, "quantity": 2, "unit_price": 50.0}
        ],
        "buyer": {"id": 7, "nickname": "PAGADOR"},
    }
    _patch_order(monkeypatch, order_payload)

    result = app_module.fetch_and_store_preview("/orders/999")

    assert result["status"] == "paid"
    assert "cancelled_params" not in patched, "paid order must NOT hit ml_cancelled_orders"
    pv = patched["preview_params"]
    assert pv[PV_STATUS] == "paid"
