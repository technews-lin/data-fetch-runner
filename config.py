"""Load runtime configuration from RUNNER_CONFIG env var (JSON)."""
from __future__ import annotations
import json
import os


def load_config() -> dict:
    raw = os.environ.get("RUNNER_CONFIG")
    if not raw:
        raise SystemExit("RUNNER_CONFIG env var not set")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"RUNNER_CONFIG invalid JSON: {e}")


CFG = load_config()
BASE = CFG["target_base_url"]
CAPTCHA_MARKERS = tuple(CFG.get("captcha_markers", []))
REQUIRED_KEYWORDS = tuple(CFG.get("required_keywords", []))
SOURCE_PREFIX = CFG.get("source_prefix", "backfill")
SEARCH_URL_PATH = CFG.get("search_url_path", "")
DETAIL_REFERER_PATH = CFG.get("detail_referer_path", "/")
LIST_URL_PATH = CFG.get("list_url_path", "")
LIST_PAGE_PARAM = CFG.get("list_page_param", "page")
DAILY_SOURCES = CFG.get("daily_sources", [])

# Regex to find detail links in list/search HTML. group(1) = url, group(2) = kind discriminator.
DETAIL_LINK_REGEX = CFG.get(
    "detail_link_regex",
    r'href="([^"]*?/detail\?id=[\w=%]+)"'
)
# Maps the discriminator captured in group(2) to a kind label stored in DB.
KIND_MAP = CFG.get("kind_map", {})


def search_url(query: str, year_or_param: str) -> str:
    """Build the per-query search URL."""
    return BASE + SEARCH_URL_PATH.format(q=query, year=year_or_param)


def detail_referer() -> str:
    return BASE + DETAIL_REFERER_PATH


def list_url(start: str, end: str, page: int | None = None) -> str:
    """Build list URL for a date range. Optionally append page param."""
    url = BASE + LIST_URL_PATH.format(start=start, end=end)
    if page is not None:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{LIST_PAGE_PARAM}={page}"
    return url
