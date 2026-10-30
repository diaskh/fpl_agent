import asyncio
import copy
import json
import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import Forbidden, TimedOut

from bot import Config, Service, graph_command, race_command, report_command
from charts import chart_spec
from form import form_spec, render_form
from race import race_spec
from data import FPL, Store, calculate, due_fixture_groups, connect, historical_ranks, latest_completed, month_phase, rows
from formatting import blocks, escaped, format_messages, render


def fixture():
    bootstrap = {
        "events": [{"id": g, "name": f"GW{g}", "finished": True, "data_checked": True, "deadline_time": f"2026-08-{g:02}T00:00:00Z"} for g in range(1, 4)],
        "phases": [{"id": 1, "name": "Overall", "start_event": 1, "stop_event": 3},
                   {"id": 2, "name": "August", "start_event": 1, "stop_event": 2},
                   {"id": 3, "name": "September", "start_event": 3, "stop_event": 3}],
        "elements": [{"id": p, "web_name": f"Player_{p}"} for p in range(1, 16)]}
    bootstrap.update(teams=[{"id": 1, "name": "Club"}], chips=[], element_types=[], element_stats=[], game_settings={}, game_config={}, total_players=2)
    members = [{"entry": e, "entry_name": f"Team_{e}!*", "player_name": f"Manager {e}"} for e in (1, 2)]
    histories = {1: [{"event": 1, "points": 60, "total_points": 60}, {"event": 2, "points": 20, "total_points": 80}, {"event": 3, "points": 999, "total_points": 879}],
                 2: [{"event": 1, "points": 40, "total_points": 40}, {"event": 2, "points": 50, "total_points": 90}, {"event": 3, "points": 1, "total_points": 91}]}
    picks = {"picks": [{"element": p, "position": p, "is_captain": p == 1, "multiplier": 2 if p == 1 else (1 if p < 12 else 0)} for p in range(1, 16)],
             "automatic_subs": [{"element_in": 12, "element_out": 2}]}
    live = {"elements": [{"id": p, "stats": {"total_points": {1: 5, 12: 10, 13: 2, 14: -1, 15: 3}.get(p, 0)}} for p in range(1, 16)]}
    return bootstrap, members, histories, picks, live


class DataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = await connect(str(Path(self.temp.name) / "test.db"))
        self.bootstrap, self.members, self.histories, self.picks, self.live = fixture()
        self.fpl = SimpleNamespace(get=AsyncMock(side_effect=self.get), members=AsyncMock(return_value=self.members))
        self.store = Store(self.db, self.fpl, 836115)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def get(self, path):
        if path == "bootstrap-static/":
            return self.bootstrap
        if path.endswith("/history/"):
            return {"current": self.histories[int(path.split("/")[1])], "chips": []}
        if path.endswith("/picks/"):
            result = copy.deepcopy(self.picks)
            if path.startswith("entry/2/"):
                result["picks"][0]["multiplier"] = 3
                result["picks"][0]["is_captain"] = False  # Acting vice captain.
            return result
        if path.startswith("fixtures/"):
            return []
        return self.live

    async def seed(self):
        await self.store.ensure_through(2, self.bootstrap)

    async def make_live(self):
        self.bootstrap["events"][2].update(finished=False, data_checked=False, is_current=True)
        self.picks["entry_history"] = {"event": 3, "points": 999, "total_points": 999,
                                       "event_transfers_cost": 4, "event_transfers": 1}
        await self.store.ensure_through(3, self.bootstrap)

    async def test_live_scores_and_provisional_status(self):
        await self.make_live()
        stats = await calculate(self.db, 3)
        self.assertTrue(stats["provisional"])
        self.assertEqual([r["points"] for r in stats["top_scorers"]], [11, 6])
        totals = await rows(self.db, "SELECT total_points FROM standings WHERE gw_id=3 ORDER BY entry_id")
        self.assertEqual([r["total_points"] for r in totals], [86, 101])
        self.assertIn("Предварительно", blocks(stats)[0]["GW_TITLE"])
        status = await rows(self.db, "SELECT provisional FROM result_status WHERE gw_id=3")
        self.assertEqual(status[0]["provisional"], 1)
        self.assertEqual(len(await rows(self.db, "SELECT * FROM picks WHERE gw_id=3")), 30)

    async def test_fixture_snapshot_stores_scores_and_status(self):
        await self.store.sync_bootstrap(self.bootstrap)
        await self.store.sync_fixtures([{
            'id': 10, 'event': 2, 'kickoff_time': '2026-08-02T14:00:00Z',
            'team_h': 1, 'team_a': 1, 'team_h_score': 3, 'team_a_score': 1,
            'started': True, 'finished': True, 'finished_provisional': False, 'minutes': 90,
        }])
        fixture_rows = await rows(self.db, 'SELECT * FROM fixtures')
        self.assertEqual((fixture_rows[0]['team_h_score'], fixture_rows[0]['finished']), (3, 1))

    async def test_live_refresh_failure_and_finalization(self):
        await self.make_live()
        self.live["elements"][0]["stats"]["total_points"] = 8
        await self.store.ensure_through(3, self.bootstrap)
        self.assertEqual((await calculate(self.db, 3))["top_scorers"][0]["points"], 20)
        original = self.live["elements"]
        self.live["elements"] = []
        with self.assertRaises(ValueError):
            await self.store.ensure_through(3, self.bootstrap)
        self.assertEqual((await calculate(self.db, 3))["top_scorers"][0]["points"], 20)
        self.live["elements"] = original
        self.bootstrap["events"][2].update(finished=True, data_checked=True)
        await self.store.ensure_through(3, self.bootstrap)
        stats = await calculate(self.db, 3)
        self.assertFalse(stats["provisional"])
        self.assertEqual(stats["top_scorers"][0]["points"], 999)

    async def test_default_report_live_does_not_mark_final_delivered(self):
        await self.make_live()
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), args=[], bot=bot)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(reply_text=AsyncMock()))
        await report_command(update, context)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIn("Предварительно", bot.send_message.call_args_list[0].kwargs["text"])
        self.assertEqual(await rows(self.db, "SELECT * FROM reports"), [])

    async def test_live_poll_refreshes_without_live_broadcast(self):
        await self.make_live()
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        service.broadcast = AsyncMock()
        self.live["elements"][0]["stats"]["total_points"] = 9
        await service.poll(None)
        self.assertEqual((await calculate(self.db, 3))["top_scorers"][0]["points"], 23)
        service.broadcast.assert_not_awaited()

    async def test_exact_metrics_and_as_of_month(self):
        await self.seed()
        await self.store.sync_gameweek(3)
        stats = await calculate(self.db, 2)
        self.assertEqual([r["points"] for r in stats["top_scorers"]], [50, 20])
        self.assertEqual([r["points"] for r in stats["bench_warmers"]], [4, 4])
        self.assertEqual(stats["best_captain"][0]["points"], 15)
        self.assertEqual(stats["highest_climb"][0]["entry_id"], 2)
        self.assertEqual(stats["highest_drop"][0]["movement"], 1)
        self.assertEqual(stats["manager_of_month"][0]["points"], 90)
        self.assertEqual(stats["season_bench_warmer"][0]["points"], 8)
        self.assertEqual(stats["season_best_captain"][0]["points"], 30)
        first = await calculate(self.db, 1)
        self.assertEqual(first["highest_climb"], [])
        self.assertEqual(first["manager_of_month"][0]["points"], 60)

    async def test_cumulative_sections_show_three_ranked_managers(self):
        # Four tied managers exercise the top-three cutoff and stable tie order.
        self.members[:] = [{"entry": e, "entry_name": f"Team {e}", "player_name": f"Manager {e}"}
                           for e in (4, 3, 2, 1)]
        for entry in (2, 3, 4):
            self.histories[entry] = copy.deepcopy(self.histories[1])
        await self.seed()
        await self.db.execute("UPDATE picks SET multiplier=2 WHERE element_id=1")
        await self.db.commit()
        stats = await calculate(self.db, 2)
        sections = blocks(stats)[1]
        for key, block in (("manager_of_month", "MONTH"),
                           ("season_bench_warmer", "SEASON_BENCH"),
                           ("season_best_captain", "SEASON_CAPTAIN")):
            self.assertEqual([r["entry_id"] for r in stats[key]], [1, 2, 3])
            for place, entry in enumerate((1, 2, 3), 1):
                self.assertIn(f"{place}. Team {entry} (Manager {entry})", sections[block])
            self.assertNotIn("Team 4", sections[block])
        messages = await format_messages(None, stats)
        self.assertEqual(len(messages), 2)
        self.assertIn("Team 3", messages[1])

    async def test_bench_boost_points_are_not_unused_bench_points(self):
        await self.seed()
        await self.db.execute("UPDATE picks SET multiplier=1 WHERE entry_id=1 AND gw_id=2 AND position>=12")
        await self.db.commit()
        stats = await calculate(self.db, 2)
        self.assertEqual([(r["entry_id"], r["points"]) for r in stats["bench_warmers"]], [(2, 4), (1, 0)])
        self.assertEqual(stats["season_bench_warmer"][0]["entry_id"], 2)
        self.assertEqual(stats["season_bench_warmer"][0]["points"], 8)

    async def test_top_ten_and_all_weekly_award_ties(self):
        await self.seed()
        # Add enough managers to exercise the top-ten cutoff and tied awards.
        for entry in range(3, 13):
            await self.db.execute("INSERT INTO managers VALUES (?,?,?)", (entry, f"Team {entry}", f"Manager {entry}"))
            await self.db.execute("INSERT INTO standings VALUES (?,2,7,14,100,?)", (entry, 100-entry))
        await self.db.execute("UPDATE standings SET rank=7,last_rank=14 WHERE gw_id=2 AND entry_id=1")
        await self.db.execute("UPDATE standings SET rank=16,last_rank=10 WHERE gw_id=2 AND entry_id IN (2,3)")
        await self.db.execute("UPDATE picks SET multiplier=3 WHERE gw_id=2 AND element_id=1")
        await self.db.commit()
        stats = await calculate(self.db, 2)
        self.assertEqual(len(stats["top_scorers"]), 10)
        self.assertEqual([r["entry_id"] for r in stats["top_scorers"]], list(range(3, 13)))
        self.assertEqual([r["entry_id"] for r in stats["best_captain"]], [1, 2])
        self.assertEqual([r["entry_id"] for r in stats["highest_climb"]], [1] + list(range(4, 13)))
        self.assertEqual([r["entry_id"] for r in stats["highest_drop"]], [2, 3])
        sections = blocks(stats)
        for key, field in (("CAPTAIN", "best_captain"), ("CLIMB", "highest_climb"), ("DROP", "highest_drop")):
            for row in stats[field]:
                self.assertIn(row["entry_name"] + " (" + row["player_name"] + ")", sections[0][key])
        await self.db.execute("UPDATE standings SET last_rank=rank WHERE gw_id=2")
        await self.db.execute("UPDATE picks SET multiplier=1 WHERE gw_id=2")
        empty = await calculate(self.db, 2)
        for field in ("best_captain", "highest_climb", "highest_drop"):
            self.assertEqual(empty[field], [])

    async def test_resync_replaces_autosubs_and_does_not_duplicate(self):
        await self.seed()
        self.picks["automatic_subs"] = []
        await self.store.sync_gameweek(2)
        stats = await calculate(self.db, 2)
        self.assertEqual(stats["bench_warmers"][0]["points"], 14)
        self.assertEqual((await rows(self.db, "SELECT COUNT(*) AS n FROM picks"))[0]["n"], 60)

    async def test_failed_snapshot_preserves_existing_data(self):
        await self.seed()
        self.live["elements"] = []
        with self.assertRaises(ValueError):
            await self.store.sync_gameweek(2)
        self.assertEqual((await calculate(self.db, 2))["season_best_captain"][0]["points"], 30)

    async def test_incomplete_cumulative_data_rejected(self):
        await self.store.sync_gameweek(2)
        with self.assertRaises(ValueError):
            await calculate(self.db, 2)

    async def test_league_and_season_guard(self):
        await self.seed()
        with self.assertRaises(ValueError):
            await Store(self.db, self.fpl, 99).prepare(self.bootstrap)

    async def test_formatter_validation_and_escaping(self):
        await self.seed()
        stats = await calculate(self.db, 2)
        sections = blocks(stats)
        raw = json.dumps(["\n\n".join(f"@@{k}@@" for k in s) for s in sections])
        result = render(raw, sections)
        self.assertIn(r"Team\_2\!\*", result[0])
        self.assertIn("90", result[1])
        for bad in ['["a"]', raw.replace("@@BENCH@@", "999"), raw.replace("@@MONTH@@", "@@SCORERS@@")]:
            with self.assertRaises(ValueError):
                render(bad, sections)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError()))))
        self.assertEqual(await format_messages(client, stats), result)

    async def test_delivery_resume_and_dedup(self):
        await self.db.execute("INSERT INTO reports(gw_id,messages) VALUES (2,?)", (json.dumps(["one", "two"]),))
        await self.db.commit()
        config = Config("", -100, 1, 836115, "", "")
        service = Service(config, self.db, self.fpl, None)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, Forbidden("blocked")]))
        with patch("bot.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(Forbidden):
                await service.broadcast(bot, 2, self.bootstrap)
            self.assertEqual((await rows(self.db, "SELECT sent_count FROM reports"))[0]["sent_count"], 1)
            bot.send_message = AsyncMock()
            await service.broadcast(bot, 2, self.bootstrap)
            self.assertEqual(bot.send_message.call_count, 1)
            self.assertEqual(bot.send_message.call_args.kwargs["text"], "two")
            self.assertFalse(await service.broadcast(bot, 2, self.bootstrap))
            self.assertEqual(bot.send_message.call_count, 1)

    async def test_ambiguous_delivery_is_not_retried(self):
        await self.db.execute("INSERT INTO reports(gw_id,messages) VALUES (2,?)", (json.dumps(["one", "two"]),))
        await self.db.commit()
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
        with self.assertRaises(TimedOut):
            await service.broadcast(bot, 2, self.bootstrap)
        with self.assertRaises(RuntimeError):
            await service.broadcast(bot, 2, self.bootstrap)
        self.assertEqual(bot.send_message.call_count, 1)

    async def test_database_failure_rolls_back_whole_snapshot(self):
        await self.seed()
        # The unknown live player triggers a foreign-key error after DELETE/INSERT work.
        self.live["elements"].append({"id": 999, "stats": {"total_points": 9}})
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.sync_gameweek(2)
        self.assertEqual((await calculate(self.db, 2))["season_best_captain"][0]["points"], 30)
        self.assertEqual((await rows(self.db, "SELECT COUNT(*) AS n FROM picks"))[0]["n"], 60)

    async def test_poll_sends_two_hours_after_kickoff_regardless_of_status(self):
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        self.bootstrap["events"][2].update(finished=False, data_checked=False, is_current=True)
        self.picks["entry_history"] = {"event": 3, "points": 6, "total_points": 86,
                                       "event_transfers_cost": 4, "event_transfers": 1}
        fixtures = [
            {"id": 1, "event": 3, "team_h": 1, "team_a": 1, "kickoff_time": "2026-09-19T16:30:00Z", "finished": True},
            {"id": 2, "event": 3, "team_h": 1, "team_a": 1, "kickoff_time": "2026-09-19T19:00:00Z", "finished": False},
            {"id": 3, "event": 3, "team_h": 1, "team_a": 1, "kickoff_time": "2026-09-19T19:00:00Z", "finished": False},
        ]
        original_get = self.get
        async def get_with_fixtures(path, **kwargs):
            return fixtures if path.startswith("fixtures/") else await original_get(path)
        self.fpl.get = AsyncMock(side_effect=get_with_fixtures)
        service.fpl = self.fpl
        service.store.fpl = self.fpl
        service.broadcast_fixture_group = AsyncMock()
        # First poll establishes a baseline and does not replay completed groups.
        with patch("bot.due_fixture_groups", side_effect=lambda games: due_fixture_groups(
                games, datetime(2026, 9, 19, 20, 59, 59, tzinfo=timezone.utc))):
            await service.poll(None)
            service.broadcast_fixture_group.assert_not_awaited()
            fixtures[1]["finished"] = True
            await service.poll(None)
            service.broadcast_fixture_group.assert_not_awaited()
        with patch("bot.due_fixture_groups", side_effect=lambda games: due_fixture_groups(
                games, datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc))):
            await service.poll(None)
        service.broadcast_fixture_group.assert_awaited_once_with(None, 3, "2026-09-19T19:00:00Z", self.bootstrap)

    async def test_membership_change_invalidates_later_snapshots(self):
        await self.store.ensure_through(3, self.bootstrap)
        self.members.pop()
        await self.store.ensure_through(2, self.bootstrap)
        self.assertEqual([r["gw_id"] for r in await rows(self.db, "SELECT gw_id FROM sync_runs ORDER BY gw_id")], [1, 2])

    async def test_authorized_explicit_report_targets_group(self):
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        service.broadcast = AsyncMock()
        bot = object()
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), args=["2"], bot=bot)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=SimpleNamespace(reply_text=AsyncMock()))
        await report_command(update, context)
        service.broadcast.assert_awaited_once_with(bot, 2, self.bootstrap, force=True)

    async def test_graph_command_refreshes_and_sends_latest_chart(self):
        await self.seed()
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2, is_bot=False),
                                 effective_chat=SimpleNamespace(id=-100), effective_message=message)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), args=[])
        self.fpl.members.return_value = [dict(m, total=163-i, event_total=999) for i,m in enumerate(self.members)]
        with patch("bot.render_density", return_value=SimpleNamespace(name="chart.png")) as renderer:
            await graph_command(update, context)
        self.assertEqual(message.reply_photo.await_count, 1)
        self.assertEqual(renderer.call_args.args[0], [163, 162])
        self.assertIn("общей таблицы", message.reply_photo.call_args.kwargs["caption"])

    async def test_form_uses_stored_scores_and_same_cohort_means(self):
        await self.store.ensure_through(3, self.bootstrap)
        await self.db.execute("UPDATE standings SET total_points=200,event_points=30 WHERE entry_id=1 AND gw_id=3")
        await self.db.execute("UPDATE standings SET total_points=100,event_points=10 WHERE entry_id=2 AND gw_id=3")
        await self.db.execute("INSERT OR REPLACE INTO result_status VALUES (2,1)")
        await self.db.commit()
        spec = await form_spec(self.db, 3)
        self.assertEqual(spec["season_mean"], 150)
        self.assertEqual([t["recent"] for t in spec["rows"]], [110, 100])
        self.assertEqual(spec["recent_mean"], 105)
        self.assertTrue(spec["provisional"])
        from PIL import Image
        photo = render_form(spec)
        self.assertEqual(photo.name, "elimai-league-form-gw3.png")
        self.assertEqual(Image.open(photo).size, (2200, 1640))
        await self.db.execute("DELETE FROM standings WHERE entry_id=2 AND gw_id=2")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            await form_spec(self.db, 3)

    async def test_form_requires_three_synced_gameweeks(self):
        await self.seed()
        with self.assertRaisesRegex(ValueError, "three gameweeks"):
            await form_spec(self.db, 2)
        with self.assertRaisesRegex(ValueError, "not synced"):
            await form_spec(self.db, 4)

    async def test_graph_form_refreshes_database_and_sends_one_image(self):
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2, is_bot=False),
                                 effective_chat=SimpleNamespace(id=-100), effective_message=message)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), args=["form"])
        with patch("bot.render_form", return_value=SimpleNamespace(name="form.png")) as renderer:
            await graph_command(update, context)
        message.reply_text.assert_not_awaited()
        message.reply_photo.assert_awaited_once()
        spec = renderer.call_args.args[0]
        self.assertEqual(spec["gw"], 3)
        self.assertEqual(len(spec["rows"]), 2)
        self.assertIn("Сила сезона", message.reply_photo.call_args.kwargs["caption"])
        self.assertEqual(len(await rows(self.db, "SELECT * FROM sync_runs")), 3)

    async def test_chart_spec_supports_top_ten_and_all_scopes(self):
        await self.store.ensure_through(3, self.bootstrap)
        latest = await chart_spec(self.db, 2, "latest", "top10")
        self.assertEqual(latest["kind"], "latest")
        self.assertEqual(latest["median"], 35)
        historical = await chart_spec(self.db, 3, "history", "all")
        self.assertEqual(historical["kind"], "history")
        self.assertEqual({r["entry_name"] for r in historical["rows"]}, {"Team_1!*", "Team_2!*"})

    async def test_race_command_sends_whole_league_video(self):
        await self.seed()
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        message = SimpleNamespace(reply_text=AsyncMock(), reply_video=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=2, is_bot=False),
                                 effective_chat=SimpleNamespace(id=-100), effective_message=message)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), args=[])
        with patch("bot.render_race", return_value=SimpleNamespace(name="race.mp4")):
            await race_command(update, context)
        message.reply_video.assert_awaited_once()
        sent = message.reply_video.call_args.kwargs
        self.assertTrue(sent["supports_streaming"])
        self.assertIn("Вся мини-лига", sent["caption"])

    async def test_race_spec_contains_every_manager_and_week(self):
        await self.seed()
        spec = await race_spec(self.db, 2)
        self.assertEqual(len(spec["rows"]), 4)
        self.assertEqual(set(spec["labels"]), {1, 2})

    async def test_race_spec_allows_late_manager_without_inventing_zeros(self):
        await self.seed()
        await self.db.execute("INSERT INTO managers VALUES (3,'Late Team','Late Manager')")
        await self.db.execute("INSERT INTO standings VALUES (3,2,3,NULL,25,25)")
        await self.db.commit()
        spec = await race_spec(self.db, 2)
        late = [r for r in spec["rows"] if r["entry_id"] == 3]
        self.assertEqual([(r["gw_id"], r["total_points"]) for r in late], [(2, 25)])

    async def test_unauthorized_command_is_silent(self):
        service = Service(Config("", -100, 1, 836115, "", ""), self.db, self.fpl, None)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=SimpleNamespace(reply_text=AsyncMock()))
        await report_command(update, context)
        update.effective_message.reply_text.assert_not_called()
        self.fpl.get.assert_not_called()


