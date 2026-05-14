"""Daily list-page scraper.

每天 6am 跑：
1. fetch 5 個 PCC list URL（無 captcha 風險）
2. parse 出 rows（含 pk + detail_url）
3. POST 到 CF /api/daily-bulk-insert（UNIQUE(source, pk) 自動 dedup）

注意：list page 用 today's date，dateType=isNow 已自動處理。
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

warnings.filterwarnings("ignore")

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
HEADERS_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

BASE = "https://web.pcc.gov.tw"
TODAY = datetime.utcnow().strftime("%Y/%m/%d")
TODAY_DASH = datetime.utcnow().strftime("%Y-%m-%d")

# 5 個 PCC 來源（每個來源獨立 source key）
# max_pages: 限制翻頁數，因為某些 list 會把舊 active 案件全列出來
SOURCES = [
    {
        "source": "pcc_tender",
        "subtype": "WAY_1",
        "max_pages": 50,
        "url": (
            f"{BASE}/prkms/tender/common/basic/readTenderBasic"
            f"?pageSize=100&firstSearch=true&searchType=basic&isBinding=N&isLogIn=N&level_1=on"
            f"&orgName=&orgId=&tenderName=&tenderId=&tenderType=TENDER_DECLARATION"
            f"&tenderWay=TENDER_WAY_1&dateType=isNow"
            f"&tenderStartDate={urllib.parse.quote(TODAY)}&tenderEndDate={urllib.parse.quote(TODAY)}"
            f"&radProctrgCate=&policyAdvocacy="
        ),
    },
    {
        "source": "pcc_tender",
        "subtype": "WAY_4",
        "max_pages": 50,
        "url": (
            f"{BASE}/prkms/tender/common/basic/readTenderBasic"
            f"?pageSize=100&firstSearch=false&searchType=basic&isBinding=N&isLogIn=N&level_1=on"
            f"&orgName=&orgId=&tenderName=&tenderId=&tenderType=TENDER_DECLARATION"
            f"&tenderWay=TENDER_WAY_4&dateType=isNow"
            f"&tenderStartDate={urllib.parse.quote(TODAY)}&tenderEndDate={urllib.parse.quote(TODAY)}"
            f"&radProctrgCate=&policyAdvocacy="
        ),
    },
    {
        "source": "pcc_quote",
        "subtype": None,
        "max_pages": 4,  # 公開徵求 list 累積 active 案件，限 4 頁 = 400 筆
        "url": (
            f"{BASE}/prkms/tpAppeal/common/readTpAppeal/basic/returnToBasic"
            f"?orgName=&tenderName=&endDate={urllib.parse.quote(TODAY)}"
            f"&searchType=basic&isBinding=N&firstSearch=true&pageSize=100&radProctrgCate="
            f"&tenderId=&orgId=&isLogIn=N&tenderType=SEARCH_APPEAL&dateType=isNow"
            f"&policyAdvocacy=&level_1=on&startDate={urllib.parse.quote(TODAY)}"
        ),
    },
    {
        "source": "pcc_tpread",
        "subtype": None,
        "max_pages": 5,  # 公開閱覽量小，5 頁夠
        "url": (
            f"{BASE}/prkms/tpRead/common/readTpRead"
            f"?orgName=&tenderName=&queryStartDate={urllib.parse.quote(TODAY)}"
            f"&searchType=basic&isBinding=N&firstSearch=true&radProctrgCate=&tenderId=&orgId="
            f"&isLogIn=N&tenderType=PUBLIC_READ&dateType=isNow"
            f"&queryEndDate={urllib.parse.quote(TODAY)}&policyAdvocacy="
        ),
    },
    {
        "source": "pcc_obtain",
        "subtype": "WAY_12",
        "max_pages": 50,
        "url": (
            f"{BASE}/prkms/tender/common/basic/readTenderBasic"
            f"?pageSize=100&firstSearch=true&searchType=basic&isBinding=N&isLogIn=N&level_1=on"
            f"&orgName=&orgId=&tenderName=&tenderId=&tenderType=TENDER_DECLARATION"
            f"&tenderWay=TENDER_WAY_12&dateType=isNow"
            f"&tenderStartDate={urllib.parse.quote(TODAY)}&tenderEndDate={urllib.parse.quote(TODAY)}"
            f"&radProctrgCate=&policyAdvocacy="
        ),
    },
    {
        "source": "pcc_obtain",
        "subtype": "WAY_2",
        "max_pages": 50,
        "url": (
            f"{BASE}/prkms/tender/common/basic/readTenderBasic"
            f"?pageSize=100&firstSearch=true&searchType=basic&isBinding=N&isLogIn=N&level_1=on"
            f"&orgName=&orgId=&tenderName=&tenderId=&tenderType=TENDER_DECLARATION"
            f"&tenderWay=TENDER_WAY_2&dateType=isNow"
            f"&tenderStartDate={urllib.parse.quote(TODAY)}&tenderEndDate={urllib.parse.quote(TODAY)}"
            f"&radProctrgCate=&policyAdvocacy="
        ),
    },
]


def parse_pcc_list(html: str) -> list[dict]:
    """從 PCC list HTML 解析每筆 row → {pk, case_no, org, case_name, detail_url, list_announce_date, list_deadline, budget_text}."""
    rows = []
    seen_pks = set()
    # 一個 row 在 <tr>...</tr>
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
    for m in tr_re.finditer(html):
        row_html = m.group(1)
        # 找 detail link 在這 row 內
        link_m = re.search(
            r'href="([^"]*?/urlSelector/common/(tpam|tpAppeal|tpRead|atm|nonAtm)[^"]*?pk=([^"&]+))"',
            row_html,
        )
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

        # 通用提取（PCC tender / obtain）
        case_no = ""
        org = ""
        case_name = ""
        list_announce_date = ""
        list_deadline = ""
        budget_text = ""
        if len(text_cells) >= 2:
            org = text_cells[1] if len(text_cells) > 1 else ""
        # 案號 + 名稱 一般在第 3 欄（pcc_tender/obtain）或第 3-4 欄（pcc_quote）
        if len(text_cells) >= 3:
            cn_cell = text_cells[2]
            case_no = cn_cell.split()[0] if cn_cell else ""
            case_name = " ".join(cn_cell.split()[1:]) if len(cn_cell.split()) > 1 else ""
        # tender 結構：[項次, 機關, 案號標題, 傳輸次數, 招標方式, 採購性質, 公告日, 截止日, 預算]
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
    """retry on timeout/connection. GHA Azure → PCC 偶爾被 slow-lane."""
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
    """抓 PCC list page。如果有翻頁，跟著翻完。受 max_pages 限制。"""
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
        # 每個 source 新 session（隔離一個 source 的 slow-lane 影響另一個）
        session = cr.Session(impersonate="chrome120", verify=False, timeout=60)
        try:
            session.get(f"{BASE}/prkms/tender/common/bulletion/readBulletion", timeout=30)
        except Exception as e:
            print(f"  warmup err: {e}", flush=True)
        try:
            html = fetch_list(src["url"], session, max_pages=src.get("max_pages", 50))
            parsed = parse_pcc_list(html)
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
