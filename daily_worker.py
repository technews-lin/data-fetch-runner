"""Daily detail worker.

從 daily_tasks queue 拉 pending → fetch detail URL → POST 結果回 CF。
比 backfill 簡單：沒 search step。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import warnings
from curl_cffi import requests as cr

warnings.filterwarnings("ignore")

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
RUNNER_ID = os.environ.get("RUNNER_ID", "1")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
MAX_ITER = int(os.environ.get("MAX_ITER", "8"))

PACING_SEC = 15.0
HEADERS_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

BASE = "https://web.pcc.gov.tw"

# captcha 偵測（PCC detail page）
CAPTCHA_MARKERS = ("撲克", "請選擇相同", "請選出", "JCaptcha", "驗證碼")
DETAIL_MIN_BYTES = 50 * 1024
DETAIL_CAPTCHA_CEILING = 70 * 1024


def detect_captcha(html: str) -> str | None:
    for m in CAPTCHA_MARKERS:
        if m in html:
            return m
    return None


def validate(html: str) -> tuple[bool, bool, str, int]:
    """returns (ok, is_captcha, reason, size_bytes)"""
    size = len(html.encode("utf-8"))
    if size < DETAIL_MIN_BYTES:
        m = detect_captcha(html)
        if m:
            return False, True, f"captcha {size}b m={m}", size
        return False, False, f"too small {size}b", size
    if size < DETAIL_CAPTCHA_CEILING:
        m = detect_captcha(html)
        if m:
            return False, True, f"suspect captcha {size}b", size
    return True, False, "", size


def claim_tasks(n: int):
    r = cr.get(
        f"{API_ENDPOINT}/api/daily-claim?n={n}&platform=gha-daily-{RUNNER_ID}",
        headers=HEADERS_AUTH,
        timeout=30,
    )
    return r.json().get("tasks", [])


def save_result(task_id: int, result: str, **kwargs):
    body = {"task_id": task_id, "result": result, **kwargs}
    cr.post(
        f"{API_ENDPOINT}/api/daily-save",
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=60,
    )


def process_task(session, task, rate_state):
    # rate pacing
    if rate_state["last"] > 0:
        elapsed = time.time() - rate_state["last"]
        if elapsed < PACING_SEC:
            time.sleep(PACING_SEC - elapsed)
    try:
        r = session.get(
            task["detail_url"],
            headers={"Referer": f"{BASE}/prkms/tender/common/bulletion/readBulletion"},
            timeout=30,
        )
        rate_state["last"] = time.time()
        if r.status_code != 200:
            save_result(task["id"], "failed", error=f"HTTP {r.status_code}")
            return False
        ok, is_captcha, reason, size = validate(r.text)
        if not ok:
            if is_captcha:
                save_result(task["id"], "captcha", error=reason)
                return False  # signal captcha streak
            save_result(task["id"], "failed", error=reason)
            return False
        save_result(
            task["id"], "done",
            html_b64=base64.b64encode(r.text.encode("utf-8")).decode("ascii"),
        )
        return True
    except Exception as e:
        rate_state["last"] = time.time()
        save_result(task["id"], "failed", error=f"{type(e).__name__}: {e}")
        return False


def main():
    print(f"daily runner {RUNNER_ID} start, batch={BATCH_SIZE}, max_iter={MAX_ITER}")
    session = cr.Session(impersonate="chrome120", verify=False, timeout=30)
    try:
        session.get(f"{BASE}/prkms/tender/common/bulletion/readBulletion")
    except Exception:
        pass

    rate_state = {"last": 0.0}
    total_done = 0
    total_failed = 0
    streak = 0

    for it in range(MAX_ITER):
        tasks = claim_tasks(BATCH_SIZE)
        if not tasks:
            print(f"iter {it}: queue empty, exit")
            break
        print(f"iter {it}: claimed {len(tasks)} tasks")
        for t in tasks:
            ok = process_task(session, t, rate_state)
            if ok:
                total_done += 1
                streak = 0
            else:
                total_failed += 1
                streak += 1
                if streak >= 3:
                    print(f"3 consecutive failures, exiting runner {RUNNER_ID}")
                    print(json.dumps({"done": total_done, "failed": total_failed}))
                    return

    print(json.dumps({"done": total_done, "failed": total_failed}))


if __name__ == "__main__":
    main()
