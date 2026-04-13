# Performance Baseline / Rollout Tracking

## Baseline Template

- Date:
- Commit:
- Environment:
- Dataset / topic volume:
- `WEBHOOK_PREVIEW_ASYNC`:
- `WEBHOOKS_CURSOR_MODE`:

### Metrics

| Endpoint | Runs | p50 | p95 | Notes |
|---|---:|---:|---:|---|
| `GET /api/webhooks?topic=items&limit=100` |  |  |  |  |
| `POST /webhook` |  |  |  |  |

## Rollout Plan

1. Apply SQL migrations from `migrations/`.
2. Capture baseline with `python scripts/perf_webhooks.py` and store values above.
3. Enable `WEBHOOK_PREVIEW_ASYNC=1` and deploy `worker_preview.py`.
4. Re-run measurements and compare p50/p95 against baseline.
5. Enable `WEBHOOKS_CURSOR_MODE=1`.
6. Re-run measurements and validate success criteria.

## Success Criteria Checklist

- [ ] `GET /api/webhooks` p95 reduced by at least 60% vs baseline.
- [ ] Home first page loads in under 2 seconds under representative volume.
- [ ] `POST /webhook` returns without waiting for remote preview enrichment.
- [ ] No functional regression in filters, pagination, or preview refresh flow.

## Offset Deprecation Decision

- Current status: dual mode (`offset` + `cursor`) enabled for safe rollout.
- External consumers identified:
- Deprecation target date:
- Removal approved by:
