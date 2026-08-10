import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, selectinload

from db import get_db, init_db
from models import (
    AssetLine, AssetLineMonth, Campaign, CampaignProduct, TacticLine, TacticLineMonth, TeamBudget, TeamSubteam,
)
from schemas import CampaignIn, CampaignOut, TeamBudgetIn, TeamBudgetOut, TeamIn, TeamSubteamIn

DEFAULT_YEAR = 2026  # the only year in play so far - a new team registers a budget row for this year

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
    (see module docstring on AssetLine): different expense buckets, additive,
    not two views of the same money."""
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
    asset_lines = _rollup_lines([(al, 'asset', al.asset_name) for al in c.asset_lines], rollup)
    tactic_lines = _rollup_lines([(tl, 'tactic', tl.tactic_name) for tl in c.tactic_lines], rollup)

    return {
        'id': c.id,
        'campaign': c.campaign_name,
        'campaignId': c.source_campaign_id,
        'product': c.product,
        'products': [p.product for p in c.products],
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
    c.products = [CampaignProduct(product=p) for p in data.products]
    c.asset_lines = [
        AssetLine(
            asset_name=al.asset,
            months=[
                AssetLineMonth(year=int(year_str), month=m, plan=v.plan, forecast=v.forecast, commit=v.commit, actual=v.actual)
                for year_str, month_map in al.months.items()
                for m, v in month_map.items()
            ],
        )
        for al in data.assetLines
    ]
    c.tactic_lines = [
        TacticLine(
            tactic_name=tl.tactic,
            months=[
                TacticLineMonth(year=int(year_str), month=m, plan=v.plan, forecast=v.forecast, commit=v.commit, actual=v.actual)
                for year_str, month_map in tl.months.items()
                for m, v in month_map.items()
            ],
        )
        for tl in data.tacticLines
    ]


@app.get('/api/health')
def health():
    return {'status': 'ok'}


LINES_EAGER = [
    selectinload(Campaign.asset_lines).selectinload(AssetLine.months),
    selectinload(Campaign.tactic_lines).selectinload(TacticLine.months),
]


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
    b = db.get(TeamBudget, (team, year, quarter))
    if not b:
        b = TeamBudget(team=team, year=year, quarter=quarter)
        db.add(b)
    b.amount = data.amount
    db.commit()
    db.refresh(b)
    return b


@app.get('/api/teams')
def list_teams(db: Session = Depends(get_db)):
    """A team 'exists' if it has a team_budgets row (any year, any amount,
    including null) - this is the durable team registry, independent of
    whether any campaign has been created for it yet."""
    rows = db.query(TeamBudget.team).distinct().order_by(TeamBudget.team).all()
    return [r[0] for r in rows]


@app.post('/api/teams', status_code=201)
def create_team(data: TeamIn, db: Session = Depends(get_db)):
    """Idempotent: registers the team by ensuring a budget row exists for it
    (just Q1 is enough to mark existence - the team registry only needs one
    row), without touching the amount if one is already there."""
    existing = db.get(TeamBudget, (data.team, DEFAULT_YEAR, 'Q1'))
    if not existing:
        db.add(TeamBudget(team=data.team, year=DEFAULT_YEAR, quarter='Q1', amount=None))
        db.commit()
    return {'team': data.team}


@app.get('/api/subteams')
def list_subteams(db: Session = Depends(get_db)):
    rows = db.query(TeamSubteam).order_by(TeamSubteam.team, TeamSubteam.sub_team).all()
    return [{'team': r.team, 'subTeam': r.sub_team} for r in rows]


@app.post('/api/subteams', status_code=201)
def create_subteam(data: TeamSubteamIn, db: Session = Depends(get_db)):
    existing = db.get(TeamSubteam, (data.team, data.subTeam))
    if not existing:
        db.add(TeamSubteam(team=data.team, sub_team=data.subTeam))
        db.commit()
    return {'team': data.team, 'subTeam': data.subTeam}


@app.delete('/api/teams/{team}', status_code=204)
def delete_team(team: str, db: Session = Depends(get_db)):
    in_use = db.query(Campaign).filter(Campaign.team == team).count()
    if in_use:
        raise HTTPException(400, f'Cannot delete "{team}": {in_use} campaign(s) still use this team.')
    db.query(TeamBudget).filter(TeamBudget.team == team).delete()
    db.query(TeamSubteam).filter(TeamSubteam.team == team).delete()
    db.commit()


@app.delete('/api/subteams/{team}/{sub_team}', status_code=204)
def delete_subteam(team: str, sub_team: str, db: Session = Depends(get_db)):
    in_use = db.query(Campaign).filter(Campaign.team == team, Campaign.sub_team == sub_team).count()
    if in_use:
        raise HTTPException(400, f'Cannot delete "{sub_team}": {in_use} campaign(s) still use this sub team.')
    row = db.get(TeamSubteam, (team, sub_team))
    if row:
        db.delete(row)
        db.commit()
