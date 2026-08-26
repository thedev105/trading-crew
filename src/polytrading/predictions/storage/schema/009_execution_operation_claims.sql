CREATE TABLE execution_operation_claims (
    claim_key VARCHAR PRIMARY KEY,
    intent_id UUID NOT NULL,
    account_fingerprint VARCHAR NOT NULL,
    operation VARCHAR NOT NULL CHECK (
        operation IN ('SUBMIT_INTENT', 'FIRST_FILL_REVALIDATION')
    ),
    occurrence_hash VARCHAR NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);

CREATE INDEX execution_operation_claims_intent_id_idx
    ON execution_operation_claims (intent_id);

CREATE INDEX execution_operation_claims_account_idx
    ON execution_operation_claims (account_fingerprint);
