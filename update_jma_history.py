#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build data/svgjma-history.json from JMA daily weather pages.

This version deliberately does NOT use the JMA "Past Weather Data Download"
(obsdl) POST service.  It reads the public daily-value pages
/stats/etrn/view/daily_s1.php instead.

The output schema remains compatible with PhoteoSync's existing index.html.
"""
from __future__ import annotations

import io
import json
import math
import re
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

JMA_AMEDAS_URL = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
JMA_DAILY_URL = "https://www.data.jma.go.jp/stats/etrn/view/daily_s1.php"
STATIONS_RDA_URL = "https://raw.githubusercontent.com/uribo/jmastats/master/data/stations.rda"

OUTPUT = Path("data/svgjma-history.json")

# JMA asks users not to make excessive automated requests.
# One monthly page contains the entire month, so one request covers all
# requested days in that month.
REQUEST_PAUSE_SECONDS = 1.2
REQUEST_TIMEOUT = 45
RETRY_COUNT = 3

WINDOW_DAYS = 14
YEARS_BACK = 5


def jst_today() -> date:
    return (datetime.now(UTC) + timedelta(hours=9)).date()


def comparison_dates(today: date) -> list[date]:
    return [today - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]


def shift_to_year(d: date, year: int) -> date:
    if d.month == 2 and d.day == 29:
        if not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
            return date(year, 2, 28)
    return date(year, d.month, d.day)


def value(text: str) -> float | None:
    s = (text or "").strip().replace("−", "-").replace("＊", "").replace("*", "")
    s = s.strip("()[]")
    if not s or s in {"--", "---", "///", "×", "...", "欠測"}:
        return None
    # Remove a trailing JMA quality/display marker such as ")".
    s = re.sub(r"[)\]]$", "", s).strip()
    try:
        return float(s)
    except ValueError:
        return None


def get_amedas_stations(session: requests.Session) -> list[dict[str, Any]]:
    r = session.get(JMA_AMEDAS_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    raw = r.json()

    result = []
    for station_id, s in raw.items():
        try:
            lat = float(s["lat"][0]) + float(s["lat"][1]) / 60.0
            lon = float(s["lon"][0]) + float(s["lon"][1]) / 60.0
        except (KeyError, TypeError, ValueError, IndexError):
            continue

        elems = str(s.get("elems") or "")
        # Need temperature and precipitation.
        if len(elems) < 2 or elems[0] == "0" or elems[1] == "0":
            continue

        result.append({
            "id": str(station_id),
            "name": str(s.get("kjName") or s.get("enName") or station_id),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        })

    result.sort(key=lambda x: x["id"])
    if not result:
        raise RuntimeError("JMA AMeDAS station list is empty")
    return result


def load_jmastats_mapping(session: requests.Session) -> dict[str, dict[str, str]]:
    """Load station_no -> prec_no/block_no from jmastats' public station table.

    jmastats maintains the mapping needed by JMA's daily_s1 pages:
    AMeDAS station_no, JMA prec_no and block_no.
    """
    try:
        import pyreadr  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyreadr is required to read the jmastats station mapping. "
            "Install it with: python -m pip install pyreadr"
        ) from exc

    r = session.get(STATIONS_RDA_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".rda", delete=False) as f:
        f.write(r.content)
        tmp = Path(f.name)

    try:
        objects = pyreadr.read_r(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)

    if not objects:
        raise RuntimeError("Could not read jmastats stations.rda")

    # The object is normally named "stations".
    df = objects.get("stations") or next(iter(objects.values()))

    required = {"station_no", "prec_no", "block_no"}
    if not required.issubset(df.columns):
        raise RuntimeError(
            f"jmastats station table is missing columns: {sorted(required - set(df.columns))}"
        )

    mapping: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        station_no = str(row["station_no"]).strip()
        prec_no = str(row["prec_no"]).strip()
        block_no = str(row["block_no"]).strip()

        if station_no.lower() in {"nan", "none"}:
            continue
        if not re.fullmatch(r"\d{1,3}", prec_no):
            continue
        if not re.fullmatch(r"\d{4,5}", block_no):
            continue

        # If a station appears more than once, prefer a 5-digit block number.
        old = mapping.get(station_no)
        if old is None or (len(block_no) == 5 and len(old["block_no"]) != 5):
            mapping[station_no] = {
                "prec_no": prec_no,
                "block_no": block_no,
            }

    if not mapping:
        raise RuntimeError("jmastats station mapping is empty")
    return mapping


def daily_url(prec_no: str, block_no: str, year: int, month: int) -> str:
    # daily_s1 is the current public daily page for 5-digit international
    # station/block numbers. The same endpoint is used by current examples
    # and data consumers.
    return (
        f"{JMA_DAILY_URL}?prec_no={prec_no}&block_no={block_no}"
        f"&year={year}&month={month:02d}&day=&view=p1"
    )


def parse_daily_page(content: bytes, year: int, month: int) -> dict[int, dict[str, float | None]]:
    soup = BeautifulSoup(content, "html.parser")

    # Prefer tablefix1, the table used by JMA's daily page.
    table = soup.find("table", id="tablefix1")
    if table is None:
        # Fallback: choose the table containing a "日" header.
        for candidate in soup.find_all("table"):
            text = candidate.get_text(" ", strip=True)
            if "日" in text and ("平均気温" in text or "最高気温" in text):
                table = candidate
                break

    if table is None:
        raise RuntimeError("JMA daily page did not contain the expected daily table")

    rows = table.find_all("tr")

    # Current JMA AMeDAS daily_s1 layout:
    # c00 day
    # c01 precipitation
    # c06 average temperature
    # c07 maximum temperature
    # c08 minimum temperature
    #
    # We still inspect header text first so minor table changes do not silently
    # produce wrong values.
    header_text = " ".join(
        th.get_text(" ", strip=True) for th in table.find_all("th")
    )

    result: dict[int, dict[str, float | None]] = {}
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 9:
            continue

        m = re.fullmatch(r"\d{1,2}", cells[0])
        if not m:
            continue

        day = int(cells[0])
        if day < 1 or day > 31:
            continue

        # AMeDAS daily_s1 has the four values at these positions.
        # If the page is not an AMeDAS daily table, fail loudly rather than
        # generating a subtly incorrect JSON file.
        rain = value(cells[1])
        avg = value(cells[6])
        max_temp = value(cells[7])
        min_temp = value(cells[8])

        result[day] = {
            "avg": avg,
            "min": min_temp,
            "max": max_temp,
            "rain": rain,
        }

    if not result:
        title = soup.title.get_text(" ", strip=True) if soup.title else "(no title)"
        raise RuntimeError(f"No daily rows found on JMA page: {title}")

    numeric_count = sum(
        1
        for vals in result.values()
        for v in vals.values()
        if v is not None
    )
    if numeric_count == 0:
        raise RuntimeError("JMA daily table was found, but all four requested values were missing")

    return result


def fetch_month(
    session: requests.Session,
    prec_no: str,
    block_no: str,
    year: int,
    month: int,
) -> dict[int, dict[str, float | None]]:
    url = daily_url(prec_no, block_no, year, month)

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            r = session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ja,en;q=0.8",
                },
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()

            if b"<html" not in r.content[:4096].lower():
                raise RuntimeError("JMA daily endpoint did not return HTML")

            return parse_daily_page(r.content, year, month)
        except Exception as exc:
            if attempt == RETRY_COUNT:
                raise RuntimeError(
                    f"JMA daily fetch failed: prec_no={prec_no}, "
                    f"block_no={block_no}, year={year}, month={month}: {exc}"
                ) from exc
            print(f"    retry {attempt}/{RETRY_COUNT - 1}: {exc}", file=sys.stderr)
            time.sleep(4 * attempt)

    raise AssertionError("unreachable")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_station_history(
    session: requests.Session,
    station: dict[str, Any],
    mapping: dict[str, dict[str, str]],
    years: list[int],
    dates: list[date],
) -> dict[str, dict[str, float | None]]:
    info = mapping.get(station["id"])
    if not info:
        raise RuntimeError(
            f"No JMA daily block mapping for AMeDAS station {station['id']} {station['name']}"
        )

    needed_months = sorted({(d.month) for d in dates})
    out: dict[str, dict[str, float | None]] = {}

    for year in years:
        for month in needed_months:
            monthly = fetch_month(
                session, info["prec_no"], info["block_no"], year, month
            )
            for day_num, vals in monthly.items():
                try:
                    d = date(year, month, day_num)
                except ValueError:
                    continue
                out[d.isoformat()] = vals
            time.sleep(REQUEST_PAUSE_SECONDS)

    return out


def build_output(
    stations: list[dict[str, Any]],
    today: date,
    raw: dict[str, dict[str, dict[str, float | None]]],
) -> dict[str, Any]:
    dates = comparison_dates(today)
    years = [today.year - i for i in range(YEARS_BACK)]
    labels = [f"{d.month:02d}-{d.day:02d}" for d in dates]

    result_stations = []
    for st in stations:
        by_date = raw.get(st["id"], {})
        years_obj: dict[str, Any] = {}

        for year in years:
            arr = {"avg": [], "min": [], "max": [], "rain": []}
            for base in dates:
                d = shift_to_year(base, year)
                vals = by_date.get(d.isoformat(), {})
                for key in arr:
                    arr[key].append(vals.get(key))
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
        "windowDays": WINDOW_DAYS,
        "dates": labels,
        "years": years,
        "stations": result_stations,
    }


def main() -> None:
    today = jst_today()
    dates = comparison_dates(today)
    years = [today.year - i for i in range(YEARS_BACK)]

    print(f"PhoteoSync JMA history (daily_s1): base date={today}, years={years}")

    session = requests.Session()
    stations = get_amedas_stations(session)
    print(f"AMeDAS stations with temp+rain: {len(stations)}")

    print("Loading AMeDAS -> JMA prec_no/block_no mapping...")
    mapping = load_jmastats_mapping(session)

    mapped = [s for s in stations if s["id"] in mapping]
    unmapped = [s for s in stations if s["id"] not in mapping]

    # Optional smoke-test limiter. Normal GitHub Actions runs should leave this
    # unset so every mapped station is updated. Set JMA_STATION_LIMIT=5 for a
    # quick first validation of the daily_s1 pipeline.
    limit_text = (Path(".jma_station_limit").read_text(encoding="utf-8").strip()
                  if Path(".jma_station_limit").exists() else "")
    if limit_text:
        try:
            limit = max(1, int(limit_text))
            mapped = mapped[:limit]
            print(f"JMA station limit enabled: {limit}")
        except ValueError:
            raise RuntimeError(".jma_station_limit must contain an integer")

    print(f"Mapped stations: {len(mapped)} / {len(stations)}")
    if unmapped:
        print(
            "WARNING: unmapped AMeDAS stations (first 20): "
            + ", ".join(f"{s['id']}:{s['name']}" for s in unmapped[:20])
        )

    raw: dict[str, dict[str, dict[str, float | None]]] = {}

    # The page is monthly, so the 14-day window normally requires only two
    # months per year. This is much less traffic than downloading one day at a
    # time and completely avoids obsdl's POST/session mechanism.
    total_requests = len(mapped) * len(years) * len({d.month for d in dates})
    print(f"Planned JMA monthly page requests: {total_requests}")
    print("JMA requests are deliberately serialized with a pause between pages.")

    for idx, station in enumerate(mapped, start=1):
        info = mapping[station["id"]]
        print(
            f"[{idx}/{len(mapped)}] {station['id']} {station['name']} "
            f"(prec_no={info['prec_no']}, block_no={info['block_no']})",
            flush=True,
        )

        raw[station["id"]] = fetch_station_history(
            session, station, mapping, years, dates
        )

        numeric_count = sum(
            1
            for vals in raw[station["id"]].values()
            for v in vals.values()
            if v is not None
        )
        print(f"    collected {numeric_count} numeric values", flush=True)

    # Safety: never overwrite a good JSON with an all-null result.
    numeric_count = sum(
        1
        for by_date in raw.values()
        for vals in by_date.values()
        for v in vals.values()
        if v is not None
    )
    if numeric_count == 0:
        raise RuntimeError(
            "No numeric JMA weather values were collected; refusing to write JSON"
        )

    print(
        f"Collected {len(raw)} stations / {numeric_count} numeric values",
        flush=True,
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result = build_output(stations, today, raw)

    # Validate that at least the mapped stations contain numeric values in the
    # requested comparison window.
    valid_stations = 0
    for st in result["stations"]:
        all_values = [
            v
            for year_obj in st["years"].values()
            for arr in year_obj.values()
            for v in arr
            if v is not None
        ]
        if all_values:
            valid_stations += 1

    if valid_stations == 0:
        raise RuntimeError(
            "Generated output contains no station with numeric comparison data"
        )

    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(OUTPUT)

    print(
        f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes), "
        f"stations_with_data={valid_stations}",
        flush=True,
    )


if __name__ == "__main__":
    main()
