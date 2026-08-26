CREATE TABLE live_execution_plans (
    plan_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE execution_intents (
    intent_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE signed_order_envelopes (
    intent_id UUID PRIMARY KEY,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE venue_order_events (
    event_id UUID PRIMARY KEY,
    intent_id UUID,
    received_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE venue_trade_events (
    trade_event_id UUID PRIMARY KEY,
    intent_id UUID,
    received_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE live_ledger_postings (
    posting_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    intent_id UUID,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE live_reconciliations (
    reconciliation_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE execution_kill_events (
    kill_event_id UUID PRIMARY KEY,
    scope VARCHAR NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE activation_evidence (
    activation_evidence_id UUID PRIMARY KEY,
    capability_digest VARCHAR NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE TABLE protocol_conformance_results (
    conformance_result_id UUID PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
