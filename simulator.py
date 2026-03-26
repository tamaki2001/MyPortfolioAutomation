"""
資産寿命シミュレーション & グラフ生成
=====================================
- 105歳までの資産推移を年単位で計算
- 年金収入（私的年金＋公的年金）を反映
- 段階的支出モデル（100% / 75% / 50%）
- 前年同月比 20%以上減少時の警告ロジック
- Matplotlib でグラフ出力
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # ヘッドレス環境用
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path


# 日本語フォント設定（GitHub Actions の ubuntu 環境用）
plt.rcParams["font.family"] = ["IPAexGothic", "Noto Sans CJK JP", "DejaVu Sans"]


def run_simulation(
    portfolio_data: dict,
    history_df: pd.DataFrame,
    params: dict,
) -> dict:
    """
    資産寿命シミュレーションを実行する。

    Args:
        portfolio_data: 現在のポートフォリオデータ
        history_df: 過去の資産推移 DataFrame
        params: SIMULATION_PARAMS 辞書

    Returns:
        {
            "projection": pd.DataFrame,   # 年齢ごとの資産推移
            "depletion_age": int | None,   # 資産枯渇年齢（None=枯渇しない）
            "warnings": list[str],         # 警告メッセージ
            "yoy_change_pct": float | None, # 前年同月比変動率
            "current_total": float,
            "annual_spending": float,       # 現在の年間支出額（推定）
        }
    """
    current_total = portfolio_data["total_value"]

    # --- 前年同月比チェック ---
    warnings = []
    yoy_change_pct = _calc_yoy_change(history_df, current_total)
    if yoy_change_pct is not None and yoy_change_pct <= -params["decline_threshold"]:
        warnings.append(
            f"前年同月比で資産が {yoy_change_pct*100:.1f}% 減少しています。"
            f"支出を {params['spending_cut_rate']*100:.0f}% 削減することを推奨します。"
        )

    # --- 年間支出推定 ---
    # history.csv から直近12ヶ月の資産減少 + 収入を考慮して推定
    annual_spending = _estimate_annual_spending(history_df, params)

    # --- 105歳までの資産推移を計算 ---
    projection = _project_assets(
        current_total=current_total,
        annual_spending=annual_spending,
        params=params,
        spending_cut=yoy_change_pct is not None and yoy_change_pct <= -params["decline_threshold"],
    )

    # 資産枯渇年齢
    depleted = projection[projection["assets"] <= 0]
    depletion_age = int(depleted.iloc[0]["noriko_age"]) if not depleted.empty else None

    return {
        "projection": projection,
        "depletion_age": depletion_age,
        "warnings": warnings,
        "yoy_change_pct": yoy_change_pct,
        "current_total": current_total,
        "annual_spending": annual_spending,
    }


def generate_charts(
    sim_result: dict,
    history_df: pd.DataFrame,
    output_dir: str,
    tag: str,
) -> list[str]:
    """
    グラフを生成して保存する。

    Returns:
        生成されたグラフファイルのパスリスト
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []

    # --- グラフ1: 資産寿命シミュレーション ---
    path1 = str(out / f"{tag}_simulation.png")
    _plot_simulation(sim_result["projection"], sim_result, path1)
    paths.append(path1)

    # --- グラフ2: 1年前との比較 ---
    if not history_df.empty and len(history_df) >= 2:
        path2 = str(out / f"{tag}_comparison.png")
        _plot_yearly_comparison(history_df, path2)
        paths.append(path2)

    return paths


# ================================================================
# 内部関数
# ================================================================

def _calc_yoy_change(history_df: pd.DataFrame, current_total: float) -> float | None:
    """前年同月比の変動率を算出する。"""
    if history_df.empty or len(history_df) < 12:
        return None

    try:
        # 12ヶ月前のデータ
        past_value = history_df.iloc[-12]["total_value"]
        if past_value <= 0:
            return None
        return (current_total - past_value) / past_value
    except (IndexError, KeyError):
        return None


def _estimate_annual_spending(history_df: pd.DataFrame, params: dict) -> float:
    """
    年間支出を推定する。
    history.csv のデータが十分にある場合は実績ベース、
    なければデフォルト値を使用。
    """
    # デフォルト: 年間 400万円（月 約33万円）
    DEFAULT_ANNUAL_SPENDING = 400_0000

    if history_df.empty or len(history_df) < 12:
        return DEFAULT_ANNUAL_SPENDING

    try:
        # 直近12ヶ月の資産減少額 + 推定収入 = 推定支出
        start_val = history_df.iloc[-12]["total_value"]
        end_val = history_df.iloc[-1]["total_value"]

        # 年金収入見込み（現在受給中かどうかで分岐）
        income = 0
        noriko_age = params["noriko_age"]
        if noriko_age >= 65:
            income += params["public_pension_annual"]
        if noriko_age < 65 and noriko_age >= 60:
            income += params["private_pension_annual"]

        spending = (start_val - end_val) + income
        # 妥当性チェック: 200万〜1200万の範囲に収まるか
        if 200_0000 <= spending <= 1200_0000:
            return spending
    except (IndexError, KeyError):
        pass

    return DEFAULT_ANNUAL_SPENDING


