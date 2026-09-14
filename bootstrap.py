"""Latest-only bootstrap storage; scalar fields are columns, nested values are JSON."""
import json

SECTIONS = {
    'elements': ('players', 'id'), 'events': ('gameweeks', 'id'),
    'phases': ('phases', 'id'), 'teams': ('teams', 'id'),
    'chips': ('chips', 'id'), 'element_types': ('player_positions', 'id'),
    'element_stats': ('stat_definitions', 'name'),
}
BOOTSTRAP_TABLES = tuple(t for t, _ in SECTIONS.values()) + (
    'game_settings', 'game_config', 'bootstrap_summary', 'bootstrap_extra')
RETAINED = {'players': {'id', 'web_name'},
            'gameweeks': {'id', 'name', 'finished', 'data_checked', 'phase_id'},
            'phases': {'id', 'name', 'start_event', 'stop_event'}}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def value(item):
    return json.dumps(item, ensure_ascii=False, allow_nan=False) if isinstance(item, (list, dict)) else item


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Bootstrap must be an object')
    for section, (_, key) in SECTIONS.items():
        records = payload.get(section)
        if not isinstance(records, list) or (section in ('elements', 'events', 'phases', 'teams') and not records):
            raise ValueError(f'Invalid bootstrap section: {section}')
        ids = []
        for record in records:
            if not isinstance(record, dict) or key not in record or record[key] is None:
                raise ValueError(f'Invalid bootstrap record: {section}')
            if (key == 'id' and type(record[key]) is not int) or (key == 'name' and not isinstance(record[key], str)):
                raise ValueError(f'Invalid bootstrap key: {section}')
            if 'bootstrap_current' in record:
                raise ValueError('Reserved bootstrap field')
            ids.append(record[key])
        if len(set(ids)) != len(ids):
            raise ValueError(f'Duplicate bootstrap keys: {section}')
    for section in ('game_settings', 'game_config'):
        if not isinstance(payload.get(section), dict):
            raise ValueError(f'Invalid bootstrap section: {section}')
    if type(payload.get('total_players')) is not int:
        raise ValueError('Invalid total_players')
    # Reject non-JSON values before touching the database.
    json.dumps(payload, allow_nan=False)


async def save(db, payload, league_id):
    """Migrate and replace all latest data in one transaction; preserve historical FKs."""
    validate(payload)
    identity = json.dumps([league_id, str(payload['events'][0]['deadline_time'])])
    async with db.execute("SELECT value FROM metadata WHERE key='identity'") as cursor:
        existing = await cursor.fetchone()
    if existing and existing[0] != identity:
        raise ValueError('Database belongs to another league/season; use a new DATABASE_PATH')
    try:
        await db.execute('BEGIN')
        await db.execute("INSERT OR IGNORE INTO metadata VALUES ('identity',?)", (identity,))
        for section, (table, key) in SECTIONS.items():
            records = payload[section]
            await db.execute(f'CREATE TABLE IF NOT EXISTS {quote(table)} ({quote(key)} PRIMARY KEY)')
            async with db.execute(f'PRAGMA table_info({quote(table)})') as cursor:
                existing_columns = {r[1] for r in await cursor.fetchall()}
            fields = set().union(*(r.keys() for r in records)) if records else {key}
            for field in sorted((fields | {'bootstrap_current'}) - existing_columns):
                # No affinity preserves upstream scalar types, including numeric strings.
                await db.execute(f'ALTER TABLE {quote(table)} ADD COLUMN {quote(field)}')
            columns = existing_columns | fields | {'bootstrap_current'}
            if table in RETAINED:
                clear = columns - RETAINED[table] - {'bootstrap_current'}
                assignments = ['bootstrap_current=0'] + [f'{quote(c)}=NULL' for c in sorted(clear)]
                await db.execute(f'UPDATE {quote(table)} SET ' + ','.join(assignments))
            else:
                await db.execute(f'DELETE FROM {quote(table)}')
            ordered = sorted(fields | {'bootstrap_current'})
            updates = ','.join(f'{quote(c)}=excluded.{quote(c)}' for c in ordered if c != key)
            sql = (f'INSERT INTO {quote(table)} ({",".join(map(quote, ordered))}) '
                   f'VALUES ({",".join("?" for _ in ordered)}) ON CONFLICT({quote(key)}) DO UPDATE SET {updates}')
            await db.executemany(sql, [tuple(1 if c == 'bootstrap_current' else value(r.get(c)) for c in ordered) for r in records])
        for section in ('game_settings', 'game_config'):
            await db.execute(f'DELETE FROM {section}')
            await db.executemany(f'INSERT INTO {section}(key,value) VALUES (?,?)',
                                 [(k, json.dumps(v, ensure_ascii=False)) for k, v in payload[section].items()])
        await db.execute('DELETE FROM bootstrap_extra')
        known = set(SECTIONS) | {'game_settings', 'game_config', 'total_players'}
        await db.executemany('INSERT INTO bootstrap_extra(key,value) VALUES (?,?)',
                             [(k, json.dumps(v, ensure_ascii=False)) for k, v in payload.items() if k not in known])
        await db.execute('INSERT INTO bootstrap_summary(id,total_players,fetched_at) VALUES (1,?,CURRENT_TIMESTAMP) '
                         'ON CONFLICT(id) DO UPDATE SET total_players=excluded.total_players,fetched_at=excluded.fetched_at',
                         (payload['total_players'],))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
