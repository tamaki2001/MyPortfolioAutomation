"""
レポート生成モジュール（Claude API）
====================================
Claude API を呼び出し、report_template.txt に従って
月次レポートを執筆する。

Ultra C 要件:
  「知識での武装」を揺さぶる鋭い問いかけを必ず含める。
  特に個別株のストーリーと現実の乖離が見られる場合、
  そのギャップを指摘する問いを生成する。
"""

import os
import json
from datetime import datetime

import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"  # Claude Sonnet 4
MAX_TOKENS = 8192


def generate_report(
    template: str,
    portfolio_data: dict,
    sim_result: dict,
    financial_policy: str,
    stock_stories: str,
    news: dict[str, list[dict]],
) -> str:
    """
    Claude API でレポートを生成する。

    Args:
        template: report_template.txt の内容
        portfolio_data: 現在のポートフォリオデータ
        sim_result: シミュレーション結果
        financial_policy: financialPolicy.md の内容
        stock_stories: stock_stories.json の内容
        news: 銘柄ごとのニュース記事

    Returns:
        Markdown 形式のレポート文字列
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_report(portfolio_data, sim_result)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # --- テンプレートの日付プレースホルダーを現在日付で置換 ---
    now = datetime.now()
    template = template.replace("{year}", str(now.year)).replace("{month}", str(now.month))

    # --- stock_stories を dict としてパース（マッピング用）---
    try:
        stories_dict = json.loads(stock_stories)
    except Exception:
        stories_dict = {}

    # --- シミュレーション結果の要約 ---
    sim_summary = _summarize_simulation(sim_result)

    # --- ニュースの要約 ---
    news_summary = _summarize_news(news, stories_dict)

    # --- プロンプト構築 ---
    system_prompt = _build_system_prompt(financial_policy, stock_stories)
    user_prompt = _build_user_prompt(
        template=template,
        portfolio_data=portfolio_data,
        sim_summary=sim_summary,
        news_summary=news_summary,
        stories_dict=stories_dict,
        report_date=now,
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


# ================================================================
# プロンプト構築
# ================================================================

def _build_system_prompt(financial_policy: str, stock_stories: str) -> str:
    """Claude API の system プロンプトを構築する。"""
    return f"""あなたは、FIRE生活を送る浅野智明様（57歳）の専属資産管理アドバイザーです。
配偶者の紀子様（51歳）と共に暮らしています。

## あなたの役割
- 月次ポートフォリオレポートを、テンプレートに従って執筆する
- データに基づいた冷静な分析を提供する
- **最重要**: 「知識での武装」を揺さぶる鋭い問いかけを必ず含めること

## 「Ultra C」ルール
浅野様は個別株について独自の投資仮説（ストーリー）を持っています。
あなたは以下を必ず実行してください:
1. 各個別株のストーリーと、最新ニュースを照合する
2. ストーリーと現実に乖離がある場合、その事実を明確に指摘する
3. 「このストーリーはまだ有効ですか？」「何が変われば撤退しますか？」
   「知識が確信に変わっていませんか？」といった揺さぶりの問いを投げかける
4. 心地よい確認バイアスではなく、建設的な異論を提示する

## 運用方針
{financial_policy}

## 個別株の投資仮説（ストーリーライン）
{stock_stories}

## トーン
- 敬語で丁寧に、しかし忖度なく率直に
- データファーストで感情論を排除
- 長期視点を常に意識"""


def _build_user_prompt(
    template: str,
    portfolio_data: dict,
    sim_summary: str,
    news_summary: str,
    stories_dict: dict | None = None,
    report_date: "datetime | None" = None,
) -> str:
    """Claude API の user プロンプトを構築する。"""
    holdings_text = _format_holdings(portfolio_data)

    if report_date is None:
        report_date = datetime.now()
    date_str = f"{report_date.year}年{report_date.month}月"

    # --- 銘柄名⇔ストーリーキーのマッピング表 ---
    mapping_text = _build_ticker_mapping(portfolio_data, stories_dict or {})

    return f"""今日の日付: {report_date.strftime('%Y年%m月%d日')}
以下のデータに基づき、テンプレートに従って月次レポートを作成してください。
レポートのタイトルは「{date_str}」として作成してください。

## レポートテンプレート
{template}

## 現在のポートフォリオデータ
- 総資産: {portfolio_data['total_value']:,.0f} 円
- 日本円現金: {portfolio_data['cash_jpy']:,.0f} 円
- 米ドル現金: {portfolio_data['cash_usd']:,.0f} 円
- 株式時価合計: {portfolio_data['stock_value']:,.0f} 円
- 投資信託時価合計: {portfolio_data.get('fund_value', 0):,.0f} 円

### 保有銘柄
{holdings_text}

{mapping_text}

## 資産寿命シミュレーション結果
{sim_summary}

