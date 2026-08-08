"""
Loads campaign/team/budget data into Postgres. Two ways to run:

    python seed.py
        Normal rebuild flow - loads scripts/budget_data.json (produced by
        fetch_from_backend.py or extract.py). Safe to re-run any time.

    python seed.py --snapshot-dir <dir>
        One-time migration mode - loads a full pre-migration snapshot
        (campaigns_export.json, teams.json, subteams.json, budgets.json, as
        captured straight from the live API before a schema change) so real
        budget amounts and zero-campaign teams/sub-teams survive exactly,
        not just whatever's inferable from campaigns.

Either way this wipes and re-inserts every campaign - never run it against a
database you haven't snapshotted first if it holds real data.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'api'))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    AssetLine, AssetLineMonth, Base, Campaign, CampaignProduct, TacticLine, TacticLineMonth, TeamBudget, TeamSubteam,
)

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://budget:budget@localhost:5432/budget')
DATA_FILE = Path(__file__).parent.parent / 'scripts' / 'budget_data.json'
DEFAULT_YEAR = 2026

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

_unspecified_fallbacks = 0


def parse_date(s):
    """Sheet dates come through as 'M/D/YYYY' strings (or already-ISO if the cell was
    a real Excel date). Falls back to None for anything that doesn't parse."""
    if not s:
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def build_lines(campaign_data):
    """Returns (asset_lines, tactic_lines) - each a list of {asset|tactic: name,
    months: {year: {month: {...}}}}. Assets and tactics are independent
    expense buckets, additive for the campaign total (see AssetLine's
    docstring in models.py) - never merge one asset with one tactic onto a
    single line. Accepts three shapes:
      - already-split (assetLines + tacticLines present, no spendLines): a
        fresh export from the current backend, months already year-keyed.
      - merged spendLines shape (this session's short-lived intermediate
        architecture): each spend line becomes an asset line (real $
        preserved) and, if it had a tactic tag, a separate tactic line with
        EMPTY months - there's no historical per-tactic $ to recover, and
        keeping the old mirrored amount would double the campaign total now
        that asset + tactic are additive. Real tactic $ starts fresh from here.
      - oldest sheet shape (assetLines with bare month names + a single
        'tactic' string, no year): same treatment - one tag-only tactic line,
        no $.
    Falls back to a single asset='Unspecified' line if there are no asset
    lines but there is real campaign-level $ (counted in
    _unspecified_fallbacks so nothing is silently mis-tagged)."""
    global _unspecified_fallbacks

    if 'assetLines' in campaign_data and 'tacticLines' in campaign_data:
        asset_lines = [
            {'asset': al['asset'], 'months': {int(y): m for y, m in al.get('months', {}).items()}}
            for al in campaign_data['assetLines']
        ]
        tactic_lines = [
            {'tactic': tl['tactic'], 'months': {int(y): m for y, m in tl.get('months', {}).items()}}
            for tl in campaign_data['tacticLines']
        ]
        return asset_lines, tactic_lines

    if 'spendLines' in campaign_data:
        asset_lines = []
        tactic_names = []
        for sl in campaign_data['spendLines']:
            months = {int(y): m for y, m in sl.get('months', {}).items()}
            asset_lines.append({'asset': sl['asset'], 'months': months})
            if sl.get('tactic') and sl['tactic'] not in tactic_names:
                tactic_names.append(sl['tactic'])
        return asset_lines, [{'tactic': t, 'months': {}} for t in tactic_names]

    # oldest sheet shape
    asset_lines = [
        {'asset': al['asset'], 'months': {DEFAULT_YEAR: al.get('months', {})}}
        for al in campaign_data.get('assetLines', [])
    ]
    tactic = campaign_data.get('tactic')
    tactic_lines = [{'tactic': tactic, 'months': {}}] if tactic else []
    if not asset_lines:
        months_src = campaign_data.get('months', {})
        has_real_spend = any(
            v.get('plan') or v.get('forecast') or v.get('commit') or v.get('actual')
            for v in months_src.values()
        )
        if has_real_spend:
            _unspecified_fallbacks += 1
            asset_lines = [{'asset': 'Unspecified', 'months': {DEFAULT_YEAR: months_src}}]
    return asset_lines, tactic_lines


