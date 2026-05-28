# Data Fetch Runner

Generic ETL fetch worker. Claims work items from a configured backend, fetches
the URL referenced by each item, posts the result back. All target-specific
behaviour (URL templates, validation markers, source identifiers) is loaded
from a single `RUNNER_CONFIG` JSON secret — the runner code is target-agnostic.

## Secrets

| Name | Description |
|---|---|
| `API_ENDPOINT` | Backend base URL |
| `API_TOKEN` | Bearer token for the backend API |
| `RUNNER_CONFIG` | JSON: `target_base_url`, URL paths, captcha markers, required keywords, source prefix, list-source definitions, regex patterns |

## Workflows

| Workflow | Purpose |
|---|---|
| `daily.yml` | Drains today's daily queue (`source_filter=daily`) |
| `backfill.yml` | Drains the backfill queue (`source_filter=backfill`) |
| `scrape_list.yml` ("List Page Fetch") | One-off list-page fetch (page range) |
| `scrape_quarter.yml` ("Quarter List Fetch") | Matrix fan-out over a date range |
| `run.yml` | Legacy search+detail worker (manual only) |

## Local dev

```bash
export API_ENDPOINT=... API_TOKEN=... RUNNER_CONFIG="$(cat config.json)"
python3 daily_worker.py
```
