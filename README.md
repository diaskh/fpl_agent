# Elimai League FPL Telegram bot

Python 3.10+ bot for classic league **836115**, configured from your [league link](https://fantasy.premierleague.com/en/leagues/836115/standings/c). Sends a Russian gameweek recap and Russian cumulative report to one Telegram group.

## Quick start

From this directory, on Linux or macOS:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`: bot token from BotFather, the target group's numeric chat ID, your numeric Telegram user ID, and an OpenAI API key. `FPL_LEAGUE_ID=836115` is already set. Add the bot to the group with permission to send messages. Keep `.env` private (`chmod 600 .env`).

```sh
.venv/bin/python bot.py backfill
.venv/bin/python bot.py preview 2
.venv/bin/python bot.py run
```

`backfill` immediately syncs GW1 and GW2 without sending messages or calling OpenAI. `preview N` syncs all missing gameweeks through N and prints exact JSON, also without broadcasting or using OpenAI. These commands need only `FPL_LEAGUE_ID`; export it or use `.env`.

`run` starts Telegram long polling and an immediate FPL check, then repeats every 15 minutes. Fixtures are grouped by their exact kickoff time. The bot sends one cumulative live report only after every match in a kickoff group is marked finished; simultaneous matches produce one report, never one report per match. Each report refreshes picks, player points, calculated manager scores and ranks. On first startup, already-finished kickoff groups are recorded as a baseline and are not replayed into the chat. A sync failure leaves the group unprocessed and retries at the next poll. No need to expose an inbound port.

Only `ADMIN_TELEGRAM_ID` can use `/report` or `/report 2`. Other users are silently ignored. An admin report deliberately rebroadcasts a previously completed report; a partially delivered report resumes its saved messages. Reports always go to `TELEGRAM_CHAT_ID`, even when the command is issued in a private chat. The default is the current gameweek after its deadline, falling back to the latest completed gameweek. Live reports refresh the database first and show provisional results with a UTC timestamp. Future gameweeks are rejected. Live reports do not mark the final automatic report delivered.

## Questions in the group

Every human member of the configured Telegram group can ask `/ask Who scored most in GW2?`
(or `/ask@YourBotUsername ...`). Members can also mention `@YourBotUsername` or reply to a
bot message with a question. Ordinary conversation is ignored; other chats cannot query
this league. `/report` remains admin-only. Explicit `/ask@YourBotUsername` is the most
reliable group trigger with Telegram privacy mode enabled; mentions depend on Telegram
actually delivering the message to the bot. No privacy-setting change is required for
commands. Replies include the directly replied-to bot message as context; there is no
persistent conversation memory or automatic Telegram-to-FPL identity mapping.

Every group member can request fact-based PNGs with `/graph` or `/graph latest` for the
latest-gameweek score chart, and `/graph history` for the season-long rank race. Each
command sends two images: the current top 10 first, then the complete mini-league. The
command refreshes FPL before drawing, marks live data as provisional, includes the exact
UTC sync time, and uses only calculated database values (no model-generated numbers or
claims).

`/race` refreshes the latest data and sends an MP4 bar-chart race for every manager in
the mini-league, covering GW1 through the latest available gameweek and animating
cumulative total points. It uses `bar-chart-race==0.1.0` with a wide pandas DataFrame as
required by the package. A manager without earlier verified league snapshots remains
absent until their first stored gameweek; the bot never substitutes unknown scores with
zero. MP4 output requires FFmpeg to be installed and visible on PATH.
Race timing adapts to the number of stored gameweeks, targets roughly fifteen seconds,
renders approximately 30 frames per second, and briefly pauses on each gameweek.
Each request sends the current question, at most 2,500 characters of the directly
replied-to bot message, the database schema and synced-week coverage. Query results
are retained only for that request's query/answer loop; other group messages and prior
requests are not included. All reports, generated answers, local service messages and source footers use Russian,
regardless of the question language. Proper names remain unchanged.

Examples:

- `/ask Кто набрал больше всего очков во втором туре?`
- `/ask Compare Team One and Team Two across the stored gameweeks.`
- `/ask Who had the most captain points in GW2?`
- `/ask Менің командамның нәтижесі қандай? Entry ID: 12345`

The existing OpenAI client/model plans up to four SQLite queries and writes a concise
answer in Russian. FPL data questions must execute a successful query
before a database answer is returned. General questions are also accepted and can be
answered without querying FPL data or displaying an FPL source footer. The model selects
the answer basis, clarifies ambiguous managers, and acknowledges unavailable facts.
Answer interpretation is model-based and is not a mathematical guarantee against
misclassification or hallucination. Actual query calculations run in SQLite. Answers prefer team, manager and player names;
ID-bearing query results are enriched with available names before reaching the model.
IDs are reserved for missing names, explicit requests or disambiguation. Every database answer
includes stored gameweek coverage, the latest historical-results sync timestamp and the
separate bootstrap refresh timestamp (UTC). Neither claims that every historical week
was refreshed then.

Questions read a consistent snapshot of the existing database; they do not refresh FPL or
send reports. Invalidated gameweeks are excluded. Historical results are available only for synced gameweeks. Latest bootstrap player prices,
news/availability, positions, season statistics (including defensive contribution when supplied),
club information, future deadlines, official fixture scores and API game settings are available.
Manager transfer history, historical prices and the full rules document are not stored.
Current season totals must not be presented as individual gameweek statistics. Historical
strategy remains opinion, not a forecast.

SQL runs against an isolated in-memory snapshot with a read-only setting and an authorizer
that allows only approved FPL tables/views and SQL functions. Operational metadata and
saved reports are removed. Writes, filesystem access, extensions and schema inspection
are denied. Queries have a two-second execution budget, a 100-row result limit and a
bounded payload; oversized results are rejected rather than silently truncated. Errors can
be corrected within the query budget; exhausted budgets/API failures return an inability
to verify an answer. One question runs at a time, with a 10-second per-user cooldown and
a 1,500-character question limit. Replies are plain text and are sent as one, two, or at
most three Telegram messages according to length, split at natural boundaries.

Questions, direct bot reply context, and relevant public FPL query results are sent to
OpenAI. Secrets and unrelated chat history are not included. Restart the bot process to
load this feature. No database migration or additional environment variables are needed.

Verification for this feature: 32 local tests pass, including the original 17 tests and
15 question tests covering retrieval, invalidated rows, general question routing, read-only
SQL, result limits, query correction, group access, rate limits, triggers, quota/rate errors and API failures.
OpenAI and Telegram calls are mocked; live model classification and group delivery have
not been exercised by these tests. JSON response handling follows the
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## Complete bootstrap storage (latest only)

Every successful scheduled `bootstrap-static/` fetch replaces the latest bootstrap data
in a single SQLite transaction. No extra HTTP request is needed. CLI previews/backfills
and admin reports also refresh it. `/ask` reads stored data without refreshing it.
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

Questions can query the new tables and selected JSON functions through the existing
read-only SQL boundary. Bootstrap freshness is separate from completed-gameweek coverage.
Prices (`now_cost`) are in tenths of a million, and `players.element_type` joins
`player_positions.id`; `players.team` joins `teams.id`.

## Data and calculation decisions

- All standings pages are fetched. Requests use `User-Agent: Mozilla/5.0`, bounded retries, and sequential manager fetches. A complete gameweek is committed in one SQLite transaction; resync replaces picks/autosubs/live data without accumulating duplicate rows.
- Historical points come from `/entry/{id}/history/`, because league standings are a current snapshot and cannot be stored as historical scores. Live/current ranks and previous ranks are taken from league standings when their event matches. Older ranks are reconstructed using total points and fewer cumulative transfers (excluding wildcard/free-hit weeks), with equal keys sharing a rank. Historical reconstruction uses **current league membership**; it cannot recover departed managers or past joining dates. Consequently historical rank movement can differ from the table users saw at that time.
- This implementation supports classic leagues starting at GW1. It rejects leagues starting later. Keep one database per league per season; an identity guard refuses to mix seasons or leagues. Archive the database and start a new one next season.
- Monthly phases exclude `Overall` (phase 1). Month-to-date sums stop at the requested gameweek, even if later weeks are already stored. Every earlier gameweek must be synced before cumulative calculations run.
- Event points use FPL history's `points`, without subtracting transfer costs again. Total standings points use `total_points`. Month totals follow the requested sum of event points, which can differ from FPL's official monthly scoring treatment of transfer costs.
- Bench points count only unused points: positions 12–15 with `multiplier=0`, excluding autosub `element_in` for that same manager and GW. Negative points are retained. Bench Boost players are excluded. This applies to gameweek and season reports and the `/ask` bench_scores view.
- Captain points use the actual multiplier greater than one, including acting vice captains and triple captain. The calculation is the full multiplied score, not just the extra captain bonus.
- Tied awards use entry ID ascending as a stable tiebreak. Top lists contain at most five entries; single-winner awards return one. No climb/drop is shown when no manager moved in that direction, and GW1 has no prior ranking.

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
freshness. `/ask` can query these same persisted rows and labels provisional coverage.
Automatic publication remains limited to completed, checked gameweeks.

After stopping the running bot, run `python bot.py sync` to backfill through the current
available gameweek without sending Telegram messages, then restart `python bot.py run`.
Existing databases migrate automatically. `python bot.py preview 4` prints calculated
GW4 data without broadcasting. The running process must restart to load code changes.
