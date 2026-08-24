CREATE TABLE scan_reports (
    report_id UUID NOT NULL,
    candidate_id UUID NOT NULL,
    decision VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    record_json VARCHAR NOT NULL,
    record_hash VARCHAR NOT NULL
);