def build_campaign(team, data):
    c = Campaign(
        campaign_name=data['campaign'],
        source_campaign_id=data.get('campaignId'),
        product=data.get('product'),
        region=data.get('region'),
        team=team,
        sub_team=data.get('subTeam'),
        asset=data.get('asset'),
        campaign_type=data.get('campaignType'),
        sub_campaign_type=data.get('subCampaignType'),
        program_type=data.get('programType'),
        objective=data.get('objective'),
        theme=data.get('theme'),
        comments=data.get('comments'),
        start_date=parse_date(data.get('startDate')),
        end_date=parse_date(data.get('endDate')),
        conv_rate=data.get('convRate'),
    )
    c.products = [CampaignProduct(product=p) for p in data.get('products', [])]
    asset_lines_data, tactic_lines_data = build_lines(data)
    c.asset_lines = [
        AssetLine(
            asset_name=al['asset'],
            months=[
                AssetLineMonth(
                    year=year, month=m,
                    plan=v.get('plan'), forecast=v.get('forecast'), commit=v.get('commit'), actual=v.get('actual'),
                )
                for year, month_map in al['months'].items()
                for m, v in month_map.items()
            ],
        )
        for al in asset_lines_data
    ]
    c.tactic_lines = [
        TacticLine(
            tactic_name=tl['tactic'],
            months=[
                TacticLineMonth(
                    year=year, month=m,
                    plan=v.get('plan'), forecast=v.get('forecast'), commit=v.get('commit'), actual=v.get('actual'),
                )
                for year, month_map in tl['months'].items()
                for m, v in month_map.items()
            ],
        )
        for tl in tactic_lines_data
    ]
    return c


def load_input(snapshot_dir):
    if snapshot_dir:
        d = Path(snapshot_dir)
        campaigns = json.load(open(d / 'campaigns_export.json'))['campaigns']
        teams = json.load(open(d / 'teams.json'))
        subteams = json.load(open(d / 'subteams.json'))
        budgets = json.load(open(d / 'budgets.json'))
        return campaigns, teams, subteams, budgets

    if not DATA_FILE.exists():
        print(f'No {DATA_FILE} found - run scripts/build_dashboard.py (or extract.py) first.')
        sys.exit(1)
    data = json.load(open(DATA_FILE))
    # The backend's export is a flat, team-agnostic {campaigns:[...]} list.
    # extract.py's sheet-based path still produces the older, fixed
    # {growthMarketing, fieldMarketing} shape (the sheet only ever has those
    # two tabs) - each campaign dict already carries its own 'team' either way.
    campaigns = data.get('campaigns')
    if campaigns is None:
        campaigns = [*data.get('growthMarketing', []), *data.get('fieldMarketing', [])]
    return campaigns, None, None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--snapshot-dir', default=None,
        help='Full pre-migration snapshot dir (campaigns_export.json, teams.json, subteams.json, '
             'budgets.json) instead of scripts/budget_data.json',
    )
    args = parser.parse_args()

    campaigns, snapshot_teams, snapshot_subteams, snapshot_budgets = load_input(args.snapshot_dir)
    if not campaigns and snapshot_teams is None:
        print(f'WARNING: input has 0 campaigns - refusing to seed against what looks like an empty/broken source.')
        sys.exit(1)

    Base.metadata.create_all(engine)

    session = Session()
    existing = session.query(Campaign).count()
    if existing:
        print(f'Wiping {existing} existing campaign(s)...')
        session.query(Campaign).delete()
        session.commit()

    count = 0
    team_subteams = set()
    teams_seen = set()
    for camp in campaigns:
        team_name = camp['team']
        teams_seen.add(team_name)
        session.add(build_campaign(team_name, camp))
        count += 1
        if camp.get('subTeam'):
            team_subteams.add((team_name, camp['subTeam']))

    # Explicit snapshot teams/sub-teams (covers zero-campaign ones the inference
    # above would otherwise miss) and real budget amounts (not just None).
    if snapshot_teams is not None:
        teams_seen |= set(snapshot_teams)
    if snapshot_subteams is not None:
        team_subteams |= {(row['team'], row['subTeam']) for row in snapshot_subteams}

    budget_amounts = {}
    if snapshot_budgets is not None:
        for row in snapshot_budgets:
            budget_amounts[(row['team'], row['year'])] = row['amount']
    for team_name in teams_seen:
        budget_amounts.setdefault((team_name, DEFAULT_YEAR), None)  # ensure at least a registration row

    for (team_name, year), amount in budget_amounts.items():
        if not session.get(TeamBudget, (team_name, year)):
            session.add(TeamBudget(team=team_name, year=year, amount=amount))
    for team_name, sub_team in team_subteams:
        if not session.get(TeamSubteam, (team_name, sub_team)):
            session.add(TeamSubteam(team=team_name, sub_team=sub_team))

    session.commit()
    print(f'Seeded {count} campaigns, {len(team_subteams)} team/sub-team pair(s), '
          f'{len(budget_amounts)} budget row(s) into the database.')
    if _unspecified_fallbacks:
        print(f'WARNING: {_unspecified_fallbacks} campaign(s) had no asset lines but had campaign-level '
              f'$ - fell back to asset="Unspecified". Review these manually.')


if __name__ == '__main__':
    main()
