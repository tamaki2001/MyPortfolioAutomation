"""
Supabase ハンドラ
==================
GitHub Actions上でFolioのSupabaseインスタンスに書き込むクライアント。

環境変数:
  SUPABASE_URL          - https://xxxxx.supabase.co
  SUPABASE_SERVICE_KEY  - service_role キー（GitHub Secrets）

テーブル:
  fo_portfolio_snapshots  ポートフォリオ履歴
  fo_monthly_expenses     月次支出
  fo_stock_stories        投資仮説（iOSと共有）
  fo_reports              生成レポート
"""

import os
from typing import Optional, Any

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None  # type: ignore
    Client = None         # type: ignore


SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _get_client() -> Optional[Any]:
    if create_client is None:
        print("[WARN] supabase パッケージが未インストール。同期をスキップします。")
        return None
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[WARN] Supabase認証情報が未設定。同期をスキップします。")
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def upload_portfolio_snapshot(date_str: str, portfolio_data: dict) -> bool:
    """ポートフォリオ・スナップショットを fo_portfolio_snapshots に upsert"""
    client = _get_client()
    if not client:
        return False

    payload = {
        "date": date_str,
        "total_value": int(portfolio_data.get("total_value", 0)),
        "cash_jpy":    int(portfolio_data.get("cash_jpy", 0) or 0),
        "cash_usd":    int(portfolio_data.get("cash_usd", 0) or 0),
        "stock_value": int(portfolio_data.get("stock_value", 0) or 0),
        "fund_value":  int(portfolio_data.get("fund_value", 0) or 0),
        "holdings":    portfolio_data.get("holdings", []),
        "funds":       portfolio_data.get("funds", []),
    }

    try:
        client.table("fo_portfolio_snapshots").upsert(
            payload, on_conflict="date"
        ).execute()
        print(f"  [SUPABASE] ポートフォリオ同期完了: {date_str}")
        return True
    except Exception as e:
        print(f"  [WARN] Supabase ポートフォリオ同期失敗: {e}")
        return False


def upload_report(date_str: str, markdown: str, sim_params: dict | None = None) -> bool:
    """月次レポートを fo_reports に保存"""
    client = _get_client()
    if not client:
        return False

    payload = {
        "date": date_str,
        "markdown": markdown,
        "sim_params": sim_params or {},
    }

    try:
        client.table("fo_reports").insert(payload).execute()
        print(f"  [SUPABASE] レポート保存完了: {date_str}")
        return True
    except Exception as e:
        print(f"  [WARN] Supabase レポート保存失敗: {e}")
        return False


def upload_monthly_expense(year_month: str, total_amount: int, categories: list[dict],
                           raw_data: dict | None = None) -> bool:
    """月次支出を fo_monthly_expenses に upsert"""
    client = _get_client()
    if not client:
        return False

    payload = {
        "year_month": year_month,
        "total_amount": int(total_amount),
        "categories": categories,
        "raw_data": raw_data or {},
    }

    try:
        client.table("fo_monthly_expenses").upsert(
            payload, on_conflict="year_month"
        ).execute()
        print(f"  [SUPABASE] 支出データ同期完了: {year_month}")
        return True
    except Exception as e:
        print(f"  [WARN] Supabase 支出同期失敗: {e}")
        return False


def sync_stock_stories(stories: dict) -> bool:
    """stock_stories.json の内容を fo_stock_stories に upsert"""
    client = _get_client()
    if not client:
        return False

    rows = []
    for ticker, story in stories.items():
        rows.append({
            "ticker": ticker,
            "company":         story.get("company", ""),
            "asset_type":      story.get("asset_type", ""),
            "thesis":          story.get("thesis", ""),
            "key_metrics":     story.get("key_metrics", []),
            "exit_conditions": story.get("exit_conditions", []),
            "notes":           story.get("notes", ""),
            "added_date":      story.get("added_date"),
        })

    try:
        client.table("fo_stock_stories").upsert(rows, on_conflict="ticker").execute()
        print(f"  [SUPABASE] ストーリー同期完了: {len(rows)}件")
        return True
    except Exception as e:
        print(f"  [WARN] Supabase ストーリー同期失敗: {e}")
        return False


def fetch_stock_stories() -> dict:
    """fo_stock_stories から最新版を取得（iOSが編集した内容を反映）"""
    client = _get_client()
    if not client:
        return {}

    try:
        result = client.table("fo_stock_stories").select("*").execute()
        stories = {}
        for row in result.data:
            stories[row["ticker"]] = {
                "ticker":          row["ticker"],
                "company":         row.get("company", ""),
                "asset_type":      row.get("asset_type", ""),
                "thesis":          row.get("thesis", ""),
                "key_metrics":     row.get("key_metrics") or [],
                "exit_conditions": row.get("exit_conditions") or [],
                "notes":           row.get("notes", ""),
                "added_date":      row.get("added_date"),
            }
        print(f"  [SUPABASE] ストーリー取得完了: {len(stories)}件")
        return stories
    except Exception as e:
        print(f"  [WARN] Supabase ストーリー取得失敗: {e}")
        return {}
