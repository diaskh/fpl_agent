PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS managers (entry_id INTEGER PRIMARY KEY, entry_name TEXT NOT NULL, player_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phases (id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_event INTEGER NOT NULL, stop_event INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gameweeks (id INTEGER PRIMARY KEY, name TEXT NOT NULL, finished INTEGER NOT NULL, data_checked INTEGER NOT NULL, phase_id INTEGER REFERENCES phases(id));
CREATE TABLE IF NOT EXISTS players (id INTEGER PRIMARY KEY, web_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS standings (
 entry_id INTEGER REFERENCES managers(entry_id), gw_id INTEGER REFERENCES gameweeks(id),
 rank INTEGER NOT NULL, last_rank INTEGER, total_points INTEGER NOT NULL, event_points INTEGER NOT NULL,
 PRIMARY KEY(entry_id,gw_id));
CREATE TABLE IF NOT EXISTS picks (
 entry_id INTEGER REFERENCES managers(entry_id), gw_id INTEGER REFERENCES gameweeks(id),
 element_id INTEGER REFERENCES players(id), position INTEGER CHECK(position BETWEEN 1 AND 15),
 is_captain INTEGER NOT NULL, multiplier INTEGER NOT NULL, PRIMARY KEY(entry_id,gw_id,element_id));
CREATE TABLE IF NOT EXISTS autosubs (
 entry_id INTEGER REFERENCES managers(entry_id), gw_id INTEGER REFERENCES gameweeks(id),
 element_in INTEGER NOT NULL, element_out INTEGER NOT NULL,
 PRIMARY KEY(entry_id,gw_id,element_in,element_out));
CREATE TABLE IF NOT EXISTS live_points (element_id INTEGER REFERENCES players(id), gw_id INTEGER REFERENCES gameweeks(id), points INTEGER NOT NULL, PRIMARY KEY(element_id,gw_id));
CREATE TABLE IF NOT EXISTS sync_runs (gw_id INTEGER PRIMARY KEY, synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
 gw_id INTEGER PRIMARY KEY, messages TEXT NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count BETWEEN 0 AND 2),
 completed_at TEXT);
CREATE TABLE IF NOT EXISTS fixture_reports (
 gw_id INTEGER NOT NULL REFERENCES gameweeks(id), kickoff_time TEXT NOT NULL,
 messages TEXT NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count BETWEEN 0 AND 2),
 completed_at TEXT, PRIMARY KEY(gw_id,kickoff_time));
CREATE INDEX IF NOT EXISTS standings_gw ON standings(gw_id);
CREATE INDEX IF NOT EXISTS picks_gw ON picks(gw_id);

-- Bootstrap entity columns are added transactionally from the API by bootstrap.py.
CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS chips (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS player_positions (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS stat_definitions (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS game_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS game_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bootstrap_extra (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bootstrap_summary (id INTEGER PRIMARY KEY CHECK(id=1), total_players INTEGER NOT NULL, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS result_status (gw_id INTEGER PRIMARY KEY, provisional INTEGER NOT NULL CHECK(provisional IN (0,1)));

-- Latest official Premier League fixture state. Scores are nullable before kickoff.
CREATE TABLE IF NOT EXISTS fixtures (
 id INTEGER PRIMARY KEY, event INTEGER REFERENCES gameweeks(id), kickoff_time TEXT,
 team_h INTEGER NOT NULL REFERENCES teams(id), team_a INTEGER NOT NULL REFERENCES teams(id),
 team_h_score INTEGER, team_a_score INTEGER, started INTEGER NOT NULL,
 finished INTEGER NOT NULL, finished_provisional INTEGER NOT NULL, minutes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS fixtures_event ON fixtures(event);

-- Durable post-deadline messages; independent of points reports.
CREATE TABLE IF NOT EXISTS deadline_reports (
 gw_id INTEGER PRIMARY KEY, messages TEXT NOT NULL,
 sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count>=0));

-- Immutable cup draws, completed scores and resumable publication snapshots.
CREATE TABLE IF NOT EXISTS cups (
 id INTEGER PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cup_posts (
 cup_id INTEGER NOT NULL REFERENCES cups(id), stage INTEGER NOT NULL,
 state TEXT NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0,
 uncertain INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(cup_id,stage));
