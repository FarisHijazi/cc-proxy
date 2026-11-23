-- Run explicitly in a dedicated application database before opting in (docs/postgres-grants.md).
-- Idempotent: rerun it as the schema owner after an upgrade.
CREATE TABLE IF NOT EXISTS ccp_grants (
    namespace text NOT NULL,
    account text NOT NULL,
    ciphertext bytea NOT NULL,
    generation bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    lease_holder text,
    lease_until timestamptz,
    state text NOT NULL DEFAULT 'ok',
    PRIMARY KEY (namespace, account)
);
ALTER TABLE ccp_grants DROP CONSTRAINT IF EXISTS ccp_grants_state_check,
    ADD CONSTRAINT ccp_grants_state_check CHECK (state IN ('ok', 'needs-reconcile', 'exported'));
