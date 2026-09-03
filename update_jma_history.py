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
OBSDL_TABLE_URL = "https://www.data.jma.go.jp/risk/obsdl/show/table.html"
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


def get_obsdl_session_id(session: requests.Session) -> str:
    """Initialize the JMA obsdl session.

    Current JMA may not expose an <input id="sid"> in the initial HTML.
    The server can instead establish PHPSESSID through Set-Cookie.  Use that
    cookie first; fall back to the historical hidden sid field if present.
    """
    r = session.get(
        OBSDL_INDEX_URL,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()

    # Current/older implementations may establish the session as a cookie.
    sid_cookie = session.cookies.get("PHPSESSID")
    if sid_cookie:
        return sid_cookie.strip()

    text = r.text

    # Historical obsdl pages contained <input id="sid" value="...">.
    patterns = [
        r'<input\b[^>]*\bid=["\']sid["\'][^>]*\bvalue=["\']([^"\']+)["\']',
        r'<input\b[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bid=["\']sid["\']',
        r'\b(?:PHPSESSID|phpsessid|sid)\s*["\':=]+\s*["\']?([A-Za-z0-9_-]{10,})',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            sid = m.group(1).strip()
            if sid:
                return sid

    # Do not guess a session id.  Return an explicit diagnostic including
    # whether the server sent a cookie and a small HTML preview.
    cookie_names = ", ".join(sorted(c.name for c in session.cookies)) or "(none)"
    preview = re.sub(r"\s+", " ", text[:600])
    raise RuntimeError(
        "JMA obsdl session id was not exposed as PHPSESSID cookie or sid field; "
        f"cookies={cookie_names}; HTML preview={preview!r}"
    )


def payload(station_ids: list[str], start_year: int, start_month: int, start_day: int,
            end_year: int, end_month: int, end_day: int) -> dict[str, str]:
    # interAnnualType=2: same month/day range for each year in the selected span.
    ymd = [str(start_year), str(end_year), str(start_month), str(end_month),
           str(start_day), str(end_day)]
    return {
        "stationNumList": json.dumps(station_ids, ensure_ascii=False, separators=(",", ":")),
        "PHPSESSID": "",  # filled immediately before POST
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


def csv_rows(raw: bytes) -> list[list[str]]:
    """Decode the JMA CSV and return rows.

    JMA currently documents the CSV as:
      1) download timestamp
      2) blank line
      3-5) multi-row headers
      6-) data rows
    The response is normally Shift-JIS/CP932, but UTF-8 is also accepted.
    """
    candidates = [
        raw.decode("utf-8-sig", errors="replace"),
        raw.decode("cp932", errors="replace"),
        raw.decode("shift_jis", errors="replace"),
    ]
    # Prefer the decoding that actually contains Japanese JMA header text.
    text = next(
        (t for t in candidates if "集計開始" in t or "地点名" in t or "日平均気温" in t),
        candidates[0],
    )
    return list(csv.reader(io.StringIO(text)))


def normal(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def value(s: str) -> float | None:
    """Convert a JMA numeric cell to float; preserve missing values as None."""
    s = normal(s).replace("＊", "").replace("*", "")
    if not s or s in {"///", "--", "×", "...", "", "欠測"}:
        return None
    # JMA may append display marks such as ) or ] when non-numeric mode is used.
    s = s.strip("()[]")
    try:
        return float(s)
    except ValueError:
        return None


def _find_data_start(rows: list[list[str]]) -> int:
    """Find the first actual data row, tolerating JMA's documented header layout."""
    for i, row in enumerate(rows):
        if not row:
            continue
        first = normal(row[0])
        if re.fullmatch(r"20\d{2}/\d{1,2}/\d{1,2}", first):
            return i
        if re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", first):
            return i
        # ymdLiteral=0 fallback: YYYY,MM,DD,...
        if len(row) >= 3 and re.fullmatch(r"20\d{2}", first):
            if re.fullmatch(r"\d{1,2}", normal(row[1])) and re.fullmatch(r"\d{1,2}", normal(row[2])):
                return i
    return -1


def _iso_date(row: list[str]) -> str | None:
    if not row:
        return None
    first = normal(row[0])
    m = re.fullmatch(r"(20\d{2})/(\d{1,2})/(\d{1,2})", first) or re.fullmatch(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})", first
    )
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if len(row) >= 3 and re.fullmatch(r"20\d{2}", normal(row[0])):
        try:
            return f"{int(row[0]):04d}-{int(row[1]):02d}-{int(row[2]):02d}"
        except ValueError:
            return None
    return None


def _station_aliases(name: str) -> set[str]:
    """Return normalized forms useful for matching JMA station header text."""
    n = normal(name)
    aliases = {n}
    # JMA may append prefecture/region information in some CSV headers.
    for sep in ("（", "(", "[", "［"):
        if sep in n:
            aliases.add(n.split(sep, 1)[0])
    return {x for x in aliases if x}


def parse_batch(raw: bytes, stations: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float | None]]]:
    """Parse JMA's documented multi-row CSV into station-id/date/value records.

    Important: JMA's CSV is *not* a simple one-row header. Station names occupy
    row 3, item names occupy row 4, and optional quality columns are described
    by row 5. The previous parser treated all header rows as one string and
    therefore could silently miss every station. This parser identifies each
    station's block first, then selects the value column for each requested item.
    """
    rows = csv_rows(raw)
    if not rows:
        raise RuntimeError("JMA returned an empty CSV")

    data_start = _find_data_start(rows)
    if data_start < 0:
        preview = "\\n".join(",".join(r[:12]) for r in rows[:8])
        raise RuntimeError(f"Could not locate JMA data rows. Header preview:\\n{preview}")

    # The documented format puts station names on the third header row and
    # item names on the fourth header row. Be defensive if an extra header row
    # (for example prefecture names) is inserted.
    station_header_idx = None
    item_header_idx = None
    for i in range(min(data_start, 8)):
        joined = "".join(normal(x) for x in rows[i])
        if station_header_idx is None and ("地点名" in joined or any(
            normal(st["name"]) and normal(st["name"]) in joined for st in stations
        )):
            station_header_idx = i
        if item_header_idx is None and ("平均気温" in joined or "最高気温" in joined or "最低気温" in joined):
            item_header_idx = i

    if station_header_idx is None:
        station_header_idx = 2 if data_start > 2 else max(0, data_start - 2)
    if item_header_idx is None:
        item_header_idx = 3 if data_start > 3 else max(0, data_start - 1)

    station_row = rows[station_header_idx]
    item_row = rows[item_header_idx]
    max_cols = max(len(station_row), len(item_row), *(len(r) for r in rows[:data_start]))

    # Fill forward station names because JMA/HTML-to-CSV variants sometimes
    # leave continuation cells blank even though the official example repeats
    # them. A station block is therefore a contiguous run of columns.
    station_by_col: list[str] = [""] * max_cols
    current = ""
    for c in range(max_cols):
        raw_name = normal(station_row[c]) if c < len(station_row) else ""
        if raw_name:
            current = raw_name
        station_by_col[c] = current

    # Some responses include the six date columns first. Only columns belonging
    # to requested stations are considered below.
    requested: dict[str, dict[str, Any]] = {}
    for st in stations:
        for alias in _station_aliases(st["name"]):
            requested[alias] = st

    columns: dict[str, dict[str, int]] = {}
    for c in range(max_cols):
        st_name = station_by_col[c]
        if not st_name:
            continue
        st = None
        for alias, candidate in requested.items():
            if alias == st_name or alias in st_name or st_name in alias:
                st = candidate
                break
        if st is None:
            continue

        item = normal(item_row[c]) if c < len(item_row) else ""
        kind = None
        if re.search(r"日平均気温|平均気温", item):
            kind = "avg"
        elif re.search(r"日最高気温|最高気温", item):
            kind = "max"
        elif re.search(r"日最低気温|最低気温", item):
            kind = "min"
        elif re.search(r"降水量", item):
            kind = "rain"
        if kind and kind not in columns.setdefault(st["id"], {}):
            columns[st["id"]][kind] = c

    # Positional fallback: when station names are omitted/reformatted in the
    # header, the station blocks still occur in the same order as the request.
    # Build blocks from the item row by locating the first occurrence of each
    # requested item group, then map them in station order.
    if len(columns) < len(stations):
        # Detect runs of columns that contain at least one requested item.
        item_cols: list[tuple[int, str]] = []
        for c in range(max_cols):
            item = normal(item_row[c]) if c < len(item_row) else ""
            kind = None
            if re.search(r"日平均気温|平均気温", item): kind = "avg"
            elif re.search(r"日最高気温|最高気温", item): kind = "max"
            elif re.search(r"日最低気温|最低気温", item): kind = "min"
            elif re.search(r"降水量", item): kind = "rain"
            if kind:
                item_cols.append((c, kind))

        # Group consecutive/near-consecutive item columns into station blocks.
        blocks: list[list[tuple[int, str]]] = []
        for col_kind in item_cols:
            if not blocks or col_kind[0] - blocks[-1][-1][0] > 4:
                blocks.append([col_kind])
            else:
                blocks[-1].append(col_kind)
        if len(blocks) >= len(stations):
            for st, block in zip(stations, blocks):
                d = columns.setdefault(st["id"], {})
                for c, kind in block:
                    d.setdefault(kind, c)

    missing = [st["name"] for st in stations if set(columns.get(st["id"], {})) != {"avg", "min", "max", "rain"}]
    if missing:
        preview = "\\n".join(
            f"{i}: station={station_by_col[i]!r}, item={normal(item_row[i]) if i < len(item_row) else ''!r}"
            for i in range(min(max_cols, 80))
            if station_by_col[i] or (i < len(item_row) and normal(item_row[i]))
        )
        raise RuntimeError(
            f"Could not map all JMA elements. Missing stations: {missing[:10]}"\
            f". Header columns:\\n{preview[:6000]}"
        )

    out: dict[str, dict[str, dict[str, float | None]]] = {}
    for row in rows[data_start:]:
        iso = _iso_date(row)
        if not iso:
            continue
        for st in stations:
            cols = columns[st["id"]]
            out.setdefault(st["id"], {})[iso] = {
                key: value(row[col] if col < len(row) else "")
                for key, col in cols.items()
            }

    if not out:
        raise RuntimeError("JMA CSV contained no parseable data rows")

    # Do not allow a successful HTTP response to silently turn into an all-null
    # dataset. At least one numeric value is required from every parsed batch.
    numeric_count = sum(
        1 for by_date in out.values()
        for vals in by_date.values()
        for v in vals.values()
        if v is not None
    )
    if numeric_count == 0:
        raise RuntimeError("JMA CSV parsed, but every weather value was null")

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
                # The current JMA download service requires the hidden sid from
                # index.php as a form field. Refresh it for every batch so a
                # stale/expired session cannot poison the whole workflow.
                sid = get_obsdl_session_id(session)
                print(f"  JMA obsdl session initialized (cookie/form id: {bool(sid)})", flush=True)
                if sid:
                    data["PHPSESSID"] = sid
                time.sleep(REQUEST_PAUSE_SECONDS)

                r = session.post(
                    OBSDL_TABLE_URL,
                    data=data,
                    headers={
                        "Referer": OBSDL_INDEX_URL,
                        "Origin": "https://www.data.jma.go.jp",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "text/x-comma-separated-values,text/csv,*/*;q=0.8",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                r.raise_for_status()
                if len(r.content) < 100:
                    raise RuntimeError("JMA returned an unexpectedly small response")
                content_type = (r.headers.get("Content-Type") or "").lower()
                head = r.content[:512].lower()
                if "text/html" in content_type or b"<!doctype html" in head or b"<html" in head:
                    body = r.content.decode("utf-8-sig", errors="replace")
                    body = re.sub(r"\s+", " ", body).strip()
                    raise RuntimeError(
                        "JMA returned an HTML page instead of CSV; "
                        f"batch={ids}; response={body[:1200]!r}"
                    )
                parsed = parse_batch(r.content, batch)
                for sid, rows in parsed.items():
                    merged.setdefault(sid, {}).update(rows)
                numeric_count = sum(
                    1 for by_date in parsed.values()
                    for vals in by_date.values()
                    for v in vals.values()
                    if v is not None
                )
                print(
                    f"  batch {pos + 1}-{pos + len(batch)} / {len(stations)}: "
                    f"{len(parsed)} stations, {numeric_count} numeric values",
                    flush=True,
                )
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
    today = jst_today()
    dates = comparison_dates(today)
    years = [today.year - i for i in range(5)]
    print(f"PhoteoSync JMA history: base date={today}, years={years}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
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

    numeric_count = sum(
        1 for by_date in raw.values()
        for vals in by_date.values()
        for v in vals.values()
        if v is not None
    )
    if numeric_count == 0:
        raise RuntimeError("No numeric JMA weather values were collected; refusing to write an all-null JSON")
    print(f"Collected {len(raw)} stations / {numeric_count} numeric values", flush=True)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result = build_output(stations, today, raw)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUTPUT)
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
