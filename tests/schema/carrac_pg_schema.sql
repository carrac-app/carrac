-- carrac_pg_schema.sql — a captured Postgres schema: the reference the Postgres
-- tests and the schema-drift check compare against.
--
-- What it is: `pg_dump --schema-only --no-owner --no-privileges` of a deployed
-- database at alembic revision dropusage260826 (v0.225.5), plus the
-- user_alerts and push_subscriptions tables that useralerts261010 and
-- pushsubs261010 add, dumped from a fresh bootstrap.
-- Schema only, no data.
--
-- Who reads it:
--   * the `pg-portability` job (.github/workflows/test.yml) loads it into a
--     throwaway postgres:16 so tests/pg/ runs against the real dialect and
--     schema, then diffs it against a fresh bootstrap (init_postgres.pgsql plus
--     `alembic upgrade head`) with scripts/check_schema_drift.py;
--   * the plain (SQLite) suite parses it as text where it needs real column
--     types, e.g. tests/test_export_coverage.py, tests/test_erasure_coverage.py
--     and tests/test_permitted_derivatives.py.
--
-- Keep it current: regenerate it whenever a migration changes a table, or the
-- drift check goes red and stays red until someone does:
--   pg_dump --schema-only --no-owner --no-privileges <database> \
--       > tests/schema/carrac_pg_schema.sql
--   (then re-add this header)
--
--
-- PostgreSQL database dump
--

\restrict TlyHDnweCp3u4ZmZoXXvTcdaRwfBaBF7dpvPiRb7nKwa2PKAgtu5kaRRCQStX94

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: quant; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA quant;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_conversations (
    id integer NOT NULL,
    user_id integer NOT NULL,
    title text DEFAULT 'New Chat'::text,
    primary_asset character varying(20),
    timeframe character varying(10),
    page_context character varying(30),
    summary text,
    message_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: ai_conversations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_conversations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_conversations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_conversations_id_seq OWNED BY public.ai_conversations.id;


--
-- Name: ai_credits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_credits (
    user_id integer NOT NULL,
    balance_cents integer DEFAULT 0 NOT NULL,
    total_deposited_cents integer DEFAULT 0 NOT NULL,
    total_spent_cents integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: ai_deposits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_deposits (
    id integer NOT NULL,
    user_id integer NOT NULL,
    amount_cents integer NOT NULL,
    crypto_amount text,
    crypto_currency text,
    tx_hash text,
    nowpayments_payment_id text,
    status text DEFAULT 'confirmed'::text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: ai_deposits_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_deposits_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_deposits_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_deposits_id_seq OWNED BY public.ai_deposits.id;


--
-- Name: ai_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_messages (
    id integer NOT NULL,
    conversation_id integer NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    feature text,
    token_count integer,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ai_messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text, 'summary'::text])))
);


--
-- Name: ai_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_messages_id_seq OWNED BY public.ai_messages.id;


--
-- Name: ai_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_usage (
    id integer NOT NULL,
    user_id integer NOT NULL,
    feature text NOT NULL,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    cost_cents real DEFAULT 0,
    provider_cost_cents real DEFAULT 0,
    model text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: ai_usage_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_usage_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_usage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_usage_id_seq OWNED BY public.ai_usage.id;


--
-- Name: ai_usage_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_usage_log (
    id integer NOT NULL,
    user_id integer NOT NULL,
    feature text NOT NULL,
    asset text,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    cost_cents integer DEFAULT 0,
    charged_cents integer DEFAULT 0,
    model text,
    created_at timestamp with time zone DEFAULT now(),
    prompt_sha256 character varying(64),
    response_sha256 character varying(64),
    conversation_id character varying(64)
);


--
-- Name: ai_usage_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_usage_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_usage_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_usage_log_id_seq OWNED BY public.ai_usage_log.id;


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id integer NOT NULL,
    user_id integer NOT NULL,
    token_hash text NOT NULL,
    token_suffix text NOT NULL,
    name text DEFAULT 'Default'::text,
    scope character varying(20) DEFAULT 'full'::character varying,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false,
    is_internal boolean DEFAULT false
);


--
-- Name: api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_keys_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: api_keys_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_keys_id_seq OWNED BY public.api_keys.id;


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    actor_user_id integer,
    subject_user_id integer,
    action text NOT NULL,
    before_json json,
    after_json json,
    source text NOT NULL,
    note text
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: auth_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_providers (
    id integer NOT NULL,
    user_id integer NOT NULL,
    provider character varying(16) NOT NULL,
    provider_uid text NOT NULL,
    email_at_link text,
    email_verified boolean DEFAULT false,
    display_name text,
    raw_profile text,
    created_at timestamp with time zone DEFAULT now(),
    last_login_at timestamp with time zone,
    CONSTRAINT auth_providers_provider_check CHECK (((provider)::text = ANY ((ARRAY['google'::character varying, 'facebook'::character varying, 'apple'::character varying])::text[])))
);


--
-- Name: auth_providers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_providers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: auth_providers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_providers_id_seq OWNED BY public.auth_providers.id;


--
-- Name: backtest_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.backtest_runs (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(255),
    symbol character varying(50) NOT NULL,
    expression text NOT NULL,
    timeframe character varying(10) NOT NULL,
    lookback_bars integer NOT NULL,
    hold_bars integer,
    result jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    model character varying(40) DEFAULT 'Hold-N Signal Test'::character varying NOT NULL,
    config jsonb
);


--
-- Name: backtest_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.backtest_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: backtest_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.backtest_runs_id_seq OWNED BY public.backtest_runs.id;


