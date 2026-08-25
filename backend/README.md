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
| GET | `/api/budgets` | list all team budgets (any team, any year, any quarter) |
| PUT | `/api/budgets/{team}/{year}/{quarter}` | upsert a team's quarterly budget amount (Q1–Q4) |
| POST | `/api/budgets/bulk` | upsert/clear many quarterly budget rows in one transaction; unknown teams are registered automatically |
| GET | `/api/teams` | list registered teams (a team "exists" once it has a row in `teams`) |
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

Five tables (see `api/models.py` for exact columns) - normalized, no JSON
blobs anywhere:

- `campaigns` - one row per campaign. `products` (the Python/API attribute)
  maps to the physical column `product_tags`, a plain Postgres array of
  strings - not a join table, not JSON, just a flat inspectable list
  (`{EOR,AOR}` in any SQL client). The `products` column itself is a dead
  leftover from an earlier, reverted JSONB attempt - left in place unused
  rather than dropped, which is why the live column is named `product_tags`.
- `spend_lines` - one row per asset OR tactic spend line (`line_type` is
  `'asset'`/`'tactic'`, CHECK-constrained), FK'd to its campaign with
  `ON DELETE CASCADE`. Asset and tactic lines are independent, additive
  expense buckets - a campaign's total is sum(asset lines) + sum(tactic
  lines), not one or the other; `line_type` is just how they're told apart.
- `spend_line_months` - one row per `(spend_line_id, year, month)`, real
  numeric `plan`/`forecast`/`commit`/`actual` columns - the only place $ is
  stored, never a blob.
- `teams` - the team registry (`name` is the PK - a team exists iff it has a
  row here). `sub_teams` is a plain Postgres array of sub-team names, same
  reasoning as `campaigns.products` above - not a join table, not JSON.
- `team_budgets` - `(team, year, quarter, amount)`, PK on `(team, year,
  quarter)`, `team` FK's to `teams.name`. Quarterly $ allocated by Marketing
  Ops, set before Plan exists.

**History**: this schema went through two prior shapes. First, an 8-table
fully-normalized design (`campaign_products`, `asset_lines` +
`asset_line_months`, `tactic_lines` + `tactic_line_months`, `team_budgets`,
`team_subteams`) - correct, but more tables than needed. Then a 3-table JSONB
consolidation (`campaigns` + `products` JSONB column, `campaign_lines`,
`teams` with JSONB `budgets`/`sub_teams`) - fewer tables, but the JSONB blobs
made the data opaque to inspect directly (e.g. in DBeaver), and a leftover
`Legacy*` model/`create_all()` interaction was silently recreating empty
duplicate tables under the original names on every API restart. That was
reverted back to the 8-table design, then consolidated again into the
current 5-table shape - discriminator columns (`line_type`) and native
Postgres arrays (`product_tags`, `sub_teams`) instead of JSON, so every value
stays a real, individually inspectable column. The two now-superseded sets
of tables from the earlier attempts (`legacy2_asset_lines` etc. from the
8-table era, `deprecated_campaign_lines`/`deprecated_teams` from the JSONB
era) are kept as reference buffers rather than dropped - safe to remove once
confirmed unneeded. The API's wire format
(`months`/`products`/`assetLines`/`tacticLines` JSON shapes) hasn't changed
across any of this, so the frontend has never needed to change either.

## Data model note

The frontend divides a multi-product campaign's value evenly across its
products only when charting "By Product" (to avoid double-counting) - the
database itself just stores the plain list of products per campaign; that
division logic lives in the dashboard's JS, not here.
