import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot import Config, Service
from bootstrap import SECTIONS, save
from data import connect, rows
from questions import query, snapshot
from test_bot import fixture


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'db.sqlite')
        self.db = await connect(self.path)
        self.payload = fixture()[0]
        self.payload['elements'][0].update(now_cost=75, team=1, element_type=2,
            defensive_contribution=12, expected_goals='1.20', news='Available', extra={'a': [1, None]})
        self.payload['element_types'] = [{'id': 2, 'singular_name': 'Defender'}]
        self.payload['game_settings'] = {'nested': {'a': [1, True, None]}}
        self.payload['game_config'] = {'flag': False}
        self.payload['future_section'] = {'new': [1, 2]}

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_all_fields_and_sections_stored(self):
        await save(self.db, self.payload, 1)
        for section, (table, key) in SECTIONS.items():
            actual = {r[key]: r for r in await rows(self.db, f'SELECT * FROM {table}')}
            for record in self.payload[section]:
                for field, expected in record.items():
                    stored = actual[record[key]][field]
                    if isinstance(expected, (dict, list)):
                        stored = json.loads(stored)
                    self.assertEqual(stored, expected, (section, field))
        for section in ('game_settings', 'game_config'):
            actual = {r['key']: json.loads(r['value']) for r in await rows(self.db, f'SELECT * FROM {section}')}
            self.assertEqual(actual, self.payload[section])
        self.assertEqual(json.loads((await rows(self.db, 'SELECT value FROM bootstrap_extra'))[0]['value']), {'new': [1, 2]})
        self.assertEqual((await rows(self.db, 'SELECT total_players FROM bootstrap_summary'))[0]['total_players'], 2)

    async def test_latest_replaces_removed_values_without_harming_history(self):
        await save(self.db, self.payload, 1)
        await self.db.execute('INSERT INTO live_points VALUES (1,1,9)')
        await self.db.commit()
        changed = copy.deepcopy(self.payload)
        changed['elements'][0]['now_cost'] = 76
        del changed['elements'][0]['news']
        changed['game_settings'] = {}
        del changed['future_section']
        await save(self.db, changed, 1)
        player = (await rows(self.db, 'SELECT * FROM players WHERE id=1'))[0]
        self.assertEqual(player['now_cost'], 76)
        self.assertIsNone(player['news'])
        self.assertEqual(await rows(self.db, 'SELECT * FROM game_settings'), [])
        self.assertEqual(await rows(self.db, 'SELECT * FROM bootstrap_extra'), [])
        changed['elements'] = changed['elements'][1:]
        await save(self.db, changed, 1)
        player = (await rows(self.db, 'SELECT * FROM players WHERE id=1'))[0]
        self.assertEqual(player['bootstrap_current'], 0)
        self.assertIsNone(player['now_cost'])
        self.assertEqual((await rows(self.db, 'SELECT points FROM live_points'))[0]['points'], 9)
        self.assertEqual(len(await rows(self.db, 'SELECT * FROM bootstrap_summary')), 1)
        self.assertEqual(await rows(self.db, 'PRAGMA foreign_key_check'), [])

    async def test_validation_and_mid_transaction_failure_preserve_previous(self):
        await save(self.db, self.payload, 1)
        bad = copy.deepcopy(self.payload)
        bad['teams'] = []
        with self.assertRaises(ValueError):
            await save(self.db, bad, 1)
        bad = copy.deepcopy(self.payload)
        bad['elements'][0]['now_cost'] = 99
        bad['elements'][0]['new_column'] = 'must roll back'
        bad['events'][0]['name'] = None
        with self.assertRaises(sqlite3.IntegrityError):
            await save(self.db, bad, 1)
        self.assertEqual((await rows(self.db, 'SELECT now_cost FROM players WHERE id=1'))[0]['now_cost'], 75)
        self.assertNotIn('new_column', {r['name'] for r in await rows(self.db, 'PRAGMA table_info(players)')})
        with self.assertRaises(ValueError):
            await save(self.db, self.payload, 2)

    async def test_refresh_without_new_report(self):
        for e in self.payload['events']:
            e['finished'] = False
        async def get(path):
            return [] if path.startswith('fixtures/') else self.payload
        fpl = SimpleNamespace(get=AsyncMock(side_effect=get))
        service = Service(Config('', 0, 0, 1, '', self.path), self.db, fpl, None)
        await service.poll(None)
        self.assertEqual((await rows(self.db, 'SELECT now_cost FROM players WHERE id=1'))[0]['now_cost'], 75)
        self.payload['events'][0]['finished'] = True
        await self.db.execute('INSERT INTO reports(gw_id,messages,sent_count) VALUES (1,?,2)', ('["a","b"]',))
        await self.db.commit()
        self.payload['elements'][0]['now_cost'] = 76
        await service.poll(None)
        self.assertEqual((await rows(self.db, 'SELECT now_cost FROM players WHERE id=1'))[0]['now_cost'], 76)
        self.assertEqual(await rows(self.db, 'SELECT * FROM sync_runs'), [])

    async def test_current_data_available_to_read_only_questions(self):
        await save(self.db, self.payload, 1)
        service = Service(Config('', 0, 0, 1, '', self.path), self.db, None, None)
        db, schema, coverage = await snapshot(service)
        try:
            result = query(db, 'SELECT p.web_name,t.name,p.now_cost / 10.0 AS price,p.defensive_contribution FROM players p JOIN teams t ON p.team=t.id WHERE p.bootstrap_current=1')
            self.assertEqual(result[0]['price'], 7.5)
            self.assertEqual(result[0]['defensive_contribution'], 12)
            self.assertEqual(query(db, "SELECT json_extract(value,'$.a[0]') AS n FROM game_settings")[0]['n'], 1)
            self.assertEqual(len(query(db, 'SELECT * FROM gameweeks')), 3)
            self.assertEqual(coverage, [])
            for sql in ('SELECT * FROM metadata', "SELECT readfile('/etc/passwd')"):
                with self.assertRaises(sqlite3.Error):
                    query(db, sql)
        finally:
            db.close()

    async def test_existing_database_migrates_and_reopens(self):
        await self.db.execute("INSERT INTO players VALUES (500,'Historical')")
        await self.db.commit()
        await save(self.db, self.payload, 1)
        await self.db.close()
        self.db = await connect(self.path)
        await save(self.db, self.payload, 1)
        self.assertEqual((await rows(self.db, 'SELECT web_name FROM players WHERE id=500'))[0]['web_name'], 'Historical')
