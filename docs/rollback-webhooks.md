# Rollback Procedure — Webhooks Performance Change

## Immediate Rollback

1. Set `WEBHOOKS_CURSOR_MODE=0`.
2. Set `WEBHOOK_PREVIEW_ASYNC=0`.
3. Restart backend application.
4. Stop `worker_preview.py` if it is running.

## Validation After Rollback

- [ ] `GET /api/webhooks` answers successfully in offset mode.
- [ ] `POST /webhook` still returns `200` for valid payloads.
- [ ] Topics list still loads from `/api/webhooks/topics`.
- [ ] No queue growth remains in Redis dead-letter list.

## Notes

- `webhook_latest` table can remain in the database during rollback; it is non-destructive.
- Re-enable async/cursor only after comparing new p50/p95 metrics with the stable baseline.
