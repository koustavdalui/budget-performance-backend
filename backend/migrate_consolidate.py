"""One-time backfill from the old 8-table schema to the consolidated 3-table
schema (campaigns [+products column], campaign_lines, teams).

Safe-to-repeat by design: every mode below only READS the legacy tables
(campaign_products, asset_lines, asset_line_months, tactic_lines,
tactic_line_months, team_budgets, team_subteams) and creates/overwrites rows
in the new tables - it never modifies or deletes legacy data. Run it as many
times as you want while verifying; nothing is destructive until --finalize.

Usage (run from backend/, same DATABASE_URL convention as seed.py):

    python migrate_consolidate.py --backfill
        1. Adds the `products` column to campaigns (if missing) and creates
           campaign_lines / teams (if missing) - never touches legacy tables.
        2. Clears any existing campaign_lines/teams rows and rebuilds them
           from the legacy tables, and rebuilds campaigns.products from
           campaign_products. Safe to re-run.
        3. Prints a verification report comparing legacy vs new: campaign
           count, line count, sum of every $ metric, team count, sub-team
           count. Any mismatch is printed loudly and the script exits
           non-zero WITHOUT the --finalize step.

    python migrate_consolidate.py --report
        Just re-runs the verification report against whatever's already in
        the new tables (no writes) - use after --backfill to re-check, or any
        time you want to confirm old vs new still agree.

    python migrate_consolidate.py --finalize
        Renames the 7 legacy tables to a `legacy_` prefix (does NOT drop
        them). Only do this after --backfill's report shows a clean match
        and the API (main.py, now pointed at the new tables) has been smoke
        tested. Keep the legacy_* tables around for a rollback window; drop
        them by hand later once you're confident.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'api'))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from models import (
    Base, Campaign, CampaignLine, Team,
    LegacyAssetLine, LegacyAssetLineMonth, LegacyCampaignProduct,
    LegacyTacticLine, LegacyTacticLineMonth, LegacyTeamBudget, LegacyTeamSubteam,
)

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://budget:budget@localhost:5432/budget')
METRICS = ('plan', 'forecast', 'commit', 'actual')

LEGACY_TABLES = [
    'campaign_products', 'asset_lines', 'asset_line_months',
    'tactic_lines', 'tactic_line_months', 'team_budgets', 'team_subteams',
]


def ensure_new_schema(engine):
    """create_all only adds tables/columns that don't exist at all - it never
    alters an existing table, so campaigns.products needs an explicit ALTER."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        dialect = engine.dialect.name
        if dialect == 'postgresql':
            conn.execute(text(
                "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS products JSONB "
                "NOT NULL DEFAULT '[]'::jsonb"
            ))
        else:
            # SQLite (local testing only) has no IF NOT EXISTS / JSONB - best effort.
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(campaigns)"))]
            if 'products' not in cols:
                conn.execute(text("ALTER TABLE campaigns ADD COLUMN products JSON DEFAULT '[]'"))


def backfill_products(db):
    rows = db.execute(text('SELECT campaign_id, product FROM campaign_products')).fetchall()
    by_campaign = {}
    for campaign_id, product in rows:
        by_campaign.setdefault(campaign_id, []).append(product)
    for campaign in db.query(Campaign).all():
        campaign.products = by_campaign.get(campaign.id, [])
    return len(rows)


def _months_dict(month_rows):
    """month_rows: iterable of (year, month, plan, forecast, commit, actual)."""
    out = {}
    for year, month, plan, forecast, commit, actual in month_rows:
        out.setdefault(str(year), {})[month] = {
            'plan': float(plan) if plan is not None else None,
            'forecast': float(forecast) if forecast is not None else None,
            'commit': float(commit) if commit is not None else None,
            'actual': float(actual) if actual is not None else None,
        }
    return out


def backfill_lines(db):
    db.query(CampaignLine).delete()

    asset_rows = db.execute(text(
        'SELECT id, campaign_id, asset_name FROM asset_lines'
    )).fetchall()
    asset_month_rows = db.execute(text(
        'SELECT asset_line_id, year, month, plan, forecast, "commit", actual FROM asset_line_months'
    )).fetchall()
    tactic_rows = db.execute(text(
        'SELECT id, campaign_id, tactic_name FROM tactic_lines'
    )).fetchall()
    tactic_month_rows = db.execute(text(
        'SELECT tactic_line_id, year, month, plan, forecast, "commit", actual FROM tactic_line_months'
    )).fetchall()

    months_by_asset_line, months_by_tactic_line = {}, {}
    for line_id, year, month, plan, forecast, commit, actual in asset_month_rows:
        months_by_asset_line.setdefault(line_id, []).append((year, month, plan, forecast, commit, actual))
    for line_id, year, month, plan, forecast, commit, actual in tactic_month_rows:
        months_by_tactic_line.setdefault(line_id, []).append((year, month, plan, forecast, commit, actual))

    count = 0
    for line_id, campaign_id, name in asset_rows:
        db.add(CampaignLine(
            campaign_id=campaign_id, line_type='asset', line_name=name,
            months=_months_dict(months_by_asset_line.get(line_id, [])),
        ))
        count += 1
    for line_id, campaign_id, name in tactic_rows:
        db.add(CampaignLine(
            campaign_id=campaign_id, line_type='tactic', line_name=name,
            months=_months_dict(months_by_tactic_line.get(line_id, [])),
        ))
        count += 1
    return count


