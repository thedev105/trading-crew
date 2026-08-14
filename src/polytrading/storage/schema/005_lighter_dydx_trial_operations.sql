CREATE TABLE lighter_dydx_funding_cycles (
    cycle_id UUID PRIMARY KEY,
    cycle_end TIMESTAMPTZ NOT NULL,
    request_completed_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL,
    record_json JSON NOT NULL,
    record_hash VARCHAR NOT NULL,
    CHECK (status IN ('complete', 'degraded', 'late'))
);
