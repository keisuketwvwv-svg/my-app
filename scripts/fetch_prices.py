#!/usr/bin/env python3
"""
基準価額自動取得スクリプト
投資信託協会のファンドコードを使って基準価額を取得し prices.json に保存する。

ファンドコードの確認方法:
  https://www.toushin.or.jp/statistics/detail/
  → 銘柄名で検索してコード（英数字8文字）を確認

使い方:
  fund_codes.json にファンドコードと銘柄名を登録してください。
  GitHub Actions が毎日 17:00 JST に自動実行します。
"""

import json
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent


def fetch_nav(fund_code: str) -> dict | None:
    """
    投資信託協会APIから基準価額を取得する。

    Args:
        fund_code: ファンドコード（英数字8文字、例: 0331418A）

    Returns:
        {"price": int, "date": str} または None（取得失敗時）
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; investment-portfolio-fetcher/1.0; "
            "+https://github.com)"
        )
    }

    # ── 1st try: 投資信託協会データAPI ──────────────────────────────────
    try:
        url = (
            "https://tskrscq.machicado.co.jp/api/v1/trust/"
            f"{fund_code}"
        )
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            d = r.json()
            # レスポンスのフィールド名が異なるケースに対応
            price = (
                d.get("base_price")
                or d.get("price")
                or d.get("nav")
                or d.get("unit_price")
                or d.get("基準価額")
            )
            if price and int(price) > 0:
                return {
                    "price": int(price),
                    "date": d.get("date") or d.get("base_date") or "",
                }
    except Exception as exc:
        print(f"  [1st] {fund_code}: {exc}", file=sys.stderr)

    # ── 2nd try: 投資信託協会 公式サイト（HTMLスクレイピング） ────────────
    try:
        import re
        url2 = (
            "https://www.toushin.or.jp/statistics/detail/"
            f"?fundCode={fund_code}"
        )
        r2 = requests.get(url2, headers=headers, timeout=15)
        if r2.status_code == 200:
            # 基準価額は "XX,XXX円" 形式で出現する
            m = re.search(r"基準価額[^\d]*([\d,]+)\s*円", r2.text)
            if m:
                price_str = m.group(1).replace(",", "")
                if price_str.isdigit() and int(price_str) > 0:
                    # 日付を探す
                    dm = re.search(r"(\d{4})[年/](\d{1,2})[月/](\d{1,2})", r2.text)
                    date_str = ""
                    if dm:
                        date_str = (
                            f"{dm.group(1)}-"
                            f"{int(dm.group(2)):02d}-"
                            f"{int(dm.group(3)):02d}"
                        )
                    return {"price": int(price_str), "date": date_str}
    except Exception as exc:
        print(f"  [2nd] {fund_code}: {exc}", file=sys.stderr)

    return None


def main() -> None:
    config_path = ROOT / "fund_codes.json"

    # fund_codes.json が存在しない場合はサンプルを生成して終了
    if not config_path.exists():
        sample = {
            "_comment": (
                "ファンドコードを登録してください。"
                "コードは https://www.toushin.or.jp/statistics/detail/ で確認できます。"
                "name はアプリ内の銘柄名と完全一致させてください（価格の自動反映に使用）。"
            ),
            "funds": [
                {
                    "name": "eMAXIS Slim 全世界株式（オール・カントリー）",
                    "code": "0331418A",
                    "_note": "← ファンドコード例。実際のコードに変更してください"
                },
                {
                    "name": "eMAXIS Slim 米国株式（S&P500）",
                    "code": "0331119A"
                }
            ]
        }
        config_path.write_text(
            json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("fund_codes.json を作成しました。銘柄名とファンドコードを設定してください。")
        sys.exit(0)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    fund_list = [f for f in config.get("funds", []) if not f.get("_note")]

    if not fund_list:
        print("fund_codes.json にファンドが登録されていません。", file=sys.stderr)
        sys.exit(0)

    print(f"取得対象: {len(fund_list)} 銘柄\n")

    results: dict[str, dict] = {}
    failed: list[str] = []
    now_jst = datetime.now(JST)

    for entry in fund_list:
        code = entry.get("code", "").strip()
        name = entry.get("name", "").strip()
        if not code or not name:
            continue

        print(f"  {name} ({code}) ... ", end="", flush=True)
        data = fetch_nav(code)

        if data:
            results[name] = {
                "price": data["price"],
                "date":  data["date"],
                "code":  code,
            }
            print(f"¥{data['price']:,}/万口  ({data['date']})")
        else:
            failed.append(name)
            print("取得失敗", file=sys.stderr)

        time.sleep(0.5)  # API への過剰リクエストを避ける

    # ── 既存の prices.json を読み込み、失敗した銘柄は前回値を保持 ──────────
    out_path = ROOT / "prices.json"
    existing: dict = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            existing = prev.get("prices", {})
        except Exception:
            pass

    for name in failed:
        if name in existing:
            results[name] = {**existing[name], "stale": True}
            print(f"  {name}: 前回値を維持 (¥{existing[name]['price']:,})")

    output = {
        "updated":  now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "prices":   results,
    }
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    success = len(results) - len([v for v in results.values() if v.get("stale")])
    print(f"\n完了: {success}/{len(fund_list)} 銘柄を取得 → prices.json に保存")

    if len(failed) == len(fund_list):
        # 全銘柄失敗はエラー終了してワークフローを失敗扱いにする
        sys.exit(1)


if __name__ == "__main__":
    main()
