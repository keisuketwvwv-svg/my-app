#!/usr/bin/env python3
"""
基準価額自動取得スクリプト

データソース（優先順）:
  1. 投信総合検索ライブラリー CSV
     https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download
     ?isinCd={isin}&associFundCd={code}
     ※ fund_codes.json に isin を設定した銘柄のみ使用（推奨）
  2. Yahoo Finance Japan  https://finance.yahoo.co.jp/quote/{code}
  3. みんかぶ             https://minkabu.jp/fund/{code}

ファンドコードの確認: https://toushin-lib.fwg.ne.jp/FdsWeb/
  → 銘柄名で検索 → 8文字の投資信託協会コードと ISINコードを fund_codes.json に登録

トラブルシュート:
  python scripts/fetch_prices.py --debug  # HTML/CSV を dump して確認
"""

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

JST  = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent

# 基準価額として現実的な範囲（円 / 万口）
PRICE_MIN, PRICE_MAX = 500, 500_000

# ── HTTP セッション設定 ───────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    # br(brotli)を除外 — requests は brotli パッケージなしでは展開不可
    # gzip/deflate は urllib3 が自動展開する
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
})


# ── 価格抽出ユーティリティ ────────────────────────────────────────────────────

def _to_price(raw: str) -> int | None:
    """カンマ除去・範囲チェックして int を返す。範囲外なら None。"""
    try:
        p = int(raw.replace(",", "").replace("，", "").strip())
        return p if PRICE_MIN <= p <= PRICE_MAX else None
    except (ValueError, OverflowError):
        return None


def _extract_date(html: str) -> str:
    """HTML から YYYY-MM-DD を抽出（最初に見つかった日付）。"""
    m = re.search(r"(\d{4})[年/](\d{1,2})[月/](\d{1,2})", html)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def parse_price(html: str, debug: bool = False) -> tuple[int | None, str]:
    """
    HTML から基準価額と日付を抽出する。

    戦略:
      1. <script id="__NEXT_DATA__"> 内の JSON キーを探す（最も確実）
      2. window.__INITIAL_STATE__ / __PRELOADED_STATE__ を探す
      3. HTML テキストの正規表現マッチ
    """

    # ── 1. Next.js / SSR 埋め込み JSON ─────────────────────────────────────
    json_blocks = []

    # __NEXT_DATA__
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.+?\})\s*</script>',
        html, re.DOTALL,
    )
    if m:
        json_blocks.append(("__NEXT_DATA__", m.group(1)))

    # window.__INITIAL_STATE__ / window.__PRELOADED_STATE__
    for var in ("__INITIAL_STATE__", "__PRELOADED_STATE__", "__STATE__"):
        m2 = re.search(
            rf'window\.{re.escape(var)}\s*=\s*(\{{.+?\}});?\s*</script>',
            html, re.DOTALL,
        )
        if m2:
            json_blocks.append((var, m2.group(1)))

    # JSON ブロック内のキー名候補（基準価額に相当するもの）
    PRICE_KEYS = (
        "unitPrice", "unit_price",
        "basePrice", "base_price",
        "basicPrice", "basicprice",
        "currentPrice", "current_price",
        "nav", "NAV",
        "price", "Price",
        "netAssetValue", "net_asset_value",
        "standardPrice",
    )

    for block_name, json_text in json_blocks:
        for key in PRICE_KEYS:
            km = re.search(
                rf'"{re.escape(key)}"\s*:\s*"?(\d[\d,]*)"?',
                json_text,
            )
            if km:
                price = _to_price(km.group(1))
                if price:
                    if debug:
                        print(f"    [JSON:{block_name}] key={key} → {price:,}")
                    return price, _extract_date(html)

    # ── 2. HTML テキストパターン ─────────────────────────────────────────────
    HTML_PATTERNS = [
        # 「基準価額」直後の数値
        r"基準価額[^<\d]{0,20}(\d[\d,]{3,7})(?:\s*円|\s*<|\s*/)",
        # <dt>基準価額</dt> <dd>数値</dd>
        r"<dt[^>]*>\s*基準価額\s*</dt>\s*<dd[^>]*>\s*([\d,]+)",
        # data属性
        r'data-price="(\d[\d,]+)"',
        r'data-nav="(\d[\d,]+)"',
        # class が price / value 系の要素（数値が4〜7桁）
        r'class="[^"]*(?:fprice|price|Price|value|Value|nav|NAV)[^"]*"[^>]*>\s*([\d,]{4,8})\s*<',
        # 円マーク付き
        r"¥\s*([\d,]{4,8})",
    ]

    for pat in HTML_PATTERNS:
        m = re.search(pat, html, re.DOTALL)
        if m:
            price = _to_price(m.group(1))
            if price:
                if debug:
                    print(f"    [HTML regex] pattern={pat[:50]}… → {price:,}")
                return price, _extract_date(html)

    if debug:
        print("    [parse_price] 価格を検出できませんでした")
        snippet = html[:3000].replace("\r", "").replace("\n", " ")
        print(f"    [HTML先頭3000字] {snippet!r}")
        nums = re.findall(r"\d{1,3}(?:,\d{3})+", html)
        print(f"    [X,XXX形式の数値] {nums[:30]}")

    return None, ""


