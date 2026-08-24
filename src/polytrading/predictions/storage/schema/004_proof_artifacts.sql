CREATE TABLE proof_artifacts (
    proof_id UUID NOT NULL,
    candidate_id UUID NOT NULL,
    template VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
