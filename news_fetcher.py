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

# 銘柄ごとの検索クエリカスタマイズ（英語ニュース向け）
QUERY_MAP = {
    # 米国個別株
    "LLY":  '("Eli Lilly" OR "LLY") AND ("Novo Nordisk" OR "NVO" OR "obesity" OR "GLP-1" OR "weight loss")',
    "ISRG": '("Intuitive Surgical" OR "ISRG") AND ("market share" OR "da Vinci" OR "robotic surgery")',
    "NVO":  '("Novo Nordisk" OR "NVO") AND ("Wegovy" OR "Ozempic" OR "obesity" OR "GLP-1")',
    "NVDA": '("NVIDIA" OR "NVDA") AND ("GPU" OR "AI" OR "data center" OR "semiconductor")',
    "MSFT": '("Microsoft" OR "MSFT") AND ("Azure" OR "AI" OR "cloud" OR "Copilot")',
    "COST": '("Costco" OR "COST") AND ("membership" OR "retail" OR "comparable sales")',
    "AVGO": '("Broadcom" OR "AVGO") AND ("semiconductor" OR "AI" OR "networking")',
    # 日本個別株（英語ニュース）
    "7203": '("Toyota" OR "Toyota Motor") AND ("EV" OR "hybrid" OR "production")',
    "4661": '("Oriental Land" OR "Tokyo Disney") AND ("attendance" OR "theme park" OR "revenue")',
    "8411": '("Mizuho" OR "Mizuho Financial") AND ("bank" OR "interest rate" OR "loan")',
    # 投資信託（インデックス・市場動向）
    "eMAXIS_Slim_全世界":   '("MSCI ACWI" OR "global stock" OR "world equity") AND ("index" OR "ETF")',
    "eMAXIS_Slim_SP500":    '("S&P 500" OR "SP500") AND ("index" OR "US stock" OR "equity")',
    "SBI_V_SP500":          '("S&P 500" OR "SP500") AND ("index" OR "US stock" OR "equity")',
    "eMAXIS_Slim_先進国":   '("MSCI World" OR "developed market") AND ("index" OR "equity")',
    "eMAXIS_Slim_新興国":   '("MSCI Emerging Markets" OR "emerging market") AND ("index" OR "equity")',
}

# 資産タイプ別デフォルトクエリテンプレート
DEFAULT_QUERY_BY_TYPE = {
    "us_stock":    '"{ticker}"',
    "jp_stock":    '"{company}"',
    "fund_index":  '"{company}" AND ("index fund" OR "ETF" OR "benchmark")',
    "fund_active": '"{company}" AND ("fund" OR "return" OR "performance")',
}
DEFAULT_QUERY_TEMPLATE = '"{ticker}"'

LOOKBACK_DAYS = 30
MAX_ARTICLES = 5


def fetch_stock_news(
    tickers: list[str],
    stories: dict | None = None,
) -> dict[str, list[dict]]:
    """
    指定ティッカーの最新ニュースを取得する。

    Args:
        tickers: 識別子のリスト (例: ["LLY", "ISRG", "eMAXIS_Slim_全世界"])
        stories: stock_stories.json の内容（asset_type・company 情報を利用）

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

    stories = stories or {}
    from_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    results = {}

    for ticker in tickers:
        story = stories.get(ticker, {})
        asset_type = story.get("asset_type", "us_stock")
        company = story.get("company", "")

        # QUERY_MAP に明示定義があればそれを優先
        if ticker in QUERY_MAP:
            query = QUERY_MAP[ticker]
        else:
            # asset_type 別のデフォルトテンプレートを使用
            tmpl = DEFAULT_QUERY_BY_TYPE.get(asset_type, DEFAULT_QUERY_TEMPLATE)
            query = tmpl.format(ticker=ticker, company=company) if company else f'"{ticker}"'

        articles = _fetch_articles(query, from_date)

        # 失敗時: company 名でシンプルリトライ
        if not articles and company:
            articles = _fetch_articles(f'"{company}"', from_date)

        # それでも失敗: ticker でリトライ
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