# ── ソース別フェッチ ──────────────────────────────────────────────────────────

def _fetch_html(url: str, retries: int = 3) -> tuple[int, str]:
    """URL をフェッチして (status_code, html) を返す。失敗時はリトライ。"""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=20, allow_redirects=True)
            content_type = r.headers.get("Content-Type", "").lower()
            if "charset=" in content_type:
                html = r.text
            else:
                html = None
                for enc in ("utf-8", "euc-jp", "cp932", "shift-jis"):
                    try:
                        html = r.content.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if html is None:
                    html = r.content.decode("utf-8", errors="replace")
            return r.status_code, html
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"      リトライ {attempt}/{retries-1} ({wait}s後)…", file=sys.stderr)
                time.sleep(wait)
    raise last_exc


def _fetch_bytes(url: str, retries: int = 3) -> tuple[int, bytes]:
    """URL をフェッチして (status_code, bytes) を返す。"""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=30, allow_redirects=True)
            return r.status_code, r.content
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"      リトライ {attempt}/{retries-1} ({wait}s後)…", file=sys.stderr)
                time.sleep(wait)
    raise last_exc


def _fetch_toushin_csv(isin: str, code: str, debug: bool = False) -> dict | None:
    """
    投信総合検索ライブラリーから基準価額 CSV をダウンロードし最新値を返す。
    CSV は Shift-JIS (cp932) エンコード、日次データ、最終行が最新。
    列: 年月日, 基準価額(円), 純資産総額（百万円）, 分配金（円）
    """
    url = (
        f"https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin}&associFundCd={code}"
    )
    source = "投信ライブラリー"
    if debug:
        print(f"    [{source}] GET {url}")
    try:
        status, raw = _fetch_bytes(url)
        if debug:
            print(f"    [{source}] HTTP {status}  size={len(raw)} bytes")
        if status != 200:
            print(f"    [{source}] HTTP {status} — スキップ", file=sys.stderr)
            return None

        # エンコード検出
        text = None
        for enc in ("cp932", "shift-jis", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")

        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any(c.strip() for c in r)]
        if len(rows) < 2:
            if debug:
                print(f"    [{source}] CSV 行数不足: {len(rows)}")
            return None

        header = rows[0]
        if debug:
            print(f"    [{source}] ヘッダー: {header}")
            print(f"    [{source}] 最終行: {rows[-1]}")

        # 列インデックスを探す
        price_col = next(
            (i for i, h in enumerate(header) if "基準価額" in h), None
        )
        date_col = next(
            (i for i, h in enumerate(header)
             if "年月日" in h or "日付" in h or h.lower() in ("date",)),
            None,
        )

        if price_col is None:
            if debug:
                print(f"    [{source}] 「基準価額」列が見つかりません: {header}")
            return None

        last = rows[-1]
        price_raw = last[price_col].strip() if price_col < len(last) else ""
        price = _to_price(price_raw)
        if not price:
            if debug:
                print(f"    [{source}] 価格変換失敗: {price_raw!r}")
            return None

        date_str = ""
        if date_col is not None and date_col < len(last):
            date_raw = last[date_col].strip()
            dm = re.search(r"(\d{4})[年/](\d{1,2})[月/](\d{1,2})", date_raw)
            if dm:
                date_str = (
                    f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                )

        return {"price": price, "date": date_str, "source": source}

    except Exception as e:
        print(f"    [{source}] エラー: {e}", file=sys.stderr)
        if debug:
            import traceback
            traceback.print_exc()
        return None


