"""
メール送信モジュール
===================
Gmail SMTP を使用してレポート配信・緊急アラートを送信する。
GMAIL_USER / GMAIL_PASS（アプリパスワード）を環境変数で設定。
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

try:
    import markdown
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_PASS", "")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def send_report_email(
    to: str,
    subject: str,
    body_md: str,
    attachments: list[str] | None = None,
) -> None:
    """
    月次レポートをメール送信する。MarkdownをHTMLに変換し、画像をインライン表示する。
    """
    _send_html_email(
        to=to,
        subject=subject,
        body_md=body_md,
        attachments=attachments or [],
    )


def send_alert_email(
    to: str,
    subject: str,
    body: str,
) -> None:
    """緊急アラートメールを送信する。"""
    _send_plain_email(
        to=to,
        subject=subject,
        body=body,
    )


def _send_plain_email(to: str, subject: str, body: str) -> None:
    if not GMAIL_USER or not GMAIL_PASS:
        print("[WARN] GMAIL_USER/GMAIL_PASS 未設定のためメール送信スキップ")
        return

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
    print(f"[INFO] アラートメール送信完了: {to}")


def _send_html_email(
    to: str,
    subject: str,
    body_md: str,
    attachments: list[str],
) -> None:
    """HTML形式でデザインされたメールを送信する（画像インライン埋め込み）。"""
    if not GMAIL_USER or not GMAIL_PASS:
        print("[WARN] GMAIL_USER/GMAIL_PASS 未設定のためメール送信スキップ")
        return

    # ルートのマルチパート (related = 埋め込みリソース用)
    msg = MIMEMultipart("related")
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject

    # 代替パート (テキスト / HTML)
    msg_alt = MIMEMultipart("alternative")
    msg.attach(msg_alt)

    # 1. プレーンテキスト（フォールバック用）
    msg_alt.attach(MIMEText(body_md, "plain", "utf-8"))

    # 2. HTMLの生成
    if HAS_MARKDOWN:
        # Markdown から HTML への変換（テーブル記法を有効化）
        html_content = markdown.markdown(body_md, extensions=['tables', 'fenced_code'])
    else:
        # Markdownライブラリがない場合は単純な改行置換
        html_content = f"<pre>{body_md}</pre>"

    # 画像埋め込み用タグの生成
    images_html = ""
    for filepath in attachments:
        p = Path(filepath)
        if p.exists() and p.suffix.lower() in [".png", ".jpg", ".jpeg"]:
            images_html += f'<h3>{p.name}</h3><img src="cid:{p.name}"><br><br>'

    # CSS スタイリング
    html_template = f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
    <meta charset="UTF-8">
    <style>
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        color: #333333;
        line-height: 1.6;
        background-color: #f5f7fa;
        margin: 0;
        padding: 20px;
      }}
      .container {{
        max-width: 800px;
        margin: 0 auto;
        background-color: #ffffff;
        border-radius: 8px;
        overflow: hidden;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
      }}
      .header {{
        background-color: #2c3e50;
        color: #ffffff;
        padding: 20px 30px;
        text-align: center;
      }}
      .header h1 {{
        margin: 0;
        font-size: 24px;
        letter-spacing: 1px;
      }}
      .content {{
        padding: 30px;
      }}
      h1, h2, h3 {{
        color: #2c3e50;
        border-bottom: 2px solid #ecf0f1;
        padding-bottom: 8px;
        margin-top: 30px;
      }}
      h1:first-child, h2:first-child {{
        margin-top: 0;
      }}
      blockquote {{
        border-left: 4px solid #3498db;
        margin: 20px 0;
        padding: 15px 20px;
        background-color: #ebf5fb;
        color: #2c3e50;
        font-style: italic;
        border-radius: 0 4px 4px 0;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        margin: 20px 0;
      }}
      table, th, td {{
        border: 1px solid #bdc3c7;
      }}
      th {{
        background-color: #f8f9fa;
        color: #2c3e50;
        padding: 12px;
        text-align: left;
      }}
      td {{
        padding: 12px;
      }}
      a {{
        color: #3498db;
        text-decoration: none;
      }}
      .images {{
        margin-top: 40px;
        text-align: center;
      }}
      .images img {{
        max-width: 100%;
        height: auto;
        border: 1px solid #ecf0f1;
        border-radius: 4px;
        margin-bottom: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
      }}
      .images h3 {{
        border: none;
        color: #7f8c8d;
        font-size: 16px;
        margin-bottom: 10px;
      }}
      .footer {{
        background-color: #ecf0f1;
        color: #7f8c8d;
        text-align: center;
        padding: 15px;
        font-size: 12px;
      }}
    </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1>ポートフォリオ分析レポート</h1>
        </div>
        <div class="content">
          {html_content}
          <div class="images">
            {images_html}
          </div>
        </div>
        <div class="footer">
          Automated Portfolio Analysis System
        </div>
      </div>
    </body>
    </html>
    """
    
    msg_alt.attach(MIMEText(html_template, "html", "utf-8"))

    # 3. 画像の添付と CID 埋め込み
    for filepath in attachments:
        p = Path(filepath)
        if not p.exists():
            continue
            
        with open(p, "rb") as f:
            if p.suffix.lower() in [".png", ".jpg", ".jpeg"]:
                part = MIMEImage(f.read(), name=p.name)
                # HTMLから参照するための Content-ID をセット
                part.add_header('Content-ID', f'<{p.name}>')
                part.add_header('Content-Disposition', 'inline', filename=p.name)
            else:
                # 画像以外の添付ファイル（現状はない想定）
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{p.name}"')
                
        msg.attach(part)

    # 送信
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)

    print(f"[INFO] HTMLメール(画像付き)送信完了: {to} ({subject})")

