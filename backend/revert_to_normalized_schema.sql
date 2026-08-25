-- One-time revert: undo the 8-table -> 3-table (JSONB) consolidation and
-- restore the original normalized schema. Safe/lossless: the legacy_* tables
-- already hold the exact untouched pre-consolidation data (verified: FKs all
-- intact, zero orphans, row counts matching the original migration report).
-- The current campaign_lines/teams tables are kept (renamed, not dropped) as
-- a rollback buffer in case anything needs to be cross-checked later.
--
-- Run this against the budget-performance-db Postgres instance (e.g. via
-- DBeaver, since psql isn't available in this sandboxed environment) as ONE
-- transaction so it's all-or-nothing.

BEGIN;

-- 1. Drop the 7 empty "ghost" tables that got silently recreated (0 rows
--    each - verified before writing this script) because models.py's Legacy*
--    classes were still mapped to these original table names, and
--    Base.metadata.create_all() (called on every API startup) recreates any
--    table it doesn't see under its declared name.
DROP TABLE campaign_products;
DROP TABLE asset_lines;
DROP TABLE asset_line_months;
DROP TABLE tactic_lines;
DROP TABLE tactic_line_months;
DROP TABLE team_budgets;
DROP TABLE team_subteams;

-- 2. Restore the original table names from their legacy_* backups (created
--    by migrate_consolidate.py --finalize) - pure rename, no data movement,
--    so nothing is recomputed or re-derived.
ALTER TABLE legacy_campaign_products RENAME TO campaign_products;
ALTER TABLE legacy_asset_lines RENAME TO asset_lines;
ALTER TABLE legacy_asset_line_months RENAME TO asset_line_months;
ALTER TABLE legacy_tactic_lines RENAME TO tactic_lines;
ALTER TABLE legacy_tactic_line_months RENAME TO tactic_line_months;
ALTER TABLE legacy_team_budgets RENAME TO team_budgets;
ALTER TABLE legacy_team_subteams RENAME TO team_subteams;

-- 3. Set aside the (now unused) JSONB-consolidated tables as a rollback
--    buffer instead of dropping them outright.
ALTER TABLE campaign_lines RENAME TO deprecated_campaign_lines;
ALTER TABLE teams RENAME TO deprecated_teams;

COMMIT;

-- Note: campaigns.products (JSONB column added for the consolidated design)
-- is left in place, unused, rather than dropped - harmless dead column,
-- can be cleaned up later with:
--   ALTER TABLE campaigns DROP COLUMN products;
