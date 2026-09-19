import json
import tempfile
import unittest
import sqlite3
import httpx
from openai import RateLimitError
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot import Config, Service, ask_command, question_message, split_answer
from data import connect
from questions import answer, name_candidates, normalized_name, snapshot, query, FAILURE


def response(value):
    return NS(choices=[NS(finish_reason='stop', message=NS(content=json.dumps(value)))])


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = await connect(str(Path(self.temp.name) / 'test.db'))
        await self.db.executescript("""
INSERT INTO managers VALUES (1,'Team One','Alice'),(2,'Team Two','Bob'),(3,'riko','Rinat Sagitov');
INSERT INTO players VALUES (411,'Haaland');
INSERT INTO gameweeks VALUES (1,'GW1',1,1,NULL),(2,'GW2',1,1,NULL);
INSERT INTO standings VALUES (1,1,1,NULL,60,60),(2,1,2,NULL,40,40),(1,2,1,1,999,939);
INSERT INTO sync_runs(gw_id) VALUES (1);
INSERT INTO metadata VALUES ('secret','private');
ALTER TABLE teams ADD COLUMN name;
ALTER TABLE teams ADD COLUMN bootstrap_current;
INSERT INTO teams(id,name,bootstrap_current) VALUES (5,'Brighton',1),(9,'Liverpool',1);
INSERT INTO fixtures VALUES (44,2,'2026-08-02T14:00:00Z',5,9,2,1,1,1,0,90);
""")
        self.create = AsyncMock()
        llm = NS(chat=NS(completions=NS(create=self.create)))
        self.service = Service(Config('', -100, 1, 836115, '', ''), self.db, None, llm)
        self.context = NS(application=NS(bot_data={'service': self.service}), bot=NS(id=77, username='fpl_bot'))
        self.message = NS(text='/ask Who leads?', reply_to_message=None, reply_text=AsyncMock())
        self.update = NS(effective_user=NS(id=25,is_bot=False), effective_chat=NS(id=-100), effective_message=self.message)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_database_grounding_and_coverage(self):
        self.create.side_effect = [response({'action':'query','sql':'SELECT entry_name,total_points FROM standings JOIN managers USING(entry_id) ORDER BY total_points DESC'}), response({'action':'answer','text':'Team One leads with 60 points in GW1.'})]
        result = await answer(self.service, 'Who leads?')
        self.assertIn('60 points', result)
        self.assertIn('туры 1', result)
        payload = self.create.call_args.kwargs['messages'][-1]['content']
        self.assertIn('60', payload)
        self.assertNotIn('999', payload)
        self.assertNotIn('private', str(self.create.call_args_list))

    async def test_finished_fixture_is_available_independently_of_gameweek_status(self):
        await self.db.execute('UPDATE gameweeks SET finished=0,data_checked=0 WHERE id=2')
        await self.db.commit()
        self.create.side_effect = [
            response({'action':'query','sql':'''SELECT f.event,home.name AS home,away.name AS away,
                     f.team_h_score,f.team_a_score,f.finished FROM fixtures f
                     JOIN teams home ON home.id=f.team_h JOIN teams away ON away.id=f.team_a
                     WHERE f.event=2 AND (home.name='Brighton' OR away.name='Brighton')'''}),
            response({'action':'answer','basis':'database','text':'Brighton победил Liverpool 2:1; матч завершён.'})]
        result = await answer(self.service, 'Расскажи про матч Brighton во втором туре')
        self.assertIn('2:1', result)
        payload = self.create.call_args.kwargs['messages'][-1]['content']
        self.assertIn('"finished": 1', payload)

    async def test_cyrillic_and_fuzzy_name_resolution(self):
        db, _, _ = await snapshot(self.service)
        try:
            self.assertEqual(normalized_name('Холанд'), 'holand')
            player = name_candidates(db, 'Сколько очков у Холанда?')[0]
            self.assertEqual((player['entity'], player['id'], player['name']), ('footballer', 411, 'Haaland'))
            manager = name_candidates(db, 'Покажи очки Рината Сагитова')[0]
            self.assertEqual((manager['entity'], manager['id']), ('manager', 3))
        finally:
            db.close()

    async def test_resolved_candidates_are_supplied_to_model(self):
        self.create.return_value = response({'action':'answer', 'basis':'general', 'text':'ok'})
        await answer(self.service, 'Как сыграл Холанд?')
        prompt = self.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('Resolved name candidates', prompt)
        self.assertIn('Haaland', prompt)

    async def test_general_question_answered_without_query(self):
        self.create.return_value = response({'action':'answer', 'basis':'general', 'text':'Mix flour, eggs and milk to make pancake batter.'})
        with patch('questions.query') as execute:
            result = await answer(self.service, 'How do I make pancakes?')
            self.assertIn('pancake batter', result)
            self.assertNotIn('Источник: база FPL', result)
            execute.assert_not_called()

    async def test_model_cannot_skip_database(self):
        self.create.return_value = response({'action':'answer','text':'Invented answer'})
        self.assertEqual(await answer(self.service, 'Who leads?'), FAILURE)

    async def test_sql_boundary(self):
        db, _, _ = await snapshot(self.service)
        try:
            for sql in ['DELETE FROM managers', 'SELECT * FROM metadata', 'SELECT * FROM sqlite_master', "SELECT load_extension('evil')", "ATTACH DATABASE '/tmp/evil' AS evil", 'SELECT 1; DELETE FROM managers']:
                with self.subTest(sql=sql), self.assertRaises((ValueError, sqlite3.Error)):
                    query(db, sql)
            self.assertEqual(query(db, 'SELECT SUM(event_points) AS points FROM standings')[0]['points'], 100)
            self.assertEqual(len(query(db, 'SELECT * FROM bench_scores')), 0)
        finally:
            db.close()
        async with self.db.execute('SELECT COUNT(*) FROM managers') as cursor:
            self.assertEqual((await cursor.fetchone())[0], 3)

    async def test_oversize_result_rejected(self):
        db, _, _ = await snapshot(self.service)
        try:
            with self.assertRaises(ValueError):
                query(db, 'SELECT a.entry_id FROM managers a,managers b,managers c,managers d,managers e,managers f,managers g')
        finally:
            db.close()

    async def test_failed_query_can_be_corrected(self):
        self.create.side_effect = [response({'action':'query','sql':'SELECT absent FROM managers'}), response({'action':'query','sql':'SELECT entry_name FROM managers WHERE entry_id=1'}), response({'action':'answer','text':'Team One.'})]
        self.assertIn('Team One', await answer(self.service, 'Which team is entry 1?'))

    async def test_any_group_member_and_rate_limit(self):
        with patch('bot.answer', new_callable=AsyncMock, return_value='Answer') as mocked:
            await ask_command(self.update, self.context)
            mocked.assert_awaited_once()
            await ask_command(self.update, self.context)
            self.assertEqual(mocked.await_count, 1)
            self.assertIn('10 секунд', self.message.reply_text.call_args.args[0])

    async def test_ask_sends_one_to_three_messages_according_to_length(self):
        self.assertEqual(split_answer('short'), ['short'])
        long_answer = ('Первый абзац. ' * 350) + '\n\n' + ('Второй абзац. ' * 350)
        parts = split_answer(long_answer)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(part) <= 4000 for part in parts))
        with patch('bot.answer', new_callable=AsyncMock, return_value=long_answer):
            await ask_command(self.update, self.context)
        self.assertEqual(self.message.reply_text.await_count, 3)

    def test_ask_rejects_more_than_three_telegram_messages(self):
        with self.assertRaises(ValueError):
            split_answer('x' * 12001)

    async def test_other_chats_and_ordinary_messages_ignored(self):
        with patch('bot.answer', new_callable=AsyncMock) as mocked:
            self.update.effective_chat.id = 999
            await ask_command(self.update, self.context)
            self.update.effective_chat.id = -100
            self.message.text = 'hello everyone'
            await question_message(self.update, self.context)
            mocked.assert_not_called()
            self.message.reply_text.assert_not_called()

    async def test_mentions_and_replies(self):
        with patch('bot.answer', new_callable=AsyncMock, return_value='Answer') as mocked:
            self.message.text = '@fpl_bot who leads?'
            await question_message(self.update, self.context)
            self.assertEqual(mocked.call_args.args[1], 'who leads?')
            self.service.question_times.clear()
            self.message.text = 'And in GW1?'
            self.message.reply_to_message = NS(from_user=NS(id=77), text='Team One leads in GW2')
            await question_message(self.update, self.context)
            self.assertIn('GW2', mocked.call_args.args[2])

    async def test_api_failure_is_safe(self):
        self.create.side_effect = RuntimeError('sensitive details')
        self.assertEqual(await answer(self.service, 'Who leads?'), FAILURE)


    async def test_quota_error_explains_billing_in_russian(self):
        for body in ({'code': 'credit_balance_exhausted', 'type': 'insufficient_quota'},
                     {'error': {'code': 'insufficient_quota'}}):
            self.create.side_effect = RateLimitError('private error details', response=httpx.Response(429, request=httpx.Request('POST', 'https://example.test')), body=body)
            result = await answer(self.service, 'какой защитник набрал больше всего defconов?')
            self.assertIn('кредиты или квота', result)
            self.assertNotIn('private', result)
            self.assertNotIn('gameweek', result)

    async def test_temporary_rate_limit_does_not_claim_missing_credits(self):
        self.create.side_effect = RateLimitError('rate limited', response=httpx.Response(429, request=httpx.Request('POST', 'https://example.test')), body={'code': 'rate_limit_exceeded'})
        result = await answer(self.service, 'Who leads?')
        self.assertIn('Попробуйте ещё раз через минуту', result)
        self.assertNotIn('credits', result)


    async def test_russian_database_answer_has_russian_source(self):
        self.create.side_effect = [response({'action':'query','sql':'SELECT entry_name FROM managers WHERE entry_id=1'}), response({'action':'answer','basis':'database','text':'Команда: Team One.'})]
        result = await answer(self.service, 'Как называется команда с ID 1?', 'Previous reply in English')
        self.assertIn('Источник: база FPL', result)
        self.assertNotIn('Source:', result)

    async def test_russian_cooldown_and_failure(self):
        self.message.text = '/ask Кто лидирует?'
        with patch('bot.answer', new_callable=AsyncMock, return_value='Ответ'):
            await ask_command(self.update, self.context)
            await ask_command(self.update, self.context)
        self.assertIn('Подождите 10 секунд', self.message.reply_text.call_args.args[0])
        self.create.side_effect = RuntimeError()
        self.assertIn('Не удалось', await answer(self.service, 'Кто лидирует?'))


    async def test_id_only_lookup_is_enriched_with_names(self):
        self.create.side_effect = [response({'action':'query','sql':'SELECT entry_id,rank,total_points FROM standings WHERE rank=1'}), response({'action':'answer','basis':'database','text':'Team One (Alice) leads.'})]
        result = await answer(self.service, 'Who leads?')
        payload = self.create.call_args.kwargs['messages'][-1]['content']
        self.assertIn('"entry_name": "Team One"', payload)
        self.assertIn('"player_name": "Alice"', payload)
        self.assertIn('Team One (Alice)', result)
