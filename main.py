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
    "public_pension_annual": 240_0000,    # 公的年金 240万/年（終身、75歳～）
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
    """YYMMDD_HHMMSS形式の日付タグを返す（毎回の実行結果を上書きせずに蓄積するため）。"""
    return datetime.now().strftime("%y%m%d_%H%M%S")


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
        try:
            drive.upload_file(str(screenshot_path))
            print(f"  -> スクリーンショット Drive アップロード完了")
        except Exception as drive_err:
            print(f"  -> [WARN] スクリーンショット Drive アップロードスキップ: {drive_err}")

        # ----------------------------------------------------------
        # Step 3: history.csv に追記 → Google Drive アップロード
        # ----------------------------------------------------------
        print("[STEP 3] history.csv を更新中...")
        drive.append_history(
            csv_path=str(HISTORY_CSV),
            date_str=str(today),
            portfolio_data=portfolio_data,
        )
        try:
            drive.upload_file(str(HISTORY_CSV))
            print("  -> history.csv 更新・アップロード完了")
        except Exception as drive_err:
            print(f"  -> [WARN] Drive アップロードスキップ: {drive_err}")
            print("  -> history.csv はローカルに保存済み（ワークフロー続行）")

        # ----------------------------------------------------------
        # Step 4: 外部コンテキスト取得
        # ----------------------------------------------------------
        print("[STEP 4] 運用方針・ニュースを取得中...")
        financial_policy = FINANCIAL_POLICY.read_text(encoding="utf-8")
        stock_stories_text = STOCK_STORIES.read_text(encoding="utf-8")
        import json as _json
        stock_stories_dict = _json.loads(stock_stories_text)

        # ニュース取得対象: stock_stories.json に登録されたすべての識別子
        news_tickers = list(stock_stories_dict.keys())
        news = fetch_stock_news(news_tickers, stories=stock_stories_dict)
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
        for cp in chart_paths:
            try:
                drive.upload_file(cp)
                print(f"  -> シミュレーショングラフ Drive アップロード完了: {Path(cp).name}")
            except Exception as drive_err:
                print(f"  -> [WARN] グラフ Drive アップロードスキップ: {drive_err}")

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
            stock_stories=stock_stories_text,
            news=news,
        )

        report_path = REPORTS_DIR / f"{tag}_report.md"
        report_path.write_text(report_md, encoding="utf-8")
        print(f"  -> レポート保存: {report_path}")
        try:
            drive.upload_file(str(report_path))
            print(f"  -> レポート Drive アップロード完了: {report_path.name}")
        except Exception as drive_err:
            print(f"  -> [WARN] レポート Drive アップロードスキップ: {drive_err}")

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
