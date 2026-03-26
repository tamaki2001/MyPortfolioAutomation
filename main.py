"""
月次ポートフォリオ分析・報告システム
====================================
FIRE生活者（浅野智明様）向けの自動資産分析・レポート配信スクリプト。
GitHub Actions上で毎月末に実行される。

実行フロー:
  1. 日付判定（月末でなければ正常終了）
  2. マネーフォワード ME からデータ収集（Playwright）
  3. Google Drive の history.csv に追記
  4. 運用方針・個別株仮説の読込＋ニュース取得
  5. 資産寿命シミュレーション＋グラフ生成
  6. Claude APIでレポート生成
  7. メール配信
  ※ 異常時は緊急メール通知
"""

import os
import sys
import calendar
import traceback
from datetime import date, datetime
from pathlib import Path

# --- モジュールインポート ---
from drive_handler import DriveHandler
from scraper import scrape_portfolio  # Cookie注入方式（MF_COOKIES環境変数 or cookies.json）
from news_fetcher import fetch_stock_news
from simulator import run_simulation, generate_charts
from report_generator import generate_report
from mailer import send_report_email, send_alert_email


# ============================================================
# 定数
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
ARCHIVES_DIR = BASE_DIR / "archives"
SCREENSHOTS_DIR = ARCHIVES_DIR / "screenshots"
REPORTS_DIR = ARCHIVES_DIR / "reports"

HISTORY_CSV = DATA_DIR / "history.csv"
FINANCIAL_POLICY = CONFIG_DIR / "financialPolicy.md"
STOCK_STORIES = CONFIG_DIR / "stock_stories.json"
REPORT_TEMPLATE = CONFIG_DIR / "report_template.txt"

# VT特殊ルール: VTのうち除外する株数（米ドル現金として再集計）
VT_EXCLUDE_SHARES = 478

# 資産寿命シミュレーション パラメータ
SIMULATION_PARAMS = {
    "tomoaki_age": 57,
    "tomoaki_lifespan": 87,
    "noriko_age": 51,
    "noriko_lifespan": 105,
    "private_pension_annual": 80_0000,   # 私的年金 80万/年
    "private_pension_years": 10,          # 私的年金 10年間
    "public_pension_annual": 240_0000,    # 公的年金 240万/年（終身）
    "spending_phases": [                  # 段階的支出モデル
        {"until_age": 75, "rate": 1.00},  # 100%
        {"until_age": 90, "rate": 0.75},  # 75%
        {"until_age": 105, "rate": 0.50}, # 50%
    ],
    "decline_threshold": 0.20,            # 前年同月比 20%減で警告
    "spending_cut_rate": 0.10,            # 警告時の支出削減率
}

RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "tomoaki.asano@gmail.com")


# ============================================================
# ユーティリティ
# ============================================================
def is_last_day_of_month(d: date) -> bool:
    """指定日がその月の最終日かどうかを判定する。"""
    _, last_day = calendar.monthrange(d.year, d.month)
    return d.day == last_day


def today_tag() -> str:
    """YYMMDD形式の日付タグを返す。"""
    return datetime.now().strftime("%y%m%d")


