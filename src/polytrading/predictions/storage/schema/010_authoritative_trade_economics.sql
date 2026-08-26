CREATE TABLE authoritative_trade_economics (
    economics_fingerprint VARCHAR PRIMARY KEY,
    account_fingerprint VARCHAR NOT NULL,
    intent_id UUID NOT NULL,
    venue_order_id VARCHAR NOT NULL,
    venue_trade_id VARCHAR NOT NULL,
    trade_state VARCHAR NOT NULL CHECK (trade_state = 'CONFIRMED'),
    settlement_state VARCHAR NOT NULL CHECK (settlement_state = 'CONFIRMED'),
    occurred_at TIMESTAMPTZ NOT NULL,
    information_cutoff TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL,
    UNIQUE (account_fingerprint, venue_trade_id)
);

CREATE INDEX authoritative_trade_economics_account_cutoff_idx
    ON authoritative_trade_economics (account_fingerprint, information_cutoff);
CREATE INDEX authoritative_trade_economics_intent_cutoff_idx
    ON authoritative_trade_economics (intent_id, information_cutoff);
CREATE INDEX authoritative_trade_economics_order_idx
    ON authoritative_trade_economics (venue_order_id);
CREATE INDEX authoritative_trade_economics_trade_idx
    ON authoritative_trade_economics (venue_trade_id);
CREATE INDEX authoritative_trade_economics_state_idx
    ON authoritative_trade_economics (trade_state, settlement_state);
CREATE INDEX authoritative_trade_economics_observed_idx
    ON authoritative_trade_economics (occurred_at);
