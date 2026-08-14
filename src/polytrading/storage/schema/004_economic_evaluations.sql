CREATE TABLE economic_evaluations (
    evaluation_id UUID PRIMARY KEY,
    asset VARCHAR NOT NULL,
    known_as_of TIMESTAMPTZ NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL,
    decision VARCHAR NOT NULL,
    direction VARCHAR,
    policy_hash VARCHAR NOT NULL,
    report_json JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    record_hash VARCHAR NOT NULL
);