## 最新ニュース（各銘柄の投資仮説検証用）
{news_summary}

---
**重要**:
- 「Ultra C」として、個別株のストーリー乖離を必ず検証し、浅野様の「知識での武装」に対する鋭い問いかけをレポートに含めてください。
- 上記「銘柄名⇔ストーリーキー対応表」を使い、ポートフォリオの全銘柄（インデックスファンド・投資信託を含む）を分析してください。
- 各ニュース記事を参照する際は、出典URLをMarkdownリンク形式 [記事タイトル](URL) で記載してください。"""


# ================================================================
# ヘルパー
# ================================================================

def _summarize_simulation(sim_result: dict) -> str:
    """シミュレーション結果をテキストに要約する。"""
    lines = []
    lines.append(f"- 現在の総資産: {sim_result['current_total']:,.0f} 円")
    lines.append(f"- 推定年間支出: {sim_result['annual_spending']:,.0f} 円")

    if sim_result["yoy_change_pct"] is not None:
        lines.append(f"- 前年同月比: {sim_result['yoy_change_pct']*100:+.1f}%")

    if sim_result["depletion_age"]:
        lines.append(f"- ⚠ 資産枯渇予測: 紀子様 {sim_result['depletion_age']}歳時点")
    else:
        lines.append("- 資産枯渇リスク: 105歳まで枯渇しない見込み")

    for w in sim_result["warnings"]:
        lines.append(f"- ⚠ 警告: {w}")

    return "\n".join(lines)


def _summarize_news(news: dict[str, list[dict]], stories_dict: dict | None = None) -> str:
    """ニュースをテキストに要約する。stories_dict があれば product_name を見出しに使用する。"""
    stories = stories_dict or {}
    lines = []
    for ticker, articles in news.items():
        # 見出し: product_name があれば使い、ストーリーキーも併記
        story = stories.get(ticker, {})
        product_name = story.get("product_name") or story.get("company", "")
        if product_name and product_name != ticker:
            header = f"{product_name}（ストーリーキー: {ticker}）"
        else:
            header = ticker
        lines.append(f"\n### {header}")
        if not articles:
            lines.append("  - 該当ニュースなし")
            continue
        for a in articles:
            url = a.get("url", "")
            lines.append(f"  - [{a['source']}] {a['title']}")
            if url:
                lines.append(f"    出典: {url}")
            if a["description"]:
                lines.append(f"    {a['description'][:150]}")
    return "\n".join(lines)


def _build_ticker_mapping(portfolio_data: dict, stories_dict: dict) -> str:
    """
    ポートフォリオ銘柄名 ⇔ stock_stories キーの対応表を生成する。
    投資信託など名称が異なる銘柄の照合をClaudeが行えるようにする。
    """
    if not stories_dict:
        return ""

    lines = ["### 銘柄名⇔ストーリーキー対応表（分析時に参照してください）"]
    for story_key, story in stories_dict.items():
        product_name = story.get("product_name") or story.get("company", "")
        asset_type = story.get("asset_type", "")
        if product_name:
            lines.append(f"  - ストーリーキー `{story_key}` = {product_name}（{asset_type}）")
        else:
            lines.append(f"  - ストーリーキー `{story_key}`（{asset_type}）")
    return "\n".join(lines)


def _format_holdings(portfolio_data: dict) -> str:
    """保有銘柄（株式＋投資信託）をテキスト形式にする。"""
    lines = []
    for h in portfolio_data.get("holdings", []):
        lines.append(
            f"  - {h['ticker']}: {h['value']:,.0f} 円 "
            f"({h['quantity']:.0f}株 × {h.get('price', 0):,.0f}円)"
        )
    for f in portfolio_data.get("funds", []):
        lines.append(
            f"  - [投信] {f['name']}: {f['value']:,.0f} 円 "
            f"({f['quantity']:.0f}口 × 基準価額 {f.get('nav', 0):,.0f}円)"
        )
    return "\n".join(lines) if lines else "  - (データなし)"


def _fallback_report(portfolio_data: dict, sim_result: dict) -> str:
    """API キー未設定時のフォールバックレポート。"""
    return f"""# 月次ポートフォリオレポート（簡易版）

> ⚠ ANTHROPIC_API_KEY が未設定のため、簡易レポートを生成しました。

## 資産概要
- 総資産: {portfolio_data['total_value']:,.0f} 円
- 株式: {portfolio_data['stock_value']:,.0f} 円
- 日本円現金: {portfolio_data['cash_jpy']:,.0f} 円
- 米ドル現金: {portfolio_data['cash_usd']:,.0f} 円

## シミュレーション
- 推定年間支出: {sim_result['annual_spending']:,.0f} 円
- 資産枯渇年齢: {sim_result['depletion_age'] or '枯渇なし（105歳まで安全）'}

## 警告
{chr(10).join(sim_result['warnings']) or 'なし'}
"""
