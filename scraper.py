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
        # ボット検出を回避するためのブラウザ設定
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        # 実ブラウザと同じ User-Agent・ヘッダーを設定
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        try:
            # navigator.webdriver を隠蔽（ボット検出回避）
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                // Playwright 検出用のプロパティを除去
                delete window.__playwright;
                delete window.__pw_manual;
            """)

            # Cookie 注入
            cookies = _load_cookies()
            _inject_cookies(context, cookies)
            page = context.new_page()

            # ポートフォリオページにアクセス
            _navigate_to_portfolio(page)

            # ─────────────────────────────────────────────────────────
            # ページ全体をスクロールして AJAX データを完全ロード
            # wait_for_load_state("networkidle") で非同期リクエスト完了まで待機
            # ─────────────────────────────────────────────────────────
            print("  -> ページスクロール中（AJAX遅延読み込み対応）...")
            page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const step = 600;
                let lastH = 0;
                for (let i = 0; i < 30; i++) {
                    const h = document.body.scrollHeight;
                    window.scrollTo(0, Math.min(step * i, h));
                    await delay(300);
                    if (h === lastH && i > 5) break;
                    lastH = h;
                }
                window.scrollTo(0, document.body.scrollHeight);
                await delay(1500);
                window.scrollTo(0, 0);
            }""")
            # AJAX完了を待つ
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(2000)

            # 株式テーブルの実データ行が出現するまで待機
            # （ヘッダー行のみの場合はまだ未ロード → タイムアウトしてもスキップ）
            print("  -> 株式テーブルのデータ行を待機中...")
            try:
                # 株式テーブルの2行目以降（実データ）を待つ
                page.wait_for_selector(
                    "table tr:has(td)",   # td を持つ tr＝データ行
                    timeout=15000,
                )
                print("  -> テーブルデータ行の出現を確認")
            except Exception:
                print("  -> [WARN] テーブルデータ行タイムアウト（ページ構造を確認）")

            # スクリーンショット撮影
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  -> スクリーンショット撮影完了: {screenshot_path}")

            # ─────────────────────────────────────────────────────────
            # Playwright DOM API でテーブル行を直接抽出
            # inner_text / HTMLパース より確実
            # ─────────────────────────────────────────────────────────
            raw_data = _extract_via_playwright(page)

            # フォールバック: DOM抽出に失敗した場合はテキストベース
            if not raw_data.get("holdings") and not raw_data.get("funds"):
                print("  -> Playwright DOM抽出失敗、テキストベースにフォールバック")
                asset_text = page.inner_text("body")
                raw_data = _extract_from_text(asset_text)

            if "asset_text" not in raw_data:
                raw_data["asset_text"] = page.inner_text("body")

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
# データ抽出（Playwright DOM API — 最も確実な方式）
# ================================================================

