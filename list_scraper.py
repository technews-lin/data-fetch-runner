"""list_scraper.py — Fetch list pages and POST rows to backend in parallel.

env vars:
  API_ENDPOINT, API_TOKEN
  RUNNER_CONFIG           # JSON config (see config.py)
  START_DATE              # e.g. "2025/10/01"
  END_DATE                # e.g. "2025/12/31"
  SOURCE_TAG              # e.g. "2025q4" — final source = <source_prefix>_<tag>
  PAGE_START              # default 1
  PAGE_END                # default 50
  LIST_SEEN_DATE          # default today (YYYY-MM-DD)
  PACING_SEC              # default 2.0
  PARALLEL_POSTS          # default 10
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr

from config import (
    BASE, CAPTCHA_MARKERS, REQUIRED_KEYWORDS, SOURCE_PREFIX,
    LIST_PAGE_PARAM, DETAIL_LINK_REGEX, KIND_MAP,
    list_url, detail_referer,
)

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
START_DATE = os.environ["START_DATE"]
END_DATE = os.environ["END_DATE"]
SOURCE_TAG = os.environ["SOURCE_TAG"]
PAGE_START = int(os.environ.get("PAGE_START", "1"))
PAGE_END = int(os.environ.get("PAGE_END", "50"))
LIST_SEEN_DATE = os.environ.get("LIST_SEEN_DATE", time.strftime("%Y-%m-%d"))
PACING_SEC = float(os.environ.get("PACING_SEC", "2.0"))
PARALLEL_POSTS = int(os.environ.get("PARALLEL_POSTS", "10"))
SOURCE = f"{SOURCE_PREFIX}_{SOURCE_TAG}"

TIMEOUT = 30
RETRY = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


def fetch_page(session, page_idx):
    s = urllib.parse.quote(START_DATE, safe="")
    e = urllib.parse.quote(END_DATE, safe="")
    url = list_url(s, e, page_idx)
    for attempt in range(RETRY):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            print(f"  page {page_idx} HTTP {r.status_code}, retry {attempt+1}", flush=True)
        except Exception as ex:
            print(f"  page {page_idx} err {type(ex).__name__}: {ex}, retry {attempt+1}", flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"page {page_idx} failed after {RETRY} retries")


def parse_page(html):
    rows = []
    seen = set()
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        row_html = tr_m.group(1)
        link_m = re.search(DETAIL_LINK_REGEX, row_html)
        if not link_m: continue
        href = link_m.group(1).replace("&amp;", "&")
        kind = KIND_MAP.get(link_m.group(2), "unknown")
        abs_url = href if href.startswith("http") else BASE + href
        pk_m = re.search(r"[?&]pk=([\w=%]+)", abs_url)
        if not pk_m: continue
        pk = urllib.parse.unquote(pk_m.group(1))
        pk = urllib.parse.unquote(pk_m.group(1))
        if pk in seen: continue
        seen.add(pk)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
        text_cells = [clean(c) for c in cells]
        rows.append({"pk": pk, "kind": kind, "detail_url": abs_url, "cells": text_cells})
    return rows


def parse_cells_for_db(cells):
    out = {"org": None, "case_no": None, "case_name": None, "list_announce_date": None, "budget": None}
    if len(cells) >= 2: out["org"] = cells[1] or None
    if len(cells) >= 3:
        text = cells[2]
        m = re.match(r"\s*(\S+)", text)
        if m: out["case_no"] = m.group(1)
        m = re.search(r'pageCode2Img\(["\']([^"\']+)["\']', text)
        if m: out["case_name"] = m.group(1)
    if len(cells) >= 6: out["list_announce_date"] = cells[5] or None
    if len(cells) >= 7: out["budget"] = cells[6] or None
    return out


def post_chunk(rows):
    payload = [
        {
            "source": SOURCE,
            "source_subtype": SOURCE_PREFIX,
            "pk": r["pk"],
            "case_no": p["case_no"], "org": p["org"], "case_name": p["case_name"],
            "detail_url": r["detail_url"],
            "list_announce_date": p["list_announce_date"],
            "list_deadline": None,
            "budget_text": p["budget"],
        }
        for r in rows
        for p in [parse_cells_for_db(r["cells"])]
    ]
    for attempt in range(3):
        try:
            resp = cr.post(
                f"{API_ENDPOINT}/api/daily-bulk-insert",
                headers={"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"},
                data=json.dumps({"rows": payload, "list_seen_date": LIST_SEEN_DATE}),
                timeout=180,
            )
            j = resp.json()
            return j.get("inserted", 0), j.get("skipped", 0), None
        except Exception as ex:
            if attempt == 2: return 0, 0, str(ex)[:200]
            time.sleep(2 * (attempt + 1))


def main():
    print(f"start range={START_DATE}~{END_DATE} src={SOURCE} pages={PAGE_START}-{PAGE_END}", flush=True)
    print(f"  PACING_SEC={PACING_SEC} PARALLEL_POSTS={PARALLEL_POSTS} LIST_SEEN_DATE={LIST_SEEN_DATE}", flush=True)

    session = cr.Session(impersonate="chrome120")
    session.get(detail_referer(), headers=HEADERS, timeout=TIMEOUT)

    pool = ThreadPoolExecutor(max_workers=PARALLEL_POSTS)
    futures = []
    total_rows = 0
    total_ins = total_skip = 0
    t0 = time.time()

    consecutive_empty = 0
    for page in range(PAGE_START, PAGE_END + 1):
        if page > PAGE_START:
            time.sleep(PACING_SEC)
        t_page = time.time()
        html = fetch_page(session, page)
        fetch_sec = time.time() - t_page
        rows = parse_page(html)
        if not rows:
            consecutive_empty += 1
            print(f"  page {page}: 0 rows ({fetch_sec:.1f}s) [empty {consecutive_empty}/2]", flush=True)
            if consecutive_empty >= 2:
                print(f"  stop early: 2 consecutive empty pages (likely past last page)", flush=True)
                break
            continue
        consecutive_empty = 0
        total_rows += len(rows)
        fut = pool.submit(post_chunk, rows)
        futures.append((page, fut))
        print(f"  page {page}: {len(rows)} rows ({fetch_sec:.1f}s) → dispatched", flush=True)

    print(f"all pages fetched, waiting for {len(futures)} POST...", flush=True)
    for page, fut in futures:
        ins, skip, err = fut.result()
        total_ins += ins
        total_skip += skip
        if err: print(f"  page {page} POST ERR: {err}", flush=True)

    pool.shutdown(wait=True)
    elapsed = time.time() - t0
    n = PAGE_END - PAGE_START + 1
    print(f"DONE pages={n} rows={total_rows} ins={total_ins} skip={total_skip} elapsed={elapsed:.1f}s avg={elapsed/n:.1f}s/page", flush=True)


if __name__ == "__main__":
    main()
