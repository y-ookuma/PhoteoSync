#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhoteoSync JMA API one-station smoke test.

Purpose:
  Verify that GitHub Actions can reach JMA's Past Weather Data Download API
  with a single AMeDAS station before running the full 915-station job.

This test does NOT write svgjma-history.json.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from datetime import UTC, datetime

import requests

ROOT_URL = "https://www.data.jma.go.jp/risk/obsdl/index.php"
SHOW_URL = "https://www.data.jma.go.jp/risk/obsdl/show/table"

# Known working AMeDAS ID format: a + 5-digit station number.
# a0179 = 三戸 (青森県)
STATION_ID = "a0179"
STATION_NAME = "三戸"

# Same 11-day segment used by the current PhoteoSync job for 2026-09-03.
START_YEAR = 2022
END_YEAR = 2026
START_MONTH = 8
START_DAY = 21
END_MONTH = 8
END_DAY = 31

# Same four daily elements used by PhoteoSync.
ELEMENTS = [["201", ""], ["202", ""], ["203", ""], ["101", ""]]
TIMEOUT = 30


def make_payload() -> dict[str, str]:
    ymd = [
        str(START_YEAR), str(END_YEAR),
        str(START_MONTH), str(END_MONTH),
        str(START_DAY), str(END_DAY),
    ]
    return {
        "stationNumList": json.dumps([STATION_ID], separators=(",", ":")),
        "aggrgPeriod": "1",
        "elementNumList": json.dumps(ELEMENTS, separators=(",", ":")),
        "interAnnualType": "2",
        "ymdList": json.dumps(ymd, separators=(",", ":")),
        "optionNumList": "[]",
        "rmkFlag": "1",
        "disconnectFlag": "1",
        "kijiFlag": "0",
        "huukouFlag": "0",
        "youbiFlag": "0",
        "fukenFlag": "0",
        "downloadFlag": "true",
        "csvFlag": "1",
        "jikantaiFlag": "0",
        "jikantaiList": "[]",
        "ymdLiteral": "1",
    }


def csv_preview(content: bytes) -> str:
    text = content.decode("utf-8-sig", errors="replace")
    if "年月日" not in text and "年" not in text[:2000]:
        text = content.decode("cp932", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    # Show only a compact preview, never the whole response.
    preview_rows = rows[:8]
    return "\n".join(",".join(row[:12]) for row in preview_rows)


def main() -> int:
    print("=== PhoteoSync JMA one-station smoke test ===")
    print(f"Time (UTC): {datetime.now(UTC).isoformat()}")
    print(f"Station: {STATION_ID} ({STATION_NAME})")
    print(f"Years: {START_YEAR}-{END_YEAR}")
    print(f"Period: {START_MONTH:02d}/{START_DAY:02d}-{END_MONTH:02d}/{END_DAY:02d}")
    print(f"Elements: {ELEMENTS}")
    print("No JSON file will be written.")
    print()

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    })

    try:
        print("[1/3] GET JMA obsdl index ...", flush=True)
        top = session.get(ROOT_URL, timeout=TIMEOUT)
        top.raise_for_status()
        print(f"      OK: HTTP {top.status_code}, {len(top.content):,} bytes", flush=True)

        time.sleep(2)
        data = make_payload()
        print("[2/3] POST one station to JMA ...", flush=True)
        print(f"      stationNumList={data['stationNumList']}", flush=True)
        print(f"      interAnnualType={data['interAnnualType']}", flush=True)

        started = time.monotonic()
        response = session.post(
            SHOW_URL,
            data=data,
            headers={"Referer": ROOT_URL},
            timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - started
        print(f"      HTTP {response.status_code} in {elapsed:.1f}s; {len(response.content):,} bytes", flush=True)

        if response.status_code >= 400:
            body = response.content.decode("utf-8-sig", errors="replace")
            if not body.strip():
                body = response.content.decode("cp932", errors="replace")
            body = re.sub(r"\s+", " ", body).strip()
            print("\n*** JMA API FAILED ***", file=sys.stderr)
            print(f"HTTP {response.status_code}", file=sys.stderr)
            print(f"Response: {body[:3000]!r}", file=sys.stderr)
            print(f"Payload: {data}", file=sys.stderr)
            return 1

        if len(response.content) < 100:
            print("*** JMA API returned an unexpectedly small response ***", file=sys.stderr)
            print(response.content[:1000], file=sys.stderr)
            return 1

        print("[3/3] Inspect CSV response ...", flush=True)
        preview = csv_preview(response.content)
        print("      CSV preview:")
        print(preview[:5000])
        print()
        print("=== SUCCESS ===")
        print("JMA API accepted the one-station request.")
        print("The next step can safely test a small multi-station batch.")
        return 0

    except Exception as exc:
        print("\n*** TEST FAILED ***", file=sys.stderr)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