def _extract_via_playwright(page: Page) -> dict:
    """
    Playwright の page.evaluate() で DOM を直接走査してデータを抽出する。

    戦略:
      1. 全セクション見出しを探し、その直後のテーブルを対象とする
      2. セクション名（"株式（現物）" 等）でテーブルを分類
      3. 各テーブルの tr > td を配列として取得
    """
    result = page.evaluate("""() => {
        function num(t) {
            if (!t) return 0;
            const s = t.replace(/[円,\\s]/g, '').match(/-?[\\d.]+/);
            return s ? parseFloat(s[0]) : 0;
        }

        const out = {
            total_value: 0,
            categories: {},
            stocks: [],
            funds: [],
            cash: [],
        };

        // ─── 総資産 ───
        const bodyText = document.body.innerText;
        const totalM = bodyText.match(/資産総額[：:\\s]+([\\d,]+)円/);
        if (totalM) out.total_value = num(totalM[1]);

        // ─── カテゴリ合計 ───
        const catMap = {
            cash:         /預金・現金・暗号資産[^\\n]*?([\\d,]+)円/,
            stocks:       /株式（現物）[^\\n]*?([\\d,]+)円/,
            funds:        /投資信託[^\\n]*?([\\d,]+)円/,
            real_estate:  /不動産[^\\n]*?([\\d,]+)円/,
            points:       /ポイント・マイル[^\\n]*?([\\d,]+)円/,
        };
        for (const [k, re] of Object.entries(catMap)) {
            const m = bodyText.match(re);
            if (m) out.categories[k] = num(m[1]);
        }

        // ─── 全セクションを走査 ───
        // セクション = heading + 直後のテーブル の組み合わせ
        //
        // DOM 構造（マネーフォワード 2026年3月時点）:
        //   <section>
        //     <h2>株式（現物）</h2>
        //     <div>
        //       <h3>合計：XXX円</h3>
        //       <table>
        //         <thead><tr><th>銘柄コード</th>...</tr></thead>
        //         <tbody><tr><td>AVGO</td>...</tr></tbody>
        //       </table>
        //     </div>
        //   </section>

        let currentType = '';

        // 全要素を順番に走査してセクション → テーブルの関係を把握
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_ELEMENT,
        );

        let node = walker.nextNode();
        while (node) {
            const tag = node.tagName;
            const text = (node.textContent || '').trim();

            // セクション見出しを検出（マネーフォワードは H1 タグを使用）
            if (tag === 'H1' || tag === 'H2' || tag === 'H3') {
                if (text.includes('預金') || text.includes('現金')) {
                    currentType = 'cash';
                } else if (text.includes('株式') && text.includes('現物')) {
                    currentType = 'stocks';
                } else if (text.includes('投資信託')) {
                    currentType = 'funds';
                } else if (text.includes('不動産')) {
                    currentType = 'real_estate';
                } else if (text.includes('ポイント')) {
                    currentType = 'points';
                }
            }

            // テーブルを処理
            if (tag === 'TABLE' && currentType) {
                const rows = Array.from(node.querySelectorAll('tbody tr'));

                rows.forEach(row => {
                    const cells = Array.from(row.querySelectorAll('td'))
                                       .map(td => td.innerText.trim());
                    if (cells.length < 2) return;

                    if (currentType === 'stocks' && cells.length >= 6) {
                        // 株式: コード, 名前, 保有数, 取得単価, 現在値, 評価額, ...
                        const ticker = cells[0];
                        const name   = cells[1];
                        const qty    = num(cells[2]);
                        const price  = num(cells[4]);
                        const val    = num(cells[5]);
                        const gl     = cells.length > 7 ? num(cells[7]) : 0;
                        const glpct  = cells.length > 8 ? cells[8] : '';
                        const broker = cells.length > 9 ? cells[9] : '';

                        if (val > 0) {
                            out.stocks.push({
                                ticker, name, quantity: qty,
                                price, value: val,
                                gain_loss: gl, gain_loss_pct: glpct, broker
                            });
                        }
                    }

                    else if (currentType === 'funds' && cells.length >= 5) {
                        // 投信: 名前, 口数, 取得単価, 基準価額, 評価額, ...
                        const name  = cells[0];
                        const qty   = num(cells[1]);
                        const nav   = num(cells[3]);
                        const val   = num(cells[4]);
                        const gl    = cells.length > 6 ? num(cells[6]) : 0;
                        const glpct = cells.length > 7 ? cells[7] : '';
                        const broker= cells.length > 8 ? cells[8] : '';

                        if (val > 0) {
                            out.funds.push({
                                name, quantity: qty, nav, value: val,
                                gain_loss: gl, gain_loss_pct: glpct, broker
                            });
                        }
                    }

                    else if (currentType === 'cash' && cells.length >= 2) {
                        // 現金: 名称, 残高, 保有金融機関
                        const name   = cells[0];
                        const val    = num(cells[1]);
                        const broker = cells.length > 2 ? cells[2] : '';
                        if (val > 0 && name) {
                            out.cash.push({ name, value: val, broker });
                        }
                    }
                });
            }

            node = walker.nextNode();
        }

        return out;
    }""")

    stocks = result.get("stocks", [])
    funds  = result.get("funds", [])
    cash   = result.get("cash", [])
    cats   = result.get("categories", {})

    print(f"  -> Playwright DOM抽出: 株式={len(stocks)}, 投信={len(funds)}, 現金={len(cash)}")
    for h in stocks:
        print(f"     [{h['ticker']}] {h['name']}: {h['value']:,.0f}円 ({h['quantity']}株) @{h['broker']}")
    for f in funds[:3]:  # 投信は長名称なので先頭3件のみ
        print(f"     [投信] {f['name'][:40]}: {f['value']:,.0f}円")

    holdings = [
        {
            "ticker":        s.get("ticker", ""),
            "name":          s.get("name", ""),
            "value":         s.get("value", 0),
            "quantity":      s.get("quantity", 0),
            "price":         s.get("price", 0),
            "gain_loss":     s.get("gain_loss", 0),
            "gain_loss_pct": s.get("gain_loss_pct", ""),
            "broker":        s.get("broker", ""),
        }
        for s in stocks
    ]
    fund_list = [
        {
            "name":          f.get("name", ""),
            "value":         f.get("value", 0),
            "quantity":      f.get("quantity", 0),
            "nav":           f.get("nav", 0),
            "gain_loss":     f.get("gain_loss", 0),
            "gain_loss_pct": f.get("gain_loss_pct", ""),
            "broker":        f.get("broker", ""),
        }
        for f in funds
    ]

    total = result.get("total_value", 0)
    if total == 0:
        total = sum(cats.values())

    return {
        "total_value":       total,
        "cash_jpy":          cats.get("cash", 0),
        "cash_usd":          0.0,
        "stock_value":       cats.get("stocks", 0),
        "fund_value":        cats.get("funds", 0),
        "real_estate_value": cats.get("real_estate", 0),
        "holdings":          holdings,
        "funds":             fund_list,
        "cash_details":      cash,
    }


