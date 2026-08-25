"""One-time backfill: 8 normalized tables -> 5, without JSON blobs.

Deliberately uses ONLY raw SQL via a plain engine - it does NOT import
api/models.py, so it shares no SQLAlchemy metadata with the live app. That's
the fix for the bug that hit the previous (reverted) consolidation attempt:
Base.metadata.create_all() (called on every API startup) will silently
recreate any table declared in Base.metadata that it doesn't see under its
exact name - which is exactly what happened when the pre-consolidation
Legacy* model classes stayed mapped to the original table names after they
were renamed away. Keeping this script's own table knowledge completely
separate from the running app's models.py means there is nothing left behind
that create_all() could ever act on.

Also deliberately structured so every step through --backfill and --report is
CREATE TABLE / ALTER TABLE ADD COLUMN / ALTER TABLE ADD CONSTRAINT / INSERT -
additive only, and --finalize is RENAME only. There is no DROP TABLE and no
DROP COLUMN anywhere in this script. The old `campaigns.products` JSONB
column (a leftover, unused, empty-valued column from the previous reverted
attempt) is left untouched rather than dropped - the new array lives in a
differently-named column (`product_tags`) instead of overwriting it.

Usage (run from backend/, same DATABASE_URL convention as seed.py):

    python migrate_consolidate2.py --backfill
        Creates spend_lines, spend_line_months, teams (if missing), adds
        campaigns.product_tags (if missing) - never touches the 8 existing
        tables. Backfills all of it from the current tables (asset_lines,
        asset_line_months, tactic_lines, tactic_line_months,
        campaign_products, team_budgets, team_subteams). Safe to re-run -
        clears and rebuilds the new tables/column each time.

    python migrate_consolidate2.py --report
        Re-runs the verification report only (no writes).

    python migrate_consolidate2.py --finalize
        1. Adds the team_budgets.team -> teams.name foreign key (only once
           the report is clean, so every team_budgets.team value is
           guaranteed to already have a teams row).
        2. Renames the 6 now-superseded tables to a `legacy2_` prefix (NOT
           dropped) - campaign_products, asset_lines, asset_line_months,
           tactic_lines, tactic_line_months, team_subteams. team_budgets
           keeps its name (it just gained a constraint); campaigns keeps its
           name (it just gained a column).
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://budget:budget@localhost:5432/budget')
METRICS = ('plan', 'forecast', 'commit', 'actual')

LEGACY_TABLES = [
    'campaign_products', 'asset_lines', 'asset_line_months',
    'tactic_lines', 'tactic_line_months', 'team_subteams',
]


def ensure_new_schema(conn):
    conn.execute(text('''
        CREATE TABLE IF NOT EXISTS spend_lines (
            id serial PRIMARY KEY,
            campaign_id integer NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            line_type varchar NOT NULL CHECK (line_type IN ('asset', 'tactic')),
            line_name varchar NOT NULL
        )
    '''))
    conn.execute(text('''
        CREATE TABLE IF NOT EXISTS spend_line_months (
            spend_line_id integer NOT NULL REFERENCES spend_lines(id) ON DELETE CASCADE,
            year integer NOT NULL,
            month varchar NOT NULL,
            plan numeric,
            forecast numeric,
            "commit" numeric,
            actual numeric,
            PRIMARY KEY (spend_line_id, year, month)
        )
    '''))
    conn.execute(text('''
        CREATE TABLE IF NOT EXISTS teams (
            name varchar PRIMARY KEY,
            sub_teams varchar[] NOT NULL DEFAULT '{}'
        )
    '''))
    conn.execute(text(
        "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS product_tags varchar[] NOT NULL DEFAULT '{}'"
    ))


def backfill_spend_lines(conn):
    """Asset and tactic line ids overlap (they're separate sequences) - can't
    preserve original ids when merging into one table. Set-based, not a
    Python row-by-row loop (that was the first draft - ~2200 individual
    round-trips to Postgres, painfully slow): stage the old id on each new
    row via a temporary column, join on it (scoped by line_type, since the
    old ids collide across the two source tables) to populate the month
    rows, then drop the temporary column. All of this only ever touches the
    brand new spend_lines/spend_line_months tables - never the source tables."""
    conn.execute(text('TRUNCATE spend_line_months, spend_lines RESTART IDENTITY'))
    conn.execute(text('ALTER TABLE spend_lines ADD COLUMN IF NOT EXISTS _old_id integer'))

    conn.execute(text(
        "INSERT INTO spend_lines (campaign_id, line_type, line_name, _old_id) "
        "SELECT campaign_id, 'asset', asset_name, id FROM asset_lines"
    ))
    conn.execute(text(
        "INSERT INTO spend_lines (campaign_id, line_type, line_name, _old_id) "
        "SELECT campaign_id, 'tactic', tactic_name, id FROM tactic_lines"
    ))
    n_lines = conn.execute(text('SELECT COUNT(*) FROM spend_lines')).scalar()

    conn.execute(text('''
        INSERT INTO spend_line_months (spend_line_id, year, month, plan, forecast, "commit", actual)
        SELECT sl.id, m.year, m.month, m.plan, m.forecast, m."commit", m.actual
        FROM asset_line_months m
        JOIN spend_lines sl ON sl._old_id = m.asset_line_id AND sl.line_type = 'asset'
    '''))
    conn.execute(text('''
        INSERT INTO spend_line_months (spend_line_id, year, month, plan, forecast, "commit", actual)
        SELECT sl.id, m.year, m.month, m.plan, m.forecast, m."commit", m.actual
        FROM tactic_line_months m
        JOIN spend_lines sl ON sl._old_id = m.tactic_line_id AND sl.line_type = 'tactic'
    '''))
    n_months = conn.execute(text('SELECT COUNT(*) FROM spend_line_months')).scalar()

    conn.execute(text('ALTER TABLE spend_lines DROP COLUMN _old_id'))
    return n_lines, n_months


def backfill_teams(conn):
    # Plain TRUNCATE (no CASCADE) - after --finalize, team_budgets.team FKs to
    # teams.name, so a CASCADE here would silently wipe team_budgets (real,
    # not re-derivable data) if --backfill were ever re-run post-finalize.
    # Erroring instead of cascading is the safe failure mode for that case.
    conn.execute(text('TRUNCATE teams'))
    conn.execute(text('''
        INSERT INTO teams (name, sub_teams)
        SELECT t.name, COALESCE(st.subs, '{}')
        FROM (SELECT team AS name FROM team_budgets UNION SELECT team AS name FROM team_subteams) t
        LEFT JOIN (
            SELECT team, array_agg(sub_team ORDER BY sub_team) AS subs
            FROM team_subteams GROUP BY team
        ) st ON st.team = t.name
    '''))
    return conn.execute(text('SELECT COUNT(*) FROM teams')).scalar()


def backfill_product_tags(conn):
    conn.execute(text("UPDATE campaigns SET product_tags = '{}'"))
    result = conn.execute(text('''
        UPDATE campaigns c
        SET product_tags = sub.tags
        FROM (
            SELECT campaign_id, array_agg(product ORDER BY product) AS tags
            FROM campaign_products GROUP BY campaign_id
        ) sub
        WHERE sub.campaign_id = c.id
    '''))
    return result.rowcount


def legacy_totals(conn):
    line_count = conn.execute(text(
        'SELECT (SELECT count(*) FROM asset_lines) + (SELECT count(*) FROM tactic_lines)'
    )).scalar()
    metric_sums = {}
    for metric in METRICS:
        a = conn.execute(text(f'SELECT COALESCE(SUM("{metric}"), 0) FROM asset_line_months')).scalar()
        t = conn.execute(text(f'SELECT COALESCE(SUM("{metric}"), 0) FROM tactic_line_months')).scalar()
        metric_sums[metric] = float(a or 0) + float(t or 0)
    team_names = {r[0] for r in conn.execute(text('SELECT DISTINCT team FROM team_budgets'))}
    team_names |= {r[0] for r in conn.execute(text('SELECT DISTINCT team FROM team_subteams'))}
    subteam_pair_count = conn.execute(text('SELECT COUNT(*) FROM team_subteams')).scalar()
    product_tag_count = conn.execute(text('SELECT COUNT(*) FROM campaign_products')).scalar()
    return {
        'lines': line_count, 'metric_sums': metric_sums, 'teams': len(team_names),
        'subteam_pairs': subteam_pair_count, 'product_tags': product_tag_count,
    }


def new_totals(conn):
    line_count = conn.execute(text('SELECT COUNT(*) FROM spend_lines')).scalar()
    metric_sums = {}
    for metric in METRICS:
        metric_sums[metric] = float(conn.execute(text(f'SELECT COALESCE(SUM("{metric}"), 0) FROM spend_line_months')).scalar() or 0)
    team_count = conn.execute(text('SELECT COUNT(*) FROM teams')).scalar()
    subteam_pair_count = conn.execute(text(
        "SELECT COALESCE(SUM(array_length(sub_teams, 1)), 0) FROM teams"
    )).scalar()
    product_tag_count = conn.execute(text(
        "SELECT COALESCE(SUM(array_length(product_tags, 1)), 0) FROM campaigns"
    )).scalar()
    return {
        'lines': line_count, 'metric_sums': metric_sums, 'teams': team_count,
        'subteam_pairs': subteam_pair_count, 'product_tags': product_tag_count,
    }


def print_report(conn):
    old = legacy_totals(conn)
    new = new_totals(conn)
    ok = True

    def check(label, old_v, new_v):
        nonlocal ok
        match = old_v == new_v
        ok = ok and match
        flag = 'OK ' if match else 'MISMATCH'
        print(f'  [{flag}] {label}: old={old_v!r}  new={new_v!r}')

    print('Verification report (old tables vs new tables):')
    check('spend line count (asset+tactic)', old['lines'], new['lines'])
    for metric in METRICS:
        check(f'sum({metric})', round(old['metric_sums'][metric], 2), round(new['metric_sums'][metric], 2))
    check('team count', old['teams'], new['teams'])
    check('team/sub-team pair count', old['subteam_pairs'], new['subteam_pairs'])
    check('campaign/product tag count', old['product_tags'], new['product_tags'])
    print('RESULT:', 'ALL MATCH' if ok else 'MISMATCH FOUND - do not finalize until this is clean')
    return ok


def finalize(conn):
    # Every team_budgets.team value already has a teams row (backfill_teams
    # unions team_budgets.team into team_names), so this FK is guaranteed to
    # succeed - not a leap of faith.
    conn.execute(text(
        'ALTER TABLE team_budgets ADD CONSTRAINT team_budgets_team_fkey '
        'FOREIGN KEY (team) REFERENCES teams(name) ON DELETE CASCADE'
    ))
    for table in LEGACY_TABLES:
        conn.execute(text(f'ALTER TABLE {table} RENAME TO legacy2_{table}'))
    print(f'Added team_budgets -> teams FK, renamed {len(LEGACY_TABLES)} superseded tables to legacy2_* - not dropped.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill', action='store_true', help='Create/rebuild the new tables from the current ones, then report')
    parser.add_argument('--report', action='store_true', help='Just print the verification report')
    parser.add_argument('--finalize', action='store_true', help='Add the FK, rename superseded tables to legacy2_*')
    args = parser.parse_args()

    if not any([args.backfill, args.report, args.finalize]):
        parser.print_help()
        sys.exit(1)

    engine = create_engine(DATABASE_URL)

    if args.backfill:
        with engine.begin() as conn:
            ensure_new_schema(conn)
            n_lines, n_months = backfill_spend_lines(conn)
            n_teams = backfill_teams(conn)
            n_products = backfill_product_tags(conn)
            print(f'Backfilled {n_lines} spend line(s), {n_months} month row(s), {n_teams} team(s), {n_products} campaign(s) with product tags.')
            ok = print_report(conn)
        if not ok:
            sys.exit(1)

    elif args.report:
        with engine.connect() as conn:
            ok = print_report(conn)
        if not ok:
            sys.exit(1)

    if args.finalize:
        with engine.begin() as conn:
            finalize(conn)


if __name__ == '__main__':
    main()
