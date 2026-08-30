CREATE TABLE pilot_eligibility_attestation_refs (
    attestation_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    wallet_fingerprint VARCHAR NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_eligibility_attestation_refs_account_idx
    ON pilot_eligibility_attestation_refs (account_fingerprint, reviewed_at);

CREATE TABLE pilot_policy_profiles (
    policy_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    wallet_fingerprint VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_policy_profiles_account_idx
    ON pilot_policy_profiles (account_fingerprint, created_at);

CREATE TABLE pilot_activation_ceremonies (
    ceremony_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    stage INTEGER NOT NULL CHECK (stage BETWEEN 0 AND 4),
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_activation_ceremonies_account_idx
    ON pilot_activation_ceremonies (account_fingerprint, occurred_at);

CREATE TABLE pilot_credential_provisioning_events (
    event_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    wallet_fingerprint VARCHAR NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_credential_provisioning_events_account_idx
    ON pilot_credential_provisioning_events (account_fingerprint, occurred_at);

CREATE TABLE pilot_authorization_challenges (
    challenge_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    not_before TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_authorization_challenges_account_idx
    ON pilot_authorization_challenges (account_fingerprint, not_before);

CREATE TABLE pilot_capability_events (
    event_id UUID PRIMARY KEY,
    capability_id UUID NOT NULL,
    challenge_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_capability_events_account_idx
    ON pilot_capability_events (account_fingerprint, occurred_at);
CREATE INDEX pilot_capability_events_capability_idx
    ON pilot_capability_events (capability_id, occurred_at);

CREATE TABLE pilot_nonce_claims (
    claim_key VARCHAR PRIMARY KEY,
    scope VARCHAR NOT NULL,
    nonce VARCHAR NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    payload_hash VARCHAR NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    UNIQUE (scope, nonce)
);

CREATE INDEX pilot_nonce_claims_account_idx
    ON pilot_nonce_claims (account_fingerprint, claimed_at);

CREATE TABLE pilot_execution_sessions (
    event_id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    sequence_number INTEGER NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    UNIQUE (session_id, sequence_number)
);

CREATE INDEX pilot_execution_sessions_account_idx
    ON pilot_execution_sessions (account_fingerprint, occurred_at);

CREATE TABLE pilot_presence_events (
    event_id UUID PRIMARY KEY,
    session_id UUID,
    account_fingerprint VARCHAR NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_presence_events_account_idx
    ON pilot_presence_events (account_fingerprint, occurred_at);
CREATE INDEX pilot_presence_events_session_idx
    ON pilot_presence_events (session_id, occurred_at);

CREATE TABLE pilot_kill_clearance_events (
    clearance_event_id UUID PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    kill_event_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX pilot_kill_clearance_events_account_idx
    ON pilot_kill_clearance_events (account_fingerprint, occurred_at);
