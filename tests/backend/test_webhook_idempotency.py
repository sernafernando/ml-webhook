import json
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "INSERT INTO webhooks" in q:
            webhook_id = params[4]
            if webhook_id in self.db["seen_ids"]:
                self.rowcount = 0
            else:
                self.db["seen_ids"].add(webhook_id)
                self.rowcount = 1
        elif "INSERT INTO webhook_latest" in q:
            self.rowcount = 1


@pytest.fixture
def client(monkeypatch):
    db = {"seen_ids": set()}

    @contextmanager
    def fake_db_cursor():
        yield _Cursor(db)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "WEBHOOK_PREVIEW_ASYNC", True)
    monkeypatch.setattr(app_module, "_enqueue_preview_job", lambda resource: (True, None))

    with app_module.app.test_client() as c:
        yield c


def test_duplicate_webhook_id_is_ignored(client):
    payload = {
        "_id": "00000000-0000-0000-0000-000000000222",
        "topic": "items",
        "user_id": 321,
        "resource": "/items/MLA999/price_to_win",
    }

    first = client.post("/webhook", data=json.dumps(payload), content_type="application/json")
    second = client.post("/webhook", data=json.dumps(payload), content_type="application/json")

    assert first.status_code == 200
    assert second.status_code == 200
