-- Run explicitly in a dedicated application database before opting in (docs/postgres-grants.md).
CREATE TABLE IF NOT EXISTS ccp_grants (
    namespace text NOT NULL,
    account text NOT NULL,
    ciphertext bytea NOT NULL,
    generation bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    lease_holder text,
    lease_until timestamptz,
    state text NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'needs-reconcile')),
    PRIMARY KEY (namespace, account)
);
