"""
Google Drive ハンドラ
====================
Google Drive API を使用して以下を行う:
  - history.csv のダウンロード / アップロード
  - レポート・スクリーンショットのアップロード
  - history.csv への行追記
  - history.csv の DataFrame 読込

認証: GOOGLE_DRIVE_CREDENTIALS 環境変数に
      サービスアカウント JSON キーの内容を設定する。
"""

import io
import json
import os
from pathlib import Path

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# Google Drive API スコープ
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google Drive 上のフォルダ ID（環境変数 or デフォルト）
# MyPortfolioAutomation フォルダの各サブフォルダ ID を設定
DRIVE_FOLDER_IDS = {
    "data": os.environ.get("DRIVE_FOLDER_DATA", ""),
    "screenshots": os.environ.get("DRIVE_FOLDER_SCREENSHOTS", ""),
    "reports": os.environ.get("DRIVE_FOLDER_REPORTS", ""),
}


class DriveHandler:
    """Google Drive とのファイルやり取りを管理するクラス。"""

    def __init__(self):
        self.service = self._authenticate()
        # アップロード済みファイルの ID キャッシュ {ファイル名: file_id}
        self._file_id_cache: dict[str, str] = {}

    # ================================================================
    # 認証
    # ================================================================
    def _authenticate(self):
        """サービスアカウント JSON で認証し Drive API サービスを返す。"""
        creds_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS")
        if not creds_json:
            raise EnvironmentError(
                "環境変数 GOOGLE_DRIVE_CREDENTIALS が設定されていません。"
            )

        creds_info = json.loads(creds_json)
        credentials = Credentials.from_service_account_info(
            creds_info, scopes=SCOPES
        )
        return build("drive", "v3", credentials=credentials)

    # ================================================================
    # フォルダ ID 解決
    # ================================================================
    def _resolve_folder_id(self, local_path: str) -> str | None:
        """ローカルパスから対応する Drive フォルダ ID を推定する。"""
        p = Path(local_path)
        # パスの親ディレクトリ名でマッピング
        parent_name = p.parent.name.lower()
        if parent_name in DRIVE_FOLDER_IDS:
            folder_id = DRIVE_FOLDER_IDS[parent_name]
            return folder_id if folder_id else None

        # data フォルダ直下の場合
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
            .list(q=query, fields="files(id, name)", pageSize=5)
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

        filename = p.name
        folder_id = self._resolve_folder_id(local_path)
        mime_type = self._guess_mime_type(filename)

        media = MediaFileUpload(str(p), mimetype=mime_type, resumable=True)

        # 既存ファイルを検索
        existing_id = self._file_id_cache.get(filename) or self._find_file(
            filename, folder_id
        )

        if existing_id:
            # 更新
            file = (
                self.service.files()
                .update(fileId=existing_id, media_body=media)
                .execute()
            )
        else:
            # 新規作成
            file_metadata = {"name": filename}
            if folder_id:
                file_metadata["parents"] = [folder_id]
            file = (
                self.service.files()
                .create(body=file_metadata, media_body=media, fields="id")
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
            "total_value": float,           # 総資産額
            "cash_jpy": float,              # 日本円現金
            "cash_usd": float,              # 米ドル現金（VT除外分を含む）
            "stock_value": float,           # 株式時価（VT除外後）
            "holdings": [
                {"ticker": str, "value": float, "quantity": float},
                ...
            ],
        }
        """
        p = Path(csv_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        # 既存データ読み込み
        if p.exists():
            df = pd.read_csv(str(p))
        else:
            df = pd.DataFrame()

        # 新規行を構築
        new_row = {
            "date": date_str,
            "total_value": portfolio_data["total_value"],
            "cash_jpy": portfolio_data["cash_jpy"],
            "cash_usd": portfolio_data["cash_usd"],
            "stock_value": portfolio_data["stock_value"],
        }

        # 各銘柄の時価も列として追加
        for h in portfolio_data.get("holdings", []):
            col_name = f"holding_{h['ticker']}"
            new_row[col_name] = h["value"]

        new_df = pd.DataFrame([new_row])
        df = pd.concat([df, new_df], ignore_index=True)
        df.to_csv(str(p), index=False)

    # ================================================================
    # ヘルパー
    # ================================================================
    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        """ファイル名から MIME タイプを推定する。"""
        ext = Path(filename).suffix.lower()
        mime_map = {
            ".csv": "text/csv",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".json": "application/json",
            ".pdf": "application/pdf",
        }
        return mime_map.get(ext, "application/octet-stream")