def fetch_nav(fund_code: str, isin: str = "", debug: bool = False) -> dict | None:
    """
    ファンドコード・ISINから基準価額を取得する。
    ISIN が設定されていれば投信ライブラリー CSV を最初に試す。
    """

    # ── 1. 投信総合検索ライブラリー CSV（最優先・最も確実） ────────────────
    if isin:
        data = _fetch_toushin_csv(isin, fund_code, debug)
        if data:
            print(
                f"    [投信ライブラリー] ¥{data['price']:,}/万口"
                + (f"  ({data['date']})" if data["date"] else "")
            )
            return data
        print(f"    [投信ライブラリー] 失敗 — HTML ソースを試みます", file=sys.stderr)
        time.sleep(1)

    # ── 2. HTML スクレイピング（フォールバック） ────────────────────────────
    sources = [
        (
            "Yahoo Finance JP",
            f"https://finance.yahoo.co.jp/quote/{fund_code}",
        ),
        (
            "みんかぶ",
            f"https://minkabu.jp/fund/{fund_code}",
        ),
    ]

    for source_name, url in sources:
        if debug:
            print(f"    [{source_name}] GET {url}")
        try:
            status, html = _fetch_html(url)

            if debug:
                dump_path = ROOT / f"debug_{fund_code}_{source_name.replace(' ','_')}.html"
                dump_path.write_text(html, encoding="utf-8")
                print(f"    [{source_name}] HTTP {status} → HTML を {dump_path.name} に保存")

            if status != 200:
                print(
                    f"    [{source_name}] HTTP {status} — スキップ",
                    file=sys.stderr,
                )
                time.sleep(1)
                continue

            price, date_str = parse_price(html, debug=debug)
            if price:
                print(
                    f"    [{source_name}] ¥{price:,}/万口"
                    + (f"  ({date_str})" if date_str else "")
                )
                return {"price": price, "date": date_str, "source": source_name}

            print(
                f"    [{source_name}] HTML取得済みだが価格を検出できず",
                file=sys.stderr,
            )

        except requests.exceptions.ConnectionError as e:
            print(f"    [{source_name}] 接続エラー: {e}", file=sys.stderr)
        except requests.exceptions.Timeout:
            print(f"    [{source_name}] タイムアウト", file=sys.stderr)
        except Exception as e:
            print(f"    [{source_name}] エラー: {e}", file=sys.stderr)

        time.sleep(1.5)

    return None


# ── メイン ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="基準価額自動取得")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="詳細ログを出力し HTML/CSV を dump する",
    )
    args = parser.parse_args()

    config_path = ROOT / "fund_codes.json"
    if not config_path.exists():
        print(
            "fund_codes.json が見つかりません。"
            "README の手順に従って作成してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    fund_list = [
        f for f in config.get("funds", [])
        if f.get("code") and f.get("name") and not f.get("_note")
    ]

    if not fund_list:
        print("fund_codes.json にファンドが登録されていません。", file=sys.stderr)
        sys.exit(0)

    isin_count = sum(1 for f in fund_list if f.get("isin"))
    print(f"取得対象: {len(fund_list)} 銘柄（うち ISIN 設定済み: {isin_count} 銘柄）\n")

    # ── 既存 prices.json を読み込む（前回値保持用） ──────────────────────────
    out_path = ROOT / "prices.json"
    prev_prices: dict = {}
    if out_path.exists():
        try:
            prev_prices = json.loads(
                out_path.read_text(encoding="utf-8")
            ).get("prices", {})
        except Exception:
            pass

    results: dict = {}
    failed: list[str] = []
    now_jst = datetime.now(JST)

    for entry in fund_list:
        code = entry["code"].strip()
        isin = entry.get("isin", "").strip()
        name = entry["name"].strip()
        print(f"  {name}  ({code})")

        data = fetch_nav(code, isin=isin, debug=args.debug)

        if data:
            results[name] = {"price": data["price"], "date": data["date"], "code": code}
        else:
            failed.append(name)
            if name in prev_prices:
                results[name] = {**prev_prices[name], "stale": True}
                print(
                    f"    ⚠ 取得失敗 → 前回値を保持 (¥{prev_prices[name]['price']:,})",
                    file=sys.stderr,
                )
            else:
                print(f"    ✗ 取得失敗（前回値なし）", file=sys.stderr)

        print()
        time.sleep(2)

    # ── 結果を prices.json に保存 ─────────────────────────────────────────────
    output = {
        "updated": now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "prices":  results,
    }
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── price_history.json を更新（日次蓄積）────────────────────────────────
    hist_path = ROOT / "price_history.json"
    hist_data: dict = {}
    if hist_path.exists():
        try:
            hist_data = json.loads(
                hist_path.read_text(encoding="utf-8")
            ).get("history", {})
        except Exception:
            pass

    # 取得成功した銘柄を基準価額の日付でグループ化して追記
    for name, data in results.items():
        if data.get("stale"):
            continue
        price_date = data.get("date") or now_jst.strftime("%Y-%m-%d")
        if price_date not in hist_data:
            hist_data[price_date] = {}
        hist_data[price_date][name] = data["price"]

    hist_path.write_text(
        json.dumps({"history": hist_data}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_date = max(hist_data.keys()) if hist_data else "-"
    print(f"  → price_history.json を更新（最新: {latest_date}、累計 {len(hist_data)} 日分）")

    success = len(results) - len([v for v in results.values() if v.get("stale")])
    print(f"完了: {success}/{len(fund_list)} 銘柄を取得 → prices.json を更新")

    if failed and len(failed) == len(fund_list):
        print(
            "\n全銘柄の取得に失敗しました。\n"
            "  --debug オプションで詳細を確認してください:\n"
            "    python scripts/fetch_prices.py --debug\n"
            "  ISINコードが fund_codes.json に正しく設定されているか確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
