-- STEP 1 of 2: purely additive - creates new tables/column and backfills
-- them from the current 8 tables. Does NOT touch, rename, or drop anything
-- the currently-live app reads from, so this is safe to run right now with
-- zero risk/downtime. Run as ONE transaction.
--
-- After this commits, tell Claude - it will push the new code (which reads
-- from these newly-backfilled tables) and verify the live API, THEN hand you
-- a second, final script that renames the now-superseded tables away.

BEGIN;

-- New tables (fresh names, nothing to collide with)
CREATE TABLE IF NOT EXISTS spend_lines (
    id serial PRIMARY KEY,
    campaign_id integer NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    line_type varchar NOT NULL CHECK (line_type IN ('asset', 'tactic')),
    line_name varchar NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_line_months (
    spend_line_id integer NOT NULL REFERENCES spend_lines(id) ON DELETE CASCADE,
    year integer NOT NULL,
    month varchar NOT NULL,
    plan numeric,
    forecast numeric,
    "commit" numeric,
    actual numeric,
    PRIMARY KEY (spend_line_id, year, month)
);
CREATE TABLE IF NOT EXISTS teams (
    name varchar PRIMARY KEY,
    sub_teams varchar[] NOT NULL DEFAULT '{}'
);

-- New column on campaigns - a different physical name (product_tags) than
-- the existing (unused, dead) `products` jsonb column from the previous
-- reverted consolidation, so nothing needs to be dropped.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS product_tags varchar[] NOT NULL DEFAULT '{}';

-- Backfill spend_lines/spend_line_months. asset_lines.id and tactic_lines.id
-- overlap (separate sequences), so a temporary column stages the old id for
-- joining, scoped by line_type to avoid the collision, then gets dropped -
-- this only ever touches the brand new spend_lines table, never the source.
TRUNCATE spend_line_months, spend_lines RESTART IDENTITY;
ALTER TABLE spend_lines ADD COLUMN IF NOT EXISTS _old_id integer;

INSERT INTO spend_lines (campaign_id, line_type, line_name, _old_id)
SELECT campaign_id, 'asset', asset_name, id FROM asset_lines;
INSERT INTO spend_lines (campaign_id, line_type, line_name, _old_id)
SELECT campaign_id, 'tactic', tactic_name, id FROM tactic_lines;

INSERT INTO spend_line_months (spend_line_id, year, month, plan, forecast, "commit", actual)
SELECT sl.id, m.year, m.month, m.plan, m.forecast, m."commit", m.actual
FROM asset_line_months m
JOIN spend_lines sl ON sl._old_id = m.asset_line_id AND sl.line_type = 'asset';

INSERT INTO spend_line_months (spend_line_id, year, month, plan, forecast, "commit", actual)
SELECT sl.id, m.year, m.month, m.plan, m.forecast, m."commit", m.actual
FROM tactic_line_months m
JOIN spend_lines sl ON sl._old_id = m.tactic_line_id AND sl.line_type = 'tactic';

ALTER TABLE spend_lines DROP COLUMN _old_id;

-- Backfill teams (union of team_budgets.team and team_subteams.team, with
-- sub_teams aggregated into an array).
TRUNCATE teams;
INSERT INTO teams (name, sub_teams)
SELECT t.name, COALESCE(st.subs, '{}')
FROM (SELECT team AS name FROM team_budgets UNION SELECT team AS name FROM team_subteams) t
LEFT JOIN (
    SELECT team, array_agg(sub_team ORDER BY sub_team) AS subs
    FROM team_subteams GROUP BY team
) st ON st.team = t.name;

-- Backfill campaigns.product_tags from campaign_products.
UPDATE campaigns SET product_tags = '{}';
UPDATE campaigns c
SET product_tags = sub.tags
FROM (
    SELECT campaign_id, array_agg(product ORDER BY product) AS tags
    FROM campaign_products GROUP BY campaign_id
) sub
WHERE sub.campaign_id = c.id;

COMMIT;

-- Verification - every row here should show equal numbers on both sides.
SELECT
    (SELECT count(*) FROM asset_lines) + (SELECT count(*) FROM tactic_lines) AS old_line_count,
    (SELECT count(*) FROM spend_lines) AS new_line_count;

SELECT
    (SELECT COALESCE(SUM(plan),0) FROM asset_line_months) + (SELECT COALESCE(SUM(plan),0) FROM tactic_line_months) AS old_sum_plan,
    (SELECT COALESCE(SUM(plan),0) FROM spend_line_months) AS new_sum_plan;

SELECT
    (SELECT COALESCE(SUM("commit"),0) FROM asset_line_months) + (SELECT COALESCE(SUM("commit"),0) FROM tactic_line_months) AS old_sum_commit,
    (SELECT COALESCE(SUM("commit"),0) FROM spend_line_months) AS new_sum_commit;

SELECT
    (SELECT count(DISTINCT team) FROM team_budgets) AS old_team_hint,
    (SELECT count(*) FROM teams) AS new_team_count;

SELECT
    (SELECT count(*) FROM team_subteams) AS old_subteam_pairs,
    (SELECT COALESCE(SUM(array_length(sub_teams,1)),0) FROM teams) AS new_subteam_pairs;

SELECT
    (SELECT count(*) FROM campaign_products) AS old_product_tags,
    (SELECT COALESCE(SUM(array_length(product_tags,1)),0) FROM campaigns) AS new_product_tags;
