# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A single-page Japanese investment portfolio management app. The entire frontend is one self-contained file (`index.html`) with embedded CSS and vanilla JS — no build tools, no framework, no npm. Two Python scripts run as GitHub Actions to auto-fetch prices daily and commit the results back to the repo. The app reads those committed JSON files from GitHub's raw CDN.

## Running the price-fetch scripts locally

```bash
pip install -r requirements.txt

# Mutual fund NAV (基準価額)
python scripts/fetch_prices.py
python scripts/fetch_prices.py --debug   # dumps HTML to debug_*.html files

# Stock prices (株価)
python scripts/fetch_stock_prices.py
```

Python 3.12 is required (uses `dict | None` union syntax).

## Architecture: how data flows

```
fund_codes.json  →  scripts/fetch_prices.py       →  prices.json + price_history.json
stock_codes.json →  scripts/fetch_stock_prices.py →  stock_prices.json + stock_price_history.json
```

GitHub Actions runs both scripts daily at 17:00 JST (08:00 UTC) and commits updated JSON files to `main`. The `index.html` app fetches these JSON files at runtime from `https://raw.githubusercontent.com/keisuketwvwv-svg/my-app/main/*.json` with a cache-busting timestamp query parameter.

## Price source priority

**Funds (`fetch_prices.py`):**
1. 投信総合検索ライブラリー CSV (requires `isin` in `fund_codes.json`) — most reliable
2. Yahoo Finance Japan HTML scraping — fallback
3. みんかぶ HTML scraping — final fallback

**Stocks (`fetch_stock_prices.py`):**
1. Yahoo Finance Chart API (`/v8/finance/chart/{ticker}`)
2. stooq.com CSV — fallback (TSE stocks only, uses `.jp` suffix)

When a fetch fails, the previous value in the output JSON is preserved with `"stale": true`.

## Registering instruments

**Funds** — edit `fund_codes.json`:
- `code`: 8-character 投資信託協会コード (from toushin-lib.fwg.ne.jp)
- `isin`: ISINコード — enables the CSV source (strongly recommended)
- `name`: **must exactly match** the fund name the user enters in the app's UI; this is the key used to link fetched prices to holdings

**Stocks** — edit `stock_codes.json`:
- `code`: the identifier used inside the app's localStorage
- `ticker`: Yahoo Finance / stooq ticker (TSE stocks use `XXXX.T`)
- if `ticker` is omitted, `code` is used as the ticker

## Frontend architecture (`index.html`)

All user data is stored in `localStorage` — there is no backend or database.

| localStorage key | Contents |
|---|---|
| `tokushi_portfolio_v1` | Fund holdings array |
| `tokushi_history_v1` | Manual portfolio snapshots |
| `tokushi_settings_v1` | User settings (goal amount, start date, etc.) |
| `tokushi_dc_v1` | DC pension data |
| `tokushi_stocks_v1` | Stock holdings array |
| `tokushi_sections_v1` | UI section collapsed/expanded state |
| `tokushi_price_updated_v1` | ISO timestamp of last price auto-load |

Account types the app understands: `旧NISA`, `新NISA積立`, `新NISA成長`, `特定` (tokutei), `持株会` (mochikabu), `DC`

NISA limits enforced in JS: 積立 ¥6M, 成長 ¥12M, total ¥18M (`NISA_LIMITS` / `NISA_TOTAL_LIMIT` constants).

Key JS functions to know when modifying the frontend:
- `render()` — top-level re-render dispatcher
- `renderSummary()` — portfolio total card
- `renderAccount(acType, listId, headRightId)` — renders one account section
- `renderPriceTable()` — the editable price table on the right column
- `loadPricesJSON()` / `loadStockPricesJSON()` — fetch JSON from GitHub raw and apply prices
- `persist()` / `persistStocks()` / `persistDc()` — save to localStorage
- `buildAutoHistory()` — combines `price_history.json` data with current holdings to generate historical chart data

## GitHub Actions

Both workflows support `workflow_dispatch` for manual runs. The `fetch-prices.yml` workflow additionally accepts a `debug` boolean input that uploads scraped HTML as an artifact (retained 3 days) — useful when a price source stops working.
