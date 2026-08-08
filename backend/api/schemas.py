from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MonthValues(BaseModel):
    plan: Optional[float] = None
    forecast: Optional[float] = None
    commit: Optional[float] = None
    actual: Optional[float] = None


class AssetLineIn(BaseModel):
    """Months keyed by year (as a string) then month abbreviation, e.g.
    {"2026": {"Jan": {...}}, "2027": {"Jan": {...}}} - a line can span years."""
    asset: str
    months: dict[str, dict[str, MonthValues]] = {}


class AssetLineOut(AssetLineIn):
    pass


class TacticLineIn(BaseModel):
    tactic: str
    months: dict[str, dict[str, MonthValues]] = {}


class TacticLineOut(TacticLineIn):
    pass


class CampaignIn(BaseModel):
    campaign: str
    campaignId: Optional[str] = None
    product: Optional[str] = None
    products: list[str] = []
    region: Optional[str] = 'Unspecified'
    team: str
    subTeam: Optional[str] = 'Unspecified'
    asset: Optional[str] = None
    campaignType: Optional[str] = None
    subCampaignType: Optional[str] = None
    programType: Optional[str] = None
    objective: Optional[str] = None
    theme: Optional[str] = None
    comments: Optional[str] = None
    startDate: Optional[date] = None
    endDate: Optional[date] = None
    convRate: Optional[float] = None
    assetLines: list[AssetLineIn] = []
    tacticLines: list[TacticLineIn] = []


class CampaignOut(CampaignIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    months: dict[str, dict[str, MonthValues]] = {}  # computed rollup: sum(assetLines) + sum(tacticLines), output-only


class TeamBudgetIn(BaseModel):
    team: str
    year: int
    amount: Optional[float] = None


class TeamBudgetOut(TeamBudgetIn):
    model_config = ConfigDict(from_attributes=True)


class TeamIn(BaseModel):
    team: str


class TeamSubteamIn(BaseModel):
    team: str
    subTeam: str


class TeamSubteamOut(TeamSubteamIn):
    model_config = ConfigDict(from_attributes=True)
