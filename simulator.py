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
# fonts-noto-cjk パッケージでインストールされるフォント名を優先
import matplotlib.font_manager as fm
# フォントキャッシュを再構築
fm._load_fontmanager(try_read_cache=False)
# 利用可能な日本語フォントを検索
_jp_fonts = [f.name for f in fm.fontManager.ttflist
             if "Noto Sans CJK" in f.name or "IPA" in f.name or "Gothic" in f.name]
if _jp_fonts:
    plt.rcParams["font.family"] = [_jp_fonts[0], "DejaVu Sans"]
    print(f"  [FONT] 日本語フォント使用: {_jp_fonts[0]}")
else:
    plt.rcParams["font.family"] = ["DejaVu Sans"]
    print("  [FONT] 日本語フォントが見つかりません。DejaVu Sans を使用")


def _calculate_safe_spending_limit(current_total: float, params: dict) -> float:
    """
    105歳で資産がゼロになるための、初年度（100%期間）の安全な世帯生活費（年間予算）を逆算する。
    インフレ率2%、運用利回り4% (実質利回り約1.96%) と年金収入の現在価値を加味。
    """
    r_nominal = 0.04
    inflation = 0.02
    r_real = (1 + r_nominal) / (1 + inflation) - 1

    M = 0
    PV_pensions = 0
    
    tomoaki_age_start = params.get("tomoaki_age", 57)
    noriko_age_start = params.get("noriko_age", 51)
    tomoaki_lifespan = params.get("tomoaki_lifespan", 87)
    noriko_lifespan = params.get("noriko_lifespan", 105)
    
    # 紀子様が寿命を迎えるまでの年数
    years = noriko_lifespan - noriko_age_start + 1
    
    for t in range(1, years + 1):
        age_t = tomoaki_age_start + t - 1
        age_n = noriko_age_start + t - 1
        
        # 支出の現在価値係数
        rate = 0.50
        if age_t <= tomoaki_lifespan:
            for phase in params.get("spending_phases", []):
                if age_t <= phase["until_tomoaki_age"]:
                    rate = phase["rate"]
                    break
        
        M += rate / ((1 + r_real) ** (t - 1))
        
        # 収入（年金）の現在価値
        income = 0
        pension_start = params.get("public_pension_start_age", 65)
        
        # 私的年金
        private_start = 60
        private_end = 60 + params.get("private_pension_years", 10) - 1
        if private_start <= age_t <= private_end:
            income += params.get("private_pension_annual", 0)
            
        # 公的年金（夫）
        if pension_start <= age_t <= tomoaki_lifespan:
            income += params.get("tomoaki_public_pension_annual", 0)
            
        # 公的年金（妻）
        if age_n >= pension_start:
            income += params.get("noriko_public_pension_annual", 0)
            
        PV_pensions += income / ((1 + r_real) ** (t - 1))
        
    if M > 0:
        safe_spending = (current_total + PV_pensions) / M
        return safe_spending
    return 0.0


