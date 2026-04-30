"""
Gmail から過去レポートを Supabase に一括取り込み
=================================================
「月次ポートフォリオレポート」の件名で送られたメールの
text/plain パート（= Markdown 本文）を取り出して fo_reports に投入する。

実行方法:
  cd ~/Projects/MyPortfolioAutomation
  export GOOGLE_OAUTH_CLIENT_ID=xxx
  export GOOGLE_OAUTH_CLIENT_SECRET=yyy
  export GOOGLE_OAUTH_REFRESH_TOKEN=zzz
  export SUPABASE_URL=xxx
  export SUPABASE_SERVICE_KEY=yyy

  # ドライランで件数確認
  python scripts/import_reports_from_gmail.py --dry-run

  # 実際に取り込む
  python scripts/import_reports_from_gmail.py
"""

import os, sys, re, base64, argparse, email
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from supabase_handler import upload_report, _get_client

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_part(part: dict) -> str | None:
    data = part.get("body", {}).get("data", "")
    if not data:
        return None
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_plain(payload: dict) -> str | None:
    """メールペイロードから text/plain パートを再帰的に探す"""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts", []):
        result = _extract_plain(part)
        if result:
            return result
    return None


def _parse_date_from_subject(subject: str) -> date | None:
    """
    件名例: 月次ポートフォリオレポート (2026年04月)
           月次ポートフォリオレポート (2026年4月)
    """
    m = re.search(r"(\d{4})年(\d{1,2})月", subject)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return None


def _parse_date_from_markdown(text: str) -> date | None:
    """本文1行目の年月を拾う: # 月次ポートフォリオレポート — 2026年4月"""
    m = re.search(r"(\d{4})年(\d{1,2})月", text[:200])
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return None


def already_exists(date_str: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        res = client.table("fo_reports").select("id").eq("date", date_str).execute()
        return len(res.data) > 0
    except Exception:
        return False


def run(dry_run: bool):
    service = _gmail_service()

    # 件名でメールを検索
    query = 'subject:"月次ポートフォリオ"'
    result = service.users().messages().list(
        userId="me", q=query, maxResults=100
    ).execute()
    messages = result.get("messages", [])
    print(f"{len(messages)} 件のメールが見つかりました\n")

    imported = 0
    skipped = 0
    failed = 0

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        date_header = headers.get("Date", "")

        # Markdown 本文を取り出す
        plain = _extract_plain(msg["payload"])
        if not plain or len(plain.strip()) < 100:
            print(f"  スキップ（本文なし）: {subject}")
            failed += 1
            continue

        # 日付を推定
        report_date = _parse_date_from_subject(subject) or _parse_date_from_markdown(plain)
        if report_date is None:
            print(f"  スキップ（日付不明）: {subject}")
            failed += 1
            continue

        date_str = report_date.strftime("%Y-%m-%d")

        if already_exists(date_str):
            print(f"  スキップ（既存）: {date_str}  {subject[:40]}")
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] 取り込み予定: {date_str}  {subject[:40]}  ({len(plain)}文字)")
            imported += 1
            continue

        ok = upload_report(date_str, plain.strip())
        if ok:
            print(f"  ✓ 取り込み: {date_str}  {subject[:40]}")
            imported += 1
        else:
            print(f"  ✗ 失敗: {date_str}  {subject[:40]}")
            failed += 1

    print(f"\n完了: 取り込み {imported} 件 / スキップ {skipped} 件 / 失敗 {failed} 件{'（DRY-RUN）' if dry_run else ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for var in ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                "GOOGLE_OAUTH_REFRESH_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]:
        if not os.environ.get(var):
            print(f"ERROR: 環境変数 {var} が未設定です")
            sys.exit(1)

    run(args.dry_run)
