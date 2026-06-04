#!/usr/bin/env python3
"""Trigger /admin/sweep-shipping-costs and poll status until done. Cron-friendly."""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE = os.getenv("ML_WEBHOOK_BASE_URL", "https://ml-webhook.gaussonline.com.ar")


def http_get(url, timeout=30):
    req = Request(url, headers={"User-Agent": "sweep-shipping-costs/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Trigger and watch the seller shipping-cost sweep.")
    ap.add_argument("--base", default=DEFAULT_BASE, help="ml-webhook base URL")
    ap.add_argument("--limit", type=int, default=None, help="max MLAs to process")
    ap.add_argument("--dry-run", action="store_true", help="enumerate only, no UPSERT")
    ap.add_argument("--min-age-hours", type=int, default=0, help="skip MLAs fresher than N hours")
    ap.add_argument("--force", action="store_true", help="override the running-sweep lock")
    ap.add_argument("--poll-interval", type=int, default=30, help="seconds between status polls")
    ap.add_argument("--timeout-min", type=int, default=120, help="abort if not done in N minutes")
    args = ap.parse_args()

    params = {}
    if args.limit is not None:
        params["limit"] = args.limit
    if args.dry_run:
        params["dry_run"] = "1"
    if args.min_age_hours:
        params["min_age_hours"] = args.min_age_hours
    if args.force:
        params["force"] = "1"

    start_url = f"{args.base}/admin/sweep-shipping-costs"
    if params:
        start_url += "?" + urlencode(params)
    print(f"-> start: {start_url}")

    try:
        status, resp = http_get(start_url)
    except Exception as e:
        print(f"X failed to start sweep: {e}")
        return 2
    print(f"   http {status} -> {resp}")
    if status not in (200, 202):
        return 3

    poll_url = f"{args.base}/admin/sweep-shipping-costs?status=1"
    deadline = time.time() + args.timeout_min * 60
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        try:
            _, state = http_get(poll_url)
        except Exception as e:
            print(f"   poll error: {e}")
            continue
        print(
            f"   processed={state.get('processed')} "
            f"skipped={state.get('skipped')} "
            f"errors={state.get('errors')} "
            f"total={state.get('total_enumerated')} "
            f"last={state.get('last_mla')}"
        )
        if not state.get("running"):
            print(f"OK done: {state}")
            return 0 if state.get("errors", 0) == 0 else 1
    print("X timeout reached before sweep finished")
    return 4


if __name__ == "__main__":
    sys.exit(main())