# ================================================================
# データ抽出（HTML ベース — テーブルの td 要素から直接パース）
# ================================================================

def _extract_from_html(html: str) -> dict:
    """
    HTMLソースからテーブルデータを正規表現で直接抽出する。

    マネーフォワードのポートフォリオページのHTML構造:
      - <td> タグの中に各セルの値が入っている
      - テーブルは資産カテゴリごとにセクション分けされている
      - 株式テーブル: 銘柄コード, 銘柄名, 保有数, 取得単価, 現在値, 評価額, ...
      - 投信テーブル: 銘柄名, 保有口数, 取得単価, 基準価額, 評価額, ...
    """
    from html.parser import HTMLParser

    # --- 簡易HTML→テキスト変換（タグを除去し、td/trの区切りを保持）---
    class TableExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.sections = []  # [(section_name, [rows])]
            self.current_section = ""
            self.current_row = []
            self.current_cell = ""
            self.in_td = False
            self.in_th = False
            self.in_heading = False
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("h2", "h3"):
                self.in_heading = True
                self.current_cell = ""
            elif tag == "td":
                self.in_td = True
                self.current_cell = ""
            elif tag == "th":
                self.in_th = True
                self.current_cell = ""
            elif tag == "tr":
                self.current_row = []

        def handle_endtag(self, tag):
            if tag in ("h2", "h3"):
                self.in_heading = False
                heading_text = self.current_cell.strip()
                if heading_text:
                    self.current_section = heading_text
            elif tag == "td":
                self.in_td = False
                self.current_row.append(self.current_cell.strip())
            elif tag == "th":
                self.in_th = False
            elif tag == "tr":
                if self.current_row:
                    self.sections.append((self.current_section, self.current_row))
                self.current_row = []

        def handle_data(self, data):
            if self.in_td or self.in_heading:
                self.current_cell += data

    parser = TableExtractor()
    parser.feed(html)

    # --- 各セクションのデータを構造化 ---
    total_value = 0.0
    categories = {}
    holdings = []
    funds = []
    cash_details = []

    # 総資産を抽出
    m = re.search(r"資産総額[：:\s]+([\d,]+)円", html)
    if m:
        total_value = _parse_number(m.group(1))

    # カテゴリ合計を抽出（「資産の内訳」テーブル）
    cat_patterns = [
        ("cash", r"預金・現金・暗号資産[^<]*?([\d,]+)円"),
        ("stocks", r"株式（現物）[^<]*?([\d,]+)円"),
        ("funds", r"投資信託[^<]*?([\d,]+)円"),
        ("real_estate", r"不動産[^<]*?([\d,]+)円"),
        ("points", r"ポイント・マイル[^<]*?([\d,]+)円"),
    ]
    for key, pattern in cat_patterns:
        m_cat = re.search(pattern, html)
        if m_cat:
            categories[key] = _parse_number(m_cat.group(1))

    # --- セクションごとにテーブル行をパース ---
    current_section_type = ""

    for section_name, row_cells in parser.sections:
        # セクション判定
        if "預金" in section_name or "現金" in section_name:
            current_section_type = "cash"
        elif "株式" in section_name and "現物" in section_name:
            current_section_type = "stocks"
        elif "投資信託" in section_name:
            current_section_type = "funds"
        elif "不動産" in section_name:
            current_section_type = "real_estate"
        elif "ポイント" in section_name:
            current_section_type = "points"

        # --- 株式テーブル行 ---
        # 列: 銘柄コード, 銘柄名, 保有数, 平均取得単価, 現在値, 評価額, 前日比, 評価損益, 評価損益率, 保有金融機関
        if current_section_type == "stocks" and len(row_cells) >= 6:
            # ヘッダー行をスキップ（数値でない行）
            ticker = row_cells[0].strip()
            name = row_cells[1].strip() if len(row_cells) > 1 else ""

            # ティッカーが英字/数字で始まるか確認
            if not ticker or ticker in ("銘柄コード", "種類・名称"):
                continue

            try:
                quantity = _parse_number(row_cells[2]) if len(row_cells) > 2 else 0
                avg_cost = _parse_number(row_cells[3]) if len(row_cells) > 3 else 0
                cur_price = _parse_number(row_cells[4]) if len(row_cells) > 4 else 0
                value = _parse_number(row_cells[5]) if len(row_cells) > 5 else 0
                gain_loss = _parse_number(row_cells[7]) if len(row_cells) > 7 else 0
                gain_loss_pct = row_cells[8].strip() if len(row_cells) > 8 else ""
                broker = row_cells[9].strip() if len(row_cells) > 9 else ""

                if value > 0:
                    # VT等ティッカーなしの場合、名前からティッカーを推定
                    if not re.match(r'^[A-Z0-9]{1,5}$', ticker):
                        # ティッカーが名前に埋もれているケース
                        if "バンガード" in ticker or "バンガード" in name:
                            name = ticker + " " + name if name else ticker
                            ticker = "VT"
                        else:
                            name = ticker + " " + name if name else ticker
                            ticker = name[:5]

                    holdings.append({
                        "ticker": ticker,
                        "name": name,
                        "value": value,
                        "quantity": quantity,
                        "price": cur_price,
                        "gain_loss": gain_loss,
                        "gain_loss_pct": gain_loss_pct,
                        "broker": broker,
                    })
            except (ValueError, IndexError):
                continue

        # --- 投資信託テーブル行 ---
        # 列: 銘柄名, 保有口数, 平均取得単価, 基準価額, 評価額, 前日比, 評価損益, 評価損益率, 保有金融機関
        elif current_section_type == "funds" and len(row_cells) >= 5:
            name = row_cells[0].strip()
            if not name or name in ("銘柄名", "種類・名称"):
                continue

            try:
                quantity = _parse_number(row_cells[1]) if len(row_cells) > 1 else 0
                nav = _parse_number(row_cells[3]) if len(row_cells) > 3 else 0
                value = _parse_number(row_cells[4]) if len(row_cells) > 4 else 0
                gain_loss = _parse_number(row_cells[6]) if len(row_cells) > 6 else 0
                gain_loss_pct = row_cells[7].strip() if len(row_cells) > 7 else ""
                broker = row_cells[8].strip() if len(row_cells) > 8 else ""

                if value > 0:
                    funds.append({
                        "name": name,
                        "value": value,
                        "quantity": quantity,
                        "nav": nav,
                        "gain_loss": gain_loss,
                        "gain_loss_pct": gain_loss_pct,
                        "broker": broker,
                    })
            except (ValueError, IndexError):
                continue

        # --- 現金テーブル行 ---
        elif current_section_type == "cash" and len(row_cells) >= 2:
            name = row_cells[0].strip()
            if not name or name in ("種類・名称", "銘柄コード"):
                continue
            try:
                value = _parse_number(row_cells[1])
                broker = row_cells[2].strip() if len(row_cells) > 2 else ""
                if value > 0:
                    cash_details.append({
                        "name": name,
                        "value": value,
                        "broker": broker,
                    })
            except (ValueError, IndexError):
                continue

    print(f"  -> HTML抽出: 株式={len(holdings)}, 投信={len(funds)}, 現金={len(cash_details)}")
    for h in holdings:
        print(f"     [{h['ticker']}] {h['name']}: {h['value']:,.0f}円 ({h['quantity']}株)")
    for f in funds:
        print(f"     [投信] {f['name'][:30]}: {f['value']:,.0f}円")

    cash_jpy = categories.get("cash", 0)
    stock_value = categories.get("stocks", 0)
    fund_value = categories.get("funds", 0)
    real_estate_value = categories.get("real_estate", 0)

    if total_value == 0:
        total_value = sum(categories.values())

    return {
        "total_value": total_value,
        "cash_jpy": cash_jpy,
        "cash_usd": 0.0,
        "stock_value": stock_value,
        "fund_value": fund_value,
        "real_estate_value": real_estate_value,
        "holdings": holdings,
        "funds": funds,
        "cash_details": cash_details,
    }


