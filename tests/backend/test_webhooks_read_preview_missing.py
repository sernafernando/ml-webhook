from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Cursor:
    def __init__(self):
        self._result = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "SELECT COUNT(*) FROM webhook_latest" in q:
            self._result = [(1,)]
        elif "FROM webhook_latest wl" in q:
            self._result = [(
                {"resource": "/items/MLA999", "topic": "items"},
                None, None, None, None, None, None, None,
                datetime(2026, 4, 10, 18, 0, 0, tzinfo=timezone.utc),
                None, None, "/items/MLA999"
            )]

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def fake_db_cursor():
        yield _Cursor()

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)

    with app_module.app.test_client() as c:
        yield c


def test_missing_preview_does_not_break_response(client):
    res = client.get("/api/webhooks?topic=items&limit=100")
    body = res.get_json()

    assert res.status_code == 200
    assert len(body["events"]) == 1
    assert body["events"][0]["db_preview"]["title"] is None
    assert body["events"][0]["db_preview"]["extra_data"] == {}
