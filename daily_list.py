"""Daily list-page scraper.

Daily morning job:
1. fetch a configured set of list URLs (no captcha risk)
2. parse rows (with pk + detail_url)
3. POST to backend /api/daily-bulk-insert (UNIQUE(source, pk) dedups)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.parse
import warnings
from datetime import datetime

from curl_cffi import requests as cr

from config import BASE, DAILY_SOURCES

warnings.filterwarnings("ignore")

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
HEADERS_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

# Full Chrome 131 header set (some targets slow-lane requests lacking these).
TARGET_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
TODAY = datetime.utcnow().strftime("%Y/%m/%d")
TODAY_DASH = datetime.utcnow().strftime("%Y-%m-%d")

# Build SOURCES from config (url_path with {today} placeholder)
SOURCES = [
    {
        "source": s["source"],
        "subtype": s.get("subtype"),
        "max_pages": s.get("max_pages", 50),
        "url": BASE + s["url_path"].format(today=urllib.parse.quote(TODAY)),
    }
    for s in DAILY_SOURCES
]

from config import CFG
_DAILY_LINK_REGEX = CFG.get(
    "daily_list_link_regex",
    r'href="([^"]*?/detail/(\w+)[^"]*?id=([^"&]+))"',
)


def parse_list_html(html: str) -> list[dict]:
    """Parse list HTML into rows: {pk, case_no, org, case_name, detail_url, list_announce_date, list_deadline, budget_text}."""
    rows = []
    seen_pks = set()
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
    for m in tr_re.finditer(html):
        row_html = m.group(1)
        link_m = re.search(_DAILY_LINK_REGEX, row_html)
        if not link_m:
            continue
        href = link_m.group(1)
        pk = urllib.parse.unquote(link_m.group(3))
        if pk in seen_pks:
            continue
        seen_pks.add(pk)
        detail_url = href if href.startswith("http") else BASE + href

        # 把 row 的 cell 切出來
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        text_cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]

        # Generic cell extraction
        case_no = ""
        org = ""
        case_name = ""
        list_announce_date = ""
        list_deadline = ""
        budget_text = ""
        if len(text_cells) >= 2:
            org = text_cells[1] if len(text_cells) > 1 else ""
        # Case number + name typically in cell 3
        if len(text_cells) >= 3:
            cn_cell = text_cells[2]
            case_no = cn_cell.split()[0] if cn_cell else ""
            case_name = " ".join(cn_cell.split()[1:]) if len(cn_cell.split()) > 1 else ""
        # Typical row layout: [seq, org, id+title, ..., announce_date, deadline, budget]
        if len(text_cells) >= 9:
            list_announce_date = text_cells[6] if len(text_cells) > 6 else ""
            list_deadline = text_cells[7] if len(text_cells) > 7 else ""
            budget_text = text_cells[8] if len(text_cells) > 8 else ""

        rows.append({
            "pk": pk,
            "case_no": case_no,
            "org": org,
            "case_name": case_name,
            "detail_url": detail_url,
            "list_announce_date": list_announce_date,
            "list_deadline": list_deadline,
            "budget_text": budget_text,
        })
    return rows


def fetch_with_retry(session, url: str, attempts: int = 3, timeout: int = 60) -> str:
    """retry on timeout/connection. GHA IPs sometimes get rate-limited."""
    last_err = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if i < attempts - 1:
            time.sleep(5 * (i + 1))  # 5s, 10s
    raise RuntimeError(f"failed after {attempts} attempts: {last_err}")


def fetch_list(url: str, session, max_pages: int = 50) -> str:
    """Fetch list page; follow pagination up to max_pages."""
    all_html_parts = []
    current_url = url
    pages_fetched = 0
    while pages_fetched < max_pages:
        text = fetch_with_retry(session, current_url, attempts=3, timeout=60)
        all_html_parts.append(text)
        pages_fetched += 1
        m = re.search(r'<a href="([^"]+)"[^>]*>(?:下一頁|»|next)', text)
        if not m:
            break
        nxt = m.group(1)
        if nxt == current_url:
            break
        current_url = nxt if nxt.startswith("http") else BASE + nxt
        time.sleep(2)
    return "\n".join(all_html_parts)


def main():
    print(f"daily list scrape, today={TODAY}")
    all_rows = []
    for src in SOURCES:
        print(f"\n== {src['source']} / {src['subtype'] or '-'} (max_pages={src.get('max_pages', 50)}) ==", flush=True)
        # Fresh session per source (isolate any slow-lane state)
        session = cr.Session(impersonate="chrome120", verify=False, timeout=60)
        session.headers.update(TARGET_HEADERS)
        try:
            from config import detail_referer
            session.get(detail_referer(), timeout=30)
        except Exception as e:
            print(f"  warmup err: {e}", flush=True)
        try:
            html = fetch_list(src["url"], session, max_pages=src.get("max_pages", 50))
            parsed = parse_list_html(html)
            for p in parsed:
                p["source"] = src["source"]
                p["source_subtype"] = src["subtype"]
            print(f"  parsed {len(parsed)} rows", flush=True)
            all_rows.extend(parsed)
        except Exception as e:
            print(f"  ERR: {e}", flush=True)
        try:
            session.close()
        except Exception:
            pass
        time.sleep(3)

    print(f"\n總共 {len(all_rows)} rows，POST 到 CF...")
    # 分批 POST（避免 payload 過大）
    batch_size = 200
    total_inserted = 0
    total_skipped = 0
    for i in range(0, len(all_rows), batch_size):
        batch = all_rows[i:i+batch_size]
        r = cr.post(
            f"{API_ENDPOINT}/api/daily-bulk-insert",
            headers={**HEADERS_AUTH, "Content-Type": "application/json"},
            data=json.dumps({
                "rows": batch,
                "list_seen_date": TODAY_DASH,
            }),
            timeout=60,
        )
        if r.status_code == 200:
            j = r.json()
            total_inserted += j.get("inserted", 0)
            total_skipped += j.get("skipped", 0)
        else:
            print(f"  POST batch {i} failed: {r.status_code}")
    print(f"\n結果：inserted={total_inserted}, skipped (already exist)={total_skipped}")


if __name__ == "__main__":
    main()
