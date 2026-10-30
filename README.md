# Elimai League FPL Telegram bot

Python 3.10+ bot for classic league **836115**, configured from your [league link](https://fantasy.premierleague.com/en/leagues/836115/standings/c). Sends a Russian gameweek recap and Russian cumulative report to one Telegram group.

## Quick start

From this directory, on Linux or macOS:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.local.example .env.local
```

Fill in `.env.local` for your local test bot: bot token from BotFather, the target group's numeric chat ID, your numeric Telegram user ID, and an OpenAI API key. `FPL_LEAGUE_ID=836115` is already set. Add the bot to the group with permission to send messages. Keep credentials private (`chmod 600 .env.local`). Local runs default to `.env.local` and `fpl_local.db`; they never load the VPS `.env`. The systemd service sets `FPL_ENV=production` and uses the existing `.env` and production database. Exported environment variables override the selected file. After updating the service unit, run `sudo systemctl daemon-reload` and restart the service. For production CLI calls, prefix the command with `FPL_ENV=production`.

```sh
.venv/bin/python bot.py backfill
.venv/bin/python bot.py preview 2
.venv/bin/python bot.py run
```

`backfill` immediately syncs GW1 and GW2 without sending messages or calling OpenAI. `preview N` syncs all missing gameweeks through N and prints exact JSON, also without broadcasting or using OpenAI. These commands need only `FPL_LEAGUE_ID`; export it or use `.env.local` (or `.env` with `FPL_ENV=production`).

`run` starts Telegram long polling and an immediate FPL check, then repeats every 15 minutes. Fixtures are grouped by their exact kickoff time. The bot sends one cumulative live report at the first poll at least two hours after the kickoff group starts, regardless of FPL finished flags; simultaneous matches produce one report, never one report per match. Each report refreshes picks, player points, calculated manager scores and ranks. On first startup, kickoff groups already at least two hours old are recorded as a baseline and are not replayed into the chat. A sync failure leaves the group unprocessed and retries at the next poll. No need to expose an inbound port.

Only `ADMIN_TELEGRAM_ID` can use `/report` or `/report 2`. Other users are silently ignored. An admin report deliberately rebroadcasts a previously completed report; a partially delivered report resumes its saved messages. Reports always go to `TELEGRAM_CHAT_ID`, even when the command is issued in a private chat. The default is the current gameweek after its deadline, falling back to the latest completed gameweek. Live reports refresh the database first and show provisional results with a UTC timestamp. Future gameweeks are rejected. Live reports do not mark the final automatic report delivered.

## Charts in the group

All chart and cup renderers share the `/graph form` editorial style: warm ivory,
Oswald headlines, Inter labels, a restrained teal/blue/ochre/plum palette, subtle
rules and soft tinted regions or cards. History charts use numbered endpoints
and a separate team key. The race video uses the same bundled fonts and colours.
Styling does not change score calculations, cup progression or chart commands.

Every group member can use `/graph` (or `/graph density`) to request a Russian
league-density PNG. It uses every team's exact cumulative `total` from freshly fetched
**Overall standings (phase 1)** across all pages, never gameweek points, monthly totals,
or global FPL ranks. `/graph latest` is an alias for density. One dot represents one
team; equal totals keep identical horizontal positions. No individual team is highlighted.
The chart uses the existing Pillow renderer and Telegram photo delivery pathway.

Groups follow a deterministic gap heuristic: the longest run of distinct totals separated
by no more than the rounded-up median positive gap forms the core; the lower tail stays together,
and the upper tail splits at its largest above-median gap. Without a core of at least
three distinct totals, only gaps greater than twice the median become boundaries
(up to four groups). Group descriptions are computed from actual membership.

`/graph form` sends a Russian four-quadrant scatter plot using database standings
refreshed through the latest available gameweek. X is cumulative season points minus
the current teams' mean; Y is the sum of event points over the last three gameweeks
minus the same cohort's mean. Event points are used as stored, without subtracting
transfer costs again. Both axes cross at zero and show negative and positive values.
Every team in the current snapshot appears, numbered in season-points order beside
a team key. Actual dots retain exact coordinates; only number callouts move to avoid
crowding. The renderer uses the shared Pillow fonts and editorial palette, with the
sync timestamp and provisional status (if any recent gameweek is provisional).
At least three gameweeks and complete recent history for every team are required;
missing scores are never replaced with zero.

`/graph history` retains the two season-long rank charts (top 10 and all managers).

`/race` refreshes the latest data and sends an MP4 bar-chart race for every manager in
the mini-league, covering GW1 through the latest available gameweek and animating
cumulative total points. It uses `bar-chart-race==0.1.0` with a wide pandas DataFrame as
required by the package. A manager without earlier verified league snapshots remains
absent until their first stored gameweek; the bot never substitutes unknown scores with
zero. MP4 output requires FFmpeg to be installed and visible on PATH.
Race timing adapts to the number of stored gameweeks, targets roughly fifteen seconds,
renders approximately 30 frames per second, and briefly pauses on each gameweek.
All reports and source footers use Russian. Proper names remain unchanged.

## Complete bootstrap storage (latest only)

Every successful scheduled `bootstrap-static/` fetch replaces the latest bootstrap data
in a single SQLite transaction. No extra HTTP request is needed. CLI previews/backfills
and admin reports also refresh it.
Existing databases migrate automatically on the next refresh; restart the bot to load
this code. Migration adds tables and missing columns without replacing historical results.

| API section | Table |
|---|---|
| elements | players |
| events | gameweeks |
| phases | phases |
| teams | teams |
| chips | chips |
| element_types | player_positions |
| element_stats | stat_definitions |
| game_settings | game_settings |
| game_config | game_config |
| total_players / refresh time | bootstrap_summary |
| Future unrecognized top-level sections | bootstrap_extra |

Entity attributes are individual columns, added automatically when new API fields appear.
Nested objects/arrays are JSON strings in their respective columns. New columns use SQLite
no-affinity storage to preserve API scalar types (numeric strings remain strings; use
`CAST(... AS REAL)` for numeric ordering). Boolean values are stored as 0/1. Settings,
configuration and unknown sections use key/value rows with JSON-encoded values, including
scalar values; use `json_extract(value, '$')` to decode them. No full-response blob is stored.
`bootstrap_summary` contains one row with `total_players` and `fetched_at` in UTC.

Entity rows supplied by the latest payload have `bootstrap_current=1`. Removed players,
gameweeks and phases retain only the identity/core fields required by historical references,
with `bootstrap_current=0` and additional attributes cleared; other entity tables replace
all rows. Always filter `bootstrap_current=1` for current bootstrap queries. Removed
attributes become NULL rather than retaining stale values. Old versions are not archived.
Malformed/incomplete sections, duplicate keys, identity mismatch or database errors leave
the previous bootstrap transaction intact. Historical gameweek transactions remain separate.

Bootstrap freshness is separate from completed-gameweek coverage.
Prices (`now_cost`) are in tenths of a million, and `players.element_type` joins
`player_positions.id`; `players.team` joins `teams.id`.

## Data and calculation decisions

- All standings pages are fetched. Requests use `User-Agent: Mozilla/5.0`, bounded retries, and sequential manager fetches. A complete gameweek is committed in one SQLite transaction; resync replaces picks/autosubs/live data without accumulating duplicate rows.
- Historical points come from `/entry/{id}/history/`, because league standings are a current snapshot and cannot be stored as historical scores. Live/current ranks and previous ranks are taken from league standings when their event matches. Older ranks are reconstructed using total points and fewer cumulative transfers (excluding wildcard/free-hit weeks), with equal keys sharing a rank. Historical reconstruction uses **current league membership**; it cannot recover departed managers or past joining dates. Consequently historical rank movement can differ from the table users saw at that time.
- This implementation supports classic leagues starting at GW1. It rejects leagues starting later. Keep one database per league per season; an identity guard refuses to mix seasons or leagues. Archive the database and start a new one next season.
- Monthly phases exclude `Overall` (phase 1). Month-to-date sums stop at the requested gameweek, even if later weeks are already stored. Every earlier gameweek must be synced before cumulative calculations run.
- Event points use FPL history's `points`, without subtracting transfer costs again. Total standings points use `total_points`. Month totals follow the requested sum of event points, which can differ from FPL's official monthly scoring treatment of transfer costs.
- Bench points count only unused points: positions 12–15 with `multiplier=0`, excluding autosub `element_in` for that same manager and GW. Negative points are retained. Bench Boost players are excluded. This applies to gameweek and season reports.
- Captain points use the actual multiplier greater than one, including acting vice captains and triple captain. The calculation is the full multiplied score, not just the extra captain bonus.
- Tied awards use entry ID ascending as a stable tiebreak. Top lists contain at most five entries; cumulative month, season bench, and season captain sections show the top three managers (or fewer when fewer are available). No climb/drop is shown when no manager moved in that direction, and GW1 has no prior ranking.

## Formatting and delivery

The Russian-only system prompt is defined in `formatting.py`. Exact compact JSON goes to `gpt-4o-mini` using asynchronous Chat Completions. The user payload adds an immutable-block contract: the model returns exactly two strings containing block tokens and optional short commentary. Code validates all tokens, forbids extra digits, inserts the deterministic names/scores, and escapes all Telegram MarkdownV2 reserved characters. This prevents the model from modifying score/name associations. API failures or malformed output fall back to deterministic Russian templates. Message size is checked before sending.

`reports` stores the two final messages and their delivery count separately from `sync_runs`. A two-second delay separates the messages. A completed automatic report is not sent again. A process lock prevents multiple bot or CLI instances from using the same database.

Telegram offers no idempotency key for message sends. After a timeout or crash between a send and its database acknowledgement, the bot conservatively blocks the uncertain report rather than risk duplicating it. Inspect the group, stop the service, inspect pending state, and resolve with the number of messages **actually visible**:

```sh
sudo systemctl stop fpl_bot
cd /opt/fpl-bot
sudo -u fplbot env DATABASE_PATH=/var/lib/fpl-bot/fpl_data.db .venv/bin/python bot.py status
# Example: only the gameweek recap is visible for GW3:
sudo -u fplbot env DATABASE_PATH=/var/lib/fpl-bot/fpl_data.db .venv/bin/python bot.py resolve 3 1
sudo systemctl start fpl_bot
```

Definitive Telegram rejections (bad request, forbidden, rate limit) can retry automatically at the next poll. Fix group permissions or configuration when needed. Reports waiting for delivery keep their original saved text so both halves remain consistent.

## VPS / systemd

Copy this directory to `/opt/fpl-bot` on a VPS with Python 3.10+ and the venv package installed. Run as a dedicated service user:

```sh
sudo useradd --system --home-dir /opt/fpl-bot --shell /usr/sbin/nologin fplbot
sudo python3 -m venv /opt/fpl-bot/.venv
sudo /opt/fpl-bot/.venv/bin/pip install -r /opt/fpl-bot/requirements.txt
sudo cp /opt/fpl-bot/.env.example /opt/fpl-bot/.env
sudoedit /opt/fpl-bot/.env
sudo chown root:fplbot /opt/fpl-bot/.env
sudo chmod 640 /opt/fpl-bot/.env
sudo install -d -o fplbot -g fplbot -m 700 /var/lib/fpl-bot
cd /opt/fpl-bot
sudo -u fplbot env DATABASE_PATH=/var/lib/fpl-bot/fpl_data.db .venv/bin/python bot.py backfill
sudo cp fpl_bot.service /etc/systemd/system/fpl_bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now fpl_bot.service
sudo journalctl -u fpl_bot.service -f
```

Leave `DATABASE_PATH` commented out in `.env` for VPS deployment (systemd EnvironmentFile values override service Environment values). The service sets `DATABASE_PATH=/var/lib/fpl-bot/fpl_data.db`, creates its state directory, restarts after failure, and starts on reboot. CLI calls must use the same database path, and the service must be stopped before CLI operations. Back up the SQLite database using SQLite's backup API or stop the service before copying the database and its WAL files. The service requires outgoing HTTPS access to FPL, Telegram and OpenAI.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Local verification: all 17 tests passed, dependency compatibility checks passed, and a public-API GW1/GW2 backfill for league 836115 completed with 600 picks (20 participating managers per week). The league currently has 21 managers; one has no entry history in those weeks. SQLite integrity passed. Telegram delivery and OpenAI generation were tested with mocks; real credentials and VPS deployment were not available.

Tests use temporary databases and mocked remote services. They cover exact calculations, autosubs, negative bench points, captain multipliers, monthly cutoffs, missing history, atomic snapshot failures, resync, pagination/retries, strict formatter fallback, authorization, delivery resumption, and ambiguous sends. No real Telegram messages are sent by tests.

`requirements.txt` specifies supported major-version ranges; `requirements.lock.txt` records the exact dependency set used for local verification. To reproduce it, use `pip install -r requirements.lock.txt`.

Implementation references: [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create), [GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini), [python-telegram-bot lifecycle](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html), [APScheduler 3 AsyncIOScheduler](https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html), and the [public FPL bootstrap endpoint](https://fantasy.premierleague.com/api/bootstrap-static/). FPL's public API has no versioned compatibility guarantee; schema changes or access restrictions can require maintenance.

Weekly reports show the top 10 scorers and all managers tied for best captain, biggest climb, or biggest drop. Bench rankings remain top 5.

### Live gameweeks

`/report` defaults to the current available gameweek; `/report 4` selects GW4 explicitly.
Both refresh data before sending. Live scores use the latest player points multiplied
by the API pick multipliers, minus transfer costs. Cumulative totals add this net score
to the previous history total. Bonus points, autosubs and captain substitutions are not
predicted; totals and ranks can change until FPL checks the gameweek.
`result_status.provisional=1` identifies live snapshots; `sync_runs.synced_at` records
freshness.
Automatic publication runs two hours after each kickoff group starts, including during live gameweeks. With the 15-minute polling interval, delivery normally occurs between two hours and two hours fifteen minutes after kickoff. Scores remain provisional until FPL checks the gameweek.

After stopping the running bot, run `python bot.py sync` to backfill through the current
available gameweek without sending Telegram messages, then restart `python bot.py run`.
Existing databases migrate automatically. `python bot.py preview 4` prints calculated
GW4 data without broadcasting. The running process must restart to load code changes.

The second report message lists the top three managers for month-to-date points, season unused bench points, and season captain points, ordered by points descending and entry ID ascending for ties.

### Post-deadline transfer reports

The existing 15-minute poll sends a Russian transfer/chip report for every current
league member once locked picks and transfer histories become available. Includes
outgoing/incoming players, active chip (or none), points hit, and calculated free
transfers available for the next GW immediately after the deadline. Subsequent
private transfers are not visible. Free Hit moves are shown as squad differences
against the previous permanent squad, because public transfer history omits them.
Balances use public event transfer counts, a maximum bank of five (or the API's
`max_extra_free_transfers` plus one), and preserve the bank on Wildcard/Free Hit weeks without adding
an allowance. Missing history is labelled unavailable. Exceptional season-specific
transfer top-ups are not reconstructed by this calculation.

On first startup, past deadlines become a baseline and are not broadcast; reports
start with the next deadline. Later downtime is caught up on restart. Data-fetch
failures retry on the next poll before saving a report. `deadline_reports` is an
additive table: existing database records remain intact. Messages are saved before
sending, with confirmed progress per message. A timeout leaves a
`sending-deadline:GW` metadata marker and blocks automatic retries for that report.
After inspecting the chat, an operator must update `deadline_reports.sent_count`
to the number visibly delivered and delete that specific marker to resume. The
existing `resolve` CLI applies only to points reports.

Preview the deadline report using live endpoints, without Telegram or database access
(can run while the bot is running):

```sh
.venv/bin/python bot.py deadline-preview     # latest completed gameweek
.venv/bin/python bot.py deadline-preview 7   # specific gameweek
```

The preview prints the official UTC deadline and the exact generated messages.
It does not simulate automatic scheduling or modify deadline delivery markers.

Admin-only `/transfers 7` sends the real deadline transfer/chip report for GW7 to
`TELEGRAM_CHAT_ID`; `/transfers` selects the latest completed gameweek. Future
deadlines are rejected. This manual command does not change automatic delivery
records and can be requested again. Restart the bot after updating the code.

### Mini-league cup

After restarting the updated bot, the admin can run `/cup start` to draw a cup
starting at the next future deadline, or `/cup start 8` to select a future GW.
Every current Overall league member participates (2–32 supported). The draw,
participants, random byes, and a separate random priority for tied scores are
saved permanently. An active cup cannot be redrawn. A new cup may be created
once the previous final has been settled.

One gameweek per round. With 21 entrants there are five preliminary matches,
eleven byes, then a round of 16, quarter-finals, semi-finals, and final: five GWs.
The opening post includes rules, the tie priority, a preliminary image identifying
each winner's next opponent, and the main bracket. All graphics and messages are
Russian; no buttons are shown. `/cup` in the configured group sends both current
images (or just the main bracket when there was no preliminary round).

The existing 15-minute poll settles each round only when that GW is both
`finished` and `data_checked` in FPL. Cup scores are official entry-history
`points - event_transfers_cost`; chips, captaincy and autosubs are already included
in official points. Equal scores use the published random priority, with a note
in the results and graphic. These are this bot's cup rules, not an implementation
of the official FPL Cup tie-break rules.

The bot automatically posts round results and the updated main bracket; the
preliminary results also include the updated preliminary image. Completed
opponents and both scores remain in the saved bracket. If the bot was offline,
it catches up in round order. Missing participant scores block settlement;
leaving the league does not replace a cup entrant. Final results are frozen once
settled. Every cup must fit within the available season gameweeks.

`cups` and `cup_posts` are additive SQLite tables. Posts save a snapshot plus
confirmed delivery progress before sending. Definitive Telegram rejections retry;
ambiguous timeouts block subsequent automatic cup publications until inspected.
Stop the bot and use the same environment/database as the service:

```sh
python bot.py cup-status
# CUP_ID, STAGE (-1 for draw, 0 for first round), ITEMS_ACTUALLY_VISIBLE:
python bot.py cup-resolve 1 -1 2
```

The visible count includes each text message and each photo separately. It must
be the confirmed count or that count plus one. Restart the bot to resume. `/cup`
can still display the saved current bracket without changing automatic delivery
records. Creating and showing a cup sends to `TELEGRAM_CHAT_ID`.

### Pillow visual style

`visuals.py` owns the shared sports editorial palette and typography for gameweek
charts, table density and cup graphics: warm ivory, charcoal, sports red, condensed
uppercase Oswald headlines, and Inter body text with separate regular/bold weights.
Both variable fonts include Cyrillic and are bundled under `assets/fonts/` with
their SIL Open Font Licenses. Include that directory when deploying; image rendering
requires no font downloads or system fonts. Subtitles use measured character
tracking, and team names fit their available space before being ellipsized. Cup
winners use bold names and a red marker; scores remain right-aligned in reserved
space. Rendering changes do not alter the stored cup draw, results or graph data.
