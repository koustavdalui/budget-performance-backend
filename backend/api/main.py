import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from db import get_db, init_db
from models import Campaign, SpendLine, SpendLineMonth, Team, TeamBudget
from schemas import (
    CampaignIn, CampaignOut, TeamBudgetBulkIn, TeamBudgetBulkOut, TeamBudgetIn, TeamBudgetOut,
    TeamIn, TeamSubteamIn,
)

DEFAULT_YEAR = 2026  # the only year in play so far - a new team registers a budget row for this year
VALID_QUARTERS = ('Q1', 'Q2', 'Q3', 'Q4')


def _normalize_quarter(quarter: str) -> str:
    q = (quarter or '').strip().upper()
    if q not in VALID_QUARTERS:
        raise HTTPException(400, f'Quarter must be one of {", ".join(VALID_QUARTERS)}')
    return q


def _get_or_create_team(db: Session, name: str) -> Team:
    """Team is the explicit registry - a team exists once this row exists,
    regardless of budgets/sub_teams content. team_budgets.team FKs to this
    table, so any budget write must ensure the team row exists first."""
    t = db.get(Team, name)
    if not t:
        t = Team(name=name, sub_teams=[])
        db.add(t)
        db.flush()
    return t


app = FastAPI(title='Marketing Budget Backend')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Pilot-only access gate: unset locally/in normal dev (docker-compose never sets
# this), so local usage is completely unaffected. Set only on the temporary
# Render pilot deployment, to stop the raw API URL from being publicly callable
# by anyone who isn't going through the pilot frontend (which sends this same
# token on every request). Not real per-user auth - a single shared secret,
# proportionate to a short internal UAT trial, not a production auth scheme.
PILOT_ACCESS_TOKEN = os.environ.get('PILOT_ACCESS_TOKEN')


@app.middleware('http')
async def require_pilot_token(request: Request, call_next):
    if PILOT_ACCESS_TOKEN and request.method != 'OPTIONS' and request.url.path != '/api/health':
        if request.headers.get('x-pilot-token') != PILOT_ACCESS_TOKEN:
            return JSONResponse(status_code=403, content={'detail': 'Forbidden'})
    return await call_next(request)


@app.on_event('startup')
def on_startup():
    init_db()


def _rollup_lines(lines, rollup):
    """Builds one line's own {year: {month: {...}}} dict and accumulates its
    values into the shared campaign-level rollup (mutated in place). Used for
    both asset lines and tactic lines - the campaign total is the sum of BOTH
    (see SpendLine's docstring in models.py): different expense buckets,
    additive, not two views of the same money."""
    out = []
    for line, name_field, name in lines:
        line_months: dict = {}
        for lm in line.months:
            year_str = str(lm.year)
            vals = {
                'plan': float(lm.plan) if lm.plan is not None else None,
                'forecast': float(lm.forecast) if lm.forecast is not None else None,
                'commit': float(lm.commit) if lm.commit is not None else None,
                'actual': float(lm.actual) if lm.actual is not None else None,
            }
            line_months.setdefault(year_str, {})[lm.month] = vals

            bucket = rollup.setdefault(year_str, {}).setdefault(
                lm.month, {'plan': None, 'forecast': None, 'commit': None, 'actual': None}
            )
            for metric, v in vals.items():
                if v is not None:
                    bucket[metric] = (bucket[metric] or 0) + v

        out.append({name_field: name, 'months': line_months})
    return out


def campaign_to_dict(c: Campaign) -> dict:
    rollup: dict = {}
    asset_lines = _rollup_lines(
        [(l, 'asset', l.line_name) for l in c.spend_lines if l.line_type == 'asset'], rollup,
    )
    tactic_lines = _rollup_lines(
        [(l, 'tactic', l.line_name) for l in c.spend_lines if l.line_type == 'tactic'], rollup,
    )

    return {
        'id': c.id,
        'campaign': c.campaign_name,
        'campaignId': c.source_campaign_id,
        'product': c.product,
        'products': list(c.products or []),
        'region': c.region,
        'team': c.team,
        'subTeam': c.sub_team,
        'asset': c.asset,
        'campaignType': c.campaign_type,
        'subCampaignType': c.sub_campaign_type,
        'programType': c.program_type,
        'objective': c.objective,
        'theme': c.theme,
        'comments': c.comments,
        'startDate': c.start_date,
        'endDate': c.end_date,
        'convRate': float(c.conv_rate) if c.conv_rate is not None else None,
        'assetLines': asset_lines,
        'tacticLines': tactic_lines,
        'months': rollup,
    }


def apply_fields(c: Campaign, data: CampaignIn):
    c.campaign_name = data.campaign
    c.source_campaign_id = data.campaignId
    c.product = data.product
    c.products = list(data.products)
    c.region = data.region
    c.team = data.team
    c.sub_team = data.subTeam
    c.asset = data.asset
    c.campaign_type = data.campaignType
    c.sub_campaign_type = data.subCampaignType
    c.program_type = data.programType
    c.objective = data.objective
    c.theme = data.theme
    c.comments = data.comments
    c.start_date = data.startDate
    c.end_date = data.endDate
    c.conv_rate = data.convRate

    # Replace-all strategy for child rows - simplest correct behavior for a v1 API.
    c.spend_lines = [
        SpendLine(
            line_type='asset', line_name=al.asset,
            months=[
                SpendLineMonth(year=int(year_str), month=m, plan=v.plan, forecast=v.forecast, commit=v.commit, actual=v.actual)
                for year_str, month_map in al.months.items()
                for m, v in month_map.items()
            ],
        )
        for al in data.assetLines
    ] + [
        SpendLine(
            line_type='tactic', line_name=tl.tactic,
            months=[
                SpendLineMonth(year=int(year_str), month=m, plan=v.plan, forecast=v.forecast, commit=v.commit, actual=v.actual)
                for year_str, month_map in tl.months.items()
                for m, v in month_map.items()
            ],
        )
        for tl in data.tacticLines
    ]


@app.get('/api/health')
def health():
    return {'status': 'ok'}


LINES_EAGER = [selectinload(Campaign.spend_lines).selectinload(SpendLine.months)]


@app.get('/api/campaigns', response_model=list[CampaignOut])
def list_campaigns(team: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Campaign).options(*LINES_EAGER)
    if team:
        q = q.filter(Campaign.team == team)
    return [campaign_to_dict(c) for c in q.all()]


@app.get('/api/campaigns/export')
def export_campaigns(db: Session = Depends(get_db)):
    """Flat list for the dashboard build/refresh - team-agnostic, so it works for
    however many teams exist, not just the original two. Each campaign already
    carries its own 'team' field."""
    campaigns = []
    for c in db.query(Campaign).options(*LINES_EAGER).all():
        d = campaign_to_dict(c)
        del d['id']
        campaigns.append(d)
    return {'campaigns': campaigns}


@app.get('/api/campaigns/{campaign_id}', response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.query(Campaign).options(*LINES_EAGER).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, 'Campaign not found')
    return campaign_to_dict(c)


@app.post('/api/campaigns', response_model=CampaignOut, status_code=201)
def create_campaign(data: CampaignIn, db: Session = Depends(get_db)):
    c = Campaign()
    apply_fields(c, data)
    db.add(c)
    db.commit()
    db.refresh(c)
    return campaign_to_dict(c)


@app.put('/api/campaigns/{campaign_id}', response_model=CampaignOut)
def update_campaign(campaign_id: int, data: CampaignIn, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, 'Campaign not found')
    apply_fields(c, data)
    db.commit()
    db.refresh(c)
    return campaign_to_dict(c)


@app.delete('/api/campaigns/{campaign_id}', status_code=204)
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, 'Campaign not found')
    db.delete(c)
    db.commit()


