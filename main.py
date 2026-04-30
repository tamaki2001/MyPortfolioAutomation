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
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# ============================================================
# タイムゾーン設定 (JST)
# ============================================================
JST = timezone(timedelta(hours=+9), 'JST')

# ============================================================
# 誕生日・年齢計算

# ============================================================
# 誕生日設定（ここだけ変更すれば全体に反映される）
TOMOAKI_BIRTHDATE = date(1968, 8, 25)
# 紀子さんの誕生日が判明したら date(yyyy, mm, dd) に変更してください
# 未設定の場合は実行日時点で 51 歳になる年の 1 月 1 日を仮定します
NORIKO_BIRTHDATE: date | None = date(1974, 4, 12)


def calc_age(birthdate: date, reference: date | None = None) -> int:
    """誕生日と基準日（デフォルト: 今日）から年齢を計算する。"""
    ref = reference or date.today()
    age = ref.year - birthdate.year
    if (ref.month, ref.day) < (birthdate.month, birthdate.day):
        age -= 1
    return age


def _noriko_age_today() -> int:
    """紀子さんの年齢を返す。誕生日未設定の場合は環境変数 NORIKO_AGE_OVERRIDE を参照する。"""
    if NORIKO_BIRTHDATE is not None:
        return calc_age(NORIKO_BIRTHDATE)
    override = os.environ.get("NORIKO_AGE_OVERRIDE")
    if override and override.isdigit():
        return int(override)
    # フォールバック: ファイル先頭コメントの固定値（要更新）
    FALLBACK_NORIKO_AGE = 51
    print(
        f"[WARN] NORIKO_BIRTHDATE 未設定。フォールバック値 {FALLBACK_NORIKO_AGE} を使用します。"
        " NORIKO_BIRTHDATE を設定するか、環境変数 NORIKO_AGE_OVERRIDE を指定してください。"
    )
    return FALLBACK_NORIKO_AGE

