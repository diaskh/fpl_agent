import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
from telegram.error import TimedOut, Forbidden
from cup import Cup, draw_cup, participants, render_cup, settle, summary
from data import connect, rows
from bot import cup_command


def members(n):
    return [{'entry': i, 'entry_name': f'Команда {i}'} for i in range(1, n+1)]


def bootstrap():
    now = datetime.now(timezone.utc)
    return {'events': [{'id': i, 'deadline_time': (now + timedelta(days=i)).isoformat(),
                        'finished': False, 'data_checked': False} for i in range(1, 39)]}


class DrawTests(unittest.TestCase):
    def test_all_sizes_unique_entrants_progress_and_champion(self):
        for count in range(2, 33):
            state = draw_cup(members(count), 8, random.Random(count))
            initial = []
            for stage in state['stages']:
                for match in stage['matches']:
                    initial.extend([s['entry'] for s in match['slots'] if 'entry' in s]
                                   if 'slots' in match else participants(state, match))
            self.assertEqual(sorted(initial), list(range(1, count+1)))
            for index in range(len(state['stages'])):
                state = settle(state, index, {i: i for i in range(1, count+1)})
            self.assertEqual(state['stages'][-1]['matches'][0]['winner'], count)

    def test_preliminary_routes_and_byes(self):
        state = draw_cup(members(21), 8, random.Random(42))
        self.assertEqual([len(s['matches']) for s in state['stages']], [5, 8, 4, 2, 1])
        self.assertEqual([s['gw'] for s in state['stages']], [8, 9, 10, 11, 12])
        for i in range(5):
            match = next(m for m in state['stages'][1]['matches'] if {'from': [0, i]} in m['slots'])
            self.assertEqual(sum('entry' in slot for slot in match['slots']), 1)
        self.assertNotEqual(draw_cup(members(21), 8, random.Random(43)), state)

    def test_tie_and_render_completed_brackets(self):
        state = draw_cup(members(21), 8, random.Random(42))
        for index in range(5):
            state = settle(state, index, {i: 50 for i in range(1, 22)})
        self.assertEqual(state['stages'][-1]['matches'][0]['winner'], state['priority'][0])
        for preliminary in (True, False):
            image = Image.open(render_cup(state, preliminary))
            self.assertEqual(image.width, 1600)
            self.assertLess(image.width + image.height, 10000)
        self.assertIn('Чемпион', '\n'.join(summary(state, 4)))
        self.assertTrue(all('scores' in m for s in state['stages'] for m in s['matches']))


class CupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await connect(':memory:')
        self.fpl = SimpleNamespace(members=AsyncMock(return_value=members(21)), get=AsyncMock())
        self.cup = Cup(self.db, self.fpl, 123, -100)
        self.bootstrap = bootstrap()
        self.bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())

    async def asyncTearDown(self):
        await self.db.close()

    async def test_start_once_persist_and_deliver_once(self):
        state = await self.cup.start(self.bootstrap)
        with self.assertRaises(ValueError):
            await self.cup.start(self.bootstrap)
        self.assertEqual((await Cup(self.db, self.fpl, 123, -100).latest())[1], state)
        await self.cup.deliver(self.bot)
        self.assertEqual(self.bot.send_photo.await_count, 2)
        count = self.bot.send_message.await_count
        await self.cup.deliver(self.bot)
        self.assertEqual(self.bot.send_photo.await_count, 2)
        self.assertEqual(self.bot.send_message.await_count, count)

    async def test_invalid_start_does_not_persist(self):
        self.bootstrap['events'][0]['deadline_time'] = '2020-01-01T00:00:00Z'
        for gw in (1, 36, 39):
            with self.assertRaises(ValueError):
                await self.cup.start(self.bootstrap, gw)
        self.assertEqual(await self.cup.latest(), (None, None))

    async def test_final_scores_hits_and_no_partial_advance(self):
        original = await self.cup.start(self.bootstrap)
        await self.cup.advance(self.bootstrap)
        self.bootstrap['events'][0]['finished'] = True
        await self.cup.advance(self.bootstrap)
        self.fpl.get.assert_not_awaited()
        self.bootstrap['events'][0]['data_checked'] = True
        self.fpl.get.return_value = {'current': []}
        with self.assertRaises(ValueError):
            await self.cup.advance(self.bootstrap)
        self.assertEqual((await self.cup.latest())[1], original)
        async def history(path):
            entry = int(path.split('/')[1])
            return {'current': [{'event': 1, 'points': 60+entry, 'event_transfers_cost': 4}]}
        self.fpl.get.side_effect = history
        await self.cup.advance(self.bootstrap)
        _, state = await self.cup.latest()
        for match in state['stages'][0]['matches']:
            self.assertEqual(match['scores'], [56+e for e in participants(state, match)])
        await self.cup.advance(self.bootstrap)
        self.assertEqual(len(await rows(self.db, 'SELECT * FROM cup_posts')), 2)
        await self.cup.deliver(self.bot)
        self.assertEqual(self.bot.send_photo.await_count, 4)

    async def test_multi_round_catchup(self):
        await self.cup.start(self.bootstrap)
        for event in self.bootstrap['events']:
            event.update(finished=True, data_checked=True)
        self.fpl.get.return_value = {'current': [{'event': i, 'points': 50} for i in range(1, 39)]}
        await self.cup.advance(self.bootstrap)
        self.assertTrue((await self.cup.latest())[1]['stages'][-1]['done'])
        self.assertEqual(len(await rows(self.db, 'SELECT * FROM cup_posts')), 6)

    async def test_uncertain_delivery_blocks_retry(self):
        await self.cup.start(self.bootstrap)
        self.bot.send_message.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await self.cup.deliver(self.bot)
        self.bot.send_message.reset_mock(side_effect=True)
        await self.cup.deliver(self.bot)
        self.bot.send_message.assert_not_awaited()
        await self.db.execute('UPDATE cup_posts SET uncertain=0')
        await self.db.commit()
        self.bot.send_message.side_effect = Forbidden('blocked')
        with self.assertRaises(Forbidden):
            await self.cup.deliver(self.bot)
        self.assertEqual((await rows(self.db, 'SELECT uncertain FROM cup_posts'))[0]['uncertain'], 0)
        self.bot.send_message.reset_mock(side_effect=True)
        await self.cup.deliver(self.bot)
        self.assertEqual(self.bot.send_photo.await_count, 2)

    async def test_nonadmin_cannot_start_public_show_no_buttons(self):
        service = SimpleNamespace(config=SimpleNamespace(admin_id=1, chat_id=-100),
                                  lock=__import__('asyncio').Lock(), cup=self.cup)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2, is_bot=False),
                                 effective_chat=SimpleNamespace(id=-100), effective_message=message)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={'service': service}), bot=self.bot, args=['start'])
        await cup_command(update, context)
        self.assertEqual(await self.cup.latest(), (None, None))
        await self.cup.start(self.bootstrap)
        context.args = []
        await cup_command(update, context)
        self.assertEqual(self.bot.send_photo.await_count, 2)
        self.assertTrue(all('reply_markup' not in call.kwargs for call in self.bot.send_photo.await_args_list))