# ================================================================
# データ抽出（テキストベース — フォールバック用）
# ================================================================

def _extract_from_text(asset_text: str) -> dict:
    """
    page.inner_text("body") の結果からポートフォリオデータを抽出する。

    マネーフォワード ME のポートフォリオページは
    テキスト出力で以下のような構造になる:

      資産総額： 175,407,600円
      ...
      預金・現金・暗号資産  15,749,086円  8.98%
      株式（現物）          78,680,398円  44.86%
      投資信託              60,947,032円  34.75%
      ...
      合計：15,749,086円
      ... (現金明細)
      合計：78,680,398円
      銘柄コード  銘柄名  保有数 ...
      8136  サンリオ  300  1,996  5,178  1,553,400円 ...
      AVGO  ブロードコム  130  ...
    """
    lines = asset_text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    # --- 資産総額 ---
    total_value = 0.0
    for line in lines:
        m = re.search(r"資産総額[：:\s]+([\d,]+)円", line)
        if m:
            total_value = _parse_number(m.group(1))
            break

    # --- カテゴリ合計（「資産の内訳」テーブル部分） ---
    categories = {}
    cat_patterns = [
        ("cash", r"預金・現金・暗号資産\s+([\d,]+)円"),
        ("stocks", r"株式（現物）\s+([\d,]+)円"),
        ("funds", r"投資信託\s+([\d,]+)円"),
        ("real_estate", r"不動産\s+([\d,]+)円"),
        ("points", r"ポイント・マイル\s+([\d,]+)円"),
    ]
    for key, pattern in cat_patterns:
        for line in lines:
            m = re.search(pattern, line)
            if m:
                categories[key] = _parse_number(m.group(1))
                break

    print(f"  -> カテゴリ合計: {categories}")

    # --- セクション分割 ---
    # テキストをセクションに分割して各パートを個別にパース
    full_text = "\n".join(lines)

    # 株式（現物）セクション
    holdings = _parse_stock_section(full_text)

    # 投資信託セクション
    funds = _parse_fund_section(full_text)

    # 預金・現金セクション
    cash_details = _parse_cash_section(full_text)

    cash_jpy = categories.get("cash", 0)
    stock_value = categories.get("stocks", 0)
    fund_value = categories.get("funds", 0)
    real_estate_value = categories.get("real_estate", 0)

    if total_value == 0:
        total_value = sum(categories.values())

    print(f"  -> 抽出結果: 総資産={total_value:,.0f}円")
    print(f"     株式銘柄数: {len(holdings)}")
    print(f"     投信銘柄数: {len(funds)}")
    print(f"     現金明細数: {len(cash_details)}")

    return {
        "total_value": total_value,
        "cash_jpy": cash_jpy,
        "cash_usd": 0.0,
        "stock_value": stock_value,
        "fund_value": fund_value,
        "real_estate_value": real_estate_value,
        "holdings": holdings,
        "funds": funds,
        "cash_details": cash_details,
    }


