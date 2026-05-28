"""scrape_list_pcc.py — 在 GHA 上跑 PCC readTenderAgent 列表頁，parallel POST 進 CF daily_tasks。

env vars:
  API_ENDPOINT, API_TOKEN  # CF Worker
  START_DATE  "2025/10/01"
  END_DATE    "2025/12/31"
  SOURCE_TAG  "2025q4"
  PAGE_START  1
  PAGE_END    50
  LIST_SEEN_DATE  "2026-05-28"
  PACING_SEC  2.0
  PARALLEL_POSTS  10  # 同時最多幾個 POST
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr

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
SOURCE = f"pcc_award_backfill_{SOURCE_TAG}"

BASE = "https://web.pcc.gov.tw"
PAGE_PARAM = "d-16396-p"
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


def build_list_url(start, end):
    s = urllib.parse.quote(start, safe="")
    e = urllib.parse.quote(end, safe="")
    return (
        f"{BASE}/prkms/tender/common/agent/readTenderAgent"
        f"?pageSize=100&firstSearch=false&isQuery=&isBinding=N&isLogIn=N"
        f"&tenderStatus=TENDER_STATUS_1&tenderWay=TENDER_WAY_ALL_DECLARATION"
        f"&awardAnnounceStartDate={s}&awardAnnounceEndDate={e}"
        f"&tenderRange=TENDER_RANGE_ALL"
    )


def fetch_page(session, list_url, page_idx):
    url = f"{list_url}&{PAGE_PARAM}={page_idx}"
    for attempt in range(RETRY):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            print(f"  page {page_idx} HTTP {r.status_code}, retry {attempt+1}", flush=True)
        except Exception as e:
            print(f"  page {page_idx} err {type(e).__name__}: {e}, retry {attempt+1}", flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"page {page_idx} failed after {RETRY} retries")


def parse_page(html):
    rows = []
    seen_pks = set()
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        row_html = tr_m.group(1)
        link_m = re.search(r'href="([^"]*?/urlSelector/common/(atm|nonAtm)\?pk=[\w=%]+)"', row_html)
        if not link_m: continue
        href = link_m.group(1).replace("&amp;", "&")
        kind = "non_award" if link_m.group(2) == "nonAtm" else "award"
        abs_url = href if href.startswith("http") else BASE + href
        pk_m = re.search(r"pk=([\w=%]+)", abs_url)
        pk = urllib.parse.unquote(pk_m.group(1))
        if pk in seen_pks: continue
        seen_pks.add(pk)
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
            "source_subtype": "pcc_award_backfill",
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
        except Exception as e:
            if attempt == 2: return 0, 0, str(e)[:200]
            time.sleep(2 * (attempt + 1))


def main():
    print(f"start range={START_DATE}~{END_DATE} src={SOURCE} pages={PAGE_START}-{PAGE_END}", flush=True)
    print(f"  PACING_SEC={PACING_SEC} PARALLEL_POSTS={PARALLEL_POSTS} LIST_SEEN_DATE={LIST_SEEN_DATE}", flush=True)

    list_url = build_list_url(START_DATE, END_DATE)
    session = cr.Session(impersonate="chrome120")
    session.get(f"{BASE}/prkms/tender/common/bulletion/readBulletion", headers=HEADERS, timeout=TIMEOUT)

    # 共用 thread pool dispatch POST，邊抓邊送
    pool = ThreadPoolExecutor(max_workers=PARALLEL_POSTS)
    futures = []
    total_rows = 0
    total_ins = 0
    total_skip = 0
    t0 = time.time()

    for page in range(PAGE_START, PAGE_END + 1):
        if page > PAGE_START:
            time.sleep(PACING_SEC)
        t_page = time.time()
        html = fetch_page(session, list_url, page)
        fetch_sec = time.time() - t_page
        rows = parse_page(html)
        if not rows:
            print(f"  page {page}: 0 rows ({fetch_sec:.1f}s)", flush=True)
            continue
        total_rows += len(rows)
        # dispatch POST 不等
        fut = pool.submit(post_chunk, rows)
        futures.append((page, fut))
        print(f"  page {page}: {len(rows)} rows ({fetch_sec:.1f}s) → dispatched", flush=True)

    # 等所有 POST 完成
    print(f"all pages fetched, waiting for {len(futures)} POST to complete...", flush=True)
    for page, fut in futures:
        ins, skip, err = fut.result()
        total_ins += ins
        total_skip += skip
        if err: print(f"  page {page} POST ERR: {err}", flush=True)

    pool.shutdown(wait=True)
    elapsed = time.time() - t0
    n_pages = PAGE_END - PAGE_START + 1
    print(f"DONE pages={n_pages} rows={total_rows} ins={total_ins} skip={total_skip} elapsed={elapsed:.1f}s avg={elapsed/n_pages:.1f}s/page", flush=True)


if __name__ == "__main__":
    main()
