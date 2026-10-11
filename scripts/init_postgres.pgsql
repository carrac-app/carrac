-- Carrac PostgreSQL Schema
-- Replaces SQLite databases: users.db + drawings.db

-- =============================================
-- USER DATABASE TABLES (from user_db.py)
-- =============================================

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    tier TEXT DEFAULT 'free',
    invited_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ,
    last_active TIMESTAMPTZ,
    email_verified BOOLEAN DEFAULT FALSE,
    ai_credits_cents INTEGER DEFAULT 0,
    trader_profile TEXT,
    subscription_expires_at TIMESTAMPTZ,
    subscription_payment_id TEXT,
    paddle_customer_id TEXT,
    paddle_subscription_id TEXT,
    tos_accepted_at TIMESTAMPTZ,
    tos_accepted_ip TEXT,
    tos_accepted_ua TEXT,
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_users_paddle_cid ON users(paddle_customer_id);
CREATE INDEX IF NOT EXISTS idx_users_paddle_sid ON users(paddle_subscription_id);

CREATE TABLE IF NOT EXISTS invites (
    id SERIAL PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('email', 'code')),
    value TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    used_by INTEGER REFERENCES users(id),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    watchlist_data TEXT DEFAULT '{}',
    preferences TEXT DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    feature TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_cents REAL DEFAULT 0,
    provider_cost_cents REAL DEFAULT 0,
    model TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage(user_id, created_at);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    user_email TEXT,
    type TEXT NOT NULL CHECK(type IN ('bug', 'feature')),
    area TEXT,
    description TEXT NOT NULL,
    expected TEXT,
    actual TEXT,
    how_should_work TEXT,
    extra TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS satisfaction_ratings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_satisfaction_user ON satisfaction_ratings(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS onboarding (
    id SERIAL PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
    completed_steps TEXT DEFAULT '[]',
    completed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scenarios (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Core identity
    asset VARCHAR(20),
    timeframe VARCHAR(10),
    label TEXT,
    thesis TEXT,
    alternative_thesis TEXT,
    notes TEXT,

    -- Classification
    scenario_type VARCHAR(20) NOT NULL DEFAULT 'trade_setup',
    direction VARCHAR(10),
    conviction INTEGER CHECK (conviction BETWEEN 1 AND 5),

    -- Action plan
    entry_low REAL,
    entry_high REAL,
    invalidation REAL,
    targets JSONB DEFAULT '[]',
    stop_loss REAL,

    -- Spot accumulation
    accumulation_budget REAL,
    accumulation_used REAL DEFAULT 0,

    -- Conditions engine
    conditions JSONB DEFAULT '{"rules": [], "logic": "all"}',

    -- Milestones (staged playbook)
    milestones JSONB DEFAULT '[]',

    -- State machine
    status VARCHAR(20) NOT NULL DEFAULT 'watching',
    status_changed_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,

    -- Linking
    journal_trade_id INTEGER,
    linked_drawing_ids JSONB DEFAULT '[]',

    -- Resolution
    outcome VARCHAR(20),
    outcome_notes TEXT,
    resolved_at TIMESTAMPTZ,

    -- Metadata
    source VARCHAR(50),
    tags TEXT[],
    screenshot_path TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scenarios_user ON scenarios(user_id, asset, timeframe);
CREATE INDEX IF NOT EXISTS idx_scenarios_user_status ON scenarios(user_id, status);
CREATE INDEX IF NOT EXISTS idx_scenarios_asset ON scenarios(asset);

CREATE TABLE IF NOT EXISTS email_verifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ev_token_hash ON email_verifications(token_hash);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    ip_address TEXT
);
CREATE INDEX IF NOT EXISTS idx_prt_token_hash ON password_reset_tokens(token_hash);

-- =============================================
-- SOCIAL AUTH PROVIDER LINKS (Google / Facebook / Apple)
-- =============================================
CREATE TABLE IF NOT EXISTS auth_providers (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        VARCHAR(16) NOT NULL CHECK (provider IN ('google','facebook','apple')),
    provider_uid    TEXT NOT NULL,
    email_at_link   TEXT,
    email_verified  BOOLEAN DEFAULT FALSE,
    display_name    TEXT,
    raw_profile     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,
    UNIQUE (provider, provider_uid),
    UNIQUE (user_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_auth_providers_user ON auth_providers(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_providers_lookup ON auth_providers(provider, provider_uid);

-- Matches the prod shape (token_hash/token_suffix/revoked),
-- applied to prod via the 2026-06 catch-up SQL. The old token/is_active/
-- last_used shape below was stale and could not rebuild prod.
CREATE TABLE IF NOT EXISTS widget_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    token_hash TEXT NOT NULL,
    token_suffix TEXT NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked BOOLEAN DEFAULT FALSE,
    UNIQUE(token_hash)
);

CREATE TABLE IF NOT EXISTS checklist_templates (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    setup_type TEXT,
    checklist_items TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================
-- CONTENT DATABASE TABLES (from carrac/content_db.py)
-- =============================================

CREATE TABLE IF NOT EXISTS drawings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER DEFAULT 0,
    asset VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    drawing_type VARCHAR(30) NOT NULL,
    price_level REAL,
    price_level_upper REAL,
    label TEXT,
    color VARCHAR(20) DEFAULT '#ff0000',
    line_style VARCHAR(20) DEFAULT 'solid',
    line_width INTEGER DEFAULT 2,
    metadata TEXT,
    scenario_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_drawings_asset_tf ON drawings(asset, timeframe);
CREATE INDEX IF NOT EXISTS idx_drawings_scenario ON drawings(scenario_id);
CREATE INDEX IF NOT EXISTS idx_drawings_user ON drawings(user_id);

CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    report_type VARCHAR(50) DEFAULT 'full',
    content TEXT NOT NULL,
    assets_included TEXT,
    generation_time_ms INTEGER,
    summary TEXT,
    metadata TEXT,
    audio_script TEXT,
    -- The assembled briefing that produced `content` (shared/
    -- market reports only — never personal_report_sections, see that
    -- table's comment). Nullable, retention-pruned — see
    -- carrac/content_db.py::prune_old_source_briefings.
    source_briefing TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Deliberately NO source_briefing column here. The per-user
-- briefing (trader_profile, plays/thesis, positions, alerts) is materially
-- more sensitive than the shared market briefing above and is already
-- treated as untrusted user content (carrac/grounding/escape.py) — storing
-- it a second time widens the exposure surface for a marginal debugging
-- gain. See morning_engine/engine.py::_generate_one_user_report (build of
-- `user_briefing`) for the comment at the source.
CREATE TABLE IF NOT EXISTS personal_report_sections (
    id SERIAL PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    audio_script TEXT,
    cost_cents INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(report_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_personal_report_user ON personal_report_sections(user_id, report_id);

CREATE TABLE IF NOT EXISTS sentiment_history (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    fear_greed REAL,
    funding_rate REAL,
    open_interest_usd REAL,
    oi_change_pct REAL,
    mvrv_zscore REAL,
    alt_season REAL,
    btc_long_pct REAL,
    btc_price REAL
);
CREATE INDEX IF NOT EXISTS idx_sentiment_ts ON sentiment_history(timestamp);

CREATE TABLE IF NOT EXISTS regime_history (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    regime TEXT NOT NULL,
    score REAL,
    components TEXT
);
CREATE INDEX IF NOT EXISTS idx_regime_ts ON regime_history(timestamp);

CREATE TABLE IF NOT EXISTS trade_journal (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    asset VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10),
    direction VARCHAR(10),
    entry_price REAL,
    target_price REAL,
    stop_price REAL,
    exit_price REAL,
    exit_reason VARCHAR(20),
    rr_planned REAL,
    rr_actual REAL,
    setup_type VARCHAR(30),
    indicators_at_entry TEXT,
    drawing_id TEXT,
    scenario_id INTEGER,
    opened_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    notes TEXT,
    size REAL,
    fees REAL DEFAULT 0,
    pnl_currency REAL,
    account_tag VARCHAR(30),
    confidence INTEGER,
    mood INTEGER,
    mistake_tags TEXT,
    checklist_results TEXT,
    checklist_template_id INTEGER,
    ai_review TEXT,
    trade_grade TEXT,
    session_id INTEGER,
    tags TEXT,
    mfe REAL,
    mae REAL,
    mfe_price REAL,
    mae_price REAL,
    exit_efficiency REAL
);
CREATE INDEX IF NOT EXISTS idx_journal_user ON trade_journal(user_id, opened_at);

CREATE TABLE IF NOT EXISTS journal_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    session_label VARCHAR(30),
    pre_notes TEXT,
    post_notes TEXT,
    mood_pre INTEGER,
    mood_post INTEGER,
    rules_followed TEXT,
    ai_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, date, session_label)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_date ON journal_sessions(user_id, date);

CREATE TABLE IF NOT EXISTS journal_screenshots (
    id SERIAL PRIMARY KEY,
    journal_id INTEGER NOT NULL REFERENCES trade_journal(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    screenshot_type VARCHAR(10) NOT NULL,
    file_path TEXT NOT NULL,
    captured_at TIMESTAMPTZ DEFAULT NOW(),
    ai_analysis TEXT
);
CREATE INDEX IF NOT EXISTS idx_screenshots_journal ON journal_screenshots(journal_id);

CREATE TABLE IF NOT EXISTS setup_checklists (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    setup_type VARCHAR(30) NOT NULL,
    name VARCHAR(50),
    checklist_items TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, setup_type)
);

CREATE TABLE IF NOT EXISTS journal_views (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name VARCHAR(50) NOT NULL,
    filters TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS journal_playbooks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name VARCHAR(50) NOT NULL,
    setup_type VARCHAR(30),
    description TEXT,
    ideal_conditions TEXT,
    entry_rules TEXT,
    exit_rules TEXT,
    example_trade_ids TEXT,
    screenshot_ids TEXT,
    stats_cache TEXT,
    stats_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS journal_goals (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    goals TEXT NOT NULL,
    progress_cache TEXT,  -- prod column (was 'results' in the stale baseline)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, month)
);

CREATE TABLE IF NOT EXISTS custom_formulas (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    formula TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT 'line',
    color TEXT NOT NULL DEFAULT '#2196F3',
    pane TEXT NOT NULL DEFAULT 'new',
    line_width INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_custom_formulas_user ON custom_formulas(user_id);

-- =============================================
-- CONTEXT RECIPES (AI Context Composer)
-- =============================================

CREATE TABLE IF NOT EXISTS context_recipes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    blocks JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recipes_user ON context_recipes(user_id);
-- Only one default per user
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_default
    ON context_recipes(user_id) WHERE is_default = TRUE;

-- ── Tier admin config ──
CREATE TABLE IF NOT EXISTS tier_config (
    tier TEXT PRIMARY KEY,
    limits_json JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_tier_overrides (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    override_json JSONB NOT NULL DEFAULT '{}',
    reason TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by INTEGER REFERENCES users(id)
);

-- =============================================
-- AI CONVERSATIONS & MESSAGES
-- =============================================

CREATE TABLE IF NOT EXISTS ai_conversations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT DEFAULT 'New Chat',
    primary_asset VARCHAR(20),
    timeframe VARCHAR(10),
    page_context VARCHAR(30),
    summary TEXT,
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_conv_user ON ai_conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_conv_cleanup ON ai_conversations(updated_at);

-- =============================================
-- AI CREDITS & BILLING
-- =============================================

CREATE TABLE IF NOT EXISTS ai_credits (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    total_deposited_cents INTEGER NOT NULL DEFAULT 0,
    total_spent_cents INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_usage_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    feature TEXT NOT NULL,
    asset TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_cents INTEGER DEFAULT 0,
    charged_cents INTEGER DEFAULT 0,
    model TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_log_user ON ai_usage_log(user_id, created_at);

CREATE TABLE IF NOT EXISTS ai_deposits (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    crypto_amount TEXT,
    crypto_currency TEXT,
    tx_hash TEXT,
    nowpayments_payment_id TEXT,
    status TEXT DEFAULT 'confirmed',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS processed_payments (
    payment_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    user_id INTEGER
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'summary')),
    content TEXT NOT NULL,
    feature TEXT,
    token_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_msg_conv ON ai_messages(conversation_id, created_at);

-- =============================================
-- PLAYS (Thesis-first market belief tracking)
-- =============================================

CREATE TABLE IF NOT EXISTS plays (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Core
    title TEXT NOT NULL,
    thesis TEXT,
    type VARCHAR(20) NOT NULL DEFAULT 'directional',
    approach VARCHAR(20) NOT NULL DEFAULT 'single',
    direction VARCHAR(20) DEFAULT 'long',

    -- Assets
    primary_asset VARCHAR(20),
    related_assets JSONB DEFAULT '[]',
    trigger_asset VARCHAR(20),

    -- Tracking
    conditions JSONB DEFAULT '{"rules":[],"logic":"all"}',
    invalidation JSONB,
    timeframe VARCHAR(20),
    expiry TIMESTAMPTZ,

    -- Action plan
    action_plan TEXT,
    linked_drawing_ids JSONB DEFAULT '[]',

    -- Lifecycle
    status VARCHAR(20) NOT NULL DEFAULT 'watching',
    status_changed_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    resolution TEXT,

    -- Morning report
    report_tracking BOOLEAN DEFAULT TRUE,
    last_report_note TEXT,

    -- Score snapshot
    scorer_snapshot JSONB,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plays_user_status ON plays(user_id, status);
CREATE INDEX IF NOT EXISTS idx_plays_asset ON plays(primary_asset);
CREATE INDEX IF NOT EXISTS idx_plays_updated ON plays(updated_at DESC);

-- ─────────────────────────────────────────────────────────────────────────
-- Tables applied to prod via the 2026-06 catch-up SQL that were
-- never folded back into this baseline — captured here so a fresh bootstrap
-- (baseline + alembic) reproduces the real prod schema.
-- ─────────────────────────────────────────────────────────────────────────

-- Monthly goals (distinct from journal_goals; keeps a plaintext results field).
CREATE TABLE IF NOT EXISTS monthly_goals (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    goals TEXT NOT NULL,
    results TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, month)
);

-- On-chain metric history (BTC network + stablecoin/DEX aggregates).
CREATE TABLE IF NOT EXISTS onchain_history (
    id SERIAL PRIMARY KEY,
    "timestamp" TEXT NOT NULL,
    total_tvl REAL,
    tvl_change_24h_pct REAL,
    stablecoin_supply REAL,
    dex_volume_24h REAL,
    btc_active_addresses INTEGER,
    btc_hash_rate REAL,
    btc_exchange_netflow REAL,
    btc_mempool_mb REAL
);

-- Score snapshots captured at trade entry/exit for post-hoc analysis.
CREATE TABLE IF NOT EXISTS score_snapshots (
    id SERIAL PRIMARY KEY,
    journal_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    snapshot_type TEXT NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    captured_at TIMESTAMPTZ,
    score_trend INTEGER,
    score_momentum INTEGER,
    score_rsi INTEGER,
    score_structure INTEGER,
    score_level INTEGER,
    score_macro INTEGER,
    score_total INTEGER,
    verdict TEXT,
    vetoes TEXT,
    price REAL,
    consensio_1d_signal INTEGER,
    consensio_1d_positions INTEGER,
    consensio_3d_signal INTEGER,
    consensio_3d_positions INTEGER,
    consensio_1w_signal INTEGER,
    consensio_1w_positions INTEGER,
    consensio_total_positions INTEGER,
    ema_ribbon_trend TEXT,
    ema_ribbon_gap_pct REAL,
    macd_histogram REAL,
    macd_momentum TEXT,
    weekly_rsi REAL,
    rsi_div_daily TEXT,
    rsi_div_weekly TEXT,
    structure_trend TEXT,
    last_high_type TEXT,
    last_low_type TEXT,
    cpr_pivot REAL,
    cpr_tc REAL,
    cpr_bc REAL,
    bb_upper REAL,
    bb_lower REAL,
    bb_middle REAL,
    adx_value REAL,
    volume_ratio REAL,
    regime TEXT,
    regime_score INTEGER,
    fear_greed INTEGER,
    breadth_pct REAL,
    funding_rate REAL,
    entry_price REAL,
    stop_price REAL,
    tp1_price REAL,
    setup_type TEXT,
    setup_readiness TEXT,
    setup_confidence REAL
);

-- Custom backtest strategies (formula-based; text primary key).
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entry_formula TEXT NOT NULL,
    exit_formula TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'long',
    capital REAL NOT NULL DEFAULT 10000,
    size_percent REAL NOT NULL DEFAULT 100,
    commission_percent REAL NOT NULL DEFAULT 0.1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    -- Full config snapshot (timeframe, date range, regime,
    -- instrument, slippage, stop type, stop/target %, entry fill). The scalars
    -- above are kept and still written so old readers keep working.
    params JSONB
);
CREATE INDEX IF NOT EXISTS idx_strategies_user ON strategies(user_id);