@app.get('/api/budgets', response_model=list[TeamBudgetOut])
def list_budgets(db: Session = Depends(get_db)):
    return db.query(TeamBudget).all()


@app.put('/api/budgets/{team}/{year}/{quarter}', response_model=TeamBudgetOut)
def upsert_budget(team: str, year: int, quarter: str, data: TeamBudgetIn, db: Session = Depends(get_db)):
    quarter = _normalize_quarter(quarter)
    _get_or_create_team(db, team)
    b = db.get(TeamBudget, (team, year, quarter))
    if not b:
        b = TeamBudget(team=team, year=year, quarter=quarter)
        db.add(b)
    b.amount = data.amount
    db.commit()
    db.refresh(b)
    return b


@app.post('/api/budgets/bulk', response_model=TeamBudgetBulkOut)
def bulk_upsert_budgets(data: TeamBudgetBulkIn, db: Session = Depends(get_db)):
    """Upsert or clear many quarterly team budgets in one transaction.
    Unknown teams are registered automatically (a Team row is created on
    first touch, since team_budgets.team FKs to teams.name). action=delete
    removes that row entirely."""
    upserted = 0
    deleted = 0
    for row in data.rows:
        team = (row.team or '').strip()
        if not team:
            raise HTTPException(400, 'Each row needs a team')
        quarter = _normalize_quarter(row.quarter)
        action = (row.action or 'update').strip().lower()
        if action not in ('update', 'create', 'delete'):
            raise HTTPException(400, f'Action must be update, create, or delete (got "{row.action}")')
        if action == 'delete':
            b = db.get(TeamBudget, (team, row.year, quarter))
            if b:
                db.delete(b)
                deleted += 1
        else:
            _get_or_create_team(db, team)
            b = db.get(TeamBudget, (team, row.year, quarter))
            if not b:
                b = TeamBudget(team=team, year=row.year, quarter=quarter)
                db.add(b)
            b.amount = row.amount
            upserted += 1
    db.commit()
    return {
        'upserted': upserted,
        'deleted': deleted,
        'budgets': db.query(TeamBudget).all(),
    }


