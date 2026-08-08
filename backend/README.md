# Budget backend

Postgres + a small FastAPI service that will become the source of truth for
campaign budget data, replacing the Google Sheet. The data-entry UI (built
separately) will read/write through this API; the dashboard rebuild pulls
from it too.

## One-time setup

1. Install Docker Desktop (docker.com → Docker Desktop → macOS), open it once
   so it finishes its first-launch setup.
2. From this `backend/` folder:
   ```bash
   docker compose up -d
   ```
   This starts Postgres (port 5432) and the API (port 8000). First run builds
   the API image, which takes a minute; the app also auto-creates its tables
   on startup, so no manual migration step is needed.
3. Install the same Python deps on your host (needed for `seed.py` and the
   dashboard build scripts, which run outside Docker):
   ```bash
   pip install -r api/requirements.txt
   ```
4. Load the current Google Sheet data into the database:
   ```bash
   python seed.py
   ```
   Safe to re-run any time — it wipes and reloads every campaign from
   `../scripts/budget_data.json`.

## Day to day

- **API docs / try it live**: http://localhost:8000/docs (Swagger UI - FastAPI
  generates this automatically from the endpoints). This is what the person
  building the data-entry UI should look at first to see the exact request/
  response shapes.
- **Rebuild the dashboard from the database**:
  ```bash
  cd ../scripts && python build_dashboard.py
  ```
  This now defaults to pulling fresh data from the backend (`fetch_from_backend.py`)
  instead of the old xlsx/Google Sheet path. Use `--from-sheet` to go back to
  re-pulling from the Google Sheet instead, or `--no-refresh` to just rebuild
  from whatever's already in `budget_data.json`.
- **Stop everything**: `docker compose down` (add `-v` to also wipe the
  database volume).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness check |
| GET | `/api/campaigns` | flat list of all campaigns (optional `?team=` filter) |
| GET | `/api/campaigns/{id}` | one campaign |
| GET | `/api/campaigns/export` | `{campaigns:[...]}` - flat, team-agnostic list; exactly what the dashboard build consumes |
| POST | `/api/campaigns` | create a campaign |
| PUT | `/api/campaigns/{id}` | replace a campaign (metadata + asset/tactic lines) |
| DELETE | `/api/campaigns/{id}` | delete a campaign |
| GET | `/api/budgets` | list all team budgets (any team, any year) |
| PUT | `/api/budgets/{team}/{year}` | upsert a team's yearly budget amount |
| GET | `/api/teams` | list registered teams (a team "exists" once it has a budget row, even with a null amount) |
| POST | `/api/teams` | register a new team (idempotent) |
| DELETE | `/api/teams/{team}` | delete a team - 400 if any campaign still uses it |
| GET | `/api/subteams` | list all `{team, subTeam}` pairs |
| POST | `/api/subteams` | register a new sub team under a team (idempotent) |
| DELETE | `/api/subteams/{team}/{subTeam}` | delete a sub team - 400 if any campaign still uses it |

**No more direct $ entry at the campaign level.** A campaign's `months` field
in every response is a *computed rollup* - it's not stored anywhere and
`CampaignIn` doesn't accept it. $ lives on two INDEPENDENT, ADDITIVE lists:
`assetLines: [{asset, months}]` (money spent creating/owning something
reusable) and `tacticLines: [{tactic, months}]` (money spent running an
action/method, often using an asset but not always - e.g. paid social spend).
The campaign total is `sum(assetLines) + sum(tacticLines)` - different
expense buckets, not two views of the same money, so never merge one asset
with one tactic onto a single line. Each line's `months` is keyed by year
*then* month: `{"2026": {"Jan": {plan, forecast, commit, actual}, ...}, "2027": {...}}`
- a line (and therefore a campaign) can span more than one year.
`extract.py`'s sheet-based path still produces the older, fixed
`{growthMarketing:[...], fieldMarketing:[...]}` shape with bare month names
and a single `tactic` string (the sheet only ever has those two tabs, no
year-awareness, no per-tactic $) - `scripts/app_template.html` and
`backend/seed.py` both accept that shape too, synthesizing empty-$ tactic
lines from the tag (there's no historical per-tactic $ to recover).

## Schema

`campaigns` (one row per campaign) → `campaign_products` (which products it's
tagged with, many-to-many) + `asset_lines` → `asset_line_months` and
`tactic_lines` → `tactic_line_months` (year, month, Plan/Forecast/Commit/
Actual - the only place $ is stored, one pair of tables per line type). See
`api/models.py` for the exact columns. `team_budgets` doubles as the team
registry (year-aware: one row per team per year) and `team_subteams` as the
sub-team registry.

## Data model note

The frontend divides a multi-product campaign's value evenly across its
products only when charting "By Product" (to avoid double-counting) - the
database itself just stores the plain list of products per campaign; that
division logic lives in the dashboard's JS, not here.
