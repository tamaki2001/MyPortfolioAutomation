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
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

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
    月次レポートをメール送信する。

    Args:
        to: 送信先メールアドレス
        subject: 件名
        body_md: レポート本文（Markdown）
        attachments: 添付ファイルパスのリスト
    """
    _send_email(
        to=to,
        subject=subject,
        body=body_md,
        content_type="plain",
        attachments=attachments or [],
    )


def send_alert_email(
    to: str,
    subject: str,
    body: str,
) -> None:
    """
    緊急アラートメールを送信する。

    Args:
        to: 送信先メールアドレス
        subject: 件名
        body: エラー詳細テキスト
    """
    _send_email(
        to=to,
        subject=subject,
        body=body,
        content_type="plain",
        attachments=[],
    )


def _send_email(
    to: str,
    subject: str,
    body: str,
    content_type: str,
    attachments: list[str],
) -> None:
    """Gmail SMTP でメールを送信する。"""
    if not GMAIL_USER or not GMAIL_PASS:
        print("[WARN] GMAIL_USER/GMAIL_PASS が未設定のためメール送信をスキップします。")
        return

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject

    # 本文
    msg.attach(MIMEText(body, content_type, "utf-8"))

    # 添付ファイル
    for filepath in attachments:
        p = Path(filepath)
        if not p.exists():
            print(f"[WARN] 添付ファイルが見つかりません: {filepath}")
            continue

        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{p.name}"',
        )
        msg.attach(part)

    # 送信
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)

    print(f"[INFO] メール送信完了: {to} ({subject})")
