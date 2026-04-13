import json
from contextlib import contextmanager

import pytest

try:
    import app as app_module
except Exception as exc:  # pragma: no cover - entorno sin DB/Redis
    app_module = None
    pytestmark = pytest.mark.skip(reason=f"No se pudo importar app.py: {exc}")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result = []

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

    def fetchone(self):
        return self._result[0] if self._result else (0,)

    def fetchall(self):
        return self._result


@pytest.fixture
def client(monkeypatch):
    db = {"seen_ids": set()}

    @contextmanager
    def fake_db_cursor():
        yield _Cursor(db)

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "WEBHOOK_PREVIEW_ASYNC", True)
    monkeypatch.setattr(app_module, "_enqueue_preview_job", lambda resource: (True, None))
    monkeypatch.setattr(
        app_module,
        "fetch_and_store_preview",
        lambda resource: (_ for _ in ()).throw(AssertionError("No debe ejecutarse inline en async mode")),
    )

    with app_module.app.test_client() as c:
        yield c


def test_webhook_async_ack_without_blocking_preview(client):
    payload = {
        "_id": "00000000-0000-0000-0000-000000000111",
        "topic": "items",
        "user_id": 123,
        "resource": "/items/MLA123/price_to_win",
    }
    res = client.post("/webhook", data=json.dumps(payload), content_type="application/json")

    assert res.status_code == 200
    assert b"Evento recibido" in res.data


def test_webhook_returns_200_and_records_enqueue_failure_for_diagnostics(monkeypatch):
    db = {"seen_ids": set()}

    @contextmanager
    def fake_db_cursor():
        yield _Cursor(db)

    background_calls = []

    monkeypatch.setattr(app_module, "db_cursor", fake_db_cursor)
    monkeypatch.setattr(app_module, "WEBHOOK_PREVIEW_ASYNC", True)
    monkeypatch.setattr(app_module, "DEBUG_WEBHOOK", True)
    monkeypatch.setattr(app_module, "_enqueue_preview_job", lambda resource: (False, "redis_down"))
    monkeypatch.setattr(app_module, "_run_preview_in_background", lambda resource: background_calls.append(resource))

    payload = {
        "_id": "00000000-0000-0000-0000-000000000112",
        "topic": "items",
        "user_id": 123,
        "resource": "/items/MLA124/price_to_win",
    }

    with app_module.app.test_client() as test_client:
        res = test_client.post("/webhook", data=json.dumps(payload), content_type="application/json")

    body = res.get_json()
    assert res.status_code == 200
    assert background_calls == ["/items/MLA124/price_to_win"]
    assert "enqueue_preview_job: redis_down" in body["errors"]
