#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build data/svgjma-history.json for PhoteoSync.

JMA's official "Past Weather Data Download" service is used from GitHub Actions.
Only the 14-day comparison window (current day included) for the current year
and previous four years is retained. The browser therefore never contacts JMA.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

JMA_AMEDAS_URL = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
OBSDL_INDEX_URL = "https://www.data.jma.go.jp/risk/obsdl/index.php"
OBSDL_TABLE_URL = "https://www.data.jma.go.jp/risk/obsdl/show/table"
OUTPUT = Path("data/svgjma-history.json")

# Keep requests moderate because JMA explicitly asks users not to make excessive
# automated requests.
BATCH_SIZE = 5
REQUEST_PAUSE_SECONDS = 2.0
REQUEST_TIMEOUT = 120
ELEMENTS = [["201", ""], ["202", ""], ["203", ""], ["101", ""]]


def jst_today() -> date:
    # GitHub runners use UTC. JST date is UTC+9.
    return (datetime.now(UTC) + timedelta(hours=9)).date()


def comparison_dates(today: date) -> list[date]:
    return [today - timedelta(days=i) for i in range(13, -1, -1)]


def shift_to_year(d: date, year: int) -> date:
    if d.month == 2 and d.day == 29:
        # Keep 14 calendar slots even when a comparison year is not leap.
        if not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
            return date(year, 2, 28)
    return date(year, d.month, d.day)


