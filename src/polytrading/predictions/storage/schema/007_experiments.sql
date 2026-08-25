CREATE TABLE trial_families (
    family_id VARCHAR NOT NULL,
    preregistered_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (family_id, preregistered_at)
);

CREATE TABLE shadow_experiments (
    experiment_id UUID NOT NULL,
    family_id VARCHAR NOT NULL,
    proposal_id UUID NOT NULL,
    scenario_id VARCHAR NOT NULL,
    terminal_state VARCHAR NOT NULL,
    reconciled BOOLEAN NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    PRIMARY KEY (experiment_id)
);
