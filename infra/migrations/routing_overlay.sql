-- Admin-managed routing and team-limits overlay versions (ST-10): one row per version the
-- /admin/routing endpoint stored (apply or rollback), never updated or deleted by the proxy.
-- The newest row is the overlay every replica applies over its loaded config.
--
-- The proxy's startup DDL (admin_api/routing.py ROUTING_OVERLAY_DDL) creates the same table
-- when its role may; this file is for a deployment whose proxy connects as the restricted
-- runtime role (audit/enforcement.py). Same statement as ROUTING_OVERLAY_DDL (a unit test
-- holds them equal). Additive and idempotent. Down: infra/migrations/down/routing_overlay.sql.
CREATE TABLE IF NOT EXISTS routing_overlay_versions (
    version         BIGINT      PRIMARY KEY,
    fragment        JSONB       NOT NULL,
    action          TEXT        NOT NULL,
    actor           TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment         TEXT        NOT NULL DEFAULT '',
    source_version  BIGINT,
    fragment_sha256 TEXT        NOT NULL
);