--
-- Name: checklist_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checklist_templates (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name text NOT NULL,
    setup_type text,
    checklist_items text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: checklist_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.checklist_templates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: checklist_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.checklist_templates_id_seq OWNED BY public.checklist_templates.id;


--
-- Name: context_recipes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.context_recipes (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(100) NOT NULL,
    is_default boolean DEFAULT false,
    blocks jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: context_recipes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.context_recipes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: context_recipes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.context_recipes_id_seq OWNED BY public.context_recipes.id;


--
-- Name: custom_formulas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.custom_formulas (
    id text NOT NULL,
    user_id integer NOT NULL,
    name text NOT NULL,
    formula text NOT NULL,
    style text DEFAULT 'line'::text NOT NULL,
    color text DEFAULT '#2196F3'::text NOT NULL,
    pane text DEFAULT 'new'::text NOT NULL,
    line_width integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: drawings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.drawings (
    id integer NOT NULL,
    user_id integer DEFAULT 0,
    asset character varying(20) NOT NULL,
    timeframe character varying(10) NOT NULL,
    drawing_type character varying(30) NOT NULL,
    price_level real,
    price_level_upper real,
    label text,
    color character varying(20) DEFAULT '#ff0000'::character varying,
    line_style character varying(20) DEFAULT 'solid'::character varying,
    line_width integer DEFAULT 2,
    metadata text,
    scenario_id integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    version integer DEFAULT 1 NOT NULL
);


--
-- Name: drawings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.drawings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: drawings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.drawings_id_seq OWNED BY public.drawings.id;


--
-- Name: email_verifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_verifications (
    id integer NOT NULL,
    user_id integer NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: email_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.email_verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: email_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.email_verifications_id_seq OWNED BY public.email_verifications.id;


--
-- Name: exchange_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_connections (
    id integer NOT NULL,
    user_id integer NOT NULL,
    exchange character varying(20) NOT NULL,
    label character varying(50),
    credentials_encrypted text NOT NULL,
    masked_identifier character varying(80),
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    consecutive_errors integer DEFAULT 0 NOT NULL,
    last_error text,
    last_sync_at timestamp with time zone,
    last_sync_trades_imported integer DEFAULT 0 NOT NULL,
    cursor_data text DEFAULT '{}'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: exchange_connections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.exchange_connections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: exchange_connections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.exchange_connections_id_seq OWNED BY public.exchange_connections.id;


--
-- Name: exchange_import_suggestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_import_suggestions (
    id integer NOT NULL,
    connection_id integer NOT NULL,
    user_id integer NOT NULL,
    exchange character varying(20) NOT NULL,
    symbol character varying(20) NOT NULL,
    direction character varying(10) NOT NULL,
    entry_price double precision,
    exit_price double precision,
    size double precision,
    fees double precision,
    opened_at timestamp with time zone,
    closed_at timestamp with time zone,
    matched_trade_id integer,
    journal_trade_id integer,
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    resolved_at timestamp with time zone
);


--
-- Name: exchange_import_suggestions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.exchange_import_suggestions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: exchange_import_suggestions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.exchange_import_suggestions_id_seq OWNED BY public.exchange_import_suggestions.id;


--
-- Name: exchange_sync_fills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_sync_fills (
    id integer NOT NULL,
    connection_id integer NOT NULL,
    exchange_trade_id character varying(64) NOT NULL,
    symbol character varying(20) NOT NULL,
    fill_time timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: exchange_sync_fills_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.exchange_sync_fills_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: exchange_sync_fills_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.exchange_sync_fills_id_seq OWNED BY public.exchange_sync_fills.id;


--
-- Name: feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.feedback (
    id integer NOT NULL,
    user_id integer,
    user_email text,
    type text NOT NULL,
    area text,
    description text NOT NULL,
    expected text,
    actual text,
    how_should_work text,
    extra text,
    status text DEFAULT 'new'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT feedback_type_check CHECK ((type = ANY (ARRAY['bug'::text, 'feature'::text])))
);


--
-- Name: feedback_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.feedback_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: feedback_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.feedback_id_seq OWNED BY public.feedback.id;


--
-- Name: invites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invites (
    id integer NOT NULL,
    type text NOT NULL,
    value text NOT NULL,
    created_by integer NOT NULL,
    used_by integer,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT invites_type_check CHECK ((type = ANY (ARRAY['email'::text, 'code'::text])))
);


--
-- Name: invites_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.invites_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: invites_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.invites_id_seq OWNED BY public.invites.id;


--
-- Name: journal_goals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_goals (
    id integer NOT NULL,
    user_id integer NOT NULL,
    month text NOT NULL,
    goals text NOT NULL,
    progress_cache text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: journal_goals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_goals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_goals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_goals_id_seq OWNED BY public.journal_goals.id;


--
-- Name: journal_playbooks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_playbooks (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(50) NOT NULL,
    setup_type character varying(30),
    description text,
    ideal_conditions text,
    entry_rules text,
    exit_rules text,
    example_trade_ids text,
    screenshot_ids text,
    stats_cache text,
    stats_updated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: journal_playbooks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_playbooks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_playbooks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_playbooks_id_seq OWNED BY public.journal_playbooks.id;


--
-- Name: journal_screenshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_screenshots (
    id integer NOT NULL,
    journal_id integer NOT NULL,
    user_id integer NOT NULL,
    screenshot_type character varying(10) NOT NULL,
    file_path text NOT NULL,
    captured_at timestamp with time zone DEFAULT now(),
    ai_analysis text
);


--
-- Name: journal_screenshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_screenshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_screenshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_screenshots_id_seq OWNED BY public.journal_screenshots.id;


--
-- Name: journal_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_sessions (
    id integer NOT NULL,
    user_id integer NOT NULL,
    date date NOT NULL,
    session_label character varying(30),
    pre_notes text,
    post_notes text,
    mood_pre integer,
    mood_post integer,
    rules_followed text,
    ai_summary text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: journal_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_sessions_id_seq OWNED BY public.journal_sessions.id;


--
-- Name: journal_views; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.journal_views (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name character varying(50) NOT NULL,
    filters text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: journal_views_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.journal_views_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: journal_views_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.journal_views_id_seq OWNED BY public.journal_views.id;


--
-- Name: mcp_intent_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_intent_log (
    id bigint NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    tool text NOT NULL,
    tier text NOT NULL,
    user_ref text,
    user_intent text,
    unmet_need text,
    latency_ms double precision NOT NULL,
    ok boolean NOT NULL,
    result_empty boolean DEFAULT false NOT NULL,
    error_kind text,
    prev_tool text,
    error_detail text
);


--
-- Name: mcp_intent_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mcp_intent_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mcp_intent_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mcp_intent_log_id_seq OWNED BY public.mcp_intent_log.id;


--
-- Name: monthly_goals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.monthly_goals (
    id integer NOT NULL,
    user_id integer NOT NULL,
    month text NOT NULL,
    goals text NOT NULL,
    results text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: monthly_goals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.monthly_goals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: monthly_goals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.monthly_goals_id_seq OWNED BY public.monthly_goals.id;


--
-- Name: onboarding; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onboarding (
    id integer NOT NULL,
    user_id integer NOT NULL,
    completed_steps text DEFAULT '[]'::text,
    completed boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: onboarding_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.onboarding_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: onboarding_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.onboarding_id_seq OWNED BY public.onboarding.id;


--
-- Name: onchain_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onchain_history (
    id integer NOT NULL,
    "timestamp" text NOT NULL,
    total_tvl real,
    tvl_change_24h_pct real,
    stablecoin_supply real,
    dex_volume_24h real,
    btc_active_addresses integer,
    btc_hash_rate real,
    btc_exchange_netflow real,
    btc_mempool_mb real
);


--
-- Name: onchain_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.onchain_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: onchain_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.onchain_history_id_seq OWNED BY public.onchain_history.id;


--
-- Name: paddle_webhook_inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.paddle_webhook_inbox (
    id bigint NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    payload json NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    last_error text,
    retry_count integer DEFAULT 0 NOT NULL
);


--
-- Name: paddle_webhook_inbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.paddle_webhook_inbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: paddle_webhook_inbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.paddle_webhook_inbox_id_seq OWNED BY public.paddle_webhook_inbox.id;


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    id integer NOT NULL,
    user_id integer NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    ip_address text
);


--
-- Name: password_reset_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.password_reset_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: password_reset_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.password_reset_tokens_id_seq OWNED BY public.password_reset_tokens.id;


--
-- Name: pending_paddle_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_paddle_events (
    id bigint NOT NULL,
    paddle_customer_id text NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    event_json json NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone
);


--
-- Name: pending_paddle_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pending_paddle_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pending_paddle_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pending_paddle_events_id_seq OWNED BY public.pending_paddle_events.id;


--
-- Name: personal_report_sections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.personal_report_sections (
    id integer NOT NULL,
    report_id integer NOT NULL,
    user_id integer NOT NULL,
    content text NOT NULL,
    audio_script text,
    cost_cents integer DEFAULT 0,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: personal_report_sections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.personal_report_sections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: personal_report_sections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.personal_report_sections_id_seq OWNED BY public.personal_report_sections.id;


--
-- Name: play_shares; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.play_shares (
    id integer NOT NULL,
    play_id integer,
    user_id integer NOT NULL,
    slug character varying(12) NOT NULL,
    snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    include_thesis boolean DEFAULT true,
    include_conditions boolean DEFAULT true,
    include_plan boolean DEFAULT true,
    image_path text,
    view_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    revoked_at timestamp with time zone,
    expires_at timestamp with time zone,
    kind character varying(16) DEFAULT 'play'::character varying NOT NULL,
    include_outcome boolean DEFAULT false,
    trade_id integer
);


--
-- Name: play_shares_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.play_shares_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: play_shares_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.play_shares_id_seq OWNED BY public.play_shares.id;


--
-- Name: plays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plays (
    id integer NOT NULL,
    user_id integer NOT NULL,
    title text NOT NULL,
    thesis text,
    type character varying(20) DEFAULT 'directional'::character varying NOT NULL,
    approach character varying(20) DEFAULT 'single'::character varying NOT NULL,
    direction character varying(20) DEFAULT 'long'::character varying,
    primary_asset character varying(20),
    related_assets jsonb DEFAULT '[]'::jsonb,
    trigger_asset character varying(20),
    conditions jsonb DEFAULT '{"logic": "all", "rules": []}'::jsonb,
    invalidation jsonb,
    timeframe character varying(20),
    expiry timestamp with time zone,
    action_plan text,
    linked_drawing_ids jsonb DEFAULT '[]'::jsonb,
    status character varying(20) DEFAULT 'watching'::character varying NOT NULL,
    status_changed_at timestamp with time zone,
    resolved_at timestamp with time zone,
    resolution text,
    report_tracking boolean DEFAULT true,
    last_report_note text,
    scorer_snapshot jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    metadata text DEFAULT '{}'::text
);


--
-- Name: plays_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.plays_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: plays_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.plays_id_seq OWNED BY public.plays.id;


--
-- Name: processed_payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_payments (
    payment_id text NOT NULL,
    processed_at timestamp with time zone DEFAULT now(),
    user_id integer
);


--
-- Name: push_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.push_subscriptions (
    user_id integer NOT NULL,
    subscription jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: regime_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.regime_history (
    id integer NOT NULL,
    "timestamp" text NOT NULL,
    regime text NOT NULL,
    score real,
    components text
);


--
-- Name: regime_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.regime_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: regime_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.regime_history_id_seq OWNED BY public.regime_history.id;


--
-- Name: reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reports (
    id integer NOT NULL,
    report_type character varying(50) DEFAULT 'full'::character varying,
    content text NOT NULL,
    assets_included text,
    generation_time_ms integer,
    summary text,
    metadata text,
    audio_script text,
    created_at timestamp with time zone DEFAULT now(),
    generation_date date,
    source_briefing text
);


--
-- Name: reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.reports_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.reports_id_seq OWNED BY public.reports.id;


--
-- Name: satisfaction_ratings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.satisfaction_ratings (
    id integer NOT NULL,
    user_id integer NOT NULL,
    rating integer NOT NULL,
    comment text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT satisfaction_ratings_rating_check CHECK (((rating >= 1) AND (rating <= 5)))
);


--
-- Name: satisfaction_ratings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.satisfaction_ratings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: satisfaction_ratings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.satisfaction_ratings_id_seq OWNED BY public.satisfaction_ratings.id;


--
-- Name: scanner_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scanner_history (
    id bigint NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    captured_date date NOT NULL,
    symbol character varying(50) NOT NULL,
    direction character varying(10) NOT NULL,
    regime character varying(20) NOT NULL,
    total integer NOT NULL,
    verdict_v1 character varying(20) NOT NULL,
    criteria jsonb NOT NULL,
    vetoes jsonb,
    price numeric,
    bar_settled boolean DEFAULT false NOT NULL,
    setup_type character varying(30),
    setup_confidence numeric(3,2)
);


--
-- Name: scanner_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scanner_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scanner_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scanner_history_id_seq OWNED BY public.scanner_history.id;


--
-- Name: scenarios; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scenarios (
    id integer NOT NULL,
    user_id integer NOT NULL,
    asset character varying(20),
    timeframe character varying(10),
    label text,
    thesis text,
    alternative_thesis text,
    notes text,
    scenario_type character varying(20) DEFAULT 'trade_setup'::character varying NOT NULL,
    direction character varying(10),
    conviction integer,
    entry_low real,
    entry_high real,
    invalidation real,
    targets jsonb DEFAULT '[]'::jsonb,
    stop_loss real,
    accumulation_budget real,
    accumulation_used real DEFAULT 0,
    conditions jsonb DEFAULT '{"logic": "all", "rules": []}'::jsonb,
    milestones jsonb DEFAULT '[]'::jsonb,
    status character varying(20) DEFAULT 'watching'::character varying NOT NULL,
    status_changed_at timestamp with time zone DEFAULT now(),
    is_active boolean DEFAULT true,
    journal_trade_id integer,
    linked_drawing_ids jsonb DEFAULT '[]'::jsonb,
    outcome character varying(20),
    outcome_notes text,
    resolved_at timestamp with time zone,
    source character varying(50),
    tags text[],
    screenshot_path text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT scenarios_conviction_check CHECK (((conviction >= 1) AND (conviction <= 5)))
);


--
-- Name: scenarios_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scenarios_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scenarios_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scenarios_id_seq OWNED BY public.scenarios.id;


--
-- Name: score_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.score_snapshots (
    id integer NOT NULL,
    journal_id integer NOT NULL,
    user_id integer NOT NULL,
    snapshot_type text NOT NULL,
    asset text NOT NULL,
    direction text NOT NULL,
    captured_at timestamp with time zone,
    score_trend integer,
    score_momentum integer,
    score_rsi integer,
    score_structure integer,
    score_level integer,
    score_macro integer,
    score_total integer,
    verdict text,
    vetoes text,
    price real,
    consensio_1d_signal integer,
    consensio_1d_positions integer,
    consensio_3d_signal integer,
    consensio_3d_positions integer,
    consensio_1w_signal integer,
    consensio_1w_positions integer,
    consensio_total_positions integer,
    ema_ribbon_trend text,
    ema_ribbon_gap_pct real,
    macd_histogram real,
    macd_momentum text,
    weekly_rsi real,
    rsi_div_daily text,
    rsi_div_weekly text,
    structure_trend text,
    last_high_type text,
    last_low_type text,
    cpr_pivot real,
    cpr_tc real,
    cpr_bc real,
    bb_upper real,
    bb_lower real,
    bb_middle real,
    adx_value real,
    volume_ratio real,
    regime text,
    regime_score integer,
    fear_greed integer,
    breadth_pct real,
    funding_rate real,
    entry_price real,
    stop_price real,
    tp1_price real,
    setup_type text,
    setup_readiness text,
    setup_confidence real
);


--
-- Name: score_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.score_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: score_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.score_snapshots_id_seq OWNED BY public.score_snapshots.id;


--
-- Name: sentiment_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sentiment_history (
    id integer NOT NULL,
    "timestamp" text NOT NULL,
    fear_greed real,
    funding_rate real,
    open_interest_usd real,
    oi_change_pct real,
    mvrv_zscore real,
    alt_season real,
    btc_long_pct real,
    btc_price real
);


--
-- Name: sentiment_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sentiment_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sentiment_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sentiment_history_id_seq OWNED BY public.sentiment_history.id;


--
-- Name: setup_checklists; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.setup_checklists (
    id integer NOT NULL,
    user_id integer NOT NULL,
    setup_type character varying(30) NOT NULL,
    name character varying(50),
    checklist_items text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: setup_checklists_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.setup_checklists_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: setup_checklists_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.setup_checklists_id_seq OWNED BY public.setup_checklists.id;


--
-- Name: strategies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strategies (
    id text NOT NULL,
    user_id integer NOT NULL,
    name text NOT NULL,
    entry_formula text NOT NULL,
    exit_formula text NOT NULL,
    direction text DEFAULT 'long'::text NOT NULL,
    capital real DEFAULT 10000 NOT NULL,
    size_percent real DEFAULT 100 NOT NULL,
    commission_percent real DEFAULT 0.1 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    params jsonb
);


--
-- Name: tier_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tier_config (
    tier text NOT NULL,
    limits_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by integer
);


--
-- Name: trade_journal; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trade_journal (
    id integer NOT NULL,
    user_id integer NOT NULL,
    asset character varying(20) NOT NULL,
    timeframe character varying(10),
    direction character varying(10),
    entry_price real,
    target_price real,
    stop_price real,
    exit_price real,
    exit_reason character varying(20),
    rr_planned real,
    rr_actual real,
    setup_type character varying(30),
    indicators_at_entry text,
    drawing_id text,
    scenario_id integer,
    opened_at timestamp with time zone DEFAULT now(),
    closed_at timestamp with time zone,
    notes text,
    size real,
    fees real DEFAULT 0,
    pnl_currency real,
    account_tag character varying(30),
    confidence integer,
    mood integer,
    mistake_tags text,
    checklist_results text,
    checklist_template_id integer,
    ai_review text,
    trade_grade text,
    session_id integer,
    tags text,
    mfe real,
    mae real,
    mfe_price real,
    mae_price real,
    exit_efficiency real,
    mfe_computed_at timestamp without time zone
);


--
-- Name: trade_journal_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trade_journal_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trade_journal_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trade_journal_id_seq OWNED BY public.trade_journal.id;


--
-- Name: user_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_alerts (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    alert_type text DEFAULT 'price'::text NOT NULL,
    symbol text,
    active boolean DEFAULT true NOT NULL,
    triggered boolean DEFAULT false NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_settings (
    id integer NOT NULL,
    user_id integer NOT NULL,
    watchlist_data text DEFAULT '{}'::text,
    preferences text DEFAULT '{}'::text,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: user_settings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_settings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: user_settings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_settings_id_seq OWNED BY public.user_settings.id;


--
-- Name: user_tier_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_tier_overrides (
    user_id integer NOT NULL,
    override_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    reason text,
    updated_at timestamp with time zone DEFAULT now(),
    updated_by integer
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    email text NOT NULL,
    password_hash text NOT NULL,
    display_name text,
    is_admin boolean DEFAULT false,
    is_active boolean DEFAULT true,
    tier text DEFAULT 'free'::text,
    invited_by integer,
    created_at timestamp with time zone DEFAULT now(),
    last_login timestamp with time zone,
    last_active timestamp with time zone,
    email_verified boolean DEFAULT false,
    ai_credits_cents integer DEFAULT 0,
    trader_profile text,
    subscription_expires_at timestamp with time zone,
    subscription_payment_id text,
    paddle_customer_id text,
    paddle_subscription_id text,
    tos_accepted_at timestamp with time zone,
    tos_accepted_ip text,
    tos_accepted_ua text,
    deleted_at timestamp with time zone,
    age_attested_at timestamp without time zone,
    age_attested_ip character varying(45),
    flags jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: waitlist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.waitlist (
    id integer NOT NULL,
    email text NOT NULL,
    status text DEFAULT 'waiting'::text NOT NULL,
    source text,
    cohort text,
    invite_code text,
    ip text,
    created_at timestamp with time zone DEFAULT now(),
    invited_at timestamp with time zone,
    registered_at timestamp with time zone,
    CONSTRAINT waitlist_status_check CHECK ((status = ANY (ARRAY['waiting'::text, 'invited'::text, 'registered'::text])))
);


--
-- Name: waitlist_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.waitlist_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: waitlist_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.waitlist_id_seq OWNED BY public.waitlist.id;


--
-- Name: widget_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.widget_tokens (
    id integer NOT NULL,
    user_id integer NOT NULL,
    name text,
    created_at timestamp with time zone DEFAULT now(),
    token_hash text NOT NULL,
    token_suffix text NOT NULL,
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false
);


--
-- Name: widget_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.widget_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: widget_tokens_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.widget_tokens_id_seq OWNED BY public.widget_tokens.id;


--
-- Name: anomalies; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.anomalies (
    id integer NOT NULL,
    ts date NOT NULL,
    domain text NOT NULL,
    metric text NOT NULL,
    asset text,
    zscore numeric NOT NULL,
    raw_value numeric,
    window_days integer NOT NULL,
    resolved_at date,
    metadata jsonb
);


--
-- Name: anomalies_id_seq; Type: SEQUENCE; Schema: quant; Owner: -
--

CREATE SEQUENCE quant.anomalies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: anomalies_id_seq; Type: SEQUENCE OWNED BY; Schema: quant; Owner: -
--

ALTER SEQUENCE quant.anomalies_id_seq OWNED BY quant.anomalies.id;


--
-- Name: collection_log; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.collection_log (
    id integer NOT NULL,
    collector text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text DEFAULT 'running'::text NOT NULL,
    rows_upserted integer DEFAULT 0,
    error_msg text,
    metadata jsonb
);


--
-- Name: collection_log_id_seq; Type: SEQUENCE; Schema: quant; Owner: -
--

CREATE SEQUENCE quant.collection_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: collection_log_id_seq; Type: SEQUENCE OWNED BY; Schema: quant; Owner: -
--

ALTER SEQUENCE quant.collection_log_id_seq OWNED BY quant.collection_log.id;


--
-- Name: correlations; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.correlations (
    ts date NOT NULL,
    metric_a text NOT NULL,
    metric_b text NOT NULL,
    window_days integer NOT NULL,
    correlation numeric NOT NULL,
    p_value numeric,
    n_obs integer,
    method text DEFAULT 'pearson'::text NOT NULL,
    stability numeric,
    q_value numeric,
    ci_low numeric,
    ci_high numeric,
    significant boolean
);


--
-- Name: derivatives; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.derivatives (
    ts date NOT NULL,
    asset text NOT NULL,
    funding_rate_avg numeric,
    oi_usd numeric,
    oi_change_pct numeric,
    long_short_ratio numeric,
    liquidations_long numeric,
    liquidations_short numeric,
    basis_pct numeric,
    options_put_call_ratio numeric,
    options_oi_usd numeric,
    options_max_pain numeric,
    options_iv_atm numeric,
    dvol numeric,
    skew_25d numeric,
    iv_7d numeric,
    iv_30d numeric,
    iv_90d numeric,
    term_structure_slope numeric,
    gex_net numeric,
    gex_flip_price numeric,
    iv_rv_premium numeric,
    funding_rate_cross_avg numeric
);


--
-- Name: events; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.events (
    id integer NOT NULL,
    ts date NOT NULL,
    event_type text NOT NULL,
    title text NOT NULL,
    description text,
    impact text,
    metadata jsonb,
    actual text,
    actual_filled_at timestamp with time zone,
    expected text
);


--
-- Name: events_id_seq; Type: SEQUENCE; Schema: quant; Owner: -
--

CREATE SEQUENCE quant.events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: events_id_seq; Type: SEQUENCE OWNED BY; Schema: quant; Owner: -
--

ALTER SEQUENCE quant.events_id_seq OWNED BY quant.events.id;


--
-- Name: liq_heatmap_snapshots; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.liq_heatmap_snapshots (
    symbol text NOT NULL,
    cycle_ts timestamp with time zone NOT NULL,
    price numeric,
    band_low numeric,
    band_high numeric,
    bands jsonb NOT NULL
);


--
-- Name: liquidation_events; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.liquidation_events (
    id integer NOT NULL,
    ts timestamp with time zone NOT NULL,
    asset text NOT NULL,
    side text NOT NULL,
    price numeric NOT NULL,
    qty numeric,
    notional_usd numeric
);


--
-- Name: liquidation_events_id_seq; Type: SEQUENCE; Schema: quant; Owner: -
--

CREATE SEQUENCE quant.liquidation_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: liquidation_events_id_seq; Type: SEQUENCE OWNED BY; Schema: quant; Owner: -
--

ALTER SEQUENCE quant.liquidation_events_id_seq OWNED BY quant.liquidation_events.id;


--
-- Name: macro; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.macro (
    ts date NOT NULL,
    metric text NOT NULL,
    value numeric NOT NULL,
    released_at timestamp with time zone,
    revision integer DEFAULT 0
);


--
-- Name: onchain; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.onchain (
    ts date NOT NULL,
    asset text NOT NULL,
    metric text NOT NULL,
    value numeric NOT NULL,
    source text
);


--
-- Name: price_daily; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.price_daily (
    ts date NOT NULL,
    asset text NOT NULL,
    source text DEFAULT 'binance'::text NOT NULL,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    volume numeric
);


--
-- Name: sentiment; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.sentiment (
    ts date NOT NULL,
    metric text NOT NULL,
    asset text DEFAULT '_GLOBAL_'::text NOT NULL,
    value numeric NOT NULL,
    source text,
    released_at timestamp with time zone
);


--
-- Name: signals; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.signals (
    ts date NOT NULL,
    source text NOT NULL,
    metric text NOT NULL,
    asset text DEFAULT '_GLOBAL_'::text NOT NULL,
    value numeric NOT NULL,
    metadata jsonb
);


--
-- Name: zscores; Type: TABLE; Schema: quant; Owner: -
--

CREATE TABLE quant.zscores (
    ts date NOT NULL,
    domain text NOT NULL,
    metric text NOT NULL,
    asset text DEFAULT '_GLOBAL_'::text NOT NULL,
    window_days integer NOT NULL,
    zscore numeric NOT NULL,
    raw_value numeric,
    mean numeric,
    std numeric,
    percentile numeric
);


--
-- Name: ai_conversations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_conversations ALTER COLUMN id SET DEFAULT nextval('public.ai_conversations_id_seq'::regclass);


--
-- Name: ai_deposits id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_deposits ALTER COLUMN id SET DEFAULT nextval('public.ai_deposits_id_seq'::regclass);


--
-- Name: ai_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_messages ALTER COLUMN id SET DEFAULT nextval('public.ai_messages_id_seq'::regclass);


--
-- Name: ai_usage id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage ALTER COLUMN id SET DEFAULT nextval('public.ai_usage_id_seq'::regclass);


--
-- Name: ai_usage_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_log ALTER COLUMN id SET DEFAULT nextval('public.ai_usage_log_id_seq'::regclass);


--
-- Name: api_keys id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys ALTER COLUMN id SET DEFAULT nextval('public.api_keys_id_seq'::regclass);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: auth_providers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_providers ALTER COLUMN id SET DEFAULT nextval('public.auth_providers_id_seq'::regclass);


--
-- Name: backtest_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_runs ALTER COLUMN id SET DEFAULT nextval('public.backtest_runs_id_seq'::regclass);


--
-- Name: checklist_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_templates ALTER COLUMN id SET DEFAULT nextval('public.checklist_templates_id_seq'::regclass);


--
-- Name: context_recipes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_recipes ALTER COLUMN id SET DEFAULT nextval('public.context_recipes_id_seq'::regclass);


--
-- Name: drawings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.drawings ALTER COLUMN id SET DEFAULT nextval('public.drawings_id_seq'::regclass);


--
-- Name: email_verifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verifications ALTER COLUMN id SET DEFAULT nextval('public.email_verifications_id_seq'::regclass);


--
-- Name: exchange_connections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_connections ALTER COLUMN id SET DEFAULT nextval('public.exchange_connections_id_seq'::regclass);


--
-- Name: exchange_import_suggestions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_import_suggestions ALTER COLUMN id SET DEFAULT nextval('public.exchange_import_suggestions_id_seq'::regclass);


--
-- Name: exchange_sync_fills id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_sync_fills ALTER COLUMN id SET DEFAULT nextval('public.exchange_sync_fills_id_seq'::regclass);


--
-- Name: feedback id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback ALTER COLUMN id SET DEFAULT nextval('public.feedback_id_seq'::regclass);


--
-- Name: invites id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites ALTER COLUMN id SET DEFAULT nextval('public.invites_id_seq'::regclass);


--
-- Name: journal_goals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_goals ALTER COLUMN id SET DEFAULT nextval('public.journal_goals_id_seq'::regclass);


--
-- Name: journal_playbooks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_playbooks ALTER COLUMN id SET DEFAULT nextval('public.journal_playbooks_id_seq'::regclass);


--
-- Name: journal_screenshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_screenshots ALTER COLUMN id SET DEFAULT nextval('public.journal_screenshots_id_seq'::regclass);


--
-- Name: journal_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_sessions ALTER COLUMN id SET DEFAULT nextval('public.journal_sessions_id_seq'::regclass);


--
-- Name: journal_views id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_views ALTER COLUMN id SET DEFAULT nextval('public.journal_views_id_seq'::regclass);


--
-- Name: mcp_intent_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_intent_log ALTER COLUMN id SET DEFAULT nextval('public.mcp_intent_log_id_seq'::regclass);


--
-- Name: monthly_goals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monthly_goals ALTER COLUMN id SET DEFAULT nextval('public.monthly_goals_id_seq'::regclass);


--
-- Name: onboarding id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding ALTER COLUMN id SET DEFAULT nextval('public.onboarding_id_seq'::regclass);


--
-- Name: onchain_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onchain_history ALTER COLUMN id SET DEFAULT nextval('public.onchain_history_id_seq'::regclass);


--
-- Name: paddle_webhook_inbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paddle_webhook_inbox ALTER COLUMN id SET DEFAULT nextval('public.paddle_webhook_inbox_id_seq'::regclass);


--
-- Name: password_reset_tokens id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens ALTER COLUMN id SET DEFAULT nextval('public.password_reset_tokens_id_seq'::regclass);


--
-- Name: pending_paddle_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_paddle_events ALTER COLUMN id SET DEFAULT nextval('public.pending_paddle_events_id_seq'::regclass);


--
-- Name: personal_report_sections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_report_sections ALTER COLUMN id SET DEFAULT nextval('public.personal_report_sections_id_seq'::regclass);


--
-- Name: play_shares id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares ALTER COLUMN id SET DEFAULT nextval('public.play_shares_id_seq'::regclass);


--
-- Name: plays id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plays ALTER COLUMN id SET DEFAULT nextval('public.plays_id_seq'::regclass);


--
-- Name: regime_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_history ALTER COLUMN id SET DEFAULT nextval('public.regime_history_id_seq'::regclass);


--
-- Name: reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports ALTER COLUMN id SET DEFAULT nextval('public.reports_id_seq'::regclass);


--
-- Name: satisfaction_ratings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.satisfaction_ratings ALTER COLUMN id SET DEFAULT nextval('public.satisfaction_ratings_id_seq'::regclass);


--
-- Name: scanner_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanner_history ALTER COLUMN id SET DEFAULT nextval('public.scanner_history_id_seq'::regclass);


--
-- Name: scenarios id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenarios ALTER COLUMN id SET DEFAULT nextval('public.scenarios_id_seq'::regclass);


--
-- Name: score_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.score_snapshots ALTER COLUMN id SET DEFAULT nextval('public.score_snapshots_id_seq'::regclass);


--
-- Name: sentiment_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sentiment_history ALTER COLUMN id SET DEFAULT nextval('public.sentiment_history_id_seq'::regclass);


--
-- Name: setup_checklists id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.setup_checklists ALTER COLUMN id SET DEFAULT nextval('public.setup_checklists_id_seq'::regclass);


--
-- Name: trade_journal id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_journal ALTER COLUMN id SET DEFAULT nextval('public.trade_journal_id_seq'::regclass);


--
-- Name: user_settings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_settings ALTER COLUMN id SET DEFAULT nextval('public.user_settings_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: waitlist id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.waitlist ALTER COLUMN id SET DEFAULT nextval('public.waitlist_id_seq'::regclass);


--
-- Name: widget_tokens id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_tokens ALTER COLUMN id SET DEFAULT nextval('public.widget_tokens_id_seq'::regclass);


--
-- Name: anomalies id; Type: DEFAULT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.anomalies ALTER COLUMN id SET DEFAULT nextval('quant.anomalies_id_seq'::regclass);


--
-- Name: collection_log id; Type: DEFAULT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.collection_log ALTER COLUMN id SET DEFAULT nextval('quant.collection_log_id_seq'::regclass);


--
-- Name: events id; Type: DEFAULT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.events ALTER COLUMN id SET DEFAULT nextval('quant.events_id_seq'::regclass);


--
-- Name: liquidation_events id; Type: DEFAULT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.liquidation_events ALTER COLUMN id SET DEFAULT nextval('quant.liquidation_events_id_seq'::regclass);


--
-- Name: ai_conversations ai_conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_conversations
    ADD CONSTRAINT ai_conversations_pkey PRIMARY KEY (id);


--
-- Name: ai_credits ai_credits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_credits
    ADD CONSTRAINT ai_credits_pkey PRIMARY KEY (user_id);


--
-- Name: ai_deposits ai_deposits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_deposits
    ADD CONSTRAINT ai_deposits_pkey PRIMARY KEY (id);


--
-- Name: ai_messages ai_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_messages
    ADD CONSTRAINT ai_messages_pkey PRIMARY KEY (id);


--
-- Name: ai_usage_log ai_usage_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_log
    ADD CONSTRAINT ai_usage_log_pkey PRIMARY KEY (id);


--
-- Name: ai_usage ai_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT ai_usage_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_token_hash_key UNIQUE (token_hash);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: auth_providers auth_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_providers
    ADD CONSTRAINT auth_providers_pkey PRIMARY KEY (id);


--
-- Name: auth_providers auth_providers_provider_provider_uid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_providers
    ADD CONSTRAINT auth_providers_provider_provider_uid_key UNIQUE (provider, provider_uid);


--
-- Name: auth_providers auth_providers_user_id_provider_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_providers
    ADD CONSTRAINT auth_providers_user_id_provider_key UNIQUE (user_id, provider);


--
-- Name: backtest_runs backtest_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_runs
    ADD CONSTRAINT backtest_runs_pkey PRIMARY KEY (id);


--
-- Name: checklist_templates checklist_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_templates
    ADD CONSTRAINT checklist_templates_pkey PRIMARY KEY (id);


--
-- Name: context_recipes context_recipes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_recipes
    ADD CONSTRAINT context_recipes_pkey PRIMARY KEY (id);


--
-- Name: custom_formulas custom_formulas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_formulas
    ADD CONSTRAINT custom_formulas_pkey PRIMARY KEY (id);


--
-- Name: drawings drawings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.drawings
    ADD CONSTRAINT drawings_pkey PRIMARY KEY (id);


--
-- Name: email_verifications email_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT email_verifications_pkey PRIMARY KEY (id);


--
-- Name: exchange_connections exchange_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_connections
    ADD CONSTRAINT exchange_connections_pkey PRIMARY KEY (id);


--
-- Name: exchange_connections exchange_connections_user_id_exchange_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_connections
    ADD CONSTRAINT exchange_connections_user_id_exchange_key UNIQUE (user_id, exchange);


--
-- Name: exchange_import_suggestions exchange_import_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_import_suggestions
    ADD CONSTRAINT exchange_import_suggestions_pkey PRIMARY KEY (id);


--
-- Name: exchange_sync_fills exchange_sync_fills_connection_id_exchange_trade_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_sync_fills
    ADD CONSTRAINT exchange_sync_fills_connection_id_exchange_trade_id_key UNIQUE (connection_id, exchange_trade_id);


--
-- Name: exchange_sync_fills exchange_sync_fills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_sync_fills
    ADD CONSTRAINT exchange_sync_fills_pkey PRIMARY KEY (id);


--
-- Name: feedback feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);


--
-- Name: invites invites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_pkey PRIMARY KEY (id);


--
-- Name: journal_goals journal_goals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_goals
    ADD CONSTRAINT journal_goals_pkey PRIMARY KEY (id);


--
-- Name: journal_goals journal_goals_user_id_month_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_goals
    ADD CONSTRAINT journal_goals_user_id_month_key UNIQUE (user_id, month);


--
-- Name: journal_playbooks journal_playbooks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_playbooks
    ADD CONSTRAINT journal_playbooks_pkey PRIMARY KEY (id);


--
-- Name: journal_screenshots journal_screenshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_screenshots
    ADD CONSTRAINT journal_screenshots_pkey PRIMARY KEY (id);


--
-- Name: journal_sessions journal_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_sessions
    ADD CONSTRAINT journal_sessions_pkey PRIMARY KEY (id);


--
-- Name: journal_sessions journal_sessions_user_id_date_session_label_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_sessions
    ADD CONSTRAINT journal_sessions_user_id_date_session_label_key UNIQUE (user_id, date, session_label);


--
-- Name: journal_views journal_views_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_views
    ADD CONSTRAINT journal_views_pkey PRIMARY KEY (id);


--
-- Name: mcp_intent_log mcp_intent_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_intent_log
    ADD CONSTRAINT mcp_intent_log_pkey PRIMARY KEY (id);


--
-- Name: monthly_goals monthly_goals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monthly_goals
    ADD CONSTRAINT monthly_goals_pkey PRIMARY KEY (id);


--
-- Name: monthly_goals monthly_goals_user_id_month_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.monthly_goals
    ADD CONSTRAINT monthly_goals_user_id_month_key UNIQUE (user_id, month);


--
-- Name: onboarding onboarding_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding
    ADD CONSTRAINT onboarding_pkey PRIMARY KEY (id);


--
-- Name: onboarding onboarding_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding
    ADD CONSTRAINT onboarding_user_id_key UNIQUE (user_id);


--
-- Name: onchain_history onchain_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onchain_history
    ADD CONSTRAINT onchain_history_pkey PRIMARY KEY (id);


--
-- Name: paddle_webhook_inbox paddle_webhook_inbox_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paddle_webhook_inbox
    ADD CONSTRAINT paddle_webhook_inbox_event_id_key UNIQUE (event_id);


--
-- Name: paddle_webhook_inbox paddle_webhook_inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.paddle_webhook_inbox
    ADD CONSTRAINT paddle_webhook_inbox_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);


--
-- Name: pending_paddle_events pending_paddle_events_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_paddle_events
    ADD CONSTRAINT pending_paddle_events_event_id_key UNIQUE (event_id);


--
-- Name: pending_paddle_events pending_paddle_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_paddle_events
    ADD CONSTRAINT pending_paddle_events_pkey PRIMARY KEY (id);


--
-- Name: personal_report_sections personal_report_sections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_report_sections
    ADD CONSTRAINT personal_report_sections_pkey PRIMARY KEY (id);


--
-- Name: personal_report_sections personal_report_sections_report_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_report_sections
    ADD CONSTRAINT personal_report_sections_report_id_user_id_key UNIQUE (report_id, user_id);


--
-- Name: play_shares play_shares_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares
    ADD CONSTRAINT play_shares_pkey PRIMARY KEY (id);


--
-- Name: play_shares play_shares_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares
    ADD CONSTRAINT play_shares_slug_key UNIQUE (slug);


--
-- Name: plays plays_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plays
    ADD CONSTRAINT plays_pkey PRIMARY KEY (id);


--
-- Name: processed_payments processed_payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_payments
    ADD CONSTRAINT processed_payments_pkey PRIMARY KEY (payment_id);


--
-- Name: push_subscriptions push_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.push_subscriptions
    ADD CONSTRAINT push_subscriptions_pkey PRIMARY KEY (user_id);


--
-- Name: regime_history regime_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.regime_history
    ADD CONSTRAINT regime_history_pkey PRIMARY KEY (id);


--
-- Name: reports reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reports
    ADD CONSTRAINT reports_pkey PRIMARY KEY (id);


--
-- Name: satisfaction_ratings satisfaction_ratings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.satisfaction_ratings
    ADD CONSTRAINT satisfaction_ratings_pkey PRIMARY KEY (id);


--
-- Name: scanner_history scanner_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanner_history
    ADD CONSTRAINT scanner_history_pkey PRIMARY KEY (id);


--
-- Name: scanner_history scanner_history_symbol_captured_date_direction_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanner_history
    ADD CONSTRAINT scanner_history_symbol_captured_date_direction_key UNIQUE (symbol, captured_date, direction);


--
-- Name: scenarios scenarios_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenarios
    ADD CONSTRAINT scenarios_pkey PRIMARY KEY (id);


--
-- Name: score_snapshots score_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.score_snapshots
    ADD CONSTRAINT score_snapshots_pkey PRIMARY KEY (id);


--
-- Name: sentiment_history sentiment_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sentiment_history
    ADD CONSTRAINT sentiment_history_pkey PRIMARY KEY (id);


--
-- Name: setup_checklists setup_checklists_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.setup_checklists
    ADD CONSTRAINT setup_checklists_pkey PRIMARY KEY (id);


--
-- Name: setup_checklists setup_checklists_user_id_setup_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.setup_checklists
    ADD CONSTRAINT setup_checklists_user_id_setup_type_key UNIQUE (user_id, setup_type);


--
-- Name: strategies strategies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_pkey PRIMARY KEY (id);


--
-- Name: tier_config tier_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tier_config
    ADD CONSTRAINT tier_config_pkey PRIMARY KEY (tier);


--
-- Name: trade_journal trade_journal_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_journal
    ADD CONSTRAINT trade_journal_pkey PRIMARY KEY (id);


--
-- Name: user_alerts user_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_alerts
    ADD CONSTRAINT user_alerts_pkey PRIMARY KEY (id);


--
-- Name: user_settings user_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_settings
    ADD CONSTRAINT user_settings_pkey PRIMARY KEY (id);


--
-- Name: user_settings user_settings_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_settings
    ADD CONSTRAINT user_settings_user_id_key UNIQUE (user_id);


--
-- Name: user_tier_overrides user_tier_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tier_overrides
    ADD CONSTRAINT user_tier_overrides_pkey PRIMARY KEY (user_id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: waitlist waitlist_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.waitlist
    ADD CONSTRAINT waitlist_email_key UNIQUE (email);


--
-- Name: waitlist waitlist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.waitlist
    ADD CONSTRAINT waitlist_pkey PRIMARY KEY (id);


--
-- Name: widget_tokens widget_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_tokens
    ADD CONSTRAINT widget_tokens_pkey PRIMARY KEY (id);


--
-- Name: widget_tokens widget_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_tokens
    ADD CONSTRAINT widget_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: anomalies anomalies_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.anomalies
    ADD CONSTRAINT anomalies_pkey PRIMARY KEY (id);


--
-- Name: collection_log collection_log_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.collection_log
    ADD CONSTRAINT collection_log_pkey PRIMARY KEY (id);


--
-- Name: correlations correlations_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.correlations
    ADD CONSTRAINT correlations_pkey PRIMARY KEY (ts, metric_a, metric_b, window_days, method);


--
-- Name: derivatives derivatives_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.derivatives
    ADD CONSTRAINT derivatives_pkey PRIMARY KEY (ts, asset);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: liq_heatmap_snapshots liq_heatmap_snapshots_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.liq_heatmap_snapshots
    ADD CONSTRAINT liq_heatmap_snapshots_pkey PRIMARY KEY (symbol, cycle_ts);


--
-- Name: liquidation_events liquidation_events_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.liquidation_events
    ADD CONSTRAINT liquidation_events_pkey PRIMARY KEY (id);


--
-- Name: macro macro_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.macro
    ADD CONSTRAINT macro_pkey PRIMARY KEY (ts, metric);


--
-- Name: onchain onchain_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.onchain
    ADD CONSTRAINT onchain_pkey PRIMARY KEY (ts, asset, metric);


--
-- Name: price_daily price_daily_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.price_daily
    ADD CONSTRAINT price_daily_pkey PRIMARY KEY (ts, asset, source);


--
-- Name: sentiment sentiment_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.sentiment
    ADD CONSTRAINT sentiment_pkey PRIMARY KEY (ts, metric, asset);


--
-- Name: signals signals_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.signals
    ADD CONSTRAINT signals_pkey PRIMARY KEY (ts, source, metric, asset);


--
-- Name: zscores zscores_pkey; Type: CONSTRAINT; Schema: quant; Owner: -
--

ALTER TABLE ONLY quant.zscores
    ADD CONSTRAINT zscores_pkey PRIMARY KEY (ts, domain, metric, asset, window_days);


--
-- Name: idx_ai_conv_cleanup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_conv_cleanup ON public.ai_conversations USING btree (updated_at);


--
-- Name: idx_ai_conv_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_conv_user ON public.ai_conversations USING btree (user_id, updated_at DESC);


--
-- Name: idx_ai_msg_conv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_msg_conv ON public.ai_messages USING btree (conversation_id, created_at);


--
-- Name: idx_ai_usage_log_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_usage_log_conversation ON public.ai_usage_log USING btree (user_id, conversation_id);


--
-- Name: idx_ai_usage_log_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_usage_log_user ON public.ai_usage_log USING btree (user_id, created_at);


--
-- Name: idx_ai_usage_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_usage_user ON public.ai_usage USING btree (user_id, created_at);


--
-- Name: idx_api_keys_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_hash ON public.api_keys USING btree (token_hash);


--
-- Name: idx_api_keys_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_user ON public.api_keys USING btree (user_id);


--
-- Name: idx_auth_providers_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_providers_lookup ON public.auth_providers USING btree (provider, provider_uid);


--
-- Name: idx_auth_providers_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_providers_user ON public.auth_providers USING btree (user_id);


--
-- Name: idx_backtest_runs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_runs_created_at ON public.backtest_runs USING btree (created_at DESC);


--
-- Name: idx_backtest_runs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_runs_user_id ON public.backtest_runs USING btree (user_id);


--
-- Name: idx_backtest_runs_user_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_backtest_runs_user_model ON public.backtest_runs USING btree (user_id, model, created_at DESC);


--
-- Name: idx_custom_formulas_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_custom_formulas_user ON public.custom_formulas USING btree (user_id);


--
-- Name: idx_drawings_asset_tf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_drawings_asset_tf ON public.drawings USING btree (asset, timeframe);


--
-- Name: idx_drawings_scenario; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_drawings_scenario ON public.drawings USING btree (scenario_id);


--
-- Name: idx_drawings_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_drawings_user ON public.drawings USING btree (user_id);


--
-- Name: idx_ev_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ev_token_hash ON public.email_verifications USING btree (token_hash);


--
-- Name: idx_exchange_connections_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exchange_connections_status ON public.exchange_connections USING btree (status);


--
-- Name: idx_exchange_connections_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exchange_connections_user ON public.exchange_connections USING btree (user_id);


--
-- Name: idx_exchange_import_suggestions_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exchange_import_suggestions_user ON public.exchange_import_suggestions USING btree (user_id, status);


--
-- Name: idx_exchange_sync_fills_conn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exchange_sync_fills_conn ON public.exchange_sync_fills USING btree (connection_id);


--
-- Name: idx_journal_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_journal_user ON public.trade_journal USING btree (user_id, opened_at);


--
-- Name: idx_personal_report_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_personal_report_user ON public.personal_report_sections USING btree (user_id, report_id);


--
-- Name: idx_play_shares_play; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_play_shares_play ON public.play_shares USING btree (play_id);


--
-- Name: idx_play_shares_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_play_shares_slug ON public.play_shares USING btree (slug);


--
-- Name: idx_play_shares_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_play_shares_user ON public.play_shares USING btree (user_id, created_at DESC);


--
-- Name: idx_plays_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plays_asset ON public.plays USING btree (primary_asset);


--
-- Name: idx_plays_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plays_updated ON public.plays USING btree (updated_at DESC);


--
-- Name: idx_plays_user_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plays_user_status ON public.plays USING btree (user_id, status);


--
-- Name: idx_prt_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prt_token_hash ON public.password_reset_tokens USING btree (token_hash);


--
-- Name: idx_recipes_default; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_recipes_default ON public.context_recipes USING btree (user_id) WHERE (is_default = true);


--
-- Name: idx_recipes_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recipes_user ON public.context_recipes USING btree (user_id);


--
-- Name: idx_regime_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_regime_ts ON public.regime_history USING btree ("timestamp");


--
-- Name: idx_reports_type_day; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_reports_type_day ON public.reports USING btree (report_type, generation_date) WHERE (generation_date IS NOT NULL);


--
-- Name: idx_satisfaction_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_satisfaction_user ON public.satisfaction_ratings USING btree (user_id, created_at DESC);


--
-- Name: idx_scanner_history_captured_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scanner_history_captured_at ON public.scanner_history USING btree (captured_at DESC);


--
-- Name: idx_scanner_history_symbol_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scanner_history_symbol_date ON public.scanner_history USING btree (symbol, captured_date DESC);


--
-- Name: idx_scenarios_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenarios_asset ON public.scenarios USING btree (asset);


--
-- Name: idx_scenarios_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenarios_user ON public.scenarios USING btree (user_id, asset, timeframe);


--
-- Name: idx_scenarios_user_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scenarios_user_status ON public.scenarios USING btree (user_id, status);


--
-- Name: idx_screenshots_journal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_screenshots_journal ON public.journal_screenshots USING btree (journal_id);


--
-- Name: idx_sentiment_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sentiment_ts ON public.sentiment_history USING btree ("timestamp");


--
-- Name: idx_sessions_user_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sessions_user_date ON public.journal_sessions USING btree (user_id, date);


--
-- Name: idx_strategies_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strategies_user ON public.strategies USING btree (user_id);


--
-- Name: idx_user_alerts_type_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_alerts_type_active ON public.user_alerts USING btree (alert_type, active);


--
-- Name: idx_user_alerts_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_alerts_user ON public.user_alerts USING btree (user_id);


--
-- Name: idx_users_paddle_cid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_paddle_cid ON public.users USING btree (paddle_customer_id);


--
-- Name: idx_users_paddle_sid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_paddle_sid ON public.users USING btree (paddle_subscription_id);


--
-- Name: idx_waitlist_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_waitlist_status ON public.waitlist USING btree (status, created_at);


--
-- Name: ix_audit_log_action_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_action_ts ON public.audit_log USING btree (action, ts);


--
-- Name: ix_audit_log_subject_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_subject_ts ON public.audit_log USING btree (subject_user_id, ts);


--
-- Name: ix_mcp_intent_log_tier_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_intent_log_tier_ts ON public.mcp_intent_log USING btree (tier, ts);


--
-- Name: ix_mcp_intent_log_tool_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_intent_log_tool_ts ON public.mcp_intent_log USING btree (tool, ts);


--
-- Name: ix_mcp_intent_log_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_intent_log_ts ON public.mcp_intent_log USING btree (ts);


--
-- Name: ix_mcp_intent_log_unmet_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_mcp_intent_log_unmet_ts ON public.mcp_intent_log USING btree (ts) WHERE (unmet_need IS NOT NULL);


--
-- Name: ix_paddle_inbox_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_paddle_inbox_event_type ON public.paddle_webhook_inbox USING btree (event_type, received_at);


--
-- Name: ix_paddle_inbox_unprocessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_paddle_inbox_unprocessed ON public.paddle_webhook_inbox USING btree (processed_at, retry_count);


--
-- Name: ix_pending_paddle_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pending_paddle_customer ON public.pending_paddle_events USING btree (paddle_customer_id);


--
-- Name: ix_pending_paddle_unprocessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pending_paddle_unprocessed ON public.pending_paddle_events USING btree (processed_at);


--
-- Name: idx_anomalies_active; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_anomalies_active ON quant.anomalies USING btree (ts DESC) WHERE (resolved_at IS NULL);


--
-- Name: idx_clog_collector; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_clog_collector ON quant.collection_log USING btree (collector, started_at DESC);


--
-- Name: idx_correlations_metric_b; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_correlations_metric_b ON quant.correlations USING btree (metric_b, ts);


--
-- Name: idx_events_ts; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_events_ts ON quant.events USING btree (ts);


--
-- Name: idx_events_type; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_events_type ON quant.events USING btree (event_type, ts);


--
-- Name: idx_liq_heatmap_snapshots_lookup; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_liq_heatmap_snapshots_lookup ON quant.liq_heatmap_snapshots USING btree (symbol, cycle_ts DESC);


--
-- Name: idx_liquidation_events_asset_ts; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_liquidation_events_asset_ts ON quant.liquidation_events USING btree (asset, ts DESC);


--
-- Name: idx_macro_metric; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_macro_metric ON quant.macro USING btree (metric, ts);


--
-- Name: idx_price_daily_asset; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_price_daily_asset ON quant.price_daily USING btree (asset, ts);


--
-- Name: idx_signals_lookup; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_signals_lookup ON quant.signals USING btree (source, metric, asset, ts DESC);


--
-- Name: idx_zscores_lookup; Type: INDEX; Schema: quant; Owner: -
--

CREATE INDEX idx_zscores_lookup ON quant.zscores USING btree (domain, metric, asset, window_days, ts DESC);


--
-- Name: ai_conversations ai_conversations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_conversations
    ADD CONSTRAINT ai_conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: ai_credits ai_credits_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_credits
    ADD CONSTRAINT ai_credits_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: ai_deposits ai_deposits_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_deposits
    ADD CONSTRAINT ai_deposits_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: ai_messages ai_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_messages
    ADD CONSTRAINT ai_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.ai_conversations(id) ON DELETE CASCADE;


--
-- Name: ai_usage_log ai_usage_log_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage_log
    ADD CONSTRAINT ai_usage_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: ai_usage ai_usage_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_usage
    ADD CONSTRAINT ai_usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: auth_providers auth_providers_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_providers
    ADD CONSTRAINT auth_providers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: backtest_runs backtest_runs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.backtest_runs
    ADD CONSTRAINT backtest_runs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: checklist_templates checklist_templates_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checklist_templates
    ADD CONSTRAINT checklist_templates_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: context_recipes context_recipes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.context_recipes
    ADD CONSTRAINT context_recipes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: custom_formulas custom_formulas_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.custom_formulas
    ADD CONSTRAINT custom_formulas_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: email_verifications email_verifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verifications
    ADD CONSTRAINT email_verifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: exchange_connections exchange_connections_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_connections
    ADD CONSTRAINT exchange_connections_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: exchange_import_suggestions exchange_import_suggestions_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_import_suggestions
    ADD CONSTRAINT exchange_import_suggestions_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.exchange_connections(id) ON DELETE CASCADE;


--
-- Name: exchange_import_suggestions exchange_import_suggestions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_import_suggestions
    ADD CONSTRAINT exchange_import_suggestions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: exchange_sync_fills exchange_sync_fills_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_sync_fills
    ADD CONSTRAINT exchange_sync_fills_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.exchange_connections(id) ON DELETE CASCADE;


--
-- Name: feedback feedback_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: journal_goals fk_journal_goals_user_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_goals
    ADD CONSTRAINT fk_journal_goals_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: invites invites_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: invites invites_used_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_used_by_fkey FOREIGN KEY (used_by) REFERENCES public.users(id);


--
-- Name: journal_screenshots journal_screenshots_journal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.journal_screenshots
    ADD CONSTRAINT journal_screenshots_journal_id_fkey FOREIGN KEY (journal_id) REFERENCES public.trade_journal(id) ON DELETE CASCADE;


--
-- Name: onboarding onboarding_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding
    ADD CONSTRAINT onboarding_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: personal_report_sections personal_report_sections_report_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_report_sections
    ADD CONSTRAINT personal_report_sections_report_id_fkey FOREIGN KEY (report_id) REFERENCES public.reports(id) ON DELETE CASCADE;


--
-- Name: personal_report_sections personal_report_sections_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_report_sections
    ADD CONSTRAINT personal_report_sections_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: play_shares play_shares_play_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares
    ADD CONSTRAINT play_shares_play_id_fkey FOREIGN KEY (play_id) REFERENCES public.plays(id) ON DELETE CASCADE;


--
-- Name: play_shares play_shares_trade_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares
    ADD CONSTRAINT play_shares_trade_id_fkey FOREIGN KEY (trade_id) REFERENCES public.trade_journal(id) ON DELETE CASCADE;


--
-- Name: play_shares play_shares_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.play_shares
    ADD CONSTRAINT play_shares_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: plays plays_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plays
    ADD CONSTRAINT plays_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: satisfaction_ratings satisfaction_ratings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.satisfaction_ratings
    ADD CONSTRAINT satisfaction_ratings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: scenarios scenarios_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scenarios
    ADD CONSTRAINT scenarios_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: strategies strategies_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategies
    ADD CONSTRAINT strategies_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: tier_config tier_config_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tier_config
    ADD CONSTRAINT tier_config_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);


--
-- Name: user_settings user_settings_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_settings
    ADD CONSTRAINT user_settings_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: user_tier_overrides user_tier_overrides_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tier_overrides
    ADD CONSTRAINT user_tier_overrides_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);


--
-- Name: user_tier_overrides user_tier_overrides_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tier_overrides
    ADD CONSTRAINT user_tier_overrides_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: users users_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.users(id);


--
-- Name: widget_tokens widget_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_tokens
    ADD CONSTRAINT widget_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- PostgreSQL database dump complete
--

\unrestrict TlyHDnweCp3u4ZmZoXXvTcdaRwfBaBF7dpvPiRb7nKwa2PKAgtu5kaRRCQStX94

