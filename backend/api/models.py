from sqlalchemy import (
    CheckConstraint, Column, Integer, String, Text, Numeric, Date, DateTime, ForeignKey, func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

# Generic JSON everywhere except Postgres, where it becomes real JSONB (indexable,
# more efficient) - lets the same models work against SQLite in tests without a
# live Postgres instance, while production still gets JSONB.
JSONType = JSON().with_variant(JSONB, 'postgresql')


class Campaign(Base):
    __tablename__ = 'campaigns'

    id = Column(Integer, primary_key=True)
    campaign_name = Column(String, nullable=False)
    source_campaign_id = Column(String)  # the original Salesforce-style Campaign ID from the sheet
    team = Column(String, nullable=False)  # any team registered in `teams` - not limited to the original two
    sub_team = Column(String)
    region = Column(String)
    product = Column(String)  # raw combined string e.g. "EOR, AOR, Global Payroll" - kept for display
    products = Column(JSONType, nullable=False, default=list)  # ["EOR","AOR",...] - replaces campaign_products table
    asset = Column(String)  # GM's single asset tag; null for FM
    campaign_type = Column(String)
    sub_campaign_type = Column(String)
    program_type = Column(String)
    objective = Column(String)
    theme = Column(String)
    comments = Column(Text)
    start_date = Column(Date)
    end_date = Column(Date)
    conv_rate = Column(Numeric)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    lines = relationship('CampaignLine', cascade='all, delete-orphan', backref='campaign')


class CampaignLine(Base):
    """One row per spend line (asset OR tactic) on a campaign - replaces the old
    asset_lines/tactic_lines/asset_line_months/tactic_line_months four-table
    split. `months` is a JSONB blob keyed by year (string) then month
    abbreviation, e.g. {"2026": {"Jan": {"plan": 1000, "forecast": 1000,
    "commit": 900, "actual": null}}} - the exact same nested shape the API
    already reads/writes (schemas.AssetLineIn/TacticLineIn.months), just
    stored as one JSON value instead of exploded into one row per
    (line, year, month). Uniqueness of (year, month) within a line is
    enforced by dict-key semantics rather than a composite primary key -
    equivalent guarantee, enforced one layer up (in the API/Pydantic layer)
    instead of by Postgres itself.

    Asset and tactic lines stay independent, additive expense buckets (a
    campaign's total is sum(asset lines) + sum(tactic lines), not one or the
    other) - line_type is how they're told apart now instead of living in
    separate tables. 'By Asset'/'By Tactic' groups by line_name within each type."""
    __tablename__ = 'campaign_lines'
    __table_args__ = (
        CheckConstraint("line_type IN ('asset', 'tactic')", name='ck_campaign_lines_line_type'),
    )

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    line_type = Column(String, nullable=False)  # 'asset' or 'tactic'
    line_name = Column(String, nullable=False)
    months = Column(JSONType, nullable=False, default=dict)


class Team(Base):
    """Explicit team registry - replaces team_budgets + team_subteams. A team
    exists iff it has a row here (name is the PK), independent of whether it
    has any budget or sub team yet - no more piggy-backing team existence on
    a budget row like the old team_budgets table did.

    `budgets` is {year_str: {quarter: amount}} - quarterly $ allocated by
    Marketing Ops, set before Plan even exists, not tied to any campaign or
    month. `sub_teams` is a plain list of sub-team names registered under
    this team. Both replace what used to be their own tables; uniqueness of
    sub-team names is enforced by the API (de-duping on insert) rather than a
    composite primary key, same trade-off as CampaignLine.months above."""
    __tablename__ = 'teams'

    name = Column(String, primary_key=True)
    sub_teams = Column(JSONType, nullable=False, default=list)
    budgets = Column(JSONType, nullable=False, default=dict)


# ---------------------------------------------------------------------------
# Legacy tables (pre-consolidation 8-table schema). Kept ONLY so
# backend/migrate_consolidate.py can read the old data during the one-time
# backfill. Not used by main.py. Safe to delete these classes once the
# backfill has run and been verified against every environment, and the
# underlying tables have been dropped (see migrate_consolidate.py --finalize).
# ---------------------------------------------------------------------------

class LegacyCampaignProduct(Base):
    __tablename__ = 'campaign_products'

    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), primary_key=True)
    product = Column(String, primary_key=True)


class LegacyAssetLine(Base):
    __tablename__ = 'asset_lines'

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    asset_name = Column(String, nullable=False)

    months = relationship('LegacyAssetLineMonth', cascade='all, delete-orphan', backref='asset_line')


class LegacyAssetLineMonth(Base):
    __tablename__ = 'asset_line_months'

    asset_line_id = Column(Integer, ForeignKey('asset_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class LegacyTacticLine(Base):
    __tablename__ = 'tactic_lines'

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    tactic_name = Column(String, nullable=False)

    months = relationship('LegacyTacticLineMonth', cascade='all, delete-orphan', backref='tactic_line')


class LegacyTacticLineMonth(Base):
    __tablename__ = 'tactic_line_months'

    tactic_line_id = Column(Integer, ForeignKey('tactic_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class LegacyTeamBudget(Base):
    __tablename__ = 'team_budgets'

    team = Column(String, primary_key=True)
    year = Column(Integer, primary_key=True)
    quarter = Column(String, primary_key=True)
    amount = Column(Numeric)


class LegacyTeamSubteam(Base):
    __tablename__ = 'team_subteams'

    team = Column(String, primary_key=True)
    sub_team = Column(String, primary_key=True)
