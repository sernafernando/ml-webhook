from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._result = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "SELECT COUNT(*) FROM webhook_latest" in q:
            topic = params[0]
            self._result = [(sum(1 for row in self.db if row[12] == topic),)]
            return

        if "FROM webhook_latest wl" in q:
            topic = params[0]
            rows = [row for row in self.db if row[12] == topic]
            if "AND (wl.received_at, wl.resource) <" in q:
                cursor_ts, cursor_resource = params[1], params[2]
                rows = [row for row in rows if (row[8], row[11]) < (cursor_ts, cursor_resource)]
                limit = params[3]
            elif "OFFSET" in q:
                limit, offset = params[1], params[2]
                rows = rows[offset:offset + limit]
                self._result = rows
                return
            else:
                limit = params[1]
            self._result = rows[:limit]

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result


@pytest.fixture
def client(monkeypatch):
    row1 = (
        {"resource": "/items/MLA1", "topic": "items"},
        "Item 1", 10, "ARS", None, None, None, "winning",
        datetime(2026, 4, 10, 18, 0, 0, tzinfo=timezone.utc),
        "Brand 1", {}, "/items/MLA1", "items",
    )
    row2 = (
        {"resource": "/items/MLA2", "topic": "items"},
        "Item 2", 20, "ARS", None, None, None, "competing",
        datetime(2026, 4, 10, 17, 59, 0, tzinfo=timezone.utc),
        "Brand 2", {}, "/items/MLA2", "items",
    )
    db = [row1, row2]

    @contextmanager
    def fake_db_cursor():
        yield _Cursor(db)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "WEBHOOKS_CURSOR_MODE", True)

    with app_module.app.test_client() as c:
        yield c


def test_limit_is_clamped_to_max(client):
    res = client.get("/api/webhooks?topic=items&limit=9999")
    body = res.get_json()

    assert res.status_code == 200
    assert body["pagination"]["limit"] == app_module.WEBHOOKS_MAX_LIMIT


def test_omitted_limit_uses_default_and_returns_pagination_metadata(client):
    res = client.get("/api/webhooks?topic=items")
    body = res.get_json()

    assert res.status_code == 200
    assert body["pagination"]["limit"] == app_module.WEBHOOKS_DEFAULT_LIMIT
    assert body["pagination"]["total"] == 2
    assert "mode" in body["pagination"]


def test_invalid_cursor_returns_400(client):
    res = client.get("/api/webhooks?topic=items&cursor=esto-no-es-base64")

    assert res.status_code == 400
    assert "cursor" in res.get_json()["error"]


def test_cursor_pagination_returns_next_cursor_without_duplicates(client):
    first = client.get("/api/webhooks?topic=items&limit=1")
    first_body = first.get_json()
    second = client.get(f"/api/webhooks?topic=items&limit=1&cursor={first_body['pagination']['next_cursor']}")
    second_body = second.get_json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_body["events"][0]["resource"] != second_body["events"][0]["resource"]


def test_latest_event_per_resource_uses_most_recent_snapshot_row(client, monkeypatch):
    duplicate_resource_rows = [
        (
            {"resource": "/items/MLA1", "topic": "items", "marker": "new"},
            "Item 1 newest", 11, "ARS", None, None, None, "winning",
            datetime(2026, 4, 10, 18, 1, 0, tzinfo=timezone.utc),
            "Brand 1", {}, "/items/MLA1", "items",
        ),
        (
            {"resource": "/items/MLA1", "topic": "items", "marker": "old"},
            "Item 1 old", 9, "ARS", None, None, None, "competing",
            datetime(2026, 4, 10, 18, 0, 0, tzinfo=timezone.utc),
            "Brand 1", {}, "/items/MLA1", "items",
        ),
    ]

    class LatestOnlyCursor(_Cursor):
        def execute(self, query, params=None):
            q = " ".join(query.split())
            if "SELECT COUNT(*) FROM webhook_latest" in q:
                self._result = [(1,)]
                return
            if "FROM webhook_latest wl" in q:
                resource_groups = {}
                for row in duplicate_resource_rows:
                    current = resource_groups.get(row[11])
                    if current is None or row[8] > current[8]:
                        resource_groups[row[11]] = row
                self._result = list(resource_groups.values())
                return
            super().execute(query, params)

    @contextmanager
    def fake_db_cursor():
        yield LatestOnlyCursor(duplicate_resource_rows)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)

    with app_module.app.test_client() as test_client:
        res = test_client.get("/api/webhooks?topic=items&limit=100")

    body = res.get_json()
    assert res.status_code == 200
    assert len(body["events"]) == 1
    assert body["events"][0]["marker"] == "new"