# ============================================================
# メインワークフロー
# ============================================================
def main():
    today = date.today()
    tag = today_tag()

    # ----------------------------------------------------------
    # Step 1: 日付判定（FORCE_RUN=true なら月末チェックをスキップ）
    # ----------------------------------------------------------
    force_run = os.environ.get("FORCE_RUN", "false").lower() == "true"
    if not force_run and not is_last_day_of_month(today):
        print(f"[INFO] {today} は月末ではないため正常終了します。")
        sys.exit(0)

    print(f"[INFO] 月次ポートフォリオ分析を開始します ({today})")

    # Google Drive ハンドラ初期化
    drive = DriveHandler()

    try:
        # ----------------------------------------------------------
        # Step 2: データ収集 (Playwright)
        # ----------------------------------------------------------
        print("[STEP 2] マネーフォワードからデータを収集中...")
        screenshot_path = SCREENSHOTS_DIR / f"{tag}_portfolio.png"
        portfolio_data = scrape_portfolio(
            screenshot_path=str(screenshot_path),
            vt_exclude_shares=VT_EXCLUDE_SHARES,
        )
        print(f"  -> スクリーンショット保存: {screenshot_path}")
        print(f"  -> 銘柄数: {len(portfolio_data['holdings'])}")

        # ----------------------------------------------------------
        # Step 3: history.csv に追記 → Google Drive アップロード
        # ----------------------------------------------------------
        print("[STEP 3] history.csv を更新中...")
        drive.append_history(
            csv_path=str(HISTORY_CSV),
            date_str=str(today),
            portfolio_data=portfolio_data,
        )
        drive.upload_file(str(HISTORY_CSV))
        print("  -> history.csv 更新・アップロード完了")

        # ----------------------------------------------------------
        # Step 4: 外部コンテキスト取得
        # ----------------------------------------------------------
        print("[STEP 4] 運用方針・ニュースを取得中...")
        financial_policy = FINANCIAL_POLICY.read_text(encoding="utf-8")
        stock_stories = STOCK_STORIES.read_text(encoding="utf-8")
        news = fetch_stock_news(["LLY", "ISRG", "NVO"])
        print(f"  -> ニュース記事数: {sum(len(v) for v in news.values())}")

        # ----------------------------------------------------------
        # Step 5: シミュレーション & グラフ生成
        # ----------------------------------------------------------
        print("[STEP 5] 資産寿命シミュレーション実行中...")
        history_df = drive.load_history(str(HISTORY_CSV))
        sim_result = run_simulation(
            portfolio_data=portfolio_data,
            history_df=history_df,
            params=SIMULATION_PARAMS,
        )

        chart_paths = generate_charts(
            sim_result=sim_result,
            history_df=history_df,
            output_dir=str(SCREENSHOTS_DIR),
            tag=tag,
        )
        print(f"  -> グラフ生成完了: {chart_paths}")

        # ----------------------------------------------------------
        # Step 6: レポート生成 (Claude API)
        # ----------------------------------------------------------
        print("[STEP 6] Claude APIでレポート生成中...")
        template = REPORT_TEMPLATE.read_text(encoding="utf-8")
        report_md = generate_report(
            template=template,
            portfolio_data=portfolio_data,
            sim_result=sim_result,
            financial_policy=financial_policy,
            stock_stories=stock_stories,
            news=news,
        )

        report_path = REPORTS_DIR / f"{tag}_report.md"
        report_path.write_text(report_md, encoding="utf-8")
        drive.upload_file(str(report_path))
        print(f"  -> レポート保存: {report_path}")

        # ----------------------------------------------------------
        # Step 7: メール配信
        # ----------------------------------------------------------
        print("[STEP 7] レポートをメール送信中...")
        attachments = [str(screenshot_path)] + chart_paths
        send_report_email(
            to=RECIPIENT_EMAIL,
            subject=f"月次ポートフォリオレポート ({today.strftime('%Y年%m月')})",
            body_md=report_md,
            attachments=attachments,
        )
        print("[完了] 月次レポートの配信が完了しました。")

    except Exception as e:
        # ----------------------------------------------------------
        # 異常検知: 緊急メール通知
        # ----------------------------------------------------------
        error_detail = traceback.format_exc()
        print(f"[ERROR] 実行中にエラーが発生しました:\n{error_detail}")
        try:
            send_alert_email(
                to=RECIPIENT_EMAIL,
                subject=f"[緊急] ポートフォリオ分析エラー ({today})",
                body=f"自動分析の実行中にエラーが発生しました。\n\n{error_detail}",
            )
        except Exception as mail_err:
            print(f"[CRITICAL] 緊急メール送信にも失敗: {mail_err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