def get_stations(session: requests.Session) -> list[dict[str, Any]]:
    r = session.get(JMA_AMEDAS_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    raw = r.json()
    stations: list[dict[str, Any]] = []
    for sid, s in raw.items():
        # Current AMeDAS metadata uses id/name/lat/lon. A few records may be
        # disabled or have incomplete coordinates; skip those safely.
        try:
            lat = float(s["lat"][0]) + float(s["lat"][1]) / 60.0
            lon = float(s["lon"][0]) + float(s["lon"][1]) / 60.0
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        # Current JMA amedastable.json no longer provides the old isTarget field.
        # elems is the station capability flag: 1st digit=temperature,
        # 2nd digit=precipitation. We need both for the daily dataset.
        elems = str(s.get("elems") or "")
        if len(elems) < 2 or elems[0] == "0" or elems[1] == "0":
            continue
        obsdl_id = f"a{int(sid):04d}"
        stations.append({
            "id": str(sid),
            "obsdlId": obsdl_id,
            "name": str(s.get("kjName") or s.get("enName") or sid),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })
    stations.sort(key=lambda x: x["id"])
    if not stations:
        raise RuntimeError("AMeDAS station list is empty")
    return stations


def payload(station_ids: list[str], start_year: int, start_month: int, start_day: int,
            end_year: int, end_month: int, end_day: int) -> dict[str, str]:
    # interAnnualType=2: same month/day range for each year in the selected span.
    ymd = [str(start_year), str(end_year), str(start_month), str(end_month),
           str(start_day), str(end_day)]
    return {
        "stationNumList": json.dumps(station_ids, ensure_ascii=False, separators=(",", ":")),
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
    }


def csv_rows(raw: bytes) -> list[list[str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    # JMA CSV is sometimes Shift-JIS depending on service output.
    if "年月日" not in text and "年" not in text[:2000]:
        text = raw.decode("cp932", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def normal(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def value(s: str) -> float | None:
    s = normal(s).replace("＊", "").replace("*", "")
    if not s or s in {"///", "--", "×", "..."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_batch(raw: bytes, stations: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float | None]]]:
    """Return station-id -> ISO date -> values."""
    rows = csv_rows(raw)
    if not rows:
        return {}

    data_start = -1
    for i, row in enumerate(rows):
        if row and re.match(r"^20\d{2}[/-]\d{1,2}[/-]\d{1,2}", row[0].strip()):
            data_start = i
            break
    if data_start < 0:
        # Some outputs have year/month/day in separate columns.
        for i, row in enumerate(rows):
            if len(row) >= 3 and re.match(r"^20\d{2}$", row[0].strip()) and row[1].strip().isdigit():
                data_start = i
                break
    if data_start < 0:
        return {}

    headers = rows[:data_start]
    max_cols = max(len(r) for r in rows)
    col_text = [normal(" ".join((r[c] if c < len(r) else "") for r in headers)) for c in range(max_cols)]

    out: dict[str, dict[str, dict[str, float | None]]] = {}
    for st in stations:
        name = normal(st["name"])
        cols = {"avg": -1, "min": -1, "max": -1, "rain": -1}
        for c, h in enumerate(col_text):
            if name not in h:
                continue
            if cols["avg"] < 0 and re.search(r"平均気温|日平均気温", h): cols["avg"] = c
            if cols["min"] < 0 and re.search(r"最低気温|日最低気温", h): cols["min"] = c
            if cols["max"] < 0 and re.search(r"最高気温|日最高気温", h): cols["max"] = c
            if cols["rain"] < 0 and re.search(r"降水量", h): cols["rain"] = c
        if any(v < 0 for v in cols.values()):
            continue

        sid = st["id"]
        out.setdefault(sid, {})
        for row in rows[data_start:]:
            if not row:
                continue
            first = row[0].strip()
            m = re.match(r"^(20\d{2})[/-](\d{1,2})[/-](\d{1,2})$", first)
            if m:
                iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            elif len(row) >= 3 and re.match(r"^20\d{2}$", row[0].strip()):
                try:
                    iso = f"{int(row[0]):04d}-{int(row[1]):02d}-{int(row[2]):02d}"
                except ValueError:
                    continue
            else:
                continue
            out[sid][iso] = {
                "avg": value(row[cols["avg"]] if cols["avg"] < len(row) else ""),
                "min": value(row[cols["min"]] if cols["min"] < len(row) else ""),
                "max": value(row[cols["max"]] if cols["max"] < len(row) else ""),
                "rain": value(row[cols["rain"]] if cols["rain"] < len(row) else ""),
            }
    return out


def fetch_segment(session: requests.Session, stations: list[dict[str, Any]], years: list[int],
                  start_month: int, start_day: int, end_month: int, end_day: int) -> dict[str, dict[str, dict[str, float | None]]]:
    merged: dict[str, dict[str, dict[str, float | None]]] = {}
    for pos in range(0, len(stations), BATCH_SIZE):
        batch = stations[pos:pos + BATCH_SIZE]
        ids = [s["obsdlId"] for s in batch]
        data = payload(ids, min(years), start_month, start_day, max(years), end_month, end_day)
        for attempt in range(3):
            try:
                top = session.get(OBSDL_INDEX_URL, timeout=REQUEST_TIMEOUT)
                top.raise_for_status()
                time.sleep(REQUEST_PAUSE_SECONDS)
                r = session.post(OBSDL_TABLE_URL, data=data,
                                 headers={"Referer": OBSDL_INDEX_URL}, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                if len(r.content) < 100:
                    raise RuntimeError("JMA returned an unexpectedly small response")
                parsed = parse_batch(r.content, batch)
                for sid, rows in parsed.items():
                    merged.setdefault(sid, {}).update(rows)
                print(f"  batch {pos + 1}-{pos + len(batch)} / {len(stations)}: {len(parsed)} stations", flush=True)
                break
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"JMA batch failed at {pos}: {exc}") from exc
                print(f"  retry {attempt + 1}/2 after error: {exc}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        time.sleep(REQUEST_PAUSE_SECONDS)
    return merged


def build_output(stations: list[dict[str, Any]], today: date, raw: dict[str, dict[str, dict[str, float | None]]]) -> dict[str, Any]:
    dates = comparison_dates(today)
    years = [today.year - i for i in range(5)]
    labels = [f"{d.month:02d}-{d.day:02d}" for d in dates]
    result_stations = []
    for st in stations:
        by_date = raw.get(st["id"], {})
        years_obj: dict[str, Any] = {}
        for year in years:
            arr = {"avg": [], "min": [], "max": [], "rain": []}
            for base in dates:
                d = shift_to_year(base, year)
                v = by_date.get(d.isoformat(), {})
                for key in arr:
                    arr[key].append(v.get(key))
            years_obj[str(year)] = arr
        result_stations.append({
            "id": st["id"],
            "name": st["name"],
            "lat": st["lat"],
            "lon": st["lon"],
            "years": years_obj,
        })
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "baseDate": today.isoformat(),
        "windowDays": 14,
        "dates": labels,
        "years": years,
        "stations": result_stations,
    }


def main() -> None:
    print("=== PhoteoSync JMA production update ===", flush=True)
    print("Processing all eligible AMeDAS stations in batches of 5.", flush=True)
    today = jst_today()
    dates = comparison_dates(today)
    years = [today.year - i for i in range(5)]
    print(f"PhoteoSync JMA history: base date={today}, years={years}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "PhoteoSync/1.0 (GitHub Actions; JMA historical data updater)",
        "Accept-Language": "ja,en;q=0.8",
    })
    stations = get_stations(session)
    print(f"AMeDAS stations: {len(stations)}")

    # The 14-day window can cross a month boundary. Fetch two calendar segments
    # using obsdl's interAnnualType=2 (same period in each of the five years).
    first, last = dates[0], dates[-1]
    segments = [(first.month, first.day, last.month, last.day)]
    if first.month != last.month:
        # Month boundary: first segment to month-end, second segment from 1st.
        next_month = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
        month_end = next_month - timedelta(days=1)
        segments = [(first.month, first.day, month_end.month, month_end.day),
                    (last.month, 1, last.month, last.day)]

    raw: dict[str, dict[str, dict[str, float | None]]] = {}
    for seg in segments:
        print(f"Fetching {seg[0]:02d}/{seg[1]:02d} - {seg[2]:02d}/{seg[3]:02d} for all stations")
        got = fetch_segment(session, stations, years, *seg)
        for sid, rows in got.items():
            raw.setdefault(sid, {}).update(rows)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result = build_output(stations, today, raw)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUTPUT)
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
