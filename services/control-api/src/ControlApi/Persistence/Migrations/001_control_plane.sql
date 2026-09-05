CREATE TABLE control_runs (
    run_id text PRIMARY KEY CHECK (run_id ~ '^run_[0-9a-f]{32}$'),
    subject text NOT NULL,
    client_request_id text NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL,
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT control_runs_subject_request UNIQUE (subject, client_request_id),
    CHECK (metadata->>'Id' = run_id),
    CHECK (metadata->>'CreatedBy' = subject),
    CHECK (metadata->>'ClientRequestId' = client_request_id),
    CHECK ((metadata->>'Version')::bigint = version),
    CHECK (octet_length(metadata::text) <= 16777216)
);
CREATE INDEX control_runs_created ON control_runs (created_at DESC, run_id);

CREATE TABLE control_feedback (
    run_id text NOT NULL REFERENCES control_runs(run_id),
    submission_id text NOT NULL,
    feedback jsonb NOT NULL CHECK (jsonb_typeof(feedback) = 'object'),
    PRIMARY KEY (run_id, submission_id),
    CHECK (feedback->>'RunId' = run_id),
    CHECK (feedback->>'SubmissionId' = submission_id),
    CHECK (octet_length(feedback::text) <= 16384)
);

-- A permanent, one-shot intent, not an expiring lease. Reclaim needs backend fencing (#34).
CREATE TABLE control_start_dispatch (
    run_id text PRIMARY KEY REFERENCES control_runs(run_id),
    generation bigint NOT NULL CHECK (generation = 1),
    claimed_at timestamptz NOT NULL DEFAULT now()
);
