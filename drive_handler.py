"""
Google Drive ハンドラ
====================
Google Drive API を使用して以下を行う:
  - history.csv のダウンロード / アップロード
  - レポート・スクリーンショットのアップロード
  - history.csv への行追記
  - history.csv の DataFrame 読込

認証: OAuth2 リフレッシュトークン方式（無料・ユーザーの Drive クォータを使用）
  環境変数:
    GOOGLE_OAUTH_CLIENT_ID       ... OAuth2 クライアント ID
    GOOGLE_OAUTH_CLIENT_SECRET   ... OAuth2 クライアント シークレット
    GOOGLE_OAUTH_REFRESH_TOKEN   ... リフレッシュトークン（初回セットアップで取得）

初回セットアップ:
  setup/get_oauth_token.py を参照
"""

import io
import os
from pathlib import Path

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Google Drive API スコープ
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google Drive 上のフォルダ ID（環境変数 or デフォルト）
DRIVE_FOLDER_IDS = {
    "data":        os.environ.get("DRIVE_FOLDER_DATA", ""),
    "screenshots": os.environ.get("DRIVE_FOLDER_SCREENSHOTS", ""),
    "reports":     os.environ.get("DRIVE_FOLDER_REPORTS", ""),
}


class DriveHandler:
    """Google Drive とのファイルやり取りを管理するクラス。"""

    def __init__(self):
        self.service = self._authenticate()
        # アップロード済みファイルの ID キャッシュ {ファイル名: file_id}
        self._file_id_cache: dict[str, str] = {}

    # ================================================================
    # 認証（OAuth2 リフレッシュトークン）
    # ================================================================
    def _authenticate(self):
        """OAuth2 リフレッシュトークンで認証し Drive API サービスを返す。

        サービスアカウントとは異なり、ユーザー自身のクォータを使用するため
        通常の My Drive フォルダへの書き込みが可能。
        """
        client_id     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

        if not all([client_id, client_secret, refresh_token]):
            missing = [
                name for name, val in [
                    ("GOOGLE_OAUTH_CLIENT_ID",     client_id),
                    ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
                    ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
                ] if not val
            ]
            raise EnvironmentError(
                f"以下の環境変数が未設定です: {', '.join(missing)}\n"
                "setup/get_oauth_token.py を実行して各値を取得してください。"
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        # リフレッシュトークンでアクセストークンを取得
        creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

    # ================================================================
    # フォルダ ID 解決
    # ================================================================
    def _resolve_folder_id(self, local_path: str) -> str | None:
        """ローカルパスから対応する Drive フォルダ ID を推定する。"""
        p = Path(local_path)
        parent_name = p.parent.name.lower()
        if parent_name in DRIVE_FOLDER_IDS:
            folder_id = DRIVE_FOLDER_IDS[parent_name]
            return folder_id if folder_id else None

        if "data" in str(p).lower():
            folder_id = DRIVE_FOLDER_IDS.get("data")
            return folder_id if folder_id else None

        return None

    # ================================================================
    # ファイル検索
    # ================================================================
    def _find_file(self, filename: str, folder_id: str | None = None) -> str | None:
        """Drive 上でファイル名を検索し、最初に見つかった file_id を返す。"""
        query = f"name = '{filename}' and trashed = false"
        if folder_id:
            query += f" and '{folder_id}' in parents"

        results = (
            self.service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=5,
            )
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None

    # ================================================================
    # アップロード
    # ================================================================
    def upload_file(self, local_path: str) -> str:
        """
        ローカルファイルを Google Drive にアップロードする。
        既存ファイルがあれば更新、なければ新規作成。
        戻り値: Drive 上の file_id
        """
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(f"ファイルが見つかりません: {local_path}")

        filename  = p.name
        folder_id = self._resolve_folder_id(local_path)
        mime_type = self._guess_mime_type(filename)
        media     = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)

        # 既存ファイルを検索
        existing_id = self._file_id_cache.get(filename) or self._find_file(
            filename, folder_id
        )

        if existing_id:
            # 既存ファイルを上書き更新
            file = (
                self.service.files()
                .update(
                    fileId=existing_id,
                    media_body=media,
                )
                .execute()
            )
        else:
            # 新規作成
            if not folder_id:
                print(f"  [WARN] フォルダIDが未設定のため Drive アップロードをスキップ: {filename}")
                return ""
            file_metadata = {"name": filename, "parents": [folder_id]}
            file = (
                self.service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                )
                .execute()
            )

        file_id = file.get("id", existing_id)
        self._file_id_cache[filename] = file_id
        return file_id

    # ================================================================
    # ダウンロード
    # ================================================================
    def download_file(self, file_id: str, local_path: str) -> None:
        """Drive 上のファイルをローカルにダウンロードする。"""
        request = self.service.files().get_media(fileId=file_id)
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        with open(p, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    # ================================================================
    # history.csv 操作
    # ================================================================
    def load_history(self, csv_path: str) -> pd.DataFrame:
        """
        history.csv を読み込んで DataFrame として返す。
        ファイルが存在しない場合は空の DataFrame を返す。
        """
        p = Path(csv_path)
        if not p.exists():
            return pd.DataFrame()
        return pd.read_csv(str(p), parse_dates=["date"])

    def append_history(
        self,
        csv_path: str,
        date_str: str,
        portfolio_data: dict,
    ) -> None:
        """
        history.csv に最新月の1行を追加する。

        portfolio_data の期待構造:
        {
            "total_value": float,
            "cash_jpy": float,
            "cash_usd": float,
            "stock_value": float,
            "holdings": [
                {"ticker": str, "value": float, "quantity": float},
                ...
            ],
        }
        """
        p = Path(csv_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if p.exists():
            df = pd.read_csv(str(p))
        else:
            df = pd.DataFrame()

        new_row = {
            "date":        date_str,
            "total_value": portfolio_data["total_value"],
            "cash_jpy":    portfolio_data["cash_jpy"],
            "cash_usd":    portfolio_data["cash_usd"],
            "stock_value": portfolio_data["stock_value"],
            "fund_value":  portfolio_data.get("fund_value", 0),
        }

        for h in portfolio_data.get("holdings", []):
            col_name = f"holding_{h['ticker']}"
            new_row[col_name] = h["value"]

        for f in portfolio_data.get("funds", []):
            # 投信名は長いので短縮キーを使用
            fund_key = f["name"][:30].replace(",", "")
            col_name = f"holding_{fund_key}"
            new_row[col_name] = new_row.get(col_name, 0) + f["value"]

        new_df = pd.DataFrame([new_row])
        df = pd.concat([df, new_df], ignore_index=True)

        # 同一日付の重複は最新1件のみ残す（同日に複数回実行した場合の上書き）
        df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
        df = df.drop_duplicates(subset=["date"], keep="last")
        df = df.sort_values("date").reset_index(drop=True)

        df.to_csv(str(p), index=False)

    # ================================================================
    # ヘルパー
    # ================================================================
    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        """ファイル名から MIME タイプを推定する。"""
        ext = Path(filename).suffix.lower()
        mime_map = {
            ".csv":  "text/csv",
            ".md":   "text/markdown",
            ".txt":  "text/plain",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".json": "application/json",
            ".pdf":  "application/pdf",
        }
        return mime_map.get(ext, "application/octet-stream")
