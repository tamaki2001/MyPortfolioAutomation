"""
マネーフォワード ポートフォリオ スクレイパー
============================================
Playwright を使用して MoneyForward ME にログインし、
ポートフォリオページ (https://moneyforward.com/bs/portfolio) の
スクリーンショット撮影＋HTMLスクレイピングを行う。

特殊ルール:
  VT のうち VT_EXCLUDE_SHARES 株分を
  「株式」から除外し「米ドル現金」として集計し直す。

環境変数:
  MF_EMAIL    - マネーフォワード ログインメールアドレス
  MF_PASSWORD - マネーフォワード ログインパスワード
"""

import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, Page


# マネーフォワード URL
# メールログインは id.moneyforward.com/sign_in/email に直接アクセスする
# （moneyforward.com/sign_in → id.moneyforward.com へのリダイレクトを回避）
MF_LOGIN_URL = "https://id.moneyforward.com/sign_in/email"
MF_PORTFOLIO_URL = "https://moneyforward.com/bs/portfolio"

# 環境変数から認証情報を取得
MF_EMAIL = os.environ.get("MF_EMAIL", "")
MF_PASSWORD = os.environ.get("MF_PASSWORD", "")


def scrape_portfolio(
    screenshot_path: str,
    vt_exclude_shares: int = 478,
) -> dict:
    """
    マネーフォワード ME にログインし、ポートフォリオデータを取得する。

    Returns:
        {
            "total_value": float,           # 総資産額（円換算）
            "cash_jpy": float,              # 日本円 預金・現金
            "cash_usd": float,              # 米ドル換算現金（VT除外分含む）
            "stock_value": float,           # 株式時価合計（VT除外後）
            "holdings": [
                {"ticker": str, "name": str, "value": float,
                 "quantity": float, "price": float},
                ...
            ],
        }
    """
    Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ja-JP",
        )
        page = context.new_page()

        try:
            _login(page)
            _navigate_to_portfolio(page)

            # スクリーンショット撮影
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  -> スクリーンショット撮影完了: {screenshot_path}")

            # データ抽出
            raw_data = _extract_portfolio_data(page)

            # VT 特殊ルール適用
            portfolio = _apply_vt_rule(raw_data, vt_exclude_shares)

            return portfolio

        finally:
            context.close()
            browser.close()


# ================================================================
# ログイン処理
# ================================================================