class PureTests(unittest.TestCase):
    def test_due_fixture_groups_use_time_and_ignore_status(self):
        fixtures = [{"kickoff_time": "2026-09-19T16:30:00Z", "finished": False},
                    {"kickoff_time": "2026-09-19T19:00:00Z", "finished": True},
                    {"kickoff_time": "2026-09-19T19:00:00Z", "finished": False},
                    {"kickoff_time": None, "finished": True}]
        before = datetime(2026, 9, 19, 20, 59, 59, tzinfo=timezone.utc)
        boundary = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(due_fixture_groups(fixtures, before), ["2026-09-19T16:30:00Z"])
        self.assertEqual(due_fixture_groups(fixtures, boundary),
                         ["2026-09-19T16:30:00Z", "2026-09-19T19:00:00Z"])
        self.assertEqual(due_fixture_groups([], boundary), [])

    def test_transfer_tiebreak(self):
        history = {1: [{"event": 1, "total_points": 50, "rank_transfers": 2}],
                   2: [{"event": 1, "total_points": 50, "rank_transfers": 0}],
                   3: [{"event": 1, "total_points": 40, "rank_transfers": 0}]}
        self.assertEqual(historical_ranks(history, 1), {2: 1, 1: 2, 3: 3})

    def test_phases_and_completion(self):
        bootstrap, *_ = fixture()
        self.assertEqual(month_phase(bootstrap["phases"], 2)["id"], 2)
        bootstrap["events"][2]["data_checked"] = False
        self.assertEqual(latest_completed(bootstrap), 2)

    def test_historical_ties_and_late_entry(self):
        history = {1: [{"event": 1, "total_points": 50}], 2: [{"event": 1, "total_points": 50}], 3: [{"event": 2, "total_points": 20}]}
        self.assertEqual(historical_ranks(history, 1), {1: 1, 2: 1})


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_and_retry(self):
        fpl = FPL()
        await fpl.client.aclose()
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503)
            self.assertEqual(request.url.params["phase"], "1")
            page = int(request.url.params["page_standings"])
            return httpx.Response(200, json={"league": {"start_event": 1}, "standings": {"results": [{"entry": page}], "has_next": page == 1}})
        fpl.client = httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler))
        try:
            with patch("data.asyncio.sleep", new_callable=AsyncMock):
                self.assertEqual(await fpl.members(1), [{"entry": 1}, {"entry": 2}])
            self.assertEqual(len(calls), 3)
        finally:
            await fpl.client.aclose()


if __name__ == "__main__":
    unittest.main()
