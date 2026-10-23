-- Rollback for the routing overlay versions table (infra/migrations/routing_overlay.sql, ST-10).
--
-- Not run by run.sh or the migration image: apply it by hand, AFTER rolling the proxy back to
-- a release without /admin/routing (a newer proxy recreates the table at startup when its role
-- may). Dropping the table loses every stored version and its who/when history: export it first
-- if it matters. To stop applying an overlay without losing history, roll back to version 0
-- (POST /admin/routing/rollback {"version": 0}) instead.
--
--   psql -v ON_ERROR_STOP=1 -f infra/migrations/down/routing_overlay.sql
DROP TABLE IF EXISTS routing_overlay_versions;
