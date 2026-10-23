-- Rollback for the team-level (ST-1) and usage-tags (ST-2) columns on usage_events.
--
-- Not run by run.sh or the migration image: apply it by hand, AFTER rolling the proxy back
-- to a release that does not write these columns (the newer release inserts them and would
-- fail against a table without them). The columns are additive, so the older release runs
-- against the newer table unchanged: rolling back the code does not require this script.
-- Dropping a column loses its data: export it first if the per-team or per-run history
-- matters.
--
--   psql -v ON_ERROR_STOP=1 -f infra/migrations/down/usage_events_team_tags.sql
ALTER TABLE usage_events DROP COLUMN IF EXISTS tags;
ALTER TABLE usage_events DROP COLUMN IF EXISTS team;
