CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE prediction_raw_envelopes (
    event_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    venue_timestamp TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    received_monotonic_ns BIGINT NOT NULL,
    request_latency_ms DECIMAL(18, 6) NOT NULL,
    source_version VARCHAR NOT NULL,
    payload_json VARCHAR NOT NULL,
    source_hash VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE venue_manifests (
    venue VARCHAR NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, reviewed_at)
);

CREATE TABLE markets (
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    rule_version_id UUID NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, rule_version_id)
);

CREATE TABLE rule_versions (
    rule_version_id UUID PRIMARY KEY,
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE trades (
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    trade_id VARCHAR NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, trade_id)
);

CREATE TABLE prediction_books (
    cycle_id UUID NOT NULL,
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    outcome_token_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX prediction_books_key
    ON prediction_books (cycle_id, venue, market_id, outcome_token_id);

CREATE TABLE prediction_fee_rates (
    venue VARCHAR NOT NULL,
    market_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX prediction_fee_rates_key ON prediction_fee_rates (venue, market_id, observed_at);