def _parse_stock_section(text: str) -> list[dict]:
    """
    株式（現物）セクションからテキストベースで銘柄を抽出する。

    テキスト内の株式銘柄パターン:
      行が ティッカー(英字or4桁数字) で始まり、その後に銘柄名と数値が続く。

    実データ例（アクセシビリティツリーから取得済み）:
      8136  サンリオ  300  1,996  5,178  1,553,400円  -36,600円  954,600円  159.42%  楽天証券
      AVGO  ブロードコム  130  339.60  318.81  6,609,696円  ...  SBI証券
      バンガード トータル ワールド ストックETF  478  146.10  139.17  10,609,129円  ...  SBI証券
    """
    holdings = []

    # 株式セクションの開始を見つける
    stock_start = text.find("株式（現物）")
    if stock_start == -1:
        return holdings

    # 次のセクション（投資信託）の手前までを対象
    stock_end = text.find("投資信託", stock_start + 10)
    if stock_end == -1:
        stock_end = len(text)

    stock_text = text[stock_start:stock_end]
    lines = stock_text.split("\n")

    # パターン1: ティッカー付き行 (AVGO, GOOG, 8136 etc.)
    # パターン2: 名前のみ行 (バンガード トータル ワールド ストックETF)
    ticker_pattern = re.compile(
        r"^([A-Z]{1,5}|\d{4})\s+"  # ティッカー or 銘柄コード
        r"(.+?)\s+"                 # 銘柄名
        r"([\d,.]+)\s+"             # 保有数
        r"([\d,.]+)\s+"             # 平均取得単価
        r"([\d,.]+)\s+"             # 現在値
        r"([\d,]+)円"               # 評価額
    )

    # VTなどの名前が長い銘柄用（ティッカーなし）
    name_pattern = re.compile(
        r"^(バンガード[^\d]+?|eMAXIS[^\d]+?)\s+"  # 銘柄名
        r"([\d,.]+)\s+"                            # 保有数
        r"([\d,.]+)\s+"                            # 平均取得単価
        r"([\d,.]+)\s+"                            # 現在値
        r"([\d,]+)円"                              # 評価額
    )

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # パターン1: ティッカー付き
        m = ticker_pattern.match(line)
        if m:
            ticker = m.group(1)
            name = m.group(2).strip()
            quantity = _parse_number(m.group(3))
            price = _parse_number(m.group(5))
            value = _parse_number(m.group(6))

            # 評価損益・損益率・金融機関を残りテキストから抽出
            rest = line[m.end():]
            gain_loss = 0.0
            gain_loss_pct = ""
            broker = ""

            nums = re.findall(r"-?[\d,]+円", rest)
            if len(nums) >= 2:
                gain_loss = _parse_number(nums[1])  # 2番目が評価損益
            pct = re.search(r"-?[\d.]+%", rest)
            if pct:
                gain_loss_pct = pct.group()
            broker_m = re.search(r"(SBI証券|楽天証券|マネックス証券|auカブコム証券)", rest)
            if broker_m:
                broker = broker_m.group(1)

            if value > 0:
                holdings.append({
                    "ticker": ticker,
                    "name": name,
                    "value": value,
                    "quantity": quantity,
                    "price": price,
                    "gain_loss": gain_loss,
                    "gain_loss_pct": gain_loss_pct,
                    "broker": broker,
                })
            continue

        # パターン2: 名前のみ（VT等）
        m = name_pattern.match(line)
        if m:
            name = m.group(1).strip()
            quantity = _parse_number(m.group(2))
            price = _parse_number(m.group(4))
            value = _parse_number(m.group(5))

            # VTのティッカー推定
            ticker = ""
            if "バンガード" in name and "トータル" in name and "ワールド" in name:
                ticker = "VT"

            rest = line[m.end():]
            broker_m = re.search(r"(SBI証券|楽天証券)", rest)
            broker = broker_m.group(1) if broker_m else ""

            if value > 0:
                holdings.append({
                    "ticker": ticker or name[:10],
                    "name": name,
                    "value": value,
                    "quantity": quantity,
                    "price": price,
                    "gain_loss": 0.0,
                    "gain_loss_pct": "",
                    "broker": broker,
                })

    # フォールバック: 正規表現で取れなかった場合、
    # 「XX,XXX,XXX円」パターンの金額をキーワードと紐付け
    if not holdings:
        print("  -> [WARNING] 正規表現パースに失敗。フォールバック抽出を実行")
        holdings = _fallback_stock_extract(stock_text)

    return holdings


