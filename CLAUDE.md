# MyPortfolioAutomation

> **このプロジェクトは休眠中** — 今後の機能開発予定はなく、部品取り・名称整合性確保のために保持。バグ修正対応は通常通り。詳細はメモリ `project_dormant_projects` 参照。

FIRE生活者（浅野智明）のための月次ポートフォリオ分析・レポート自動生成システム。

## システム概要

MoneyForwardからポートフォリオを取得し、Claude APIで投資仮説の検証レポートを生成、メールで配信する。
GitHub Actionsで毎月末に自動実行される。

### 実行フロー（main.py）

1. 日付チェック（月末 or FORCE_RUN=true）
2. MoneyForwardスクレイピング（Playwright + Cookie認証）
3. history.csv更新 + Google Driveアップロード
4. 外部コンテキスト読込（financialPolicy.md, stock_stories.json, Yahoo Finance RSS）
5. 資産寿命シミュレーション + グラフ生成
6. Claude APIでレポート生成（"Ultra C" = 投資仮説への挑戦的検証）
7. HTML形式メール配信（画像埋め込み）

## ファイル構成

```
main.py               # エントリーポイント・オーケストレーション
scraper.py             # MoneyForward ME スクレイパー（Playwright）
drive_handler.py       # Google Drive API連携（OAuth2）
report_generator.py    # Claude APIレポート生成（claude-sonnet-4-6）
simulator.py           # 資産寿命シミュレーション・グラフ描画
mailer.py              # Gmail SMTP配信
news_fetcher.py        # Yahoo Finance RSSニュース取得
story_manager.html     # stock_stories.json のGUIエディタ（単体HTML）
config/
  stock_stories.json   # 各銘柄の投資仮説（thesis/key_metrics/exit_conditions）
  financialPolicy.md   # 資産配分ルール・支出モデル・年金情報
  report_template.txt  # レポート構成テンプレート（7セクション）
data/
  history.csv          # 月次ポートフォリオ時系列データ
archives/
  reports/             # 生成されたレポート（Markdown）
  screenshots/         # スクリーンショット・シミュレーショングラフ
setup/
  get_oauth_token.py   # Google OAuth2初期トークン取得（ローカル1回実行）
.github/workflows/
  monthly_report.yml   # GitHub Actions（毎月28-31日 20:00 JST）
```

## 重要な仕様・ドメイン知識

- **VT特殊ルール**: VT 478株はUSD現金として扱い、株式ポジションとしてカウントしない
- **支出フェーズ**: 智明の年齢で段階的に支出率が変化（~75歳:100%, 76-83:75%, 84-87:50%）
- **年金**: 企業年金 80万/年（60-69歳）、公的年金 智明282.3万/年・紀子240万/年（75歳～）
- **リターン前提**: 名目5%（75歳未満）、4%（75歳以上）、インフレ2%
- **"Ultra C"**: レポートの核心機能。投資仮説（ストーリー）を市場ニュースと照合し、確証バイアスを防ぐ挑戦的な問いを生成する
- **安全支出上限**: 紀子105歳まで資産が持つ最大年間支出額を逆算

## 技術スタック

（標準スタック。詳細は `~/.claude/stack-catalog.md`）
- Python 3.11 + Playwright + pandas + matplotlib + anthropic SDK（`claude-sonnet-4-6`）
- Google Drive API / Gmail SMTP / GitHub Actions（月次自動実行）

## 開発時の注意

- 環境変数・シークレットはGitHub Secretsで管理。`.env`やcookies/はgitignore済み
- history.csv、archives/配下のレポート・スクリーンショットはGitで履歴管理する方針
- story_manager.htmlは単体で動作するHTMLファイル。APIキーはブラウザから直接Anthropic APIを呼ぶ構成
- シミュレーションのパラメータ（年金額、支出額、リターン率等）はsimulator.pyにハードコードされている

<!-- コスト追跡対象は `~/.claude/stack-catalog.md` および costviewer/CLAUDE.md 参照 -->
