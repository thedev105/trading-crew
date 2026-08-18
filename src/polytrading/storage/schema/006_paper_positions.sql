CREATE TABLE paper_positions (
    position_id UUID PRIMARY KEY,
    source_evaluation_id UUID NOT NULL,
    asset VARCHAR NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    record_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE paper_position_closures (
    position_id UUID PRIMARY KEY,
    closed_at TIMESTAMPTZ NOT NULL,
    close_reason VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (close_reason IN ('REGIME_REVERSED', 'MAX_HORIZON_REACHED', 'OPERATOR_CLOSED'))
);
