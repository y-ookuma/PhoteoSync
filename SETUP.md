# PhoteoSync JMA historical data setup

## Files

- `index.html` — PhoteoSync frontend. The JMA mode reads `data/svgjma-history.json`.
- `update_jma_history.py` — GitHub Actions updater. It obtains nationwide AMeDAS metadata and the same 14-day calendar window for the current year plus previous four years.
- `.github/workflows/jma-history.yml` — daily scheduled workflow and manual `workflow_dispatch`.
- `data/svgjma-history.json` — generated data file. The repository initially contains an empty schema placeholder; the first workflow run fills it.

## GitHub setup

1. Copy these files into the `y-ookuma/PhoteoSync` repository.
2. Confirm the workflow is exactly at `.github/workflows/jma-history.yml`.
3. Open **Actions → Update JMA history data → Run workflow** once manually.
4. After a successful run, `data/svgjma-history.json` will contain the nationwide AMeDAS data.
5. GitHub Pages will then serve the JSON together with `index.html`.

The frontend selects the nearest station from the latitude/longitude obtained by the browser and uses only that station's data from the single JSON file.

The JMA download service states that each request has a data-size limit and asks users not to make excessive automated requests, so the updater batches stations and pauses between requests.
