import json

import pytest

import worker_preview


class FakeRedis:
    def __init__(self):
        self.calls = []

    def rpush(self, key, payload):
        self.calls.append((key, json.loads(payload)))


@pytest.fixture
def fake_redis(monkeypatch):
    redis_client = FakeRedis()
    monkeypatch.setattr(worker_preview, "_redis_client", redis_client)
    monkeypatch.setattr(worker_preview.time, "sleep", lambda _: None)
    return redis_client


def test_retry_or_dead_requeues_preview_job_before_max_attempts(fake_redis):
    message = {"resource": "/items/MLA123", "attempt": 1}

    worker_preview._retry_or_dead(message, "temporary failure")

    assert len(fake_redis.calls) == 1
    queue_key, payload = fake_redis.calls[0]
    assert queue_key == worker_preview.PREVIEW_QUEUE_KEY
    assert payload["resource"] == "/items/MLA123"
    assert payload["attempt"] == 2
    assert payload["last_error"] == "temporary failure"


def test_retry_or_dead_sends_message_to_dead_letter_after_max_attempts(fake_redis):
    message = {"resource": "/items/MLA123", "attempt": worker_preview.MAX_ATTEMPTS}

    worker_preview._retry_or_dead(message, "permanent failure")

    assert len(fake_redis.calls) == 1
    queue_key, payload = fake_redis.calls[0]
    assert queue_key == worker_preview.PREVIEW_DEAD_QUEUE_KEY
    assert payload["resource"] == "/items/MLA123"
    assert payload["error"] == "permanent failure"
