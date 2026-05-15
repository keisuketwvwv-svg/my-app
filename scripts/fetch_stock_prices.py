#!/usr/bin/env python3
"""
株価自動取得スクリプト
stock_codes.json に登録した銘柄の株価を Yahoo Finance / stooq から取得し
stock_prices.json に保存する。

ティッカーシンボル:
  東証銘柄は "<コード>.T"（例: 5243.T = note株式会社、8308.T = りそなHD）

使い方:
  stock_codes.json に銘柄コードと銘柄名を登録してください。
  GitHub Actions が毎日 17:00 JST に自動実行します。
"""

import json
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_yahoo_chart(ticker: str) -> dict | None:
    """
    Yahoo Finance Chart API から最新終値を取得。
    日本株は ".T" サフィックス（例: 5243.T）。
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        result = (d.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price and float(price) > 0:
            ts = meta.get("regularMarketTime")
            date_str = ""
            if ts:
                date_str = datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")
            return {"price": float(price), "date": date_str, "source": "yahoo"}
    except Exception as exc:
        print(f"  [yahoo] {ticker}: {exc}", file=sys.stderr)
    return None


def fetch_stooq(ticker: str) -> dict | None:
    """
    stooq.com から最新終値を取得（フォールバック）。
    東証は ".jp"（例: 5243.jp）。
    """
    if not ticker.endswith(".T"):
        return None
    code = ticker[:-2].lower() + ".jp"
    try:
        url = f"https://stooq.com/q/l/?s={code}&f=sd2t2ohlcv&h&e=csv"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200 or "N/D" in r.text:
            return None
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        cols = lines[1].split(",")
        # Symbol,Date,Time,Open,High,Low,Close,Volume
        if len(cols) >= 7:
            close = float(cols[6])
            date_str = cols[1] if cols[1] != "N/D" else ""
            if close > 0:
                return {"price": close, "date": date_str, "source": "stooq"}
    except Exception as exc:
        print(f"  [stooq] {ticker}: {exc}", file=sys.stderr)
    return None


def fetch_price(ticker: str) -> dict | None:
    """Yahoo → stooq の順でフォールバック"""
    return fetch_yahoo_chart(ticker) or fetch_stooq(ticker)


def main() -> None:
    config_path = ROOT / "stock_codes.json"

    if not config_path.exists():
        sample = {
            "_comment": (
                "ティッカーシンボル（東証コード + .T）を登録してください。"
                "code はアプリの株式銘柄に登録した code と完全一致させてください。"
            ),
            "stocks": [
                {"code": "5243.T", "name": "note"},
                {"code": "8308.T", "name": "りそなホールディングス"}
            ]
        }
        config_path.write_text(
            json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("stock_codes.json を作成しました。銘柄コードを設定してください。")
        sys.exit(0)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    stock_list = [s for s in config.get("stocks", []) if not s.get("_note")]

    if not stock_list:
        print("stock_codes.json に銘柄が登録されていません。", file=sys.stderr)
        sys.exit(0)

    print(f"取得対象: {len(stock_list)} 銘柄\n")

    results: dict[str, dict] = {}
    failed: list[str] = []
    now_jst = datetime.now(JST)

    for entry in stock_list:
        code = entry.get("code", "").strip()
        name = entry.get("name", "").strip()
        if not code:
            continue

        print(f"  {name or code} ({code}) ... ", end="", flush=True)
        data = fetch_price(code)

        if data:
            results[code] = {
                "price": data["price"],
                "date":  data["date"],
                "name":  name,
                "source": data["source"],
            }
            print(f"¥{data['price']:,.0f}  ({data['date']}, {data['source']})")
        else:
            failed.append(code)
            print("取得失敗", file=sys.stderr)

        time.sleep(0.5)

    # 既存の stock_prices.json を読み込み、失敗銘柄は前回値を保持
    out_path = ROOT / "stock_prices.json"
    existing: dict = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            existing = prev.get("prices", {})
        except Exception:
            pass

    for code in failed:
        if code in existing:
            results[code] = {**existing[code], "stale": True}
            print(f"  {code}: 前回値を維持 (¥{existing[code]['price']:,.0f})")

    output = {
        "updated": now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "prices":  results,
    }
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    success = len(results) - len([v for v in results.values() if v.get("stale")])
    print(f"\n完了: {success}/{len(stock_list)} 銘柄を取得 → stock_prices.json に保存")

    if len(failed) == len(stock_list):
        sys.exit(1)


if __name__ == "__main__":
    main()
