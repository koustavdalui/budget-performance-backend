from sqlalchemy import CheckConstraint, Column, Integer, String, Text, Numeric, Date, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


class Campaign(Base):
    __tablename__ = 'campaigns'

    id = Column(Integer, primary_key=True)
    campaign_name = Column(String, nullable=False)
    source_campaign_id = Column(String)  # the original Salesforce-style Campaign ID from the sheet
    team = Column(String, nullable=False)  # any team registered in `teams` - not limited to the original two
    sub_team = Column(String)
    region = Column(String)
    product = Column(String)  # raw combined string e.g. "EOR, AOR, Global Payroll" - kept for display
    # Physical column is `product_tags`, not `products` - the original
    # `products` column is a leftover JSONB column from a previous, reverted
    # consolidation attempt, left in place (unused) rather than dropped.
    products = Column('product_tags', ARRAY(String), nullable=False, default=list)
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

    spend_lines = relationship('SpendLine', cascade='all, delete-orphan', backref='campaign')


class SpendLine(Base):
    """One row per spend line (asset OR tactic) on a campaign - replaces the
    old asset_lines/tactic_lines table pair. `line_type` tells them apart;
    they stay independent, additive expense buckets (a campaign's total is
    sum(asset lines) + sum(tactic lines), not one or the other) - merging
    them into one table with a discriminator column doesn't change that
    meaning, it just avoids two near-identical tables. Real $ lives in the
    child `spend_line_months` table, one real numeric column per metric -
    never a JSON blob."""
    __tablename__ = 'spend_lines'
    __table_args__ = (
        CheckConstraint("line_type IN ('asset', 'tactic')", name='ck_spend_lines_line_type'),
    )

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    line_type = Column(String, nullable=False)  # 'asset' or 'tactic'
    line_name = Column(String, nullable=False)

    months = relationship('SpendLineMonth', cascade='all, delete-orphan', backref='spend_line')


class SpendLineMonth(Base):
    __tablename__ = 'spend_line_months'

    spend_line_id = Column(Integer, ForeignKey('spend_lines.id', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    month = Column(String, primary_key=True)
    plan = Column(Numeric)
    forecast = Column(Numeric)
    commit = Column(Numeric)
    actual = Column(Numeric)


class Team(Base):
    """Explicit team registry - `name` is the PK, a team exists iff it has a
    row here (independent of whether it has any budget or campaign yet).
    `sub_teams` is a flat array of sub-team names - replaces the old
    team_subteams table, same reasoning as Campaign.products above."""
    __tablename__ = 'teams'

    name = Column(String, primary_key=True)
    sub_teams = Column(ARRAY(String), nullable=False, default=list)


class TeamBudget(Base):
    """Quarterly $ allocated by Marketing Ops per team - set before Plan even
    exists, not tied to any campaign or month."""
    __tablename__ = 'team_budgets'

    team = Column(String, ForeignKey('teams.name', ondelete='CASCADE'), primary_key=True)
    year = Column(Integer, primary_key=True)
    quarter = Column(String, primary_key=True)
    amount = Column(Numeric)