def _fallback_stock_extract(stock_text: str) -> list[dict]:
    """
    正規表現パースに失敗した場合のフォールバック。
    テキスト内の既知の銘柄名と金額を紐付ける。
    """
    known_tickers = {
        "サンリオ": "8136",
        "ブロードコム": "AVGO",
        "コストコ": "COST",
        "アルファベット": "GOOG",
        "インテューイティブ": "ISRG",
        "イーライ リリィ": "LLY",
        "イーライリリー": "LLY",
        "マイクロソフト": "MSFT",
        "エヌビディア": "NVDA",
        "バンガード トータル ワールド ストックETF": "VT",
        "バンガード": "VT",
    }

    holdings = []
    lines = stock_text.split("\n")

    for i, line in enumerate(lines):
        for name_key, ticker in known_tickers.items():
            if name_key in line:
                # この行と前後の行から金額を探す
                context = "\n".join(lines[max(0, i-1):min(len(lines), i+3)])
                amounts = re.findall(r"([\d,]+)円", context)
                if amounts:
                    # 最大の金額を評価額とする
                    values = [_parse_number(a) for a in amounts]
                    value = max(values) if values else 0
                    if value > 10000:
                        # 重複チェック
                        if not any(h["ticker"] == ticker and h["value"] == value for h in holdings):
                            holdings.append({
                                "ticker": ticker,
                                "name": name_key,
                                "value": value,
                                "quantity": 0,
                                "price": 0,
                                "gain_loss": 0,
                                "gain_loss_pct": "",
                                "broker": "",
                            })
                break

    return holdings


