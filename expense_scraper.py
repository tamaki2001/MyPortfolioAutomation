"""
MoneyForward 家計簿（支出）スクレイパー
=========================================
家計簿ページから月次支出を取得する。

対象URL:
  https://moneyforward.com/cf?month=YYYY/MM   月次の収支一覧

取得データ:
  {
    "year_month": "2026-04",
    "total_amount": 1234567,         # 月次支出合計
    "categories": [
      {"name": "食費", "amount": 80000},
      {"name": "住居", "amount": 150000},
      ...
    ],
    "raw_text": "...",                # ページ全文（デバッグ用）
  }

認証は scraper.py と共通（MF_COOKIES 環境変数 or cookies.json）。
"""

import json
import os
import re
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

from scraper import _load_cookies, _inject_cookies, _save_updated_cookies, SAMESITE_MAP


MF_CF_URL = "https://moneyforward.com/cf"


def scrape_monthly_expense(
    target_year_month: str | None = None,
    screenshot_path: str | None = None,
) -> dict:
    """
    指定月（YYYY-MM）の支出データを取得する。
    target_year_month が None の場合は前月を対象とする。
    MF は URL での月指定を受け付けないため、UI の◄ボタンで月を戻す。
    """
    if target_year_month is None:
        target_year_month = _previous_month()

    if screenshot_path:
        Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        try:
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                delete window.__playwright;
                delete window.__pw_manual;
            """)

            cookies = _load_cookies()
            _inject_cookies(context, cookies)

            page = context.new_page()
            print(f"  -> 家計簿ページにアクセス: {MF_CF_URL}")
            page.goto(MF_CF_URL, wait_until="domcontentloaded", timeout=60000)

            # ログイン確認
            if "/sign_in" in page.url or "/users/sign_in" in page.url:
                raise RuntimeError("MoneyForwardの認証に失敗（Cookieが期限切れの可能性）")

            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(2000)

            # 表示月を target_year_month まで巻き戻す（◄ボタン）
            _navigate_to_month(page, target_year_month)

            if screenshot_path:
                page.screenshot(path=screenshot_path, full_page=True)
                print(f"  -> スクリーンショット撮影: {screenshot_path}")

            # データ抽出
            result = _extract_expense_data(page, target_year_month)

            _save_updated_cookies(context)
            return result

        finally:
            context.close()
            browser.close()


def _previous_month() -> str:
    """直近の完了月を YYYY-MM で返す。"""
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}-12"
    return f"{today.year}-{today.month - 1:02d}"


def _navigate_to_month(page: Page, target_year_month: str) -> None:
    """◄ボタンを必要回数クリックして指定月まで戻す。"""
    MAX_CLICKS = 24

    for attempt in range(MAX_CLICKS):
        current_ym = _wait_for_displayed_month(page)
        if current_ym == target_year_month:
            print(f"  -> 表示月を {target_year_month} に合わせました")
            return
        if current_ym is None:
            # 検出失敗だが、後段の抽出フォールバックがあるので警告のみ
            print("  -> [INFO] 表示月の検出に失敗（後段フォールバックに任せます）")
            return

        # 月差を計算
        cur_y, cur_m = map(int, current_ym.split("-"))
        tgt_y, tgt_m = map(int, target_year_month.split("-"))
        diff = (cur_y - tgt_y) * 12 + (cur_m - tgt_m)
        if diff <= 0:
            return

        if not _click_prev_month(page):
            print("  -> [WARN] 前月ボタンが見つかりませんでした")
            return

        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(800)

    print(f"  -> [WARN] {MAX_CLICKS}回試行しても{target_year_month}に到達できませんでした")


def _wait_for_displayed_month(page: Page, max_attempts: int = 8) -> str | None:
    """日付範囲のテキストが現れるまで待ち、表示月を返す。"""
    for _ in range(max_attempts):
        text = page.inner_text("body")
        ym = _extract_displayed_month(text)
        if ym:
            return ym
        page.wait_for_timeout(400)
    return None


def _click_prev_month(page: Page) -> bool:
    """前月ボタン（◄）をクリックする。複数の戦略を試行。"""

    # 戦略1: Playwright の text= ロケーター（◄ を厳密にマッチ）
    try:
        page.locator("text=◄").first.click(timeout=2500)
        return True
    except Exception:
        pass

    # 戦略2: <a> 要素の中の ◄
    try:
        page.locator("a", has_text="◄").first.click(timeout=2500)
        return True
    except Exception:
        pass

    # 戦略3: JavaScript で全要素を走査して◄を含む clickable を探す
    try:
        clicked = page.evaluate("""() => {
            const els = document.querySelectorAll('a, button, [onclick], [role="button"]');
            for (const el of els) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t === '◄' || t === '◄') {
                    el.click();
                    return true;
                }
            }
            // ◄ を含む要素の親をクリック（アイコンが span 内などにある場合）
            const candidates = document.querySelectorAll('*');
            for (const el of candidates) {
                if ((el.innerText || el.textContent || '').trim() === '◄') {
                    let p = el;
                    while (p && p !== document.body) {
                        if (p.onclick || p.tagName === 'A' || p.tagName === 'BUTTON') {
                            p.click();
                            return true;
                        }
                        p = p.parentElement;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            return True
    except Exception:
        pass

    return False


def _extract_expense_data(page: Page, requested_year_month: str) -> dict:
    """
    家計簿ページから支出合計とカテゴリ別内訳を抽出する。
    """
    raw_text = page.inner_text("body")

    # 表示中の年月を検出（MF側で月切替がうまくいかなかった場合に備える）
    displayed_year_month = _extract_displayed_month(raw_text) or requested_year_month
    if displayed_year_month != requested_year_month:
        print(f"  -> [WARN] 要求 {requested_year_month} に対し表示は {displayed_year_month}")

    # 支出合計を抽出
    total = _extract_total_expense(raw_text)
    print(f"  -> 当月支出: ¥{total:,}")

    # カテゴリ別内訳（このページでは取れない場合があるので空でOK）
    categories = _extract_categories_dom(page) or _extract_categories_from_text(raw_text)
    if categories:
        print(f"  -> カテゴリ {len(categories)} 件取得")

    return {
        "year_month": displayed_year_month,
        "total_amount": total,
        "categories": categories,
        "raw_text": raw_text[:5000],
    }


def _extract_displayed_month(text: str) -> str | None:
    """ページ上部の「2026/4/1 - 2026/4/30」表記から年月を抽出する。"""
    m = re.search(r"(\d{4})/(\d{1,2})/\d{1,2}\s*-\s*\d{4}/\d{1,2}/\d{1,2}", text)
    if m:
        year, month = m.group(1), m.group(2)
        return f"{year}-{int(month):02d}"
    return None


def _extract_total_expense(text: str) -> int:
    """
    支出合計を抽出する。MFは
      当月収入  当月支出  当月収支
      105,210円 ―  6,698,245円 ＝ -6,593,035円
    の形式で出すので、「数字円 ― 数字円 ＝」のパターンで2番目を取る。
    """
    # 主パターン: NUM円 ― NUM円 ＝ （収入 ― 支出 ＝ 収支）
    m = re.search(
        r"([\d,]+)\s*円\s*[―ー－\-]\s*([\d,]+)\s*円\s*[＝=]",
        text,
        re.DOTALL,
    )
    if m:
        return _parse_yen(m.group(2))

    # フォールバック
    for pattern in [
        r"支出合計[^\d]{0,20}([\d,]+)",
        r"月次支出[^\d]{0,20}([\d,]+)",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            return _parse_yen(m.group(1))
    return 0


def _extract_categories_dom(page: Page) -> list[dict]:
    """
    DOMから「大項目」のカテゴリ別支出を抽出する。
    MFの家計簿ページは大カテゴリの集計テーブルを持つ。
    """
    categories: list[dict] = []
    try:
        # カテゴリ別集計テーブル候補（複数のセレクタを試行）
        selectors = [
            "table.cf-detail-table tr",
            "table.summary-table tr",
            "section.expense-summary tr",
            "div[class*='category'] tr",
        ]
        for sel in selectors:
            rows = page.query_selector_all(sel)
            if not rows:
                continue
            for row in rows:
                cells = row.query_selector_all("td, th")
                if len(cells) < 2:
                    continue
                name = (cells[0].inner_text() or "").strip()
                amount_str = (cells[-1].inner_text() or "").strip()
                amount = _parse_yen(amount_str)
                if name and amount > 0 and not _is_header(name):
                    categories.append({"name": name, "amount": amount})
            if categories:
                break
    except Exception as e:
        print(f"  -> [WARN] DOM抽出失敗: {e}")

    return categories


def _is_header(text: str) -> bool:
    """ヘッダー行や合計行をフィルタ。"""
    headers = {"カテゴリ", "支出", "収入", "金額", "合計", "総合計"}
    return text in headers


def _extract_categories_from_text(text: str) -> list[dict]:
    """
    フォールバック: テキストから「カテゴリ名 ¥金額」のパターンを抽出。
    日本語カテゴリ名 + 円表記を緩めに検出する。
    """
    categories: list[dict] = []
    pattern = re.compile(r"([一-龥ぁ-んァ-ヶー・]{2,10})\s*[\¥￥]?\s*([\d,]+)\s*円")
    seen = set()
    for m in pattern.finditer(text):
        name = m.group(1)
        amount = _parse_yen(m.group(2))
        if name in seen or amount < 100:
            continue
        # 既知のカテゴリ名にマッチするもののみ採用
        if name in _KNOWN_CATEGORIES:
            categories.append({"name": name, "amount": amount})
            seen.add(name)
    return categories


_KNOWN_CATEGORIES = {
    "食費", "日用品", "趣味", "交際費", "交通費", "衣服",
    "美容", "医療", "教養", "通信", "光熱費", "住居",
    "保険", "教育", "自動車", "ペット", "現金", "未分類",
    "税・社会保険", "特別な支出", "その他",
}


def _parse_yen(s: str) -> int:
    """「1,234,567」「¥1,234,567」「-1,234」など → 1234567 の絶対値"""
    cleaned = re.sub(r"[¥￥,円\s]", "", s)
    if not cleaned:
        return 0
    try:
        return abs(int(cleaned))
    except ValueError:
        return 0
