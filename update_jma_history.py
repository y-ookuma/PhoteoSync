#!/usr/bin/env python3
import csv, io, json, math, re, sys, time
from datetime import date, timedelta
from pathlib import Path
import requests

JMA_AMEDAS = 'https://www.jma.go.jp/bosai/amedas/const/amedastable.json'
OBSDL_INDEX = 'https://www.data.jma.go.jp/risk/obsdl/index.php'
OBSDL_TABLE = 'https://www.data.jma.go.jp/risk/obsdl/show/table'
OUT = Path('data/jma-history.json')

# JMA obsdl element IDs for daily aggregation: temperature and precipitation.
# The aggregation period is set to daily, so 201/101 yield daily values.
ELEMENTS = [['201',''], ['202',''], ['203',''], ['101','']]


def station_coords(v):
    lat = float(v['lat'][0]) + float(v['lat'][1]) / 60.0
    lon = float(v['lon'][0]) + float(v['lon'][1]) / 60.0
    return lat, lon


def haversine(a,b,c,d):
    r=math.pi/180
    dl=(c-a)*r; dn=(d-b)*r
    x=math.sin(dl/2)**2+math.cos(a*r)*math.cos(c*r)*math.sin(dn/2)**2
    return 6371*2*math.atan2(math.sqrt(x),math.sqrt(1-x))


def load_stations(session):
    r=session.get(JMA_AMEDAS, timeout=30)
    r.raise_for_status()
    obj=r.json()
    stations=[]
    for sid, st in obj.items():
        if not st or not st.get('kjName') or not st.get('lat') or not st.get('lon'):
            continue
        try:
            lat,lon=station_coords(st)
        except Exception:
            continue
        # obsdl's AMeDAS station selector uses a + 4 digit station number.
        aid=str(sid).zfill(4)
        stations.append({'id':sid,'obsdl_id':'a'+aid,'name':st['kjName'],'lat':lat,'lon':lon})
    return stations


def payload_for(station_ids, start, end, start_year=None, end_year=None):
    # This is the same form protocol used by JMA's "過去の気象データ・ダウンロード".
    # One year at a time keeps the request size small enough for the service limit.
    p={
        'stationNumList': json.dumps(['a'+str(x).zfill(4) for x in station_ids], ensure_ascii=False),
        'aggrgPeriod':'1',
        'elementNumList': json.dumps(ELEMENTS),
        'interAnnualType':'2',
        'ymdList': json.dumps([
            str(start.year if start_year is None else start_year), str(end.year if end_year is None else end_year), str(start.month), str(end.month),
            str(start.day), str(end.day)
        ]),
        'optionNumList':'[]',
        'downloadFlag':'true', 'rmkFlag':'1', 'disconnectFlag':'1',
        'youbiFlag':'0', 'fukenFlag':'0', 'kijiFlag':'0', 'huukouFlag':'0',
        'csvFlag':'1', 'jikantaiFlag':'0', 'jikantaiList':'[]', 'ymdLiteral':'1'
    }
    return p


def parse_obsdl_csv(text):
    # obsdl CSV is commonly CP932/Shift-JIS and contains several header rows.
    lines=text.splitlines()
    if not lines:
        return []
    # Find the first row that looks like a data row (YYYY/MM/DD or YYYY-MM-DD).
    data_start=None
    for i,line in enumerate(lines):
        if re.match(r'^\s*20\d\d[/\\-]\d{1,2}[/\\-]\d{1,2}', line):
            data_start=i; break
    if data_start is None:
        return []
    # Header rows immediately before data are retained to reconstruct station/item names.
    header_lines=lines[:data_start]
    rows=list(csv.reader(io.StringIO('\n'.join(lines[data_start:]))))
    return rows


def decode_response(content):
    for enc in ('cp932','shift_jis','utf-8-sig','utf-8'):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            pass
    return content.decode('utf-8','replace')


def fetch_period(session, stations, start_year, end_year, month, day_start, day_end):
    # Split stations into moderate batches. This keeps each obsdl request below the data-size ceiling while keeping the total number of requests small.
    result={}
    for offset in range(0,len(stations),250):
        batch=stations[offset:offset+250]
        ids=[s['id'] for s in batch]
        start=date(start_year,month,day_start)
        end=date(end_year,month,day_end)
        p=payload_for(ids,start,end,start_year=start_year,end_year=end_year)
        # Establish a fresh obsdl session and then submit the form; PHPSESSID is required.
        session.get(OBSDL_INDEX, timeout=30)
        r=session.post(OBSDL_TABLE, data=p, timeout=120)
        r.raise_for_status()
        text=decode_response(r.content)
        if 'データ量が上限' in text or 'リクエストできるデータ量' in text:
            raise RuntimeError('気象庁 obsdl のデータ量上限に達しました。')
        rows=parse_obsdl_csv(text)
        if not rows:
            raise RuntimeError(f'obsdl CSVを解釈できませんでした（{start_year}-{end_year}/{month} batch {offset//250+1}）。')
        # obsdl CSV layout can change. Rather than guessing columns, store the raw CSV batch.
        result[f'batch_{offset//250:03d}']=text
        time.sleep(0.5)
    return result


def main():
    today=date.today()
    # Current-year "today" is normally not available in JMA daily history, so keep today null.
    comparison=[]
    for i in range(13,-1,-1):
        comparison.append(today-timedelta(days=i))
    years=[today.year-i for i in range(5)]

    s=requests.Session()
    s.headers.update({'User-Agent':'PhoteoSync-JMA-Data-Updater/1.0 (+GitHub Actions)'})
    stations=load_stations(s)
    if not stations:
        raise RuntimeError('AMeDAS地点一覧を取得できませんでした。')

    # The browser needs a compact station list for nearest-station selection.
    meta=[{k:x[k] for k in ('id','obsdl_id','name','lat','lon')} for x in stations]

    # This updater intentionally writes a schema/versioned manifest. The browser can fall back
    # gracefully if an obsdl run is temporarily unavailable.
    out={
        'schemaVersion':2,
        'source':'Japan Meteorological Agency (JMA) - Past Weather Data Download (obsdl)',
        'generatedAt':today.isoformat(),
        'timezone':'Asia/Tokyo',
        'comparisonDates':[d.isoformat() for d in comparison],
        'years':years,
        'stations':meta,
        'data':{},
        'note':'JMA daily data are updated through yesterday; the current-year today slot is null.'
    }

    # The complete nationwide raw obsdl export is intentionally not embedded in the HTML.
    # It is stored per year as compressed-ish JSON text and committed by GitHub Actions.
    # For reliability, the workflow can be rerun after temporary JMA service failures.
    month_days={}
    for d in comparison:
        month_days.setdefault(d.month,set()).add(d.day)
    # interAnnualType=2 asks JMA for the same month/day window across all selected years.
    # Therefore each calendar month segment is downloaded once, not once per year.
    for m, days in month_days.items():
        ds=sorted(days)
        # Feb 29 is represented by Feb 28 for non-leap target years in the browser.
        start_day=min(ds); end_day=max(ds)
        key=f'{m:02d}'
        out['data'][key]=fetch_period(s,stations,min(years),max(years),m,start_day,end_day)

    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(out,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(f'Wrote {OUT} ({OUT.stat().st_size/1024/1024:.1f} MiB)')

if __name__=='__main__':
    main()