def _login(page: Page) -> None:
    """
    マネーフォワード ME にログインする。

    ログインフロー（id.moneyforward.com）:
      1. /sign_in/email に直接アクセス（メールログイン画面）
      2. メールアドレス入力 → submitBtn クリック → 画面遷移
      3. パスワード入力 → submitBtn クリック → ログイン完了
    ※ メールとパスワードは別画面で入力する（2段階フォーム）
    """
    if not MF_EMAIL or not MF_PASSWORD:
        raise EnvironmentError(
            "環境変数 MF_EMAIL / MF_PASSWORD が設定されていません。"
        )

    # --- Step 1: メールログインページに直接アクセス ---
    print("  -> ログインページにアクセス中...")
    page.goto(MF_LOGIN_URL, wait_until="networkidle")
    page.wait_for_timeout(5000)

    # デバッグ: 現在のURL・ページ状態を出力
    print(f"  -> 現在のURL: {page.url}")

    # デバッグ: ページのスクリーンショットとHTML構造を保存
    page.screenshot(path="/tmp/debug_login_page.png")
    print("  -> デバッグ: ログインページスクリーンショット保存完了")

    # ページ内の全input要素を列挙（デバッグ用）
    inputs_info = page.evaluate("""() => {
        const inputs = document.querySelectorAll('input, button, [type="submit"]');
        return Array.from(inputs).map(el => ({
            tag: el.tagName,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            className: el.className || '',
            placeholder: el.placeholder || '',
            value: el.value || '',
            visible: el.offsetParent !== null
        }));
    }""")
    print(f"  -> ページ内のinput/button要素数: {len(inputs_info)}")
    for info in inputs_info:
        print(f"     {info}")

    # --- Step 2: メールアドレス入力 ---
    # 複数のセレクタを順番に試す
    email_selectors = [
        'input[name="mfid_user[email]"]',
        'input[type="email"]',
        'input[type="text"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
        'input:not([type="hidden"]):not([type="submit"])',
    ]

    email_input = None
    for selector in email_selectors:
        try:
            email_input = page.wait_for_selector(selector, timeout=5000)
            if email_input:
                print(f"  -> メール入力欄を発見: {selector}")
                break
        except Exception:
            print(f"  -> セレクタ不一致: {selector}")
            continue

    if not email_input:
        page.screenshot(path="/tmp/login_failed_no_email_input.png")
        raise RuntimeError(
            f"メールアドレス入力欄が見つかりません。"
            f"現在のURL: {page.url} / "
            f"ページ内要素: {inputs_info}"
        )
    email_input.fill(MF_EMAIL)
    print("  -> メールアドレス入力完了")

    # 「ログインする」ボタン（class="submitBtn"）
    submit_btn = page.wait_for_selector(
        '.submitBtn, '
        'input[type="submit"], '
        'button[type="submit"]',
        timeout=10000,
    )
    submit_btn.click()
    print("  -> メールアドレス送信、パスワード画面を待機中...")

    # パスワード画面への遷移を待つ
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)

    # --- Step 3: パスワード入力（別画面） ---
    password_input = page.wait_for_selector(
        'input[name="mfid_user[password]"]',
        timeout=15000,
    )
    if not password_input:
        password_input = page.wait_for_selector(
            'input[type="password"]',
            timeout=10000,
        )
    password_input.fill(MF_PASSWORD)
    print("  -> パスワード入力完了")

    # 「ログインする」ボタン
    login_btn = page.wait_for_selector(
        '.submitBtn, '
        'input[type="submit"], '
        'button[type="submit"]',
        timeout=10000,
    )
    login_btn.click()

    # ログイン完了を待つ（moneyforward.com にリダイレクトされるまで）
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)

    # デバッグ: ログイン後のURL
    print(f"  -> ログイン後のURL: {page.url}")

    # ログイン成功確認
    # id.moneyforward.com の sign_in ページに留まっている場合は失敗
    if "sign_in" in page.url and "id.moneyforward.com" in page.url:
        # スクリーンショットを撮ってデバッグ用に保存
        page.screenshot(path="/tmp/login_failed.png")
        raise RuntimeError(
            f"マネーフォワードへのログインに失敗しました。"
            f"現在のURL: {page.url} / "
            f"認証情報を確認してください。"
        )
    print("  -> マネーフォワード ログイン成功")


def _navigate_to_portfolio(page: Page) -> None:
    """ポートフォリオページ (/bs/portfolio) に遷移する。"""
    page.goto(MF_PORTFOLIO_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)

    # ページが正しく読み込まれたか確認
    if "/bs/portfolio" not in page.url:
        raise RuntimeError(
            f"ポートフォリオページへの遷移に失敗しました。"
            f"現在のURL: {page.url}"
        )
    print("  -> ポートフォリオページ表示完了")


# ================================================================
# データ抽出
# ================================================================

def _extract_portfolio_data(page: Page) -> dict:
    """
    マネーフォワードのポートフォリオページからデータを抽出する。

    ページ構造 (2024-2026年時点の典型的なレイアウト):
      - 資産総額が上部に表示
      - 資産クラスごとのセクション（株式・投信、預金・現金 等）
      - 各セクション内にテーブルで銘柄一覧

    セクション例:
      - 株式（現物）/ 投資信託
      - 預金・現金・暗号資産
      - 年金
      - ポイント
    """
    holdings = []

    # --- 全テーブルから保有銘柄を抽出 ---
    # マネーフォワードのポートフォリオは section ごとにテーブルがある
    tables = page.query_selector_all("table.table-bordered, table.table-striped, table")

    for table in tables:
        rows = table.query_selector_all("tbody tr")
        for row in rows:
            holding = _parse_mf_holding_row(row)
            if holding:
                holdings.append(holding)

    # テーブルから取得できなかった場合、別のセレクタを試す
    if not holdings:
        holdings = _extract_holdings_fallback(page)

    # --- 現金・預金 抽出 ---
    cash_jpy = _extract_cash_deposits(page)
    cash_usd = 0.0  # マネーフォワードでは外貨は個別に抽出

    # --- 株式合計 ---
    stock_value = sum(h["value"] for h in holdings)

    # --- 総資産はページ上部から取得を試みる ---
    total_from_page = _extract_total_assets(page)
    total_value = total_from_page if total_from_page > 0 else (stock_value + cash_jpy)

    return {
        "total_value": total_value,
        "cash_jpy": cash_jpy,
        "cash_usd": cash_usd,
        "stock_value": stock_value,
        "holdings": holdings,
    }