def _parse_fund_section(text: str) -> list[dict]:
    """投資信託セクションから銘柄を抽出する。"""
    funds = []

    fund_start = text.find("投資信託")
    if fund_start == -1:
        return funds

    # 次のセクション
    fund_end = text.find("不動産", fund_start + 5)
    if fund_end == -1:
        fund_end = text.find("ポイント", fund_start + 5)
    if fund_end == -1:
        fund_end = len(text)

    fund_text = text[fund_start:fund_end]

    # 投信パターン: 銘柄名  保有口数  取得単価  基準価額  評価額
    fund_pattern = re.compile(
        r"(eMAXIS[^\n]*?|ひふみ[^\n]*?|楽天[^\n]*?投信[^\n]*?)\s+"
        r"([\d,]+)\s+"          # 保有口数
        r"([\d,]+)\s+"          # 取得単価
        r"([\d,]+)\s+"          # 基準価額
        r"([\d,]+)円"           # 評価額
    )

    for m in fund_pattern.finditer(fund_text):
        name = m.group(1).strip()
        quantity = _parse_number(m.group(2))
        nav = _parse_number(m.group(4))
        value = _parse_number(m.group(5))

        rest = fund_text[m.end():m.end() + 200]
        gain_loss_pct = ""
        pct = re.search(r"-?[\d.]+%", rest)
        if pct:
            gain_loss_pct = pct.group()
        broker_m = re.search(r"(楽天証券|SBI証券)", rest)
        broker = broker_m.group(1) if broker_m else ""

        if value > 0:
            funds.append({
                "name": name,
                "value": value,
                "quantity": quantity,
                "nav": nav,
                "gain_loss": 0.0,
                "gain_loss_pct": gain_loss_pct,
                "broker": broker,
            })

    # フォールバック: "合計：XX円" からセクション合計だけでも取得
    if not funds:
        m = re.search(r"合計[：:]([\d,]+)円", fund_text)
        if m:
            total = _parse_number(m.group(1))
            if total > 0:
                # eMAXISの文字列を探して名前を推定
                name_m = re.search(r"(eMAXIS\s+Slim[^\n]+)", fund_text)
                name = name_m.group(1).strip() if name_m else "投資信託合計"
                funds.append({
                    "name": name,
                    "value": total,
                    "quantity": 0,
                    "nav": 0,
                    "gain_loss": 0.0,
                    "gain_loss_pct": "",
                    "broker": "",
                })

    return funds


def _parse_cash_section(text: str) -> list[dict]:
    """預金・現金セクションから明細を抽出する。"""
    details = []

    cash_start = text.find("預金・現金・暗号資産")
    if cash_start == -1:
        return details

    cash_end = text.find("株式（現物）", cash_start + 10)
    if cash_end == -1:
        cash_end = min(cash_start + 3000, len(text))

    cash_text = text[cash_start:cash_end]
    lines = cash_text.split("\n")

    # 各行から「名称  金額円  金融機関」パターンを探す
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 金額パターンを含む行
        amounts = re.findall(r"([\d,]+)円", line)
        if amounts:
            value = _parse_number(amounts[0])
            if value > 0:
                # 行頭が名称
                name = re.split(r"\d", line)[0].strip()
                if not name or len(name) < 2:
                    continue
                if "合計" in name or "資産" in name:
                    continue
                details.append({
                    "name": name,
                    "value": value,
                    "broker": "",
                })

    return details


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
