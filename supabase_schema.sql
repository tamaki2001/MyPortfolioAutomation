-- ===========================================
-- Folio (旧 MyPortfolioAutomation) Supabaseスキーマ
-- 共有Supabaseインスタンスに fo_ プレフィックスで追加
-- ===========================================

-- ポートフォリオ履歴
CREATE TABLE fo_portfolio_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE NOT NULL UNIQUE,
    total_value BIGINT NOT NULL,
    cash_jpy    BIGINT,
    cash_usd    BIGINT,
    stock_value BIGINT,
    fund_value  BIGINT,
    holdings    JSONB,
    funds       JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 月次支出
CREATE TABLE fo_monthly_expenses (
    id           BIGSERIAL PRIMARY KEY,
    year_month   CHAR(7) NOT NULL UNIQUE,
    total_amount BIGINT NOT NULL,
    categories   JSONB,
    raw_data     JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 投資仮説（iOSと共有）
CREATE TABLE fo_stock_stories (
    ticker            TEXT PRIMARY KEY,
    company           TEXT NOT NULL,
    asset_type        TEXT,
    thesis            TEXT,
    key_metrics       JSONB,
    exit_conditions   JSONB,
    notes             TEXT,
    added_date        DATE,
    last_validated    TIMESTAMPTZ,
    validation_status TEXT DEFAULT 'unchecked',
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- 生成レポート
CREATE TABLE fo_reports (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE NOT NULL,
    markdown    TEXT NOT NULL,
    sim_params  JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 個人専用なのでRLSは無効化（service role keyで全権限アクセス）
ALTER TABLE fo_portfolio_snapshots DISABLE ROW LEVEL SECURITY;
ALTER TABLE fo_monthly_expenses    DISABLE ROW LEVEL SECURITY;
ALTER TABLE fo_stock_stories       DISABLE ROW LEVEL SECURITY;
ALTER TABLE fo_reports             DISABLE ROW LEVEL SECURITY;

-- アプリ設定（iOS アプリからの MF クッキー保存など）
CREATE TABLE fo_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE fo_settings DISABLE ROW LEVEL SECURITY;

-- インデックス
CREATE INDEX idx_fo_snapshots_date     ON fo_portfolio_snapshots(date DESC);
CREATE INDEX idx_fo_expenses_yearmonth ON fo_monthly_expenses(year_month DESC);
CREATE INDEX idx_fo_reports_date       ON fo_reports(date DESC);