@app.get('/api/teams')
def list_teams(db: Session = Depends(get_db)):
    """A team 'exists' if it has a row in the teams table - the explicit
    registry, independent of whether it has any budget or campaign yet."""
    rows = db.query(Team.name).order_by(Team.name).all()
    return [r[0] for r in rows]


@app.post('/api/teams', status_code=201)
def create_team(data: TeamIn, db: Session = Depends(get_db)):
    """Idempotent: registers the team if it doesn't already exist."""
    _get_or_create_team(db, data.team)
    db.commit()
    return {'team': data.team}


@app.get('/api/subteams')
def list_subteams(db: Session = Depends(get_db)):
    rows = []
    for t in db.query(Team).order_by(Team.name).all():
        for st in sorted(t.sub_teams or []):
            rows.append({'team': t.name, 'subTeam': st})
    return rows


@app.post('/api/subteams', status_code=201)
def create_subteam(data: TeamSubteamIn, db: Session = Depends(get_db)):
    t = _get_or_create_team(db, data.team)
    subs = list(t.sub_teams or [])
    if data.subTeam not in subs:
        subs.append(data.subTeam)
        t.sub_teams = subs
    db.commit()
    return {'team': data.team, 'subTeam': data.subTeam}


@app.delete('/api/teams/{team}', status_code=204)
def delete_team(team: str, db: Session = Depends(get_db)):
    in_use = db.query(Campaign).filter(Campaign.team == team).count()
    if in_use:
        raise HTTPException(400, f'Cannot delete "{team}": {in_use} campaign(s) still use this team.')
    t = db.get(Team, team)
    if t:
        db.delete(t)  # team_budgets rows cascade-delete via the FK
        db.commit()


@app.delete('/api/subteams/{team}/{sub_team}', status_code=204)
def delete_subteam(team: str, sub_team: str, db: Session = Depends(get_db)):
    in_use = db.query(Campaign).filter(Campaign.team == team, Campaign.sub_team == sub_team).count()
    if in_use:
        raise HTTPException(400, f'Cannot delete "{sub_team}": {in_use} campaign(s) still use this sub team.')
    t = db.get(Team, team)
    if t and sub_team in (t.sub_teams or []):
        t.sub_teams = [s for s in t.sub_teams if s != sub_team]
        db.commit()
