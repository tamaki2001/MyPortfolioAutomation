"""
Google OAuth2 リフレッシュトークン取得スクリプト
================================================
【初回のみ】ローカルで一度だけ実行するスクリプトです。
GitHub Actions での Drive アップロードに必要な3つの値を取得します。

■ 事前準備（Google Cloud Console）
  1. https://console.cloud.google.com を開く
  2. プロジェクト「portfolio-automation-491412」を選択
  3. 「APIとサービス」→「認証情報」
  4. 「認証情報を作成」→「OAuth 2.0 クライアント ID」
  5. アプリケーションの種類：「デスクトップアプリ」
  6. 名前：任意（例: "portfolio-bot-desktop"）→「作成」
  7. ダウンロードされた JSON から client_id と client_secret を控える

■ 実行方法
  pip install google-auth-oauthlib
  python setup/get_oauth_token.py

■ 実行後
  表示された3つの値を GitHub Secrets に設定：
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GOOGLE_OAUTH_REFRESH_TOKEN
"""

import sys

# ============================================================
# ここを編集してください
# ============================================================
CLIENT_ID     = "YOUR_CLIENT_ID"      # ← Google Cloud Console から取得
CLIENT_SECRET = "YOUR_CLIENT_SECRET"  # ← Google Cloud Console から取得
# ============================================================

if CLIENT_ID == "YOUR_CLIENT_ID" or CLIENT_SECRET == "YOUR_CLIENT_SECRET":
    print("ERROR: CLIENT_ID と CLIENT_SECRET を設定してから実行してください。")
    print("  このファイルの先頭近くにある CLIENT_ID / CLIENT_SECRET を編集してください。")
    sys.exit(1)

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("ERROR: google-auth-oauthlib が未インストールです。")
    print("  pip install google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/drive"]

client_config = {
    "installed": {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

print("ブラウザでGoogleアカウントの認証画面が開きます。")
print("ポートフォリオ自動化用のアカウント（tomoaki.asano@gmail.com）でログインしてください。\n")

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

if not creds.refresh_token:
    print("\nERROR: リフレッシュトークンが取得できませんでした。")
    print("  prompt='consent' で実行しても取得できない場合は、")
    print("  Google Cloud Console でこのクライアントIDのアクセスを一度取り消してから再試行してください。")
    sys.exit(1)

print("\n" + "=" * 65)
print("✅ 認証成功！以下の値を GitHub Secrets に設定してください")
print("=" * 65)
print(f"\n  Secret名: GOOGLE_OAUTH_CLIENT_ID")
print(f"  値:       {CLIENT_ID}")
print(f"\n  Secret名: GOOGLE_OAUTH_CLIENT_SECRET")
print(f"  値:       {CLIENT_SECRET}")
print(f"\n  Secret名: GOOGLE_OAUTH_REFRESH_TOKEN")
print(f"  値:       {creds.refresh_token}")
print("\n" + "=" * 65)
print("※ これらの値は秘密です。絶対にコードにハードコードしないでください。")
print("※ GOOGLE_DRIVE_CREDENTIALS（旧サービスアカウント）は不要になります。")
print("=" * 65)
