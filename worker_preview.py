import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app import (
    _redis_client,
    PREVIEW_QUEUE_KEY,
    PREVIEW_DEAD_QUEUE_KEY,
    fetch_and_store_preview,
)


MAX_ATTEMPTS = 3


def _enqueue_dead_letter(message: dict, error: str):
    if _redis_client is None:
        return
    payload = dict(message)
    payload["failed_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    payload["error"] = error
    _redis_client.rpush(PREVIEW_DEAD_QUEUE_KEY, json.dumps(payload))


def _retry_or_dead(message: dict, error: str):
    attempt = int(message.get("attempt", 1))
    if attempt >= MAX_ATTEMPTS:
        _enqueue_dead_letter(message, error)
        return

    next_attempt = attempt + 1
    backoff_seconds = min(10, next_attempt * 2)
    time.sleep(backoff_seconds)
    message["attempt"] = next_attempt
    message["last_error"] = error
    message["requeued_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    _redis_client.rpush(PREVIEW_QUEUE_KEY, json.dumps(message))


def run_worker():
    if _redis_client is None:
        raise RuntimeError("Redis no está disponible. No se puede iniciar worker_preview.")

    print(f"🔄 worker_preview escuchando cola: {PREVIEW_QUEUE_KEY}")
    while True:
        item = _redis_client.blpop(PREVIEW_QUEUE_KEY, timeout=5)
        if not item:
            continue

        _, raw_message = item
        try:
            message = json.loads(raw_message)
            resource = message.get("resource")
            if not resource:
                raise ValueError("Mensaje sin 'resource'")

            fetch_and_store_preview(resource)
            print(f"✅ Preview procesado: {resource}")
        except Exception as err:
            print(f"❌ Error procesando preview queue: {err}")
            try:
                parsed = message if isinstance(message, dict) else {"raw": raw_message, "attempt": 1}
            except Exception:
                parsed = {"raw": raw_message, "attempt": 1}
            _retry_or_dead(parsed, str(err))


if __name__ == "__main__":
    run_worker()
