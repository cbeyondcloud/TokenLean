-- Rollback for the proxy-key store tables (infra/migrations/proxy_keys.sql, ST-9).
--
-- DESTRUCTIVE: this deletes every proxy key held in Postgres and the record of every
-- revoked key. Run it only on a deployment that has never validated keys against Postgres
-- (key_store.backend: blob, and no host-installed Postgres store), or after moving its keys
-- elsewhere. A proxy with key_store.backend: postgres refuses every key without these
-- tables. Not run by run.sh or the migration image: apply it by hand.
--
-- Rolling the proxy back to a release without /admin/keys does NOT need this script: the
-- tables are what the Postgres store has always used.
--
--   psql -v ON_ERROR_STOP=1 -f infra/migrations/down/proxy_keys.sql
DROP TABLE IF EXISTS revoked_proxy_keys;
DROP TABLE IF EXISTS proxy_keys;
