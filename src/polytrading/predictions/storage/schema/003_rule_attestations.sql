CREATE TABLE rule_attestations (
    attestation_id UUID NOT NULL,
    venue VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    rule_version_id UUID NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
