"""
マネーフォワード ポートフォリオ スクレイパー
============================================
Cookie注入方式で MoneyForward ME にアクセスし、
ポートフォリオページ (https://moneyforward.com/bs/portfolio) の
スクリーンショット撮影＋HTMLスクレイピングを行う。

認証方式:
  1. Chrome の Cookie Editor 拡張機能でエクスポートした cookies.json を使用
  2. GitHub Actions では MF_COOKIES 環境変数 or Google Drive から取得
  3. アクセス後に更新された Cookie を保存（セッション延長）

特殊ルール:
  VT のうち VT_EXCLUDE_SHARES 株分を
  「株式」から除外し「米ドル現金」として集計し直す。

環境変数:
  MF_COOKIES - Cookie JSON文字列（GitHub Secretsに設定）
"""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext


# マネーフォワード URL
MF_PORTFOLIO_URL = "https://moneyforward.com/bs/portfolio"

# Cookie ファイルのローカルパス（ローカル実行時）
COOKIES_FILE = Path(__file__).parent / "cookies.json"

# sameSite 値の正規化マップ（Cookie Editor → Playwright 変換）
SAMESITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


def scrape_portfolio(
    screenshot_path: str,
    vt_exclude_shares: int = 478,
) -> dict:
    """
    マネーフォワード ME に Cookie 認証でアクセスし、ポートフォリオデータを取得する。

    Returns:
        {
            "total_value": float,
            "cash_jpy": float,
            "cash_usd": float,
            "stock_value": float,
            "fund_value": float,
            "real_estate_value": float,
            "holdings": [
                {"ticker": str, "name": str, "value": float,
                 "quantity": float, "price": float, "gain_loss": float,
                 "gain_loss_pct": str, "broker": str},
                ...
            ],
            "funds": [
                {"name": str, "value": float, "quantity": float,
                 "nav": float, "gain_loss": float, "gain_loss_pct": str,
                 "broker": str},
                ...
            ],
            "cash_details": [
                {"name": str, "value": float, "broker": str},
                ...
            ],
            "asset_text": str,  # ページ全文テキスト（レポート生成用）
        }
    """
    Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ja-JP",
        )

        try:
            # Cookie 注入
            cookies = _load_cookies()
            _inject_cookies(context, cookies)
            page = context.new_page()

            # ポートフォリオページにアクセス
            _navigate_to_portfolio(page)

            # スクリーンショット撮影
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  -> スクリーンショット撮影完了: {screenshot_path}")

            # ページ全文テキスト取得（レポート生成用）
            asset_text = page.inner_text("body")

            # 構造化データ抽出（JavaScript で DOM を解析）
            raw_data = _extract_portfolio_data(page)
            raw_data["asset_text"] = asset_text

            # VT 特殊ルール適用
            portfolio = _apply_vt_rule(raw_data, vt_exclude_shares)

            # Cookie を更新保存（セッション延長）
            _save_updated_cookies(context)

            return portfolio

        finally:
            context.close()
            browser.close()


# ================================================================
# Cookie 管理
# ================================================================

def _load_cookies() -> list[dict]:
    """
    Cookie を読み込む。

    優先順:
      1. 環境変数 MF_COOKIES（GitHub Actions 用）
      2. ローカルの cookies.json ファイル
    """
    # 環境変数から（GitHub Actions）
    cookies_env = os.environ.get("MF_COOKIES", "")
    if cookies_env:
        print("  -> Cookie を環境変数 (MF_COOKIES) から読み込み")
        return json.loads(cookies_env)

    # ローカルファイルから
    if COOKIES_FILE.exists():
        print(f"  -> Cookie をファイルから読み込み: {COOKIES_FILE}")
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))

    raise FileNotFoundError(
        "Cookie が見つかりません。\n"
        "以下のいずれかを設定してください:\n"
        "  1. 環境変数 MF_COOKIES に Cookie JSON を設定\n"
        "  2. cookies.json ファイルをプロジェクトルートに配置\n"
        "\n"
        "Cookie の取得方法:\n"
        "  1. Chrome に Cookie Editor 拡張機能をインストール\n"
        "  2. MoneyForward にログインした状態で bs/portfolio を開く\n"
        "  3. Cookie Editor の Export ボタンでコピー\n"
        "  4. cookies.json に貼り付けて保存"
    )


def _inject_cookies(context: BrowserContext, raw_cookies: list[dict]) -> None:
    """
    Cookie を Playwright コンテキストに注入する。
    Cookie Editor のエクスポート形式を Playwright 形式に変換。
    """
    cookies = []
    for c in raw_cookies:
        # sameSite を Playwright 形式に正規化
        same = c.get("sameSite", "")
        c["sameSite"] = SAMESITE_MAP.get(str(same).lower(), "Lax")

        # Playwright が受け付けないフィールドを除去
        for key in ["hostOnly", "session", "storeId", "id"]:
            c.pop(key, None)

        cookies.append(c)

    context.add_cookies(cookies)
    print(f"  -> {len(cookies)} 件の Cookie を注入完了")


