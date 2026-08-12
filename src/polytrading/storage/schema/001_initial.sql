CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE raw_envelopes (
    event_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    venue_timestamp TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    received_monotonic_ns UBIGINT NOT NULL,
    request_latency_ms DECIMAL(18, 6) NOT NULL,
    source_version VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE instrument_specs (
    instrument_id VARCHAR NOT NULL,
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    asset VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    contract_multiplier DECIMAL(38, 18) NOT NULL,
    index_family VARCHAR,
    oracle_family VARCHAR,
    mark_method VARCHAR,
    liquidation_method VARCHAR,
    collateral_asset VARCHAR,
    pnl_asset VARCHAR,
    funding_formula_id VARCHAR,
    funding_cap DECIMAL(38, 18),
    funding_interval_hours DECIMAL(38, 18) NOT NULL,
    funding_payment_offset_minutes INTEGER,
    min_notional DECIMAL(38, 18),
    quantity_step DECIMAL(38, 18),
    price_tick DECIMAL(38, 18),
    is_inverse BOOLEAN NOT NULL,
    is_prelaunch BOOLEAN NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (instrument_id, observed_at)
);

CREATE TABLE funding_observations (
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    asset VARCHAR NOT NULL,
    rate DECIMAL(38, 18) NOT NULL,
    interval_hours DECIMAL(38, 18) NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, symbol, effective_at, observed_at)
);

CREATE TABLE market_snapshots (
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    asset VARCHAR NOT NULL,
    bid DECIMAL(38, 18) NOT NULL,
    ask DECIMAL(38, 18) NOT NULL,
    mark DECIMAL(38, 18) NOT NULL,
    index DECIMAL(38, 18) NOT NULL,
    open_interest DECIMAL(38, 18),
    effective_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, symbol, effective_at, observed_at)
);

CREATE TABLE book_snapshots (
    cycle_id UUID NOT NULL,
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    asset VARCHAR NOT NULL,
    depth_limit INTEGER NOT NULL,
    sequence VARCHAR,
    effective_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (cycle_id, venue, symbol)
);

CREATE TABLE book_levels (
    cycle_id UUID NOT NULL,
    venue VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    side VARCHAR NOT NULL,
    level_index INTEGER NOT NULL,
    price DECIMAL(38, 18) NOT NULL,
    quantity DECIMAL(38, 18) NOT NULL,
    order_count INTEGER,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (cycle_id, venue, symbol, side, level_index),
    FOREIGN KEY (cycle_id, venue, symbol) REFERENCES book_snapshots (cycle_id, venue, symbol),
    CHECK (side IN ('bid', 'ask')),
    CHECK (level_index >= 0)
);

CREATE TABLE book_collection_cycles (
    cycle_id UUID PRIMARY KEY,
    request_completed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (status IN ('complete', 'failed', 'skew_exceeds_research_target'))
);

CREATE TABLE fee_schedules (
    venue VARCHAR NOT NULL,
    tier_name VARCHAR NOT NULL,
    maker_rate DECIMAL(38, 18) NOT NULL,
    taker_rate DECIMAL(38, 18) NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    source_url VARCHAR NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, tier_name, effective_from, observed_at)
);

CREATE TABLE experiments (
    experiment_id UUID PRIMARY KEY,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE journal_transactions (
    transaction_id UUID PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    description VARCHAR NOT NULL,
    evidence_ids JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE journal_postings (
    transaction_id UUID NOT NULL,
    posting_index INTEGER NOT NULL,
    account VARCHAR NOT NULL,
    asset VARCHAR NOT NULL,
    debit DECIMAL(38, 18) NOT NULL,
    credit DECIMAL(38, 18) NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (transaction_id, posting_index),
    FOREIGN KEY (transaction_id) REFERENCES journal_transactions (transaction_id),
    CHECK (posting_index >= 0),
    CHECK (debit >= 0),
    CHECK (credit >= 0),
    CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
);
