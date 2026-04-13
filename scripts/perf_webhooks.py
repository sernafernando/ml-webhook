import json
import os
import statistics
import time
from urllib import parse, request


BASE_URL = os.getenv("PERF_BASE_URL", "http://localhost:3000")
TOPIC = os.getenv("PERF_TOPIC", "items")
READ_RUNS = int(os.getenv("PERF_READ_RUNS", "20"))
WRITE_RUNS = int(os.getenv("PERF_WRITE_RUNS", "10"))


def measure_get_webhooks():
    durations = []
    for _ in range(READ_RUNS):
        url = f"{BASE_URL}/api/webhooks?{parse.urlencode({'topic': TOPIC, 'limit': 100})}"
        start = time.perf_counter()
        with request.urlopen(url) as response:
            response.read()
        durations.append((time.perf_counter() - start) * 1000)
    return durations


def measure_post_webhook():
    durations = []
    for i in range(WRITE_RUNS):
        payload = json.dumps({
            "_id": f"perf-{i:04d}",
            "topic": TOPIC,
            "user_id": 1,
            "resource": f"/items/MLA-PERF-{i}",
        }).encode("utf-8")
        req = request.Request(
            f"{BASE_URL}/webhook",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.perf_counter()
        with request.urlopen(req) as response:
            response.read()
        durations.append((time.perf_counter() - start) * 1000)
    return durations


def summarize(label, values):
    sorted_values = sorted(values)
    p95_index = max(0, min(len(sorted_values) - 1, round(len(sorted_values) * 0.95) - 1))
    print(f"\n{label}")
    print(f"runs={len(values)}")
    print(f"p50={statistics.median(values):.2f}ms")
    print(f"p95={sorted_values[p95_index]:.2f}ms")


if __name__ == "__main__":
    summarize("GET /api/webhooks", measure_get_webhooks())
    summarize("POST /webhook", measure_post_webhook())
