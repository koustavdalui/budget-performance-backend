-- STEP 2 of 2: run only after confirming the new API deploy is live and
-- correct (it is - verified against /api/teams, /api/campaigns/export,
-- /api/budgets, /api/subteams). The live code no longer reads asset_lines,
-- asset_line_months, tactic_lines, tactic_line_months, campaign_products, or
-- team_subteams by those names, so renaming them away now is safe. Not a
-- drop - kept as legacy2_* for a rollback window, same pattern as before.

BEGIN;

ALTER TABLE team_budgets ADD CONSTRAINT team_budgets_team_fkey
    FOREIGN KEY (team) REFERENCES teams(name) ON DELETE CASCADE;

ALTER TABLE campaign_products RENAME TO legacy2_campaign_products;
ALTER TABLE asset_lines RENAME TO legacy2_asset_lines;
ALTER TABLE asset_line_months RENAME TO legacy2_asset_line_months;
ALTER TABLE tactic_lines RENAME TO legacy2_tactic_lines;
ALTER TABLE tactic_line_months RENAME TO legacy2_tactic_line_months;
ALTER TABLE team_subteams RENAME TO legacy2_team_subteams;

COMMIT;