def _parse_mf_holding_row(row) -> dict | None:
    """
    マネーフォワードの保有銘柄テーブル行をパースする。

    典型的なカラム構成:
      銘柄名 | 保有数 | 取得単価 | 現在値 | 評価額 | 損益 | 損益率
    ※カラム数・順序はセクションにより異なる場合がある
    """
    cells = row.query_selector_all("td")
    if len(cells) < 3:
        return None

    try:
        # 最初のセルは銘柄名（リンクを含む場合がある）
        name_elem = cells[0].query_selector("a") or cells[0]
        name_text = (name_elem.inner_text() or "").strip()
        if not name_text:
            return None

        # 数値セルを右から走査して評価額を特定
        # マネーフォワードでは「評価額」が最も重要
        values = []
        for cell in cells[1:]:
            text = cell.inner_text().strip()
            num = _parse_number(text)
            values.append(num)

        if not values:
            return None

        # ティッカー抽出
        ticker = _extract_ticker(name_text)

        # 評価額の推定: 通常、数値が大きいものが評価額
        # ヒューリスティック: 値が最大のものを評価額とする
        # ただし、保有数（小さい数値）と評価額（大きい数値）を区別
        quantity = 0.0
        price = 0.0
        value = 0.0

        if len(values) >= 4:
            # 保有数, 取得単価, 現在値, 評価額 ... のパターン
            quantity = values[0]
            price = values[2] if values[2] > 0 else values[1]
            # 評価額は通常3番目か4番目の大きな数値
            value = values[3] if len(values) > 3 and values[3] > values[0] else max(values[1:])
        elif len(values) >= 2:
            quantity = values[0]
            value = values[-1]
        elif len(values) == 1:
            value = values[0]

        # 評価額が0以下なら無視
        if value <= 0:
            return None

        # 明らかに金額でない小さい値（損益率等）をフィルタ
        if value < 100:
            return None

        return {
            "ticker": ticker,
            "name": name_text,
            "value": value,
            "quantity": quantity,
            "price": price,
        }

    except (ValueError, IndexError):
        return None


def _extract_holdings_fallback(page: Page) -> list[dict]:
    """
    テーブルから取得できなかった場合のフォールバック。
    マネーフォワードのポートフォリオセクション内の
    個別要素を直接探索する。
    """
    holdings = []

    # セクションごとの保有銘柄リスト
    sections = page.query_selector_all(
        "section.bs-portfolio, "
        "div.portfolio-section, "
        "div[class*='portfolio'], "
        "div[class*='holding']"
    )

    for section in sections:
        items = section.query_selector_all(
            "div.portfolio-item, "
            "li.holding-item, "
            "tr"
        )
        for item in items:
            name_el = item.query_selector(
                "a, span.name, div.name, td:first-child"
            )
            value_el = item.query_selector(
                "span.value, span.amount, "
                "td:last-child, span[class*='price']"
            )
            if name_el and value_el:
                name = name_el.inner_text().strip()
                value = _parse_number(value_el.inner_text())
                if name and value > 100:
                    holdings.append({
                        "ticker": _extract_ticker(name),
                        "name": name,
                        "value": value,
                        "quantity": 0,
                        "price": 0,
                    })

    return holdings


def _extract_total_assets(page: Page) -> float:
    """
    ページ上部の総資産額を抽出する。
    マネーフォワードのポートフォリオページ上部に表示される数値。
    """
    selectors = [
        "div.heading-radius-box h1",
        "div.total-assets",
        "span.total-amount",
        "div.bs-total-assets",
        "h1.heading-small",
        # 「資産総額」ラベルの隣の数値
        "div.heading-radius-box",
    ]

    for selector in selectors:
        elem = page.query_selector(selector)
        if elem:
            text = elem.inner_text()
            value = _parse_number(text)
            if value > 10000:  # 1万円以上なら妥当
                return value

    return 0.0