def _project_assets(
    current_total: float,
    annual_spending: float,
    params: dict,
    spending_cut: bool,
) -> pd.DataFrame:
    """
    紀子様の105歳までの資産推移を年単位でシミュレーションする。
    基準は紀子様の年齢（より長寿の方を基準）。
    """
    noriko_age = params["noriko_age"]
    tomoaki_age = params["tomoaki_age"]
    target_age = params["noriko_lifespan"]  # 105歳

    rows = []
    assets = current_total

    for year_offset in range(target_age - noriko_age + 1):
        n_age = noriko_age + year_offset
        t_age = tomoaki_age + year_offset

        # --- 収入計算 ---
        income = 0.0

        # 私的年金（智明様 60〜69歳の10年間と仮定）
        if 60 <= t_age < 60 + params["private_pension_years"]:
            income += params["private_pension_annual"]

        # 公的年金（智明様 65歳〜終身、ただし寿命まで）
        if t_age >= 65 and t_age <= params["tomoaki_lifespan"]:
            income += params["public_pension_annual"]

        # 紀子様の公的年金（65歳〜終身）
        if n_age >= 65:
            income += params["public_pension_annual"]

        # --- 支出計算 ---
        spending_rate = 1.0
        for phase in params["spending_phases"]:
            if n_age <= phase["until_age"]:
                spending_rate = phase["rate"]
                break

        spending = annual_spending * spending_rate

        # 支出削減警告が出ている場合
        if spending_cut and year_offset < 2:
            spending *= (1 - params["spending_cut_rate"])

        # --- 資産推移 ---
        net = income - spending
        assets = max(0, assets + net)

        rows.append({
            "year_offset": year_offset,
            "noriko_age": n_age,
            "tomoaki_age": t_age,
            "income": income,
            "spending": spending,
            "net": net,
            "assets": assets,
        })

    return pd.DataFrame(rows)


def _plot_simulation(projection: pd.DataFrame, sim_result: dict, path: str) -> None:
    """資産寿命シミュレーショングラフを描画する。"""
    fig, ax = plt.subplots(figsize=(12, 6))

    ages = projection["noriko_age"]
    assets = projection["assets"] / 10000  # 万円表示

    ax.fill_between(ages, assets, alpha=0.3, color="#2196F3")
    ax.plot(ages, assets, color="#1565C0", linewidth=2, label="資産残高")

    # 枯渇ポイント
    if sim_result["depletion_age"]:
        ax.axvline(
            x=sim_result["depletion_age"], color="red",
            linestyle="--", linewidth=1.5, label=f'枯渇: 紀子様 {sim_result["depletion_age"]}歳'
        )

    # 年金開始ライン
    ax.axvline(x=65, color="green", linestyle=":", alpha=0.5, label="公的年金開始 (65歳)")

    ax.set_xlabel("紀子様の年齢", fontsize=12)
    ax.set_ylabel("資産残高 (万円)", fontsize=12)
    ax.set_title("資産寿命シミュレーション（105歳まで）", fontsize=14, fontweight="bold")
    ax.legend(loc="upper right")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_yearly_comparison(history_df: pd.DataFrame, path: str) -> None:
    """1年前との資産構成比較グラフを描画する。"""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # 直近と12ヶ月前のデータ
    current = history_df.iloc[-1]
    past = history_df.iloc[-12] if len(history_df) >= 12 else history_df.iloc[0]

    for ax, row, title in [
        (axes[0], past, f"1年前 ({row_date(past)})"),
        (axes[1], current, f"最新 ({row_date(current)})"),
    ]:
        # 現金・株式の内訳
        labels = []
        values = []

        for col in row.index:
            if col.startswith("holding_"):
                ticker = col.replace("holding_", "")
                val = row[col]
                if pd.notna(val) and val > 0:
                    labels.append(ticker)
                    values.append(val)

        # 現金
        for cash_key, cash_label in [("cash_jpy", "日本円"), ("cash_usd", "米ドル")]:
            if cash_key in row and pd.notna(row[cash_key]) and row[cash_key] > 0:
                labels.append(cash_label)
                values.append(row[cash_key])

        if values:
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.set_title(title, fontsize=12, fontweight="bold")

    fig.suptitle("ポートフォリオ構成比較", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def row_date(row) -> str:
    """DataFrame 行から日付文字列を取得する。"""
    try:
        d = row.get("date", "")
        return str(d)[:10] if d else "N/A"
    except Exception:
        return "N/A"
