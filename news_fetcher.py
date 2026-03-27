"""
ニュース取得モジュール（RSS方式）
==================================
Yahoo Finance RSS を使用して銘柄別ニュースを取得する。
- APIキー不要
- サーバー環境（GitHub Actions）から取得可能
- 無料・制限なし

日本株は Yahoo Finance Japan (.T サフィックス) で取得。
投資信託は市場全体ニュースにフォールバック。
"""

import feedparser
from datetime import datetime, timezone

# Yahoo Finance RSS (ティッカー別)
_YF_US  = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
_YF_JP  = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}.T&region=JP&lang=ja-JP"

# 日本株ティッカー → Yahoo Finance Japan 用に変換
_JP_TICKERS = {"8136", "7203", "6758", "9984", "4661", "8411", "8306", "6501"}

# 投資信託・ETFは市場全体ニュースを使用
_MARKET_RSS = {
    "VT":   _YF_US.format(ticker="VT"),
    "VOO":  _YF_US.format(ticker="VOO"),
    "default_fund": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
}

MAX_ARTICLES = 3


def fetch_stock_news(
    tickers: list[str],
    stories: dict | None = None,
) -> dict[str, list[dict]]:
    """
    指定ティッカーの最新ニュースを Yahoo Finance RSS から取得する。

    Args:
        tickers: 識別子のリスト (例: ["LLY", "ISRG", "8136"])
        stories: 未使用（後方互換のために保持）

    Returns:
        {ticker: [{"title", "description", "url", "published_at", "source"}, ...]}
    """
    results = {}
    for ticker in tickers:
        results[ticker] = _fetch_for_ticker(ticker)
    return results


def _fetch_for_ticker(ticker: str) -> list[dict]:
    """ティッカーに応じた RSS URL を選択して取得する。"""
    # 投資信託系は別扱い
    if ticker.startswith("eMAXIS") or ticker.startswith("SBI_V") or "投信" in ticker:
        url = _MARKET_RSS["default_fund"]
    elif ticker in _MARKET_RSS:
        url = _MARKET_RSS[ticker]
    elif ticker in _JP_TICKERS:
        url = _YF_JP.format(ticker=ticker)
    else:
        url = _YF_US.format(ticker=ticker)

    return _parse_rss(url, ticker)


def _parse_rss(url: str, ticker: str) -> list[dict]:
    """RSS フィードを解析して記事リストを返す。"""
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:MAX_ARTICLES]:
            published = _format_date(entry)
            articles.append({
                "title":        entry.get("title", ""),
                "description":  entry.get("summary", ""),
                "url":          entry.get("link", ""),
                "published_at": published,
                "source":       "Yahoo Finance",
            })
        if articles:
            print(f"  [NEWS] {ticker}: {len(articles)}件取得")
        else:
            print(f"  [NEWS] {ticker}: 記事なし（RSS空）")
        return articles

    except Exception as e:
        print(f"  [WARN] {ticker} RSS取得失敗: {e}")
        return []


def _format_date(entry) -> str:
    """feedparser の日付を ISO 文字列に変換する。"""
    try:
        t = entry.get("published_parsed")
        if t:
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return entry.get("published", "")
