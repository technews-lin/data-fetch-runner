"""Daily detail worker.

Claims pending tasks from queue, fetches the detail URL, posts result back.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import warnings
from curl_cffi import requests as cr

from config import CAPTCHA_MARKERS, detail_referer

warnings.filterwarnings("ignore")

API_ENDPOINT = os.environ["API_ENDPOINT"].rstrip("/")
API_TOKEN = os.environ["API_TOKEN"]
RUNNER_ID = os.environ.get("RUNNER_ID", "1")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
MAX_ITER = int(os.environ.get("MAX_ITER", "8"))

PACING_SEC = 15.0
HEADERS_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

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
    # SOURCE_FILTER: daily (default) / backfill / all
    source_filter = os.environ.get("SOURCE_FILTER", "daily")
    r = cr.get(
        f"{API_ENDPOINT}/api/daily-claim?n={n}&platform=gha-daily-{RUNNER_ID}&source_filter={source_filter}",
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
    tid = task["id"]
    if rate_state["last"] > 0:
        elapsed = time.time() - rate_state["last"]
        if elapsed < PACING_SEC:
            wait = PACING_SEC - elapsed
            print(f"  task {tid}: pacing {wait:.1f}s")
            time.sleep(wait)
    t0 = time.time()
    # Don't log the URL itself (would leak in public action logs).
    print(f"  task {tid}: fetching...")
    try:
        r = session.get(
            task["detail_url"],
            headers={"Referer": detail_referer()},
            timeout=30,
        )
        rate_state["last"] = time.time()
        dt = rate_state["last"] - t0
        if r.status_code != 200:
            print(f"  task {tid}: HTTP {r.status_code} in {dt:.1f}s -> failed")
            save_result(tid, "failed", error=f"HTTP {r.status_code}")
            return False
        ok, is_captcha, reason, size = validate(r.text)
        if not ok:
            if is_captcha:
                print(f"  task {tid}: captcha {size}b in {dt:.1f}s")
                save_result(tid, "captcha", error=reason)
                return False
            print(f"  task {tid}: invalid ({reason}) in {dt:.1f}s")
            save_result(tid, "failed", error=reason)
            return False
        print(f"  task {tid}: done {size}b in {dt:.1f}s")
        save_result(
            tid, "done",
            html_b64=base64.b64encode(r.content).decode("ascii"),
        )
        return True
    except Exception as e:
        rate_state["last"] = time.time()
        dt = rate_state["last"] - t0
        # Only log exception class name, not message (message may contain URL/host)
        print(f"  task {tid}: exception {type(e).__name__} in {dt:.1f}s")
        # Sanitize the error sent back to the API too (still useful for retry logic)
        err_msg = str(e)
        # Strip anything that looks like a URL
        import re as _re
        err_msg = _re.sub(r"https?://\S+", "<url>", err_msg)
        save_result(tid, "failed", error=f"{type(e).__name__}: {err_msg[:200]}")
        return False


def main():
    print(f"daily runner {RUNNER_ID} start, batch={BATCH_SIZE}, max_iter={MAX_ITER}")
    session = cr.Session(impersonate="chrome120", verify=False, timeout=30)
    try:
        session.get(detail_referer())
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
