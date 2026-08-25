CREATE TABLE shadow_plans (
    proposal_id UUID NOT NULL,
    candidate_id UUID NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE shadow_events (
    event_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE shadow_ledger_postings (
    posting_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    event_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE shadow_reconciliations (
    reconciliation_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
