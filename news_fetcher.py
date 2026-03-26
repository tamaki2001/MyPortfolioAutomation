"""
ニュース取得モジュール
====================
個別株の最新ニュースを取得する。
  1. NewsAPI（有効な場合）
  2. フォールバック: 静的コンテキスト（APIが利用不可の場合）

LLY は競合 NVO との比較ニュースを重視、
ISRG はシェア動向のニュースを重視する。
"""

import os
from datetime import datetime, timedelta

import requests

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
NEWS_API_URL = "https://newsapi.org/v2/everything"

# 銘柄ごとの検索クエリカスタマイズ
QUERY_MAP = {
    "LLY": '("Eli Lilly" OR "LLY") AND ("Novo Nordisk" OR "NVO" OR "obesity" OR "GLP-1" OR "weight loss")',
    "ISRG": '("Intuitive Surgical" OR "ISRG") AND ("market share" OR "da Vinci" OR "robotic surgery")',
    "NVO": '("Novo Nordisk" OR "NVO") AND ("Wegovy" OR "Ozempic" OR "obesity" OR "GLP-1")',
}

DEFAULT_QUERY_TEMPLATE = '"{ticker}"'
LOOKBACK_DAYS = 30
MAX_ARTICLES = 5


def fetch_stock_news(tickers: list[str]) -> dict[str, list[dict]]:
    """
    指定ティッカーの最新ニュースを取得する。

    Args:
        tickers: ティッカーシンボルのリスト (例: ["LLY", "ISRG", "NVO"])

    Returns:
        {
            "LLY": [
                {"title": str, "description": str, "url": str,
                 "published_at": str, "source": str},
                ...
            ],
            ...
        }
    """
    if not NEWS_API_KEY:
        print("[WARN] NEWS_API_KEY が未設定のためニュース取得をスキップします。")
        return {t: [] for t in tickers}

    from_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    results = {}

    for ticker in tickers:
        query = QUERY_MAP.get(ticker, DEFAULT_QUERY_TEMPLATE.format(ticker=ticker))
        articles = _fetch_articles(query, from_date)

        # NewsAPI が失敗した場合、シンプルなクエリでリトライ
        if not articles:
            simple_query = f'"{ticker}"'
            if simple_query != query:
                articles = _fetch_articles(simple_query, from_date)

        results[ticker] = articles

    return results


def _fetch_articles(query: str, from_date: str) -> list[dict]:
    """NewsAPI から記事を取得する。"""
    try:
        resp = requests.get(
            NEWS_API_URL,
            params={
                "q": query,
                "from": from_date,
                "sortBy": "relevancy",
                "language": "en",
                "pageSize": MAX_ARTICLES,
                "apiKey": NEWS_API_KEY,
            },
            timeout=15,
        )

        # 426 Upgrade Required = 無料プラン制限
        if resp.status_code == 426:
            print(f"[WARN] NewsAPI無料プラン制限（426）。ニュース取得をスキップ。")
            return []

        # 401 = APIキー無効
        if resp.status_code == 401:
            print(f"[WARN] NewsAPIキーが無効です（401）。")
            return []

        resp.raise_for_status()
        data = resp.json()

        articles = []
        for a in data.get("articles", []):
            articles.append({
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "url": a.get("url", ""),
                "published_at": a.get("publishedAt", ""),
                "source": a.get("source", {}).get("name", ""),
            })
        return articles

    except requests.RequestException as e:
        print(f"[WARN] ニュース取得失敗 (query={query[:40]}...): {e}")
        return []
