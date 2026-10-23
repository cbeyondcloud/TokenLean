-- Proxy-key store tables (ST-9): proxy_keys holds the sha256 of each proxy key with its
-- tenant, tier, flags and extra metadata (team, user, label, ... in `extra`); a removed key
-- is recorded in revoked_proxy_keys so no later write can bring it back.
--
-- Read by the proxy when config sets key_store.backend: postgres (and by a host that
-- installs the Postgres store itself). The proxy's startup DDL (auth/pg_key_store.py
-- PROXY_KEYS_DDL) creates the same tables when its role may; this file is for a deployment
-- whose proxy connects as the restricted runtime role (audit/enforcement.py), which cannot.
-- Byte-for-byte the same statements as PROXY_KEYS_DDL (a unit test holds them equal), so
-- either may run first. Additive and idempotent. Down: infra/migrations/down/proxy_keys.sql.
CREATE TABLE IF NOT EXISTS proxy_keys (
    key_hash   TEXT    PRIMARY KEY,
    tenant_id  TEXT    NOT NULL,
    tier       TEXT    NOT NULL DEFAULT 'free',
    admin      BOOLEAN NOT NULL DEFAULT FALSE,
    suspended  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT,
    extra      JSONB   NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_keys_tenant ON proxy_keys (tenant_id);
CREATE TABLE IF NOT EXISTS revoked_proxy_keys (
    key_hash   TEXT        PRIMARY KEY,
    tenant_id  TEXT,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
