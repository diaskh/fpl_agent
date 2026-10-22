"""FPL ingestion and deterministic metrics. One database per league and season."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import httpx

from bootstrap import save as save_bootstrap


class FPL:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url="https://fantasy.premierleague.com/api/",
                                       headers={"User-Agent": "Mozilla/5.0"}, timeout=40)

    async def get(self, path, **params):
        for attempt in range(4):
            try:
                response = await self.client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    if exc.response.status_code != 429 and exc.response.status_code < 500:
                        raise
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def members(self, league_id):
        result = {}
        for page in range(1, 10001):
            payload = await self.get(f"leagues-classic/{league_id}/standings/", page_standings=page, phase=1)
            if payload["league"].get("start_event", 1) != 1:
                raise ValueError("Only classic leagues starting at GW1 are supported")
            standings = payload["standings"]
            before = len(result)
            for row in standings["results"]:
                result[row["entry"]] = row
            if not standings["has_next"]:
                if not result:
                    raise ValueError("League has no active managers")
                return list(result.values())
            if len(result) == before:
                raise ValueError("Standings pagination made no progress")
        raise ValueError("Standings pagination limit exceeded")


async def connect(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(Path(__file__).with_name("schema.sql").read_text())
    return db


async def rows(db, sql, params=()):
    async with db.execute(sql, params) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


def latest_completed(bootstrap):
    return max((e["id"] for e in bootstrap["events"] if e["finished"] and e["data_checked"]), default=None)


def latest_available(bootstrap):
    now = datetime.now(timezone.utc)
    return max((e["id"] for e in bootstrap["events"]
                if (e["finished"] and e["data_checked"]) or
                (e.get("is_current") and datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) <= now)), default=None)


def due_fixture_groups(fixtures, now=None):
    """Return unique kickoff groups at least two hours old, regardless of status."""
    if now is None:
        now = datetime.now(timezone.utc)
    kickoffs = {f["kickoff_time"] for f in fixtures if f.get("kickoff_time")}
    return [kickoff for kickoff in sorted(kickoffs)
            if datetime.fromisoformat(kickoff.replace("Z", "+00:00")) + timedelta(hours=2) <= now]


def month_phase(phases, gw):
    # Overall (id=1) overlaps every monthly phase; never select it as a month.
    matches = [p for p in phases if p["id"] != 1 and p["start_event"] <= gw <= p["stop_event"]]
    if len(matches) != 1:
        raise ValueError(f"Expected one monthly phase for GW{gw}, found {len(matches)}")
    return matches[0]


def historical_ranks(histories, gw):
    """Reconstruct ranks by points, then fewer non-chip transfers; ties share rank."""
    totals = {entry: next(h["total_points"] for h in history if h["event"] == gw)
              for entry, history in histories.items() if any(h["event"] == gw for h in history)}
    transfers = {entry: sum(h.get("rank_transfers", h.get("event_transfers", 0)) for h in histories[entry] if h["event"] <= gw) for entry in totals}
    keys = {entry: (-totals[entry], transfers[entry]) for entry in totals}
    ordered = sorted(totals, key=lambda entry: (keys[entry], entry))
    rank, previous = 0, None
    result = {}
    for index, entry in enumerate(ordered, 1):
        if keys[entry] != previous:
            rank = index
        result[entry] = rank
        previous = keys[entry]
    return result


class Store:
    def __init__(self, db, fpl, league_id):
        self.db, self.fpl, self.league_id = db, fpl, league_id

    async def sync_bootstrap(self, bootstrap):
        await save_bootstrap(self.db, bootstrap, self.league_id)

    async def sync_fixtures(self, fixtures):
        """Replace the fixture snapshot with validated official data."""
        fields = ('id', 'event', 'kickoff_time', 'team_h', 'team_a', 'team_h_score',
                  'team_a_score', 'started', 'finished', 'finished_provisional', 'minutes')
        if not isinstance(fixtures, list):
            raise ValueError('Fixtures must be a list')
        ids = []
        values = []
        for fixture in fixtures:
            if not isinstance(fixture, dict) or type(fixture.get('id')) is not int:
                raise ValueError('Invalid fixture')
            if type(fixture.get('team_h')) is not int or type(fixture.get('team_a')) is not int:
                raise ValueError('Invalid fixture teams')
            ids.append(fixture['id'])
            values.append(tuple(int(fixture.get(field, False)) if field in
                                ('started', 'finished', 'finished_provisional') else
                                (fixture.get(field, 0) if field == 'minutes' else fixture.get(field))
                                for field in fields))
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate fixture IDs')
        try:
            await self.db.execute('BEGIN')
            await self.db.execute('DELETE FROM fixtures')
            await self.db.executemany(
                'INSERT INTO fixtures(id,event,kickoff_time,team_h,team_a,team_h_score,'
                'team_a_score,started,finished,finished_provisional,minutes) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)', values)
            await self.db.execute("INSERT INTO metadata(key,value) VALUES ('fixtures_synced_at',CURRENT_TIMESTAMP) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise

    async def prepare(self, bootstrap):
        await self.sync_bootstrap(bootstrap)
        members = await self.fpl.members(self.league_id)
        histories = {}
        # Sequential requests avoid flooding FPL; every response must succeed.
        for member in members:
            entry = member["entry"]
            payload = await self.fpl.get(f"entry/{entry}/history/")
            exempt = {c["event"] for c in payload.get("chips", []) if c["name"] in ("wildcard", "freehit")}
            histories[entry] = [dict(h, rank_transfers=0 if h["event"] in exempt else h.get("event_transfers", 0)) for h in payload["current"]]
        return bootstrap, members, histories

    async def sync_gameweek(self, gw_id, context=None):
        if context is None:
            context = await self.prepare(await self.fpl.get("bootstrap-static/"))
        bootstrap, members, histories = context
        event = next((e for e in bootstrap["events"] if e["id"] == gw_id), None)
        provisional = bool(event and not (event["finished"] and event["data_checked"]))
        if not event or (provisional and (not event.get("is_current") or latest_available(bootstrap) != gw_id)):
            raise ValueError(f"GW{gw_id} is not finished and data-checked")
        month_phase(bootstrap["phases"], gw_id)
        current_ranks = historical_ranks(histories, gw_id)
        previous_ranks = historical_ranks(histories, gw_id - 1)
        # Use official league ranks when the endpoint represents this exact event.
        current = next((e["id"] for e in bootstrap["events"] if e.get("is_current")), None)
        if current == gw_id and not provisional:
            for member in members:
                if member.get("rank"):
                    current_ranks[member["entry"]] = member["rank"]
                if member.get("last_rank") and gw_id > 1:
                    previous_ranks[member["entry"]] = member["last_rank"]
        elif current == gw_id + 1:
            for member in members:
                if member.get("last_rank"):
                    current_ranks[member["entry"]] = member["last_rank"]
        snapshots = []
        for member in members:
            entry = member["entry"]
            if not provisional and entry not in current_ranks:  # Manager registered after this GW.
                continue
            payload = await self.fpl.get(f"entry/{entry}/event/{gw_id}/picks/")
            if provisional:
                h = dict(payload["entry_history"])
                histories[entry] = [row for row in histories[entry] if row["event"] != gw_id] + [h]
            picks = payload["picks"]
            if len(picks) != 15 or len({p["element"] for p in picks}) != 15 or {p["position"] for p in picks} != set(range(1, 16)):
                raise ValueError(f"Incomplete picks for entry {entry}, GW{gw_id}")
            snapshots.append((entry, payload))
        live = await self.fpl.get(f"event/{gw_id}/live/")
        live_ids = {e["id"] for e in live["elements"]}
        if not snapshots or any(p["element"] not in live_ids for _, s in snapshots for p in s["picks"]):
            raise ValueError("Missing manager or live points data")
        if provisional:
            points = {e["id"]: e["stats"]["total_points"] for e in live["elements"]}
            for entry, payload in snapshots:
                h = next(h for h in histories[entry] if h["event"] == gw_id)
                net = sum(points[p["element"]] * p["multiplier"] for p in payload["picks"]) - h.get("event_transfers_cost", 0)
                prior = max((row for row in histories[entry] if row["event"] < gw_id), key=lambda row: row["event"], default=None)
                h.update(points=net, total_points=(prior["total_points"] if prior else 0) + net)
            current_ranks = historical_ranks(histories, gw_id)
        # Fetch everything before touching the snapshot. A failure cannot leave partial data.
        try:
            await self.db.execute("BEGIN")
            await self.db.executemany("UPDATE gameweeks SET phase_id=? WHERE id=?",
                                     [(month_phase(bootstrap["phases"], e["id"])["id"], e["id"]) for e in bootstrap["events"]])
            await self.db.executemany("INSERT INTO managers VALUES (?,?,?) ON CONFLICT(entry_id) DO UPDATE SET entry_name=excluded.entry_name,player_name=excluded.player_name",
                                     [(m["entry"], m["entry_name"], m["player_name"]) for m in members])
            for table in ("standings", "picks", "autosubs", "live_points"):
                await self.db.execute(f"DELETE FROM {table} WHERE gw_id=?", (gw_id,))
            for entry, payload in snapshots:
                h = next(h for h in histories[entry] if h["event"] == gw_id)
                await self.db.execute("INSERT INTO standings VALUES (?,?,?,?,?,?)", (entry, gw_id, current_ranks[entry], previous_ranks.get(entry), h["total_points"], h["points"]))
                await self.db.executemany("INSERT INTO picks VALUES (?,?,?,?,?,?)",
                                         [(entry, gw_id, p["element"], p["position"], p["is_captain"], p["multiplier"]) for p in payload["picks"]])
                await self.db.executemany("INSERT INTO autosubs VALUES (?,?,?,?)",
                                         [(entry, gw_id, s["element_in"], s["element_out"]) for s in payload["automatic_subs"]])
            await self.db.executemany("INSERT INTO live_points VALUES (?,?,?)", [(e["id"], gw_id, e["stats"]["total_points"]) for e in live["elements"]])
            await self.db.execute("INSERT INTO sync_runs(gw_id) VALUES (?) ON CONFLICT(gw_id) DO UPDATE SET synced_at=CURRENT_TIMESTAMP", (gw_id,))
            await self.db.execute("INSERT INTO result_status(gw_id,provisional) VALUES (?,?) ON CONFLICT(gw_id) DO UPDATE SET provisional=excluded.provisional", (gw_id, int(provisional)))
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise

    async def ensure_through(self, gw, bootstrap):
        context = await self.prepare(bootstrap)
        membership = json.dumps(sorted(m["entry"] for m in context[1]))
        saved = await rows(self.db, "SELECT value FROM metadata WHERE key='membership'")
        changed = not saved or saved[0]["value"] != membership
        synced = {r["gw_id"] for r in await rows(self.db, "SELECT gw_id FROM sync_runs WHERE gw_id NOT IN (SELECT gw_id FROM result_status WHERE provisional=1)")}
        for event in range(1, gw + 1):
            if changed or event not in synced or event == gw:
                await self.sync_gameweek(event, context)
        if changed:
            await self.db.execute("DELETE FROM sync_runs WHERE gw_id>?", (gw,))
        await self.db.execute("INSERT INTO metadata VALUES ('membership',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (membership,))
        await self.db.commit()


BENCH = """SELECT s.entry_id,s.gw_id,COALESCE(SUM(CASE WHEN p.position>=12 AND p.multiplier=0 AND NOT EXISTS
 (SELECT 1 FROM autosubs a WHERE a.entry_id=p.entry_id AND a.gw_id=p.gw_id AND a.element_in=p.element_id)
 THEN l.points ELSE 0 END),0) AS points
 FROM standings s JOIN picks p ON p.entry_id=s.entry_id AND p.gw_id=s.gw_id
 JOIN live_points l ON l.element_id=p.element_id AND l.gw_id=p.gw_id
 GROUP BY s.entry_id,s.gw_id"""
CAPTAIN = """SELECT p.entry_id,p.gw_id,p.element_id,l.points*p.multiplier AS points,p.multiplier
 FROM picks p JOIN live_points l ON l.element_id=p.element_id AND l.gw_id=p.gw_id WHERE p.multiplier>1"""


async def calculate(db, gw):
    synced = {r["gw_id"] for r in await rows(db, "SELECT gw_id FROM sync_runs WHERE gw_id<=?", (gw,))}
    if synced != set(range(1, gw + 1)):
        raise ValueError("Cumulative report requires every gameweek through the target")
    phases = await rows(db, "SELECT * FROM phases")
    phase = month_phase(phases, gw)
    async def query(sql, params=()):
        return await rows(db, sql, params)
    names = "m.entry_id,m.entry_name,m.player_name"
    top = await query(f"SELECT {names},s.event_points AS points FROM standings s JOIN managers m USING(entry_id) WHERE gw_id=? ORDER BY points DESC,entry_id LIMIT 10", (gw,))
    bench = await query(f"WITH b AS ({BENCH}) SELECT {names},b.points FROM b JOIN managers m USING(entry_id) WHERE gw_id=? ORDER BY points DESC,entry_id LIMIT 5", (gw,))
    async def leaders(sql, metric, descending=True):
        direction = "DESC" if descending else "ASC"
        return await query(f"WITH candidates AS ({sql}), ranked AS (SELECT *,DENSE_RANK() OVER (ORDER BY {metric} {direction}) AS award_rank FROM candidates) SELECT * FROM ranked WHERE award_rank=1 ORDER BY entry_id", (gw,))
    captain = await leaders(f"WITH c AS ({CAPTAIN}) SELECT {names},c.points,c.multiplier,pl.web_name FROM c JOIN managers m USING(entry_id) JOIN players pl ON pl.id=c.element_id WHERE gw_id=?", "points")
    movement = f"SELECT {names},s.rank,s.last_rank,s.last_rank-s.rank AS movement FROM standings s JOIN managers m USING(entry_id) WHERE gw_id=? AND last_rank IS NOT NULL"
    climb = await leaders(movement + " AND last_rank>rank", "movement")
    drop = await leaders(movement + " AND last_rank<rank", "movement", descending=False)
    for row in drop:
        row["movement"] = abs(row["movement"])
    month = await query(f"SELECT {names},SUM(s.event_points) AS points FROM standings s JOIN managers m USING(entry_id) WHERE gw_id BETWEEN ? AND ? GROUP BY m.entry_id ORDER BY points DESC,m.entry_id LIMIT 3", (phase["start_event"], min(gw, phase["stop_event"])))
    async def cumulative(metric):
        return await query(f"WITH b AS ({metric}) SELECT {names},SUM(b.points) AS points FROM b JOIN managers m USING(entry_id) WHERE gw_id<=? GROUP BY m.entry_id ORDER BY points DESC,m.entry_id LIMIT 3", (gw,))
    status = await rows(db, "SELECT provisional,synced_at FROM sync_runs LEFT JOIN result_status USING(gw_id) WHERE gw_id=?", (gw,))
    return {"provisional": bool(status[0]["provisional"]), "synced_at": status[0]["synced_at"], "gameweek": gw, "top_scorers": top, "bench_warmers": bench, "best_captain": captain,
            "highest_climb": climb, "highest_drop": drop, "month_phase": phase,
            "month_through_gw": gw, "manager_of_month": month,
            "season_bench_warmer": await cumulative(BENCH), "season_best_captain": await cumulative(CAPTAIN)}
