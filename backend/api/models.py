from sqlalchemy import Column, Integer, String, Text, Numeric, Date, DateTime, ForeignKey, func
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
    """One row per asset-tagged spend line on a campaign. Independent from
    tactic lines - a campaign's total is sum(asset lines) + sum(tactic
    lines), additive expense buckets, not two views of the same money."""
    __tablename__ = 'asset_lines'

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    asset_name = Column(String, nullable=False)

    months = relationship('AssetLineMonth', cascade='all, delete-orphan', backref='asset_line')


class AssetLineMonth(Base):
    __tablename__ = 'asset_line_months'

    asset_line_id = Column(Integer, ForeignKey('asset_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class TacticLine(Base):
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
    """Quarterly $ allocated by Marketing Ops per team - set before Plan even
    exists, not tied to any campaign or month. A team 'exists' in the app iff
    it has at least one row here (any year, any amount, including null) -
    this doubles as the team registry, same as before the JSONB detour."""
    __tablename__ = 'team_budgets'

    team = Column(String, primary_key=True)
    year = Column(Integer, primary_key=True)
    quarter = Column(String, primary_key=True)
    amount = Column(Numeric)


class TeamSubteam(Base):
    __tablename__ = 'team_subteams'

    team = Column(String, primary_key=True)
    sub_team = Column(String, primary_key=True)
