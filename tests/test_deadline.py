import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import Config, Service
from data import connect, rows
from deadline import build_messages, free_transfers
from telegram.error import TimedOut


class BalanceTests(unittest.TestCase):
    def test_roll_spend_hit_cap_and_chips(self):
        history = [{"event": i, "event_transfers": n} for i, n in enumerate([0, 0, 0, 1, 9, 0, 0, 0, 0, 0], 1)]
        self.assertEqual(free_transfers(history, [], 3, {}), 3)
        self.assertEqual(free_transfers(history, [], 4, {}), 3)
        self.assertEqual(free_transfers(history, [], 5, {}), 1)
        self.assertEqual(free_transfers(history, [{"event": 5, "name": "wildcard"}], 5, {}), 3)
        self.assertEqual(free_transfers(history, [{"event": 5, "name": "freehit"}], 5, {}), 3)
        self.assertEqual(free_transfers(history, [], 10, {}), 5)
        self.assertIsNone(free_transfers([history[0], history[2]], [], 3, {}))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = await connect(str(Path(self.temp.name) / 'test.db'))
        self.fpl = SimpleNamespace(members=AsyncMock(return_value=[{"entry": 1, "player_name": "Manager", "entry_name": "Team"}]), get=AsyncMock(side_effect=self.get))
        self.service = Service(Config('', -1, 1, 1, '', ''), self.db, self.fpl, None)
        self.bootstrap = {"events": [{"id": 2, "deadline_time": "2020-01-01T00:00:00Z"}], "elements": [{"id": 1, "web_name": "Out"}, {"id": 2, "web_name": "In"}]}
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def get(self, path):
        if path.endswith('/picks/'):
            return {"picks": [{}] * 15, "active_chip": "bboost", "entry_history": {"event": 2, "event_transfers": 1, "event_transfers_cost": 0}}
        if path.endswith('/history/'):
            return {"current": [{"event": 1, "event_transfers": 0}], "chips": []}
        return [{"event": 2, "element_out": 1, "element_in": 2}]

    async def test_baseline_then_send_once_across_restart(self):
        await self.service.poll_deadlines(self.bot, self.bootstrap)
        self.bot.send_message.assert_not_awaited()
        await self.service.set_metadata('deadline-baseline', '[]')
        await self.service.poll_deadlines(self.bot, self.bootstrap)
        text = self.bot.send_message.call_args.kwargs['text']
        self.assertIn('Out → In', text)
        self.assertIn('Бенч-буст', text)
        self.assertIn('GW3: 1', text)
        await self.service.poll_deadlines(self.bot, self.bootstrap)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_timeout_never_blindly_resends(self):
        await self.service.set_metadata('deadline-baseline', '[]')
        self.bot.send_message.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await self.service.poll_deadlines(self.bot, self.bootstrap)
        await self.service.poll_deadlines(self.bot, self.bootstrap)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_incomplete_data_does_not_save_or_send(self):
        await self.service.set_metadata('deadline-baseline', '[]')
        self.fpl.get = AsyncMock(side_effect=ValueError('updating'))
        with self.assertRaises(ValueError):
            await self.service.poll_deadlines(self.bot, self.bootstrap)
        self.assertEqual(await rows(self.db, 'SELECT * FROM deadline_reports'), [])
        self.bot.send_message.assert_not_awaited()

    async def test_wildcard_history_can_list_moves_with_zero_chargeable_count(self):
        original = self.get
        async def wildcard_get(path):
            payload = await original(path)
            if path.endswith('/picks/'):
                payload['active_chip'] = 'wildcard'
                payload['entry_history']['event_transfers'] = 0
            return payload
        self.fpl.get = AsyncMock(side_effect=wildcard_get)
        messages = await build_messages(self.fpl, 1, 2, self.bootstrap)
        self.assertIn('Out → In', messages[0])
        self.assertIn('Вайлдкард', messages[0])


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_has_no_database_access(self):
        from bot import deadline_preview
        from unittest.mock import patch
        fpl = SimpleNamespace(get=AsyncMock(return_value={
            'events': [{'id': 7, 'deadline_time': '2020-01-01T00:00:00Z', 'finished': True, 'data_checked': True}]
        }), client=SimpleNamespace(aclose=AsyncMock()))
        with patch('bot.FPL', return_value=fpl), patch('bot.build_messages', new=AsyncMock(return_value=['Real report'])) as build, patch('bot.connect') as connect_db, patch('builtins.print') as output:
            await deadline_preview(SimpleNamespace(league_id=1))
            connect_db.assert_not_called()
            self.assertEqual(build.call_args.args[2], 7)
            self.assertIn('Real report', output.call_args.args[0])
        fpl.client.aclose.assert_awaited_once()


class TransferCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_sends_to_configured_chat_without_delivery_state(self):
        import asyncio
        from bot import transfers_command
        from unittest.mock import patch
        service = SimpleNamespace(config=SimpleNamespace(admin_id=1, chat_id=-100, league_id=42), lock=asyncio.Lock(), fpl=SimpleNamespace(get=AsyncMock(return_value={'events':[{'id':7,'deadline_time':'2020-01-01T00:00:00Z','finished':True,'data_checked':True}]})))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=-100), effective_message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=['7'], application=SimpleNamespace(bot_data={'service':service}), bot=SimpleNamespace(send_message=AsyncMock()))
        with patch('bot.build_messages', new=AsyncMock(return_value=['Report'])):
            await transfers_command(update, context)
        context.bot.send_message.assert_awaited_once_with(chat_id=-100, text='Report')
        update.effective_user.id = 2
        context.bot.send_message.reset_mock()
        await transfers_command(update, context)
        context.bot.send_message.assert_not_awaited()
