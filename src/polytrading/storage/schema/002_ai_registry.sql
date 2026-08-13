CREATE TABLE model_cards (
    model_id VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (model_id, version)
);

CREATE TABLE ai_artifacts (
    artifact_id UUID PRIMARY KEY,
    artifact_kind VARCHAR NOT NULL,
    model_id VARCHAR NOT NULL,
    model_version VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