def run_simulation(
    portfolio_data: dict,
    history_df: pd.DataFrame,
    params: dict,
) -> dict:
    """
    資産寿命シミュレーションを実行する。
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
    annual_spending = _estimate_annual_spending(history_df, params)
    
    # --- 安全な支出上限（予算）の計算 ---
    safe_spending_limit = _calculate_safe_spending_limit(current_total, params)

    # --- リスク資産比率を算出 ---
    stock_val = portfolio_data.get("stock_value", 0)
    fund_val = portfolio_data.get("fund_value", 0)
    risk_assets = stock_val + fund_val
    risk_ratio = risk_assets / current_total if current_total > 0 else 0.0

    # --- 105歳までの資産推移を計算 ---
    projection = _project_assets(
        current_total=current_total,
        annual_spending=annual_spending,
        params=params,
        risk_ratio=risk_ratio,
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
        "safe_spending_limit": safe_spending_limit,
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

    # --- グラフ2: 過去との比較（日付ベース：最古データが今日と異なれば生成）---
    if not history_df.empty and len(history_df) >= 2:
        history_df["date"] = pd.to_datetime(history_df["date"])
        if history_df["date"].iloc[0] != history_df["date"].iloc[-1]:
            path2 = str(out / f"{tag}_comparison.png")
            _plot_yearly_comparison(history_df, path2)
            paths.append(path2)

    return paths


# ================================================================
# 内部関数
# ================================================================

def _get_past_row_by_date(history_df: pd.DataFrame, target_date: pd.Timestamp, days_tolerance: int = 45):
    """
    指定した日付から約1年前（許容誤差日数内）に最も近いデータを返す。
    見つからない場合は None を返す。
    """
    if history_df.empty:
        return None
    
    df = history_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    
    # 目標日から1年前
    one_year_ago = target_date - pd.Timedelta(days=365)
    
    # 差分（絶対値）を計算
    date_diffs = (df["date"] - one_year_ago).abs()
    
    # 最も近い日付のインデックス
    closest_idx = date_diffs.idxmin()
    
    # 許容範囲内かチェック
    if date_diffs[closest_idx].days <= days_tolerance:
        return df.loc[closest_idx]
    
    return None


def _calc_yoy_change(history_df: pd.DataFrame, current_total: float) -> float | None:
    """前年同月比の変動率を算出する。日付ベースで1年前のデータを特定する。"""
    if history_df.empty or len(history_df) < 2:
        return None

    try:
        current_date = pd.to_datetime(history_df.iloc[-1]["date"])
        past_row = _get_past_row_by_date(history_df, current_date)
        
        if past_row is not None:
            past_value = past_row["total_value"]
            if past_value > 0:
                return (current_total - past_value) / past_value
    except (IndexError, KeyError, Exception):
        pass

    return None


def _estimate_annual_spending(history_df: pd.DataFrame, params: dict) -> float:
    """
    年間支出を推定する。
    history.csv のデータから1年前の実績ベースを検索。なければデフォルト値。
    """
    # デフォルト: 2024年の生活費明細に基づく実績値（約1,225万円）
    # ※ 総額2,545万円から、妻（浅野紀子/池田紀子）への資産移動・贈与（1,000万, 100万, 110万×2）の計1,320万円を除外した額
    DEFAULT_ANNUAL_SPENDING = 1225_2536

    if history_df.empty or len(history_df) < 2:
        return DEFAULT_ANNUAL_SPENDING

    try:
        current_date = pd.to_datetime(history_df.iloc[-1]["date"])
        past_row = _get_past_row_by_date(history_df, current_date)

        if past_row is not None:
            # 実際の日数差から年率換算するための係数
            past_date = past_row["date"]
            days_diff = (current_date - past_date).days
            if days_diff <= 0:
                return DEFAULT_ANNUAL_SPENDING
                
            annualization_factor = 365.0 / days_diff

            start_val = past_row["total_value"]
            end_val = history_df.iloc[-1]["total_value"]

            # 収入計算
            income = 0
            tomoaki_age = params["tomoaki_age"]
            noriko_age = params["noriko_age"]
            pension_start = params.get("public_pension_start_age", 65)
            if tomoaki_age >= pension_start and tomoaki_age <= params["tomoaki_lifespan"]:
                income += params["tomoaki_public_pension_annual"]
            if noriko_age >= pension_start:
                income += params["noriko_public_pension_annual"]
            if 60 <= tomoaki_age < 60 + params["private_pension_years"]:
                income += params["private_pension_annual"]

            # 運用益の概算（簡易的に資産額の4%と仮定して差し引く）
            estimated_gain = end_val * 0.04

            # 期間中の実質的な支出
            period_spending = (start_val - end_val) + income + estimated_gain
            
            # 年率換算
            spending = period_spending * annualization_factor

            # 妥当性チェック: 200万〜1500万の範囲に収まるか
            if 200_0000 <= spending <= 1500_0000:
                return spending
    except (IndexError, KeyError, Exception):
        pass

    return DEFAULT_ANNUAL_SPENDING



def _project_assets(
    current_total: float,
    annual_spending: float,
    params: dict,
    risk_ratio: float = 0.0,
    spending_cut: bool = False,
) -> pd.DataFrame:
    """
    紀子様の105歳までの資産推移を年単位でシミュレーションする。
    基準は紀子様の年齢（より長寿の方を基準）。

    - 支出フェーズは智明様の年齢に基づく
    - リスク資産（risk_ratio 分）に対して年利 5%/4% の運用益を加算
    """
    noriko_age = params["noriko_age"]
    tomoaki_age = params["tomoaki_age"]
    target_age = params["noriko_lifespan"]  # 105歳
    tomoaki_lifespan = params["tomoaki_lifespan"]
    pension_start = params.get("public_pension_start_age", 65)

    rows = []
    assets = current_total

    for year_offset in range(target_age - noriko_age + 1):
        n_age = noriko_age + year_offset
        t_age = tomoaki_age + year_offset
        tomoaki_alive = t_age <= tomoaki_lifespan

        # --- 運用益計算（リスク資産分のみ）---
        if assets > 0 and risk_ratio > 0:
            rate = params.get("return_rate_before_75", 0.05) if t_age <= 75 \
                else params.get("return_rate_after_75", 0.04)
            investment_return = assets * risk_ratio * rate
        else:
            investment_return = 0.0

        # --- 収入計算 ---
        income = 0.0

        # 私的年金（智明様 60〜69歳の10年間と仮定）
        if tomoaki_alive and 60 <= t_age < 60 + params["private_pension_years"]:
            income += params["private_pension_annual"]

        # 公的年金（智明様、受給開始年齢〜寿命まで）
        if tomoaki_alive and t_age >= pension_start:
            income += params["tomoaki_public_pension_annual"]

        # 紀子様の公的年金（受給開始年齢〜終身）
        if n_age >= pension_start:
            income += params["noriko_public_pension_annual"]

        # --- 支出計算（智明の年齢ベース）---
        spending_rate = params.get("spending_rate_after_tomoaki", 0.50)
        if tomoaki_alive:
            for phase in params["spending_phases"]:
                if t_age <= phase["until_tomoaki_age"]:
                    spending_rate = phase["rate"]
                    break

        spending = annual_spending * spending_rate

        # 支出削減警告が出ている場合（直近2年のみ適用）
        if spending_cut and year_offset < 2:
            spending *= (1 - params["spending_cut_rate"])

        # --- 資産推移 ---
        net = investment_return + income - spending
        assets = max(0, assets + net)

        rows.append({
            "year_offset": year_offset,
            "noriko_age": n_age,
            "tomoaki_age": t_age,
            "income": income,
            "investment_return": investment_return,
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

    # 年金開始ライン（紀子様の年齢で表示）
    ax.axvline(x=75, color="green", linestyle=":", alpha=0.5, label="公的年金開始 (75歳)")

    # 智明様の寿命ライン（紀子様の年齢に換算）
    # 智明87歳 ≒ 紀子81歳（6歳差）
    if "tomoaki_age" in projection.columns:
        tomoaki_end_row = projection[projection["tomoaki_age"] == 87]
        if not tomoaki_end_row.empty:
            noriko_age_at_tomoaki_end = int(tomoaki_end_row.iloc[0]["noriko_age"])
            ax.axvline(
                x=noriko_age_at_tomoaki_end, color="orange",
                linestyle="--", alpha=0.6, linewidth=1.2,
                label=f"智明様寿命 (紀子様{noriko_age_at_tomoaki_end}歳時点)"
            )

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
    """過去との資産構成比較グラフを描画する（日付ベースで比較対象を選択）。"""
    import pandas as pd
    from datetime import timedelta

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    history_df = history_df.copy()
    history_df["date"] = pd.to_datetime(history_df["date"])
    current = history_df.iloc[-1]
    current_date = current["date"]

    # 「約1年前」を日付で検索：current_date - 365日 に最も近いデータ
    target_date = current_date - timedelta(days=365)
    date_diffs = (history_df["date"] - target_date).abs()
    past = history_df.loc[date_diffs.idxmin()]
    past_date = past["date"]

    # タイトル：実際の日数差を表示
    days_diff = (current_date - past_date).days
    if days_diff >= 300:
        past_label = f"約1年前 ({row_date(past)})"
    elif days_diff >= 60:
        months = round(days_diff / 30)
        past_label = f"約{months}ヶ月前 ({row_date(past)})"
    elif days_diff >= 14:
        weeks = round(days_diff / 7)
        past_label = f"約{weeks}週間前 ({row_date(past)})"
    else:
        past_label = f"{days_diff}日前 ({row_date(past)})"

    for ax, row, title in [
        (axes[0], past, past_label),
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
