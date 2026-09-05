CREATE TABLE identity_principals (
    principal_id text PRIMARY KEY,
    issuer text NOT NULL CHECK (issuer LIKE 'https://%'),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    identity_tenant text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (issuer, subject, identity_tenant)
);

CREATE TABLE identity_workspaces (
    workspace_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    active boolean NOT NULL DEFAULT true,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    UNIQUE (tenant_id, workspace_id)
);

CREATE TABLE identity_memberships (
    membership_id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES identity_workspaces(workspace_id),
    principal_id text NOT NULL REFERENCES identity_principals(principal_id),
    role text CHECK (role IN ('reader', 'contributor', 'admin')),
    active boolean NOT NULL DEFAULT true,
    UNIQUE (workspace_id, principal_id),
    UNIQUE (workspace_id, membership_id)
);

CREATE TABLE identity_groups (
    group_id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES identity_workspaces(workspace_id),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    role text NOT NULL CHECK (role IN ('reader', 'contributor', 'admin')),
    active boolean NOT NULL DEFAULT true,
    UNIQUE (workspace_id, group_id)
);

CREATE TABLE identity_group_members (
    workspace_id text NOT NULL,
    group_id text NOT NULL,
    membership_id text NOT NULL,
    PRIMARY KEY (workspace_id, group_id, membership_id),
    FOREIGN KEY (workspace_id, group_id) REFERENCES identity_groups(workspace_id, group_id),
    FOREIGN KEY (workspace_id, membership_id) REFERENCES identity_memberships(workspace_id, membership_id)
);

-- Pins authorize exact immutable content, never a mutable resource label.
CREATE TABLE identity_resource_grants (
    grant_id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES identity_workspaces(workspace_id),
    membership_id text,
    group_id text,
    resource_kind text NOT NULL,
    resource_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    permission text NOT NULL CHECK (permission IN ('resource:read', 'compiler:query')),
    allowed_ids jsonb NOT NULL CHECK (jsonb_typeof(allowed_ids) = 'object'),
    active boolean NOT NULL DEFAULT true,
    CHECK ((membership_id IS NULL) <> (group_id IS NULL)),
    FOREIGN KEY (workspace_id, membership_id) REFERENCES identity_memberships(workspace_id, membership_id),
    FOREIGN KEY (workspace_id, group_id) REFERENCES identity_groups(workspace_id, group_id)
);

-- Operational authorization ledger, not a fabricated resource audit-envelope/v1.
CREATE TABLE identity_changes (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id text NOT NULL,
    workspace_id text NOT NULL REFERENCES identity_workspaces(workspace_id),
    actor_kind text NOT NULL DEFAULT 'oidc' CHECK (actor_kind IN ('oidc', 'operator')),
    actor_id text NOT NULL,
    action text NOT NULL,
    target_id text NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, actor_kind, actor_id, request_id)
);

CREATE TABLE identity_sessions (
    session_id text PRIMARY KEY,
    ticket bytea NOT NULL CHECK (octet_length(ticket) <= 65536),
    expires_at timestamptz NOT NULL
);
CREATE INDEX identity_sessions_expiry ON identity_sessions (expires_at);

CREATE TABLE identity_assertion_uses (
    assertion_id text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);
CREATE INDEX identity_assertions_expiry ON identity_assertion_uses (expires_at);

CREATE VIEW identity_access AS
SELECT p.principal_id, p.issuer, p.subject, p.identity_tenant,
       w.tenant_id, w.workspace_id, w.name, w.revision, m.membership_id,
       array_agg(DISTINCT permission ORDER BY permission) AS permissions
FROM identity_principals p
JOIN identity_memberships m ON m.principal_id = p.principal_id AND m.active
JOIN identity_workspaces w ON w.workspace_id = m.workspace_id AND w.active
LEFT JOIN identity_group_members gm ON gm.membership_id = m.membership_id AND gm.workspace_id = w.workspace_id
LEFT JOIN identity_groups g ON g.group_id = gm.group_id AND g.workspace_id = w.workspace_id AND g.active
CROSS JOIN LATERAL unnest(
    CASE m.role
        WHEN 'admin' THEN ARRAY['run.reader','run.contributor','run.admin','workspace:admin','compiler:query','resource:read']
        WHEN 'contributor' THEN ARRAY['run.reader','run.contributor','compiler:query','resource:read']
        WHEN 'reader' THEN ARRAY['run.reader','resource:read']
        ELSE ARRAY[]::text[] END ||
    CASE g.role
        WHEN 'admin' THEN ARRAY['run.reader','run.contributor','run.admin','workspace:admin','compiler:query','resource:read']
        WHEN 'contributor' THEN ARRAY['run.reader','run.contributor','compiler:query','resource:read']
        WHEN 'reader' THEN ARRAY['run.reader','resource:read']
        ELSE ARRAY[]::text[] END
) permission
WHERE p.active
GROUP BY p.principal_id, w.workspace_id, m.membership_id;

ALTER TABLE control_runs
    ADD COLUMN tenant_id text,
    ADD COLUMN workspace_id text,
    ADD CONSTRAINT control_runs_scope_pair CHECK ((tenant_id IS NULL) = (workspace_id IS NULL)),
    ADD CONSTRAINT control_runs_workspace FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES identity_workspaces(tenant_id, workspace_id),
    DROP CONSTRAINT control_runs_subject_request,
    ADD CONSTRAINT control_runs_subject_request UNIQUE NULLS NOT DISTINCT
        (tenant_id, workspace_id, subject, client_request_id);
CREATE INDEX control_runs_scope_created ON control_runs (tenant_id, workspace_id, created_at DESC, run_id);
