"""Generic ETL fetch worker.

Loop:
  1. claim N tasks from API
  2. for each task: fetch source URL(s), validate, post result to API
  3. exit after MAX_ITER loops or queue empty

Pacing: 4 fetches/min (15s between fetches, sustained).
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import sys
import time
import urllib.parse
import warnings

from curl_cffi import requests as cr

warnings.filterwarnings("ignore")

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
RUNNER_ID = os.environ.get("RUNNER_ID", "1")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
MAX_ITER = int(os.environ.get("MAX_ITER", "5"))

PACING_SEC = 15.0  # 4 fetches per minute
HEADERS_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

BASE = "https://web.pcc.gov.tw"
SEARCH_URL_TMPL = (
    BASE
    + "/prkms/tender/common/bulletion/readBulletion"
    + "?querySentence={q}&tenderStatusType=%E6%B1%BA%E6%A8%99"
    + "&sortCol=AWARD_NOTICE_DATE&timeRange={year}&pageSize=100"
)

CAPTCHA_MARKERS = ("撲克", "請選擇相同", "請選出", "JCaptcha", "驗證碼")
DETAIL_MIN_BYTES = 50 * 1024
DETAIL_CAPTCHA_CEILING = 70 * 1024
REQUIRED_KEYWORDS = ("機關名稱", "標案名稱")
CURRENT_ROC_YEAR = 115


def detect_captcha(html: str) -> str | None:
    for m in CAPTCHA_MARKERS:
        if m in html:
            return m
    return None


def validate_detail(html: str) -> tuple[bool, bool, str, int]:
    """Returns (ok, is_captcha, reason, size)."""
    size = len(html.encode("utf-8"))
    if size < DETAIL_MIN_BYTES:
        m = detect_captcha(html)
        if m:
            return False, True, f"captcha {size}b m={m}", size
        return False, False, f"too small {size}b", size
    if size < DETAIL_CAPTCHA_CEILING:
        m = detect_captcha(html)
        if m:
            return False, True, f"suspect captcha {size}b m={m}", size
    for kw in REQUIRED_KEYWORDS:
        if kw not in html:
            return False, False, f"missing {kw}", size
    return True, False, "", size


def normalize_org(s: str) -> str:
    s = s.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", s)


def org_match(candidate: str, target: str) -> bool:
    a = normalize_org(candidate)
    b = normalize_org(target)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    if i >= 4:
        return True
    b_core = re.sub(r"\(.*?\)", "", b)
    if len(b_core) >= 4 and b_core in a:
        return True
    return False


def parse_search_results(html: str) -> list[dict]:
    """Extract detail links from search result page."""
    results = []
    seen = set()
    for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        row = tr_match.group(1)
        link_m = re.search(r'href="([^"]*?/urlSelector/common/(atm|nonAtm)\?pk=[^"]+)"', row)
        if not link_m:
            continue
        href = link_m.group(1)
        kind = "non_award" if link_m.group(2) == "nonAtm" else "award"
        abs_url = href if href.startswith("http") else BASE + href
        if abs_url in seen:
            continue
        seen.add(abs_url)
        row_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        pk_m = re.search(r"pk=([\w=%]+)", abs_url)
        pk = urllib.parse.unquote(pk_m.group(1)) if pk_m else None
        results.append({"detail_url": abs_url, "kind": kind, "row_text": row_text, "pk": pk})
    return results


def pick_matching(results: list[dict], target_org: str) -> list[dict]:
    if not results:
        return []
    matches = [r for r in results if org_match(r["row_text"], target_org)]
    if matches:
        return matches
    if len(results) == 1:
        return results
    return []


def make_session():
    s = cr.Session(impersonate="chrome120", verify=False, timeout=30)
    return s


def claim_tasks(n: int) -> list[dict]:
    r = cr.get(
        f"{API_ENDPOINT}/api/claim?n={n}&platform=gha-{RUNNER_ID}",
        headers=HEADERS_AUTH,
        timeout=30,
    )
    j = r.json()
    return j.get("tasks", [])


def save_result(task_id: int, result: str, **kwargs):
    body = {"task_id": task_id, "result": result, **kwargs}
    cr.post(
        f"{API_ENDPOINT}/api/save",
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=60,
    )


def process_task(session, task: dict, last_fetch_at: list[float]):
    case_no = task["case_no"]
    org = task["org"]
    roc_year = task["roc_year"]

    years = [roc_year]
    if roc_year < CURRENT_ROC_YEAR:
        years.append(roc_year + 1)

    searched = []
    chosen = []

    for year in years:
        searched.append(year)
        # pace
        elapsed = time.time() - last_fetch_at[0]
        if elapsed < PACING_SEC and last_fetch_at[0] > 0:
            time.sleep(PACING_SEC - elapsed)
        url = SEARCH_URL_TMPL.format(q=urllib.parse.quote(case_no), year=year)
        try:
            r = session.get(url)
            last_fetch_at[0] = time.time()
            if r.status_code != 200:
                save_result(task["id"], "failed", error=f"search HTTP {r.status_code}")
                return False
            matched = pick_matching(parse_search_results(r.text), org)
            if matched:
                chosen = matched
                break
        except Exception as e:
            save_result(task["id"], "failed", error=f"search err: {e}")
            return False

    if not chosen:
        save_result(task["id"], "not_found", searched_years=searched)
        return True

    # dedup
    seen = set()
    uniq = []
    for c in chosen:
        if c["detail_url"] not in seen:
            seen.add(c["detail_url"])
            uniq.append(c)

    details = []
    for c in uniq:
        # pace before each detail fetch
        elapsed = time.time() - last_fetch_at[0]
        if elapsed < PACING_SEC and last_fetch_at[0] > 0:
            time.sleep(PACING_SEC - elapsed)
        try:
            r = session.get(
                c["detail_url"],
                headers={"Referer": BASE + "/prkms/tender/common/bulletion/readBulletion"},
            )
            last_fetch_at[0] = time.time()
            if r.status_code != 200:
                save_result(task["id"], "failed", error=f"detail HTTP {r.status_code}")
                return False
            ok, is_captcha, reason, size = validate_detail(r.text)
            if not ok:
                if is_captcha:
                    save_result(task["id"], "captcha", error=reason)
                    return False  # signal caller to stop / cooldown
                save_result(task["id"], "failed", error=reason)
                return False
            details.append({
                "pk": c["pk"] or f"t{task['id']}_d{len(details)}",
                "url": c["detail_url"],
                "kind": c["kind"],
                "html_b64": base64.b64encode(r.text.encode("utf-8")).decode("ascii"),
            })
        except Exception as e:
            save_result(task["id"], "failed", error=f"detail err: {e}")
            return False

    save_result(
        task["id"], "done",
        details=details,
        searched_years=searched,
    )
    return True


def main():
    print(f"runner {RUNNER_ID} start, batch={BATCH_SIZE}, max_iter={MAX_ITER}")
    session = make_session()
    # warmup
    try:
        session.get(BASE + "/prkms/tender/common/bulletion/readBulletion")
    except Exception:
        pass

    last_fetch_at = [0.0]
    total_done = 0
    total_failed = 0
    captcha_streak = 0

    for it in range(MAX_ITER):
        tasks = claim_tasks(BATCH_SIZE)
        if not tasks:
            print(f"iter {it}: queue empty, exit")
            break
        print(f"iter {it}: claimed {len(tasks)} tasks")
        for t in tasks:
            ok = process_task(session, t, last_fetch_at)
            if ok:
                total_done += 1
                captcha_streak = 0
            else:
                total_failed += 1
                # Detect captcha (we passed result=captcha to save). Do tracker via in-memory only.
                # If process_task called save_result with "captcha", it returned False; we can't easily detect here.
                # Simple heuristic: if 3 failures in a row, abort (likely captcha across runner IP)
                captcha_streak += 1
                if captcha_streak >= 3:
                    print(f"3 consecutive failures, exiting runner {RUNNER_ID}")
                    print(json.dumps({"done": total_done, "failed": total_failed}))
                    return

    print(json.dumps({"done": total_done, "failed": total_failed}))


if __name__ == "__main__":
    main()