# --- モジュールインポート ---
from drive_handler import DriveHandler
from scraper import scrape_portfolio  # Cookie注入方式（MF_COOKIES環境変数 or cookies.json）
from news_fetcher import fetch_stock_news
from simulator import run_simulation, generate_charts
from report_generator import generate_report
from mailer import send_report_email, send_alert_email
from supabase_handler import (
    upload_portfolio_snapshot,
    upload_report as upload_report_to_supabase,
    sync_stock_stories,
    upload_monthly_expense,
)
from expense_scraper import scrape_monthly_expense


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
# ※ tomoaki_age / noriko_age は main() 内で実行時に動的セットされる（ここでは仮置き）
SIMULATION_PARAMS = {
    "tomoaki_age": calc_age(TOMOAKI_BIRTHDATE),  # 実行日時点の年齢を自動計算
    "tomoaki_lifespan": 87,
    "noriko_age": _noriko_age_today(),            # 誕生日設定後は calc_age(NORIKO_BIRTHDATE) に変わる
    "noriko_lifespan": 105,
    "private_pension_annual": 80_0000,   # 私的年金 80万/年
    "private_pension_years": 10,          # 私的年金 10年間
    "tomoaki_public_pension_annual": 282_3714, # 公的年金(智明) 約282万円/年（75歳～、ねんきんネット試算）
    "noriko_public_pension_annual": 240_0000,  # 公的年金(紀子) 240万円/年（推計、75歳～）
    "public_pension_start_age": 75,       # 公的年金の受給開始年齢
    # 段階的支出モデル（智明の年齢ベース）
    "spending_phases": [
        {"until_tomoaki_age": 75, "rate": 1.00},   # 智明57〜75歳: 100%
        {"until_tomoaki_age": 83, "rate": 0.75},   # 智明76〜83歳: 75%
        {"until_tomoaki_age": 87, "rate": 0.50},   # 智明84〜87歳: 50%
    ],
    "spending_rate_after_tomoaki": 0.50,  # 智明没後（紀子のみ）: 50%維持
    # リスク資産の運用利回り
    "return_rate_before_75": 0.05,        # 智明75歳まで: 年利5%
    "return_rate_after_75": 0.04,         # 智明76歳以降: 年利4%
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
    """YYMMDD_HHMMSS形式の日付タグを返す（毎回の実行結果を上書きせずに蓄積するため）。JSTを使用。"""
    return datetime.now(JST).strftime("%y%m%d_%H%M%S")


# ============================================================
# メインワークフロー
# ============================================================
def main():
    now_jst = datetime.now(JST)
    today = now_jst.date()
    tag = today_tag()

    # ----------------------------------------------------------
    # Step 1: 日付判定（FORCE_RUN=true なら月末チェックをスキップ）
    # ----------------------------------------------------------
    force_run = os.environ.get("FORCE_RUN", "false").lower() == "true"
    # RUN_TYPE: full / scrape_portfolio / scrape_expenses / report_only
    run_type = os.environ.get("RUN_TYPE", "full").lower()
    do_scrape_portfolio = run_type in ("full", "scrape_portfolio")
    do_scrape_expenses  = run_type in ("full", "scrape_expenses")
    do_report           = run_type in ("full", "report_only")

    if not force_run and not is_last_day_of_month(today):
        print(f"[INFO] {today} は月末ではないため正常終了します。")
        sys.exit(0)

    print(f"[INFO] 月次ポートフォリオ分析を開始します ({today}) [RUN_TYPE={run_type}]")

    # Google Drive ハンドラ初期化
    drive = DriveHandler()

    portfolio_data = None
    chart_paths = []
    screenshot_path = None

    try:
        # ----------------------------------------------------------
        # Step 2: ポートフォリオ取得 (Playwright)
        # ----------------------------------------------------------
        if do_scrape_portfolio:
            print("[STEP 2] マネーフォワードからポートフォリオを収集中...")
            screenshot_path = SCREENSHOTS_DIR / f"{tag}_portfolio.png"
            portfolio_data = scrape_portfolio(
                screenshot_path=str(screenshot_path),
                vt_exclude_shares=VT_EXCLUDE_SHARES,
            )
            print(f"  -> 銘柄数: {len(portfolio_data['holdings'])}")
            try:
                drive.upload_file(str(screenshot_path))
            except Exception as drive_err:
                print(f"  -> [WARN] Drive アップロードスキップ: {drive_err}")

            # Step 3: history.csv 更新
            print("[STEP 3] history.csv を更新中...")
            drive.append_history(
                csv_path=str(HISTORY_CSV),
                date_str=str(today),
                portfolio_data=portfolio_data,
            )
            try:
                drive.upload_file(str(HISTORY_CSV))
                print("  -> history.csv アップロード完了")
            except Exception as drive_err:
                print(f"  -> [WARN] Drive アップロードスキップ: {drive_err}")

            upload_portfolio_snapshot(str(today), portfolio_data)
        else:
            print("[STEP 2/3] スキップ（RUN_TYPE={run_type}）")

        # ----------------------------------------------------------
        # Step 3.5: 支出データ取得（前月分）
        # ----------------------------------------------------------
        if do_scrape_expenses:
            try:
                print("[STEP 3.5] 家計簿から前月の支出を取得中...")
                expense_screenshot = SCREENSHOTS_DIR / f"{tag}_expenses.png"
                expense_data = scrape_monthly_expense(
                    screenshot_path=str(expense_screenshot),
                )
                upload_monthly_expense(
                    year_month=expense_data["year_month"],
                    total_amount=expense_data["total_amount"],
                    categories=expense_data["categories"],
                    raw_data={"text_excerpt": expense_data.get("raw_text", "")[:1000]},
                )
                try:
                    drive.upload_file(str(expense_screenshot))
                except Exception as drive_err:
                    print(f"  -> [WARN] 支出スクリーンショット Drive アップロードスキップ: {drive_err}")
            except Exception as exp_err:
                print(f"  -> [WARN] 支出データ取得スキップ: {exp_err}")
        else:
            print("[STEP 3.5] スキップ")

        # ----------------------------------------------------------
        # Step 4〜7: レポート生成・メール配信
        # ----------------------------------------------------------
        if do_report:
            # report_only の場合はhistory.csvから最新portfolioを復元
            if portfolio_data is None:
                print("[STEP 4] history.csv から最新データを読込中...")
                history_df = drive.load_history(str(HISTORY_CSV))
                if history_df.empty:
                    raise RuntimeError("portfolio_data も history.csv も利用不可です。先にポートフォリオ取得を実行してください。")
                latest = history_df.iloc[-1]
                portfolio_data = {
                    "total": float(latest.get("total", 0)),
                    "holdings": [],
                    "date": str(latest.get("date", today)),
                }

            print("[STEP 4] 運用方針・ニュースを取得中...")
            financial_policy = FINANCIAL_POLICY.read_text(encoding="utf-8")
            stock_stories_text = STOCK_STORIES.read_text(encoding="utf-8")
            import json as _json
            stock_stories_dict = _json.loads(stock_stories_text)
            sync_stock_stories(stock_stories_dict)

            news_tickers = list(stock_stories_dict.keys())
            news = fetch_stock_news(news_tickers, stories=stock_stories_dict)
            print(f"  -> ニュース記事数: {sum(len(v) for v in news.values())}")

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
            for cp in chart_paths:
                try:
                    drive.upload_file(cp)
                except Exception as drive_err:
                    print(f"  -> [WARN] グラフ Drive アップロードスキップ: {drive_err}")

            print("[STEP 6] Claude APIでレポート生成中...")
            template = REPORT_TEMPLATE.read_text(encoding="utf-8")
            report_md = generate_report(
                template=template,
                portfolio_data=portfolio_data,
                sim_result=sim_result,
                financial_policy=financial_policy,
                stock_stories=stock_stories_text,
                news=news,
            )
            report_path = REPORTS_DIR / f"{tag}_report.md"
            report_path.write_text(report_md, encoding="utf-8")
            try:
                drive.upload_file(str(report_path))
            except Exception as drive_err:
                print(f"  -> [WARN] レポート Drive アップロードスキップ: {drive_err}")
            upload_report_to_supabase(str(today), report_md, sim_params=SIMULATION_PARAMS)

            print("[STEP 7] レポートをメール送信中...")
            attachments = ([str(screenshot_path)] if screenshot_path else []) + chart_paths
            send_report_email(
                to=RECIPIENT_EMAIL,
                subject=f"月次ポートフォリオレポート ({today.strftime('%Y年%m月')})",
                body_md=report_md,
                attachments=attachments,
            )
            print("[完了] レポートの配信が完了しました。")
        else:
            print(f"[完了] {run_type} 完了（レポート生成はスキップ）")

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
