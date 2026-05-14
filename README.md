# Data Fetch Runner

Generic ETL utility for fetching URLs and posting results to a configured API endpoint.

Configuration via secrets:
- `API_ENDPOINT` — target API base URL
- `API_TOKEN` — bearer token

Usage: triggered manually via workflow_dispatch with a `runner_id` input.
