CREATE TABLE candidate_relationships (
    candidate_id UUID NOT NULL,
    relationship_type VARCHAR NOT NULL,
    trial_family_id VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
