from sqlalchemy import (
    Column, Integer, String, Text, Numeric, Date, DateTime, ForeignKey, func
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


class Campaign(Base):
    __tablename__ = 'campaigns'

    id = Column(Integer, primary_key=True)
    campaign_name = Column(String, nullable=False)
    source_campaign_id = Column(String)  # the original Salesforce-style Campaign ID from the sheet
    team = Column(String, nullable=False)  # any team registered in team_budgets - not limited to the original two
    sub_team = Column(String)
    region = Column(String)
    product = Column(String)  # raw combined string e.g. "EOR, AOR, Global Payroll" - kept for display
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

    products = relationship('CampaignProduct', cascade='all, delete-orphan', backref='campaign')
    asset_lines = relationship('AssetLine', cascade='all, delete-orphan', backref='campaign')
    tactic_lines = relationship('TacticLine', cascade='all, delete-orphan', backref='campaign')


class CampaignProduct(Base):
    __tablename__ = 'campaign_products'

    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), primary_key=True)
    product = Column(String, primary_key=True)


class AssetLine(Base):
    """Asset = something you create/own that delivers value and can be reused
    (a whitepaper, a booth, a landing page...). A campaign can have several,
    each with its own $. Independent from TacticLine below - a campaign's
    total is the SUM OF BOTH (see campaign_to_dict): asset spend and tactic
    spend are different expense buckets (e.g. $5k producing a video, plus $3k
    running paid social to promote it = $8k total), not two views of the same
    money. 'By Asset' groups by asset_name."""
    __tablename__ = 'asset_lines'

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    asset_name = Column(String, nullable=False)

    months = relationship('AssetLineMonth', cascade='all, delete-orphan', backref='asset_line')


class AssetLineMonth(Base):
    """Multi-year: keyed by (year, month) so a line can span more than one
    calendar year."""
    __tablename__ = 'asset_line_months'

    asset_line_id = Column(Integer, ForeignKey('asset_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)  # 'Jan'..'Dec'
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class TacticLine(Base):
    """Tactic = an action/method used to reach a goal, often using an asset
    but not always (e.g. paid social spend, telemarketing). A campaign can
    have several, each with its own $ - independent from AssetLine, additive
    with it for the campaign total (see AssetLine's docstring). 'By Tactic'
    groups by tactic_name."""
    __tablename__ = 'tactic_lines'

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    tactic_name = Column(String, nullable=False)

    months = relationship('TacticLineMonth', cascade='all, delete-orphan', backref='tactic_line')


class TacticLineMonth(Base):
    __tablename__ = 'tactic_line_months'

    tactic_line_id = Column(Integer, ForeignKey('tactic_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class TeamBudget(Base):
    """Yearly budget allocated to a team by Marketing Ops - one number per
    team per year, set before Plan even exists. Not tied to any campaign or
    month, unlike plan/forecast/commit/actual. A team is considered to "exist"
    if it has a row here (even with amount=None) - this doubles as the team
    registry so a brand-new team survives before any campaign uses it."""
    __tablename__ = 'team_budgets'

    team = Column(String, primary_key=True)
    year = Column(Integer, primary_key=True)
    amount = Column(Numeric)


class TeamSubteam(Base):
    """Registers that a sub team exists under a team, independent of whether
    any campaign uses it yet - lets a user add a sub team ahead of creating
    campaigns for it."""
    __tablename__ = 'team_subteams'

    team = Column(String, primary_key=True)
    sub_team = Column(String, primary_key=True)