def _save_updated_cookies(context: BrowserContext) -> None:
    """アクセス後の更新済み Cookie を保存する（セッション延長用）。"""
    try:
        updated = context.cookies()
        # ローカルファイルに保存
        COOKIES_FILE.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  -> 更新済み Cookie を保存: {COOKIES_FILE}")
    except Exception as e:
        print(f"  -> Cookie 保存スキップ（GitHub Actions では正常）: {e}")


# ================================================================
# ページナビゲーション
# ================================================================

def _navigate_to_portfolio(page: Page) -> None:
    """
    ポートフォリオページにアクセスし、正常表示を確認する。
    ログインページにリダイレクトされた場合はエラー。
    """
    print("  -> ポートフォリオページにアクセス中...")
    page.goto(MF_PORTFOLIO_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    current_url = page.url
    print(f"  -> 現在の URL: {current_url}")

    # ログインページにリダイレクトされた場合
    if any(k in current_url for k in ["sign_in", "login", "users/sign_in"]):
        page.screenshot(path="/tmp/login_failed.png")
        raise RuntimeError(
            "ログインセッションが切れています。\n"
            "Cookie を再エクスポートしてください。\n"
            f"リダイレクト先: {current_url}"
        )

    # ポートフォリオページが正しく表示されているか
    if "/bs/portfolio" not in current_url:
        page.screenshot(path="/tmp/unexpected_page.png")
        raise RuntimeError(
            f"予期しないページにリダイレクトされました: {current_url}"
        )

    print("  -> ポートフォリオページ表示完了")


# ================================================================
# データ抽出（実際のマネーフォワード DOM 構造に基づく）
# ================================================================

def _extract_portfolio_data(page: Page) -> dict:
    """
    ポートフォリオページから全データを JavaScript で構造的に抽出する。

    マネーフォワード ME のポートフォリオページ構造（2026年3月時点）:
      - 資産総額：h2 の隣に表示
      - 「資産の内訳」テーブル: 各カテゴリの合計と割合
      - 各カテゴリセクション:
        - 「預金・現金・暗号資産」: heading + 合計 + 明細テーブル
        - 「株式（現物）」: heading + 合計 + 銘柄テーブル（コード付き）
        - 「投資信託」: heading + 合計 + 銘柄テーブル
        - 「不動産」: heading + 合計 + 物件テーブル
        - 「ポイント・マイル」: heading + 合計 + 明細テーブル
    """
    data = page.evaluate("""() => {
        // ユーティリティ: テキストから数値を抽出
        function parseNum(text) {
            if (!text) return 0;
            const cleaned = text.replace(/円/g, '').replace(/¥/g, '')
                               .replace(/,/g, '').replace(/\\s/g, '').trim();
            const m = cleaned.match(/-?[\\d]+\\.?\\d*/);
            return m ? parseFloat(m[0]) : 0;
        }

        // ユーティリティ: % テキストをそのまま返す
        function parsePct(text) {
            if (!text) return '';
            const m = text.match(/-?[\\d.]+%/);
            return m ? m[0] : text.trim();
        }

        const result = {
            total_value: 0,
            categories: {},
            stocks: [],
            funds: [],
            cash_details: [],
            real_estate: [],
        };

        // --- 資産総額 ---
        // "資産総額： XXX円" のテキストを探す
        const allText = document.body.innerText;
        const totalMatch = allText.match(/資産総額[：:]+\\s*([\\d,]+)円/);
        if (totalMatch) {
            result.total_value = parseNum(totalMatch[1]);
        }

        // --- セクションを region 要素で走査 ---
        const regions = document.querySelectorAll('section, div[class*="bs-"]');

        // --- 全 heading（h2, h3）からセクションを特定 ---
        const headings = document.querySelectorAll('h2, h3');
        headings.forEach(h => {
            const text = h.innerText.trim();

            // 合計金額のパターン: "合計：XXX円"
            const sumMatch = text.match(/合計[：:]([\\d,]+)円/);

            if (text.includes('預金') || text.includes('現金')) {
                if (sumMatch) result.categories['cash'] = parseNum(sumMatch[1]);

                // 現金明細テーブルを取得
                const section = h.closest('section, div[class*="region"], [role="region"]')
                                || h.parentElement?.parentElement;
                if (section) {
                    const table = section.querySelector('table');
                    if (table) {
                        const rows = table.querySelectorAll('tr');
                        // ヘッダー行以外を処理
                        // 現金テーブル: 種類・名称 | 金額 | 保有金融機関
                        for (let i = 0; i < rows.length; i++) {
                            const cells = rows[i].querySelectorAll('td');
                            if (cells.length >= 2) {
                                const name = cells[0]?.innerText?.trim() || '';
                                const value = parseNum(cells[1]?.innerText || '0');
                                const broker = cells[2]?.innerText?.trim() || '';
                                if (name && value > 0) {
                                    result.cash_details.push({
                                        name: name, value: value, broker: broker
                                    });
                                }
                            }
                        }
                    }
                }
            }

            if (text.includes('株式') && text.includes('現物')) {
                if (sumMatch) result.categories['stocks'] = parseNum(sumMatch[1]);

                // 株式テーブルを取得
                const section = h.closest('section, div[class*="region"], [role="region"]')
                                || h.parentElement?.parentElement;
                if (section) {
                    const table = section.querySelector('table');
                    if (table) {
                        const rows = table.querySelectorAll('tr');
                        // 株式テーブル: 銘柄コード | 銘柄名 | 保有数 | 平均取得単価 | 現在値 | 評価額 | 前日比 | 評価損益 | 評価損益率 | 保有金融機関
                        for (let i = 0; i < rows.length; i++) {
                            const cells = rows[i].querySelectorAll('td');
                            if (cells.length >= 6) {
                                // テーブル列のインデックス
                                let idx = 0;
                                const ticker = cells[idx++]?.innerText?.trim() || '';
                                const name = cells[idx++]?.innerText?.trim() || '';
                                const quantity = parseNum(cells[idx++]?.innerText);
                                const avgCost = parseNum(cells[idx++]?.innerText);
                                const curPrice = parseNum(cells[idx++]?.innerText);
                                const evalValue = parseNum(cells[idx++]?.innerText);
                                const dayChange = idx < cells.length ? parseNum(cells[idx++]?.innerText) : 0;
                                const gainLoss = idx < cells.length ? parseNum(cells[idx++]?.innerText) : 0;
                                const gainLossPct = idx < cells.length ? parsePct(cells[idx++]?.innerText) : '';
                                const broker = idx < cells.length ? cells[idx++]?.innerText?.trim() : '';

                                if (name && evalValue > 0) {
                                    result.stocks.push({
                                        ticker, name, quantity, avgCost, curPrice,
                                        value: evalValue, dayChange, gainLoss, gainLossPct, broker
                                    });
                                }
                            }
                        }
                    }
                }
            }

            if (text.includes('投資信託') && !text.includes('外国')) {
                if (sumMatch) result.categories['funds'] = parseNum(sumMatch[1]);

                // 投信テーブルを取得
                const section = h.closest('section, div[class*="region"], [role="region"]')
                                || h.parentElement?.parentElement;
                if (section) {
                    const table = section.querySelector('table');
                    if (table) {
                        const rows = table.querySelectorAll('tr');
                        // 投資信託テーブル: 銘柄名 | 保有数 | 平均取得単価 | 基準価額 | 評価額 | 前日比 | 評価損益 | 評価損益率 | 保有金融機関
                        for (let i = 0; i < rows.length; i++) {
                            const cells = rows[i].querySelectorAll('td');
                            if (cells.length >= 5) {
                                let idx = 0;
                                const name = cells[idx++]?.innerText?.trim() || '';
                                const quantity = parseNum(cells[idx++]?.innerText);
                                const avgCost = parseNum(cells[idx++]?.innerText);
                                const nav = parseNum(cells[idx++]?.innerText);
                                const evalValue = parseNum(cells[idx++]?.innerText);
                                const dayChange = idx < cells.length ? parseNum(cells[idx++]?.innerText) : 0;
                                const gainLoss = idx < cells.length ? parseNum(cells[idx++]?.innerText) : 0;
                                const gainLossPct = idx < cells.length ? parsePct(cells[idx++]?.innerText) : '';
                                const broker = idx < cells.length ? cells[idx++]?.innerText?.trim() : '';

                                if (name && evalValue > 0) {
                                    result.funds.push({
                                        name, quantity, avgCost, nav,
                                        value: evalValue, dayChange, gainLoss, gainLossPct, broker
                                    });
                                }
                            }
                        }
                    }
                }
            }

            if (text.includes('不動産')) {
                if (sumMatch) result.categories['real_estate'] = parseNum(sumMatch[1]);
            }

            if (text.includes('ポイント') || text.includes('マイル')) {
                if (sumMatch) result.categories['points'] = parseNum(sumMatch[1]);
            }
        });

        return result;
    }""")

    print(f"  -> 抽出結果: 総資産={data.get('total_value', 0):,.0f}円")
    print(f"     カテゴリ: {data.get('categories', {})}")
    print(f"     株式銘柄数: {len(data.get('stocks', []))}")
    print(f"     投信銘柄数: {len(data.get('funds', []))}")
    print(f"     現金明細数: {len(data.get('cash_details', []))}")

    # 構造化データを返却形式に変換
    holdings = []
    for s in data.get("stocks", []):
        holdings.append({
            "ticker": s.get("ticker", ""),
            "name": s.get("name", ""),
            "value": s.get("value", 0),
            "quantity": s.get("quantity", 0),
            "price": s.get("curPrice", 0),
            "gain_loss": s.get("gainLoss", 0),
            "gain_loss_pct": s.get("gainLossPct", ""),
            "broker": s.get("broker", ""),
        })

    funds = []
    for f in data.get("funds", []):
        funds.append({
            "name": f.get("name", ""),
            "value": f.get("value", 0),
            "quantity": f.get("quantity", 0),
            "nav": f.get("nav", 0),
            "gain_loss": f.get("gainLoss", 0),
            "gain_loss_pct": f.get("gainLossPct", ""),
            "broker": f.get("broker", ""),
        })

    cash_details = data.get("cash_details", [])
    categories = data.get("categories", {})

    cash_jpy = categories.get("cash", 0)
    stock_value = categories.get("stocks", 0)
    fund_value = categories.get("funds", 0)
    real_estate_value = categories.get("real_estate", 0)
    total_value = data.get("total_value", 0)

    # 合計が取れなかった場合は個別の合計から
    if total_value == 0:
        total_value = cash_jpy + stock_value + fund_value + real_estate_value

    return {
        "total_value": total_value,
        "cash_jpy": cash_jpy,
        "cash_usd": 0.0,  # VT ルール適用後に設定
        "stock_value": stock_value,
        "fund_value": fund_value,
        "real_estate_value": real_estate_value,
        "holdings": holdings,
        "funds": funds,
        "cash_details": cash_details,
    }


# ================================================================
# VT 特殊ルール
# ================================================================

def _apply_vt_rule(raw_data: dict, vt_exclude_shares: int) -> dict:
    """
    VT特殊ルール:
      VT（バンガード トータル ワールド ストックETF）のうち、
      指定株数分（478株）を株式から除外し、米ドル現金として再集計する。

      具体的な処理:
        - VT の保有が複数口座にある場合、478株を含む口座から差し引く
        - 差し引いた分の評価額を cash_usd に加算
    """
    holdings = raw_data["holdings"]
    adjusted_holdings = []
    vt_adjustment = 0.0
    remaining_exclude = vt_exclude_shares

    # VT の保有を数量降順でソート（最大保有口座から優先除外）
    vt_holdings = [(i, h) for i, h in enumerate(holdings)
                   if "VT" in h.get("ticker", "").upper()
                   or "バンガード トータル ワールド" in h.get("name", "")]
    vt_holdings.sort(key=lambda x: x[1].get("quantity", 0), reverse=True)

    excluded_indices = set()

    for idx, h in vt_holdings:
        if remaining_exclude <= 0:
            break

        qty = h.get("quantity", 0)
        if qty <= 0:
            continue

        price_per_share = h["value"] / qty if qty > 0 else 0

        if qty <= remaining_exclude:
            # この口座の VT を全て除外
            exclude_value = h["value"]
            vt_adjustment += exclude_value
            remaining_exclude -= qty
            excluded_indices.add(idx)
            print(
                f"  -> VT特殊ルール: {qty:.0f}株全て "
                f"({exclude_value:,.0f}円) を米ドル現金へ振替"
            )
        else:
            # 一部除外
            exclude_value = price_per_share * remaining_exclude
            vt_adjustment += exclude_value
            adjusted_h = {
                **h,
                "quantity": qty - remaining_exclude,
                "value": h["value"] - exclude_value,
            }
            holdings[idx] = adjusted_h
            remaining_exclude = 0
            print(
                f"  -> VT特殊ルール: {vt_exclude_shares}株 "
                f"({exclude_value:,.0f}円) を米ドル現金へ振替 "
                f"（残: {adjusted_h['quantity']:.0f}株）"
            )

    # 除外された VT を holdings から削除
    adjusted_holdings = [h for i, h in enumerate(holdings) if i not in excluded_indices]

    stock_value = raw_data["stock_value"] - vt_adjustment
    cash_usd = raw_data.get("cash_usd", 0) + vt_adjustment

    result = {
        **raw_data,
        "stock_value": stock_value,
        "cash_usd": cash_usd,
        "holdings": adjusted_holdings,
    }

    # total は再計算しない（マネーフォワードの総資産をそのまま使用）
    return result


# ================================================================
# ヘルパー
# ================================================================

def _parse_number(text: str) -> float:
    """テキストから数値を抽出する。"""
    if not text:
        return 0.0
    cleaned = text.replace("円", "").replace("¥", "").replace("$", "")
    cleaned = cleaned.replace(",", "").replace(" ", "").strip()
    match = re.search(r"-?[\d]+\.?\d*", cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return 0.0
    return 0.0