def _extract_cash_deposits(page: Page) -> float:
    """
    預金・現金カテゴリの合計額を抽出する。

    マネーフォワードのポートフォリオページでは
    「預金・現金・暗号資産」セクションに現金残高が表示される。
    """
    # セクションヘッダーから「預金」セクションを特定
    headers = page.query_selector_all(
        "h2, h3, div.heading, "
        "section header, th"
    )

    for header in headers:
        text = header.inner_text().strip()
        if "預金" in text or "現金" in text:
            # ヘッダーと同じ行、または隣接する要素から金額を取得
            parent = header.query_selector("xpath=..")
            if parent:
                # 金額要素を探す
                amount_el = parent.query_selector(
                    "span.amount, span.value, td + td"
                )
                if amount_el:
                    return _parse_number(amount_el.inner_text())

                # ヘッダーテキスト内に金額がある場合
                val = _parse_number(text)
                if val > 10000:
                    return val

    # フォールバック: テーブルから「預金」行を探す
    rows = page.query_selector_all("tr")
    for row in rows:
        row_text = row.inner_text()
        if "預金" in row_text or "普通預金" in row_text or "現金" in row_text:
            cells = row.query_selector_all("td")
            for cell in reversed(cells):
                val = _parse_number(cell.inner_text())
                if val > 10000:
                    return val

    return 0.0


# ================================================================
# ヘルパー関数
# ================================================================

def _extract_ticker(text: str) -> str:
    """
    銘柄名テキストからティッカーシンボルを抽出する。

    マネーフォワードの銘柄名パターン例:
      - "バンガード トータル ワールド ストックETF (VT)"
      - "イーライリリー (LLY)"
      - "インテュイティヴ・サージカル (ISRG)"
      - "eMAXIS Slim 全世界株式（オール・カントリー）"
      - "三菱UFJフィナンシャル・グループ (8306)"
    """
    # 括弧内の英字ティッカーを探す: (VT), [LLY] など
    match = re.search(r"[(\[（]([A-Z]{1,5})[)\]）]", text)
    if match:
        return match.group(1)

    # 括弧内の数字コード（日本株）: (8306) など
    match = re.search(r"[(\[（](\d{4,5})[)\]）]", text)
    if match:
        return match.group(1)

    # テキスト内の独立した英字列
    match = re.search(r"\b([A-Z]{1,5})\b", text)
    if match:
        return match.group(1)

    # 投資信託などティッカーがない場合は名前の先頭部分
    # 長い名前は短縮
    short = text[:20].strip()
    return short


def _parse_number(text: str) -> float:
    """
    テキストから数値を抽出する。

    マネーフォワードの数値表記:
      - "1,234,567円"
      - "¥1,234,567"
      - "-12,345"
      - "1,234.56"
      - "12,345 円"
    """
    if not text:
        return 0.0

    # 「円」「¥」「$」を除去し、カンマも除去
    cleaned = text.replace("円", "").replace("¥", "").replace("$", "")
    cleaned = cleaned.replace(",", "").replace(" ", "").strip()

    # 数値部分のみ抽出（マイナス記号と小数点を許容）
    match = re.search(r"-?[\d]+\.?\d*", cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return 0.0

    return 0.0


def _apply_vt_rule(raw_data: dict, vt_exclude_shares: int) -> dict:
    """
    VT特殊ルール:
      VT の指定株数分を株式から除外し、米ドル現金として再集計する。

      VT の1株あたり単価 × 除外株数分を:
        - holdings の VT の value / quantity から差し引く
        - cash_usd に加算する
    """
    holdings = raw_data["holdings"]
    adjusted_holdings = []
    vt_adjustment = 0.0

    for h in holdings:
        if h["ticker"] == "VT" and h["quantity"] > vt_exclude_shares:
            # 1株あたり単価
            price_per_share = h["value"] / h["quantity"] if h["quantity"] > 0 else 0
            exclude_value = price_per_share * vt_exclude_shares

            adjusted = {
                **h,
                "quantity": h["quantity"] - vt_exclude_shares,
                "value": h["value"] - exclude_value,
            }
            adjusted_holdings.append(adjusted)
            vt_adjustment = exclude_value
            print(
                f"  -> VT特殊ルール適用: {vt_exclude_shares}株 "
                f"({exclude_value:,.0f}円) を米ドル現金へ振替"
            )
        else:
            adjusted_holdings.append(h)

    stock_value = sum(h["value"] for h in adjusted_holdings)
    cash_usd = raw_data["cash_usd"] + vt_adjustment

    return {
        "total_value": stock_value + raw_data["cash_jpy"] + cash_usd,
        "cash_jpy": raw_data["cash_jpy"],
        "cash_usd": cash_usd,
        "stock_value": stock_value,
        "holdings": adjusted_holdings,
    }
