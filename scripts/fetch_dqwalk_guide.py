#!/usr/bin/env python3
"""
ドラクエウォーク攻略情報取得スクリプト

データソース（順に試行、成功したもの全てを集約）:
  1. Altema       https://altema.jp/dqwalk
  2. Game8        https://game8.jp/dqwalk
  3. AppMedia     https://appmedia.jp/dragonquest_walk
  4. KamiGame     https://kamigame.jp/dqwalk/

取得情報:
  - 開催中イベント
  - ガチャ・スカウト情報
  - ボス・強敵情報
  - 装備・こころ情報
  - 最新ニュース・更新情報

トラブルシュート:
  python scripts/fetch_dqwalk_guide.py --debug  # HTML を dump して確認
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
})

# カテゴリ判定キーワード
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("event",     ["イベント", "キャンペーン", "コラボ", "期間限定", "開催"]),
    ("gacha",     ["ガチャ", "スカウト", "召喚", "排出", "ピックアップ", "限定"]),
    ("boss",      ["ボス", "魔王", "強敵", "討伐", "メガモンスター", "れんごく", "ギガモンスター"]),
    ("equipment", ["装備", "武器", "防具", "こころ", "錬金", "宝珠", "アクセサリ"]),
    ("quest",     ["クエスト", "メインストーリー", "サブクエスト", "バトル", "章"]),
    ("news",      ["更新", "アップデート", "お知らせ", "修正", "追加", "変更"]),
]

_NAV_WORDS = {
    "ホーム", "トップ", "メニュー", "ログイン", "新規登録",
    "お問い合わせ", "利用規約", "プライバシー", "広告", "採用", "運営会社",
    "サイトマップ", "ランキング一覧", "攻略一覧", "まとめ記事",
}


def _fetch_html(url: str, retries: int = 3) -> tuple[int, str]:
    """URL をフェッチして (status_code, html) を返す。失敗時はリトライ。"""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=20, allow_redirects=True)
            content_type = r.headers.get("Content-Type", "").lower()
            if "charset=" in content_type:
                return r.status_code, r.text
            for enc in ("utf-8", "euc-jp", "cp932", "shift-jis"):
                try:
                    return r.status_code, r.content.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return r.status_code, r.content.decode("utf-8", errors="replace")
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"      リトライ {attempt}/{retries - 1} ({wait}s後)…", file=sys.stderr)
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _categorize(title: str) -> str:
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in title for kw in keywords):
            return category
    return "general"


def _extract_articles(soup: BeautifulSoup, base_url: str, limit: int = 60) -> list[dict]:
    """BeautifulSoup から記事リンクを抽出する。複数のセレクターを試す。"""
    seen_titles: set[str] = set()
    articles: list[dict] = []

    candidate_selectors = [
        "article a[href]",
        ".article a[href]",
        ".news-list a[href]",
        ".topics a[href]",
        ".newsList a[href]",
        ".p-article-list a[href]",
        ".c-card a[href]",
        "h2 a[href]", "h3 a[href]", "h4 a[href]",
        "ul li a[href]",
        "a[href]",
    ]

    for selector in candidate_selectors:
        candidates = soup.select(selector)
        if not candidates:
            continue

        batch: list[dict] = []
        for link in candidates:
            title = link.get_text(separator=" ", strip=True)
            href = link.get("href", "")

            if not title or len(title) < 6:
                continue
            if title in seen_titles:
                continue
            if any(w in title for w in _NAV_WORDS):
                continue
            if href.startswith("#") or not href:
                continue
            if href.startswith("/"):
                href = urljoin(base_url, href)
            elif not href.startswith("http"):
                continue

            seen_titles.add(title)
            batch.append({
                "title": title,
                "url": href,
                "category": _categorize(title),
            })

            if len(articles) + len(batch) >= limit:
                break

        articles.extend(batch)
        if len(articles) >= limit:
            break

    return articles[:limit]


def _scrape_site(name: str, base_url: str, debug: bool = False) -> dict | None:
    """指定サイトの DQ Walk 攻略トップページをスクレイピングして情報を返す。"""
    print(f"  [{name}] GET {base_url}")
    try:
        status, html = _fetch_html(base_url)

        if debug:
            dump_path = ROOT / f"debug_dqwalk_{name.lower().replace(' ', '_')}.html"
            dump_path.write_text(html, encoding="utf-8")
            print(f"  [{name}] HTTP {status}  → {dump_path.name} に保存")

        if status != 200:
            print(f"  [{name}] HTTP {status} — スキップ", file=sys.stderr)
            return None

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True) if title_tag else ""

        articles = _extract_articles(soup, base_url)

        by_category: dict[str, list[dict]] = {
            "event": [], "gacha": [], "boss": [],
            "equipment": [], "quest": [], "news": [], "general": [],
        }
        for article in articles:
            by_category[article["category"]].append(article)

        print(
            f"  [{name}] {len(articles)} 件取得  "
            f"(イベント:{len(by_category['event'])}  "
            f"ガチャ:{len(by_category['gacha'])}  "
            f"ボス:{len(by_category['boss'])}  "
            f"装備:{len(by_category['equipment'])})"
        )

        return {
            "source": name,
            "url": base_url,
            "page_title": page_title,
            "total_articles": len(articles),
            **by_category,
        }

    except Exception as e:
        print(f"  [{name}] エラー: {e}", file=sys.stderr)
        if debug:
            import traceback
            traceback.print_exc()
        return None


def generate_summary(sources: list[dict], updated_label: str) -> str:
    """取得した攻略情報をマークダウン形式でまとめる。"""
    lines = [
        "# ドラクエウォーク 攻略情報まとめ",
        "",
        f"更新日時: {updated_label}",
        "",
    ]

    def collect(key: str) -> list[dict]:
        seen: set[str] = set()
        result: list[dict] = []
        for src in sources:
            for item in src.get(key, []):
                if item["title"] not in seen:
                    seen.add(item["title"])
                    result.append({**item, "source": src["source"]})
        return result

    sections = [
        ("開催中イベント",         collect("event")),
        ("ガチャ・スカウト情報",   collect("gacha")),
        ("ボス・強敵情報",         collect("boss")),
        ("装備・こころ情報",       collect("equipment")),
        ("クエスト情報",           collect("quest")),
        ("最新ニュース・更新情報", collect("news")),
    ]

    has_content = False
    for heading, items in sections:
        if not items:
            continue
        has_content = True
        lines.append(f"## {heading}")
        for item in items[:10]:
            lines.append(f"- [{item['title']}]({item['url']})  （{item['source']}）")
        lines.append("")

    if not has_content:
        lines.append("*記事情報を取得できませんでした。*")
        lines.append("")

    lines.append("---")
    lines.append(f"情報ソース: {', '.join(s['source'] for s in sources)}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="ドラクエウォーク攻略情報取得")
    parser.add_argument("--debug", action="store_true", help="詳細ログ出力・HTML dump")
    args = parser.parse_args()

    print("ドラクエウォーク攻略情報を取得中...\n")

    sites = [
        ("Altema",   "https://altema.jp/dqwalk"),
        ("Game8",    "https://game8.jp/dqwalk"),
        ("AppMedia", "https://appmedia.jp/dragonquest_walk"),
        ("KamiGame", "https://kamigame.jp/dqwalk/"),
    ]

    sources: list[dict] = []
    for i, (name, url) in enumerate(sites):
        result = _scrape_site(name, url, debug=args.debug)
        if result:
            sources.append(result)
        if i < len(sites) - 1:
            time.sleep(2)

    now_jst = datetime.now(JST)
    updated_str = now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    updated_label = now_jst.strftime("%Y年%m月%d日 %H:%M JST")

    out_path = ROOT / "dqwalk_guide.json"

    # 前回データを読み込む（取得ゼロ件時に stale として保持）
    prev_data: dict = {}
    if out_path.exists():
        try:
            prev_data = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not sources:
        print("\n全ソースからの取得に失敗しました。", file=sys.stderr)
        if prev_data.get("sources"):
            print("  → 前回データ（stale）を維持します。", file=sys.stderr)
            prev_data["stale"] = True
            prev_data["last_attempt"] = updated_str
            out_path.write_text(
                json.dumps(prev_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            print("  前回データもないため dqwalk_guide.json を更新しません。", file=sys.stderr)
        print("\nトラブルシュート:", file=sys.stderr)
        print("  python scripts/fetch_dqwalk_guide.py --debug", file=sys.stderr)
        sys.exit(1)

    summary = generate_summary(sources, updated_label)

    output = {
        "updated": updated_str,
        "stale": False,
        "sources": sources,
        "summary": summary,
    }
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    total = sum(s["total_articles"] for s in sources)
    print(f"\n取得完了: {len(sources)} ソース  合計 {total} 件 → dqwalk_guide.json を更新")
    print(f"\n{'=' * 60}")
    print(summary)


if __name__ == "__main__":
    main()