def backfill_teams(db):
    db.query(Team).delete()

    budget_rows = db.execute(text('SELECT team, year, quarter, amount FROM team_budgets')).fetchall()
    subteam_rows = db.execute(text('SELECT team, sub_team FROM team_subteams')).fetchall()

    teams = {}
    for team, year, quarter, amount in budget_rows:
        t = teams.setdefault(team, {'sub_teams': [], 'budgets': {}})
        t['budgets'].setdefault(str(year), {})[quarter] = float(amount) if amount is not None else None
    for team, sub_team in subteam_rows:
        t = teams.setdefault(team, {'sub_teams': [], 'budgets': {}})
        if sub_team not in t['sub_teams']:
            t['sub_teams'].append(sub_team)

    for name, payload in teams.items():
        db.add(Team(name=name, sub_teams=payload['sub_teams'], budgets=payload['budgets']))
    return len(teams)


def legacy_totals(db):
    campaign_count = db.execute(text('SELECT COUNT(*) FROM campaigns')).scalar()
    asset_line_count = db.execute(text('SELECT COUNT(*) FROM asset_lines')).scalar()
    tactic_line_count = db.execute(text('SELECT COUNT(*) FROM tactic_lines')).scalar()
    metric_sums = {}
    for metric in METRICS:
        a = db.execute(text(f'SELECT COALESCE(SUM("{metric}"), 0) FROM asset_line_months')).scalar()
        t = db.execute(text(f'SELECT COALESCE(SUM("{metric}"), 0) FROM tactic_line_months')).scalar()
        metric_sums[metric] = float(a or 0) + float(t or 0)
    team_names = {r[0] for r in db.execute(text('SELECT DISTINCT team FROM team_budgets'))}
    team_names |= {r[0] for r in db.execute(text('SELECT DISTINCT team FROM team_subteams'))}
    subteam_pair_count = db.execute(text('SELECT COUNT(*) FROM team_subteams')).scalar()
    return {
        'campaigns': campaign_count,
        'lines': asset_line_count + tactic_line_count,
        'metric_sums': metric_sums,
        'teams': len(team_names),
        'subteam_pairs': subteam_pair_count,
    }


def new_totals(db):
    campaign_count = db.query(Campaign).count()
    lines = db.query(CampaignLine).all()
    metric_sums = {m: 0.0 for m in METRICS}
    for line in lines:
        for year_budgets in (line.months or {}).values():
            for vals in year_budgets.values():
                for metric in METRICS:
                    v = vals.get(metric)
                    if v is not None:
                        metric_sums[metric] += v
    teams = db.query(Team).all()
    subteam_pair_count = sum(len(t.sub_teams or []) for t in teams)
    return {
        'campaigns': campaign_count,
        'lines': len(lines),
        'metric_sums': metric_sums,
        'teams': len(teams),
        'subteam_pairs': subteam_pair_count,
    }


def print_report(db):
    old = legacy_totals(db)
    new = new_totals(db)
    ok = True

    def check(label, old_v, new_v):
        nonlocal ok
        match = old_v == new_v
        ok = ok and match
        flag = 'OK ' if match else 'MISMATCH'
        print(f'  [{flag}] {label}: legacy={old_v!r}  new={new_v!r}')

    print('Verification report (legacy tables vs new tables):')
    check('campaign count', old['campaigns'], new['campaigns'])
    check('spend line count (asset+tactic)', old['lines'], new['lines'])
    for metric in METRICS:
        check(f'sum({metric})', round(old['metric_sums'][metric], 2), round(new['metric_sums'][metric], 2))
    check('team count', old['teams'], new['teams'])
    check('team/sub-team pair count', old['subteam_pairs'], new['subteam_pairs'])
    print('RESULT:', 'ALL MATCH' if ok else 'MISMATCH FOUND - do not finalize until this is clean')
    return ok


def finalize(engine):
    with engine.begin() as conn:
        for table in LEGACY_TABLES:
            conn.execute(text(f'ALTER TABLE {table} RENAME TO legacy_{table}'))
    print(f'Renamed {len(LEGACY_TABLES)} legacy tables to legacy_* - not dropped. '
          f'Drop them by hand once you are confident (e.g. after a rollback window).')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill', action='store_true', help='Rebuild new tables from legacy tables, then report')
    parser.add_argument('--report', action='store_true', help='Just print the verification report')
    parser.add_argument('--finalize', action='store_true', help='Rename legacy tables to legacy_* (irreversible-ish; not a drop)')
    args = parser.parse_args()

    if not any([args.backfill, args.report, args.finalize]):
        parser.print_help()
        sys.exit(1)

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    if args.backfill:
        ensure_new_schema(engine)
        n_products = backfill_products(db)
        n_lines = backfill_lines(db)
        n_teams = backfill_teams(db)
        db.commit()
        print(f'Backfilled {n_products} product row(s), {n_lines} spend line(s), {n_teams} team(s).')
        ok = print_report(db)
        if not ok:
            sys.exit(1)

    elif args.report:
        ok = print_report(db)
        if not ok:
            sys.exit(1)

    if args.finalize:
        finalize(engine)


if __name__ == '__main__':
    main()
