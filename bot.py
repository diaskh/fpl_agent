#!/usr/bin/env python3
"""Run: python bot.py [run|backfill|preview GW|status|resolve GW COUNT]."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import dotenv_values
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler

from data import FPL, Store, calculate, due_fixture_groups, connect, latest_completed, latest_available, rows
from formatting import format_messages
from charts import chart_spec, render_chart
from race import race_spec, render_race
from density import render_density
from deadline import build_messages, passed_deadlines
from cup import Cup

LOG = logging.getLogger(__name__)


@dataclass
class Config:
    token: str
    chat_id: int
    admin_id: int
    league_id: int
    api_key: str
    database: str

    @classmethod
    def load(cls, require_secrets=True):
        environment = os.getenv("FPL_ENV", "local")
        if environment not in ("local", "production"):
            raise ValueError("FPL_ENV must be local or production")
        filename = ".env.local" if environment == "local" else ".env"
        values = {**dotenv_values(Path(__file__).with_name(filename)), **os.environ}
        required = ["FPL_LEAGUE_ID"]
        if require_secrets:
            required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ADMIN_TELEGRAM_ID", "OPENAI_API_KEY"]
        missing = [k for k in required if not values.get(k)]
        if missing:
            raise ValueError(f"Missing environment variables in {filename} ({environment}): " + ", ".join(missing))
        default_database = "fpl_local.db" if environment == "local" else "fpl_data.db"
        result = cls(values.get("TELEGRAM_BOT_TOKEN", ""), int(values.get("TELEGRAM_CHAT_ID") or 0),
                     int(values.get("ADMIN_TELEGRAM_ID") or 0), int(values["FPL_LEAGUE_ID"]),
                     values.get("OPENAI_API_KEY", ""), values.get("DATABASE_PATH") or default_database)
        if result.league_id <= 0 or (require_secrets and (result.admin_id <= 0 or result.chat_id == 0)):
            raise ValueError("Invalid league/admin/chat ID")
        return result


class Service:
    def __init__(self, config, db, fpl, llm):
        self.config, self.db, self.fpl, self.llm = config, db, fpl, llm
        self.store = Store(db, fpl, config.league_id)
        self.lock = asyncio.Lock()
        self.cup = Cup(db, fpl, config.league_id, config.chat_id)

    async def set_metadata(self, key, value):
        await self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.db.commit()

    async def broadcast(self, bot, gw, bootstrap, force=False):
        # Caller holds the pipeline lock. Save messages and progress before returning.
        pending = await rows(self.db, "SELECT * FROM reports WHERE gw_id=?", (gw,))
        if pending and pending[0]["sent_count"] == 2:
            if not force:
                return False
            await self.db.execute("DELETE FROM reports WHERE gw_id=?", (gw,))
            await self.db.commit()
            pending = []
        ambiguous = await rows(self.db, "SELECT value FROM metadata WHERE key=?", (f"sending:{gw}",))
        if ambiguous:
            raise RuntimeError(f"GW{gw} has an uncertain Telegram delivery. Inspect chat and use the resolve CLI before retrying.")
        if not pending:
            await self.store.ensure_through(gw, bootstrap)
            stats = await calculate(self.db, gw)
            messages = await format_messages(self.llm, stats)
            await self.db.execute("INSERT INTO reports(gw_id,messages) VALUES (?,?)", (gw, json.dumps(messages, ensure_ascii=False)))
            await self.db.commit()
            sent_count = 0
        else:
            messages = json.loads(pending[0]["messages"])
            sent_count = pending[0]["sent_count"]
        for index in range(sent_count, 2):
            if index:
                await asyncio.sleep(2)
            # A timeout may mean Telegram accepted the message. Do not blindly resend.
            await self.set_metadata(f"sending:{gw}", str(index))
            try:
                await bot.send_message(chat_id=self.config.chat_id, text=messages[index], parse_mode=ParseMode.MARKDOWN_V2)
            except (BadRequest, Forbidden, RetryAfter):
                await self.db.execute("DELETE FROM metadata WHERE key=?", (f"sending:{gw}",))
                await self.db.commit()
                raise
            await self.db.execute("UPDATE reports SET sent_count=?,completed_at=CASE WHEN ?=2 THEN CURRENT_TIMESTAMP ELSE NULL END WHERE gw_id=?", (index + 1, index + 1, gw))
            await self.db.execute("DELETE FROM metadata WHERE key=?", (f"sending:{gw}",))
            await self.db.commit()
        return True

    async def broadcast_fixture_group(self, bot, gw, kickoff, bootstrap):
        """Send one cumulative live report two hours after the group kickoff."""
        pending = await rows(self.db, "SELECT * FROM fixture_reports WHERE gw_id=? AND kickoff_time=?", (gw, kickoff))
        marker = f"sending-fixture:{gw}:{kickoff}"
        ambiguous = await rows(self.db, "SELECT value FROM metadata WHERE key=?", (marker,))
        if ambiguous:
            raise RuntimeError(f"GW{gw} fixture group {kickoff} has an uncertain Telegram delivery")
        if pending and pending[0]["sent_count"] == 2:
            return False
        if not pending:
            await self.store.ensure_through(gw, bootstrap)
            stats = await calculate(self.db, gw)
            stats["fixture_group_label"] = kickoff[11:16] + " UTC"
            messages = await format_messages(self.llm, stats)
            await self.db.execute("INSERT INTO fixture_reports(gw_id,kickoff_time,messages) VALUES (?,?,?)",
                                  (gw, kickoff, json.dumps(messages, ensure_ascii=False)))
            await self.db.commit()
            sent_count = 0
        else:
            messages = json.loads(pending[0]["messages"])
            sent_count = pending[0]["sent_count"]
        for index in range(sent_count, 2):
            if index:
                await asyncio.sleep(2)
            await self.set_metadata(marker, str(index))
            try:
                await bot.send_message(chat_id=self.config.chat_id, text=messages[index], parse_mode=ParseMode.MARKDOWN_V2)
            except (BadRequest, Forbidden, RetryAfter):
                await self.db.execute("DELETE FROM metadata WHERE key=?", (marker,))
                await self.db.commit()
                raise
            await self.db.execute("UPDATE fixture_reports SET sent_count=?,completed_at=CASE WHEN ?=2 THEN CURRENT_TIMESTAMP ELSE NULL END WHERE gw_id=? AND kickoff_time=?",
                                  (index + 1, index + 1, gw, kickoff))
            await self.db.execute("DELETE FROM metadata WHERE key=?", (marker,))
            await self.db.commit()
        return True

    async def poll_deadlines(self, bot, bootstrap):
        passed = passed_deadlines(bootstrap)
        baseline = await rows(self.db, "SELECT value FROM metadata WHERE key='deadline-baseline'")
        if not baseline:
            # Do not replay old deadlines when the feature is first installed.
            await self.set_metadata("deadline-baseline", json.dumps(passed))
            return
        skipped = set(json.loads(baseline[0]["value"]))
        for gw in passed:
            if gw in skipped:
                continue
            pending = await rows(self.db, "SELECT * FROM deadline_reports WHERE gw_id=?", (gw,))
            marker = f"sending-deadline:{gw}"
            if await rows(self.db, "SELECT value FROM metadata WHERE key=?", (marker,)):
                LOG.error("GW%s deadline delivery uncertain; inspect chat and saved deadline_reports before resolving metadata", gw)
                continue
            if not pending:
                messages = await build_messages(self.fpl, self.config.league_id, gw, bootstrap)
                await self.db.execute("INSERT INTO deadline_reports(gw_id,messages) VALUES (?,?)",
                                      (gw, json.dumps(messages, ensure_ascii=False)))
                await self.db.commit()
                count = 0
            else:
                messages = json.loads(pending[0]["messages"])
                count = pending[0]["sent_count"]
            for index in range(count, len(messages)):
                await self.set_metadata(marker, str(index))
                try:
                    await bot.send_message(chat_id=self.config.chat_id, text=messages[index])
                except (BadRequest, Forbidden, RetryAfter):
                    await self.db.execute("DELETE FROM metadata WHERE key=?", (marker,))
                    await self.db.commit()
                    raise
                await self.db.execute("UPDATE deadline_reports SET sent_count=? WHERE gw_id=?", (index + 1, gw))
                await self.db.execute("DELETE FROM metadata WHERE key=?", (marker,))
                await self.db.commit()

    async def poll(self, bot):
        if self.lock.locked():
            return
        async with self.lock:
            try:
                bootstrap = await self.fpl.get("bootstrap-static/")
                # Cup settlement/publication is independent of regular live reports.
                try:
                    await self.cup.advance(bootstrap)
                    await self.cup.deliver(bot)
                except Exception as exc:
                    LOG.error("Cup polling failed (%s)", type(exc).__name__)
                await self.poll_deadlines(bot, bootstrap)
                await self.store.sync_bootstrap(bootstrap)
                fixtures = await self.fpl.get("fixtures/")
                await self.store.sync_fixtures(fixtures)
                available = latest_available(bootstrap)
                if available is None:
                    return
                # Keep the live snapshot fresh even while the current fixture group is playing.
                await self.store.ensure_through(available, bootstrap)
                current_fixtures = [f for f in fixtures if f.get("event") == available]
                due = due_fixture_groups(current_fixtures)
                baseline_key = f"fixture-baseline:{available}"
                baseline = await rows(self.db, "SELECT value FROM metadata WHERE key=?", (baseline_key,))
                if not baseline:
                    # A new deployment never floods the chat with already due groups.
                    await self.set_metadata(baseline_key, json.dumps(due))
                    baseline_groups = set(due)
                else:
                    baseline_groups = set(json.loads(baseline[0]["value"]))
                delivered = {r["kickoff_time"] for r in await rows(
                    self.db, "SELECT kickoff_time FROM fixture_reports WHERE gw_id=? AND sent_count=2", (available,))}
                for kickoff in due:
                    if kickoff not in baseline_groups and kickoff not in delivered:
                        await self.broadcast_fixture_group(bot, available, kickoff, bootstrap)
            except Exception as exc:
                # Avoid logging URLs containing Telegram tokens or response bodies.
                LOG.error("Polling failed (%s). Check connectivity/configuration and pending deliveries.", type(exc).__name__)


async def cup_command(update, context):
    """Public bracket; admin-only creation, with no inline buttons."""
    service = context.application.bot_data["service"]
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not message or not user or user.is_bot or not chat:
        return
    starting = bool(context.args and context.args[0].lower() == "start")
    if starting and user.id != service.config.admin_id:
        return
    if not starting and chat.id != service.config.chat_id:
        return
    if (not starting and context.args) or (starting and len(context.args) > 2):
        await message.reply_text("Использование: /cup · администратору: /cup start [тур]")
        return
    if service.lock.locked():
        await message.reply_text("Данные уже обновляются. Попробуйте через минуту.")
        return
    async with service.lock:
        try:
            if starting:
                try:
                    gw = int(context.args[1]) if len(context.args) == 2 else None
                except ValueError:
                    await message.reply_text("Использование: /cup start [номер тура]")
                    return
                bootstrap = await service.fpl.get("bootstrap-static/")
                await service.store.sync_bootstrap(bootstrap)
                await service.cup.start(bootstrap, gw)
                await service.cup.deliver(context.bot)
            else:
                _, state = await service.cup.latest()
                if not state:
                    await message.reply_text("Кубок ещё не создан. Администратор может запустить его: /cup start [тур].")
                    return
                await service.cup.show(context.bot, state)
        except ValueError as exc:
            await message.reply_text(str(exc))
        except Exception as exc:
            LOG.error("Cup command failed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось отправить кубок. Если жеребьёвка сохранена, она не изменится. Проверьте журнал и cup-status; отправка с неопределённым статусом требует cup-resolve.")


async def report_command(update, context):
    service = context.application.bot_data["service"]
    if update.effective_user is None or update.effective_user.id != service.config.admin_id:
        return
    message = update.effective_message
    try:
        if len(context.args) > 1:
            raise ValueError()
        gw = int(context.args[0]) if context.args else None
        if gw is not None and not 1 <= gw <= 38:
            raise ValueError()
    except ValueError:
        await message.reply_text("Использование: /report [1–38]")
        return
    if service.lock.locked():
        await message.reply_text("Отчёт уже готовится. Повторите запрос после завершения.")
        return
    async with service.lock:
        try:
            bootstrap = await service.fpl.get("bootstrap-static/")
            await service.store.sync_bootstrap(bootstrap)
            await service.store.sync_fixtures(await service.fpl.get("fixtures/"))
            gw = gw or latest_available(bootstrap)
            if gw is None:
                await message.reply_text("Пока нет завершённого тура с проверенными данными.")
                return
            event = next((e for e in bootstrap["events"] if e["id"] == gw), None)
            if not event or gw > (latest_available(bootstrap) or 0):
                await message.reply_text("Этот тур ещё не завершён или его данные ещё не проверены.")
                return
            await message.reply_text(f"Готовлю отчёт за тур {gw} для настроенной группы…")
            if not (event["finished"] and event["data_checked"]):
                await service.store.ensure_through(gw, bootstrap)
                stats = await calculate(service.db, gw)
                for text in await format_messages(service.llm, stats):
                    await context.bot.send_message(chat_id=service.config.chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await service.broadcast(context.bot, gw, bootstrap, force=True)
            await message.reply_text(f"Отчёт за тур {gw} отправлен.")
        except Exception as exc:
            LOG.error("Admin report failed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось отправить отчёт. Проверьте журнал сервиса и состояние отправки; при неопределённом статусе используйте команду resolve в CLI.")


async def transfers_command(update, context):
    """Admin preview of a past deadline report in the configured Telegram chat."""
    service = context.application.bot_data["service"]
    if update.effective_user is None or update.effective_user.id != service.config.admin_id:
        return
    message = update.effective_message
    if not message:
        return
    try:
        if len(context.args) > 1:
            raise ValueError()
        gw = int(context.args[0]) if context.args else None
        if gw is not None and not 1 <= gw <= 38:
            raise ValueError()
    except ValueError:
        await message.reply_text("Использование: /transfers [1–38]")
        return
    if service.lock.locked():
        await message.reply_text("Данные уже обновляются. Попробуйте ещё раз через минуту.")
        return
    async with service.lock:
        try:
            bootstrap = await service.fpl.get("bootstrap-static/")
            gw = gw if gw is not None else latest_completed(bootstrap)
            if gw is None or gw not in passed_deadlines(bootstrap):
                await message.reply_text("Выберите тур, дедлайн которого уже прошёл.")
                return
            messages = await build_messages(service.fpl, service.config.league_id, gw, bootstrap)
            for text in messages:
                await context.bot.send_message(chat_id=service.config.chat_id, text=text)
            if update.effective_chat and update.effective_chat.id != service.config.chat_id:
                await message.reply_text(f"Трансферы за GW{gw} отправлены в настроенный чат.")
        except Exception as exc:
            LOG.error("Transfer preview failed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось отправить трансферы. Данные FPL могут обновляться; попробуйте позже.")


async def graph_command(update, context):
    """Refresh league data and reply with a sourced editorial chart."""
    service = context.application.bot_data["service"]
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not message or not user or user.is_bot or not chat or chat.id != service.config.chat_id:
        return
    if len(context.args) > 1:
        await message.reply_text("Использование: /graph [density|history]")
        return
    choice = context.args[0].lower() if context.args else "density"
    if choice not in ("density", "latest", "history"):
        await message.reply_text("Использование: /graph [density|history]")
        return
    if service.lock.locked():
        await message.reply_text("Данные уже обновляются. Попробуйте ещё раз через минуту.")
        return
    async with service.lock:
        try:
            bootstrap = await service.fpl.get("bootstrap-static/")
            await service.store.sync_bootstrap(bootstrap)
            await service.store.sync_fixtures(await service.fpl.get("fixtures/"))
            current = latest_available(bootstrap)
            gw = current
            if gw is None:
                await message.reply_text("Для этого тура ещё нет доступных данных.")
                return
            if choice in ("density", "latest"):
                # Official Overall (phase 1) table: cumulative total, never event_total.
                members = await service.fpl.members(service.config.league_id)
                totals = [m["total"] for m in members]
                if not totals:
                    await message.reply_text("В общей таблице пока нет команд.")
                    return
                if any(type(v) is not int for v in totals):
                    raise ValueError("Invalid overall standings total")
                synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                event = next(e for e in bootstrap["events"] if e["id"] == gw)
                provisional = not event.get("data_checked", False)
                photo = await asyncio.to_thread(render_density, totals, synced_at, provisional)
                await message.reply_photo(photo=photo, caption="Плотность общей таблицы мини-лиги · суммарные очки за сезон.")
                return
            await service.store.ensure_through(gw, bootstrap)
            specs = [await chart_spec(service.db, gw, choice, scope) for scope in ("top10", "all")]
            photos = [await asyncio.to_thread(render_chart, spec) for spec in specs]
            state = ("предварительный срез — цифры могут измениться"
                     if specs[0]["provisional"] else "данные подтверждены FPL")
            labels = ("Топ-10", "Вся мини-лига")
            for photo, label in zip(photos, labels):
                await message.reply_photo(photo=photo, caption=f"{label} • GW{gw}: {state}.")
        except Exception as exc:
            LOG.error("Graph generation failed (%s)", type(exc).__name__)
            await message.reply_text("Не удалось построить график по проверенным данным. Попробуйте позже.")


async def race_command(update, context):
    """Refresh the complete league history and send a cumulative-points MP4 race."""
    service = context.application.bot_data["service"]
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not message or not user or user.is_bot or not chat or chat.id != service.config.chat_id:
        return
    if context.args:
        await message.reply_text("Использование: /race")
        return
    if service.lock.locked():
        await message.reply_text("Данные уже обновляются. Попробуйте ещё раз через минуту.")
        return
    await message.reply_text("Готовлю гонку всей мини-лиги — это может занять около минуты…")
    try:
        async with service.lock:
            bootstrap = await service.fpl.get("bootstrap-static/")
            await service.store.sync_bootstrap(bootstrap)
            await service.store.sync_fixtures(await service.fpl.get("fixtures/"))
            gw = latest_available(bootstrap)
            if gw is None:
                await message.reply_text("Пока недостаточно данных для гонки.")
                return
            await service.store.ensure_through(gw, bootstrap)
            spec = await race_spec(service.db, gw)
        video = await asyncio.to_thread(render_race, spec)
        state = "предварительные данные" if spec["provisional"] else "подтверждённые данные"
        await message.reply_video(video=video, supports_streaming=True,
                                  caption=f"Вся мини-лига • GW1–GW{gw} • {state}. Очки нарастающим итогом; поздние участники появляются с первого сохранённого тура.")
    except Exception as exc:
        LOG.exception("Bar chart race generation failed (%s)", type(exc).__name__)
        await message.reply_text("Не удалось создать гонку. Проверьте bar-chart-race и FFmpeg в окружении бота.")


async def post_init(app):
    config = app.bot_data["config"]
    db = await connect(config.database)
    fpl = FPL()
    llm = AsyncOpenAI(api_key=config.api_key, timeout=45, max_retries=2)
    service = Service(config, db, fpl, llm)
    app.bot_data["service"] = service
    # Do not block startup on unavailable FPL; the first poll fills GW1/GW2 and all gaps.
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(service.poll, "interval", minutes=15, args=[app.bot], max_instances=1,
                      coalesce=True, misfire_grace_time=120, id="fpl-poll", next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    app.bot_data["scheduler"] = scheduler


async def post_stop(app):
    app.bot_data["scheduler"].shutdown(wait=False)
    # Acquire after active reporting exits/cancels so resources close in order.
    async with app.bot_data["service"].lock:
        pass


async def post_shutdown(app):
    service = app.bot_data.get("service")
    if service:
        await service.llm.close()
        await service.fpl.client.aclose()
        await service.db.close()


async def deadline_preview(config, gw=None):
    """Print real deadline messages without Telegram or database access."""
    fpl = FPL()
    try:
        bootstrap = await fpl.get("bootstrap-static/")
        gw = gw if gw is not None else latest_completed(bootstrap)
        if gw is None or gw not in passed_deadlines(bootstrap):
            raise ValueError("Choose a gameweek whose deadline has passed")
        event = next(e for e in bootstrap["events"] if e["id"] == gw)
        messages = await build_messages(fpl, config.league_id, gw, bootstrap)
        print(f"GW{gw} deadline: {event['deadline_time']} (UTC). Deadline passed.")
        print("Preview only: no Telegram messages sent; no database changes.")
        for index, message in enumerate(messages, 1):
            print(f"\n--- Message {index}/{len(messages)} ---\n{message}")
    finally:
        await fpl.client.aclose()


async def cli(args, config):
    db = await connect(config.database)
    fpl = FPL()
    try:
        if args.command == "cup-status":
            print(json.dumps(await rows(db, "SELECT cup_id,stage,sent_count,uncertain FROM cup_posts ORDER BY cup_id,stage"), indent=2))
            return
        if args.command == "cup-resolve":
            post = await rows(db, "SELECT * FROM cup_posts WHERE cup_id=? AND stage=?", (args.cup_id, args.stage))
            if not post or not post[0]["uncertain"]:
                raise ValueError("No uncertain cup delivery exists for this post")
            if args.count not in (post[0]["sent_count"], post[0]["sent_count"] + 1):
                raise ValueError("Count must equal confirmed count or confirmed count plus one")
            await db.execute("UPDATE cup_posts SET sent_count=?,uncertain=0 WHERE cup_id=? AND stage=?", (args.count, args.cup_id, args.stage))
            await db.commit()
            print("Cup delivery resolved; restart the service to resume.")
            return
        if args.command == "status":
            print(json.dumps({"reports": await rows(db, "SELECT gw_id,sent_count,completed_at FROM reports ORDER BY gw_id"), "uncertain": await rows(db, "SELECT key,value FROM metadata WHERE key LIKE 'sending:%'")}, indent=2))
            return
        if args.command == "resolve":
            report = await rows(db, "SELECT sent_count FROM reports WHERE gw_id=?", (args.gw,))
            marker = await rows(db, "SELECT value FROM metadata WHERE key=?", (f"sending:{args.gw}",))
            if not report or not marker:
                raise ValueError("No uncertain delivery exists for this gameweek")
            previous = report[0]["sent_count"]
            if args.count not in (previous, previous + 1):
                raise ValueError("Count must equal previously confirmed count or that count plus one")
            await db.execute("UPDATE reports SET sent_count=?,completed_at=CASE WHEN ?=2 THEN CURRENT_TIMESTAMP ELSE NULL END WHERE gw_id=?", (args.count, args.count, args.gw))
            await db.execute("DELETE FROM metadata WHERE key=?", (f"sending:{args.gw}",))
            await db.commit()
            print("Delivery state resolved; restart the service to resume.")
            return
        bootstrap = await fpl.get("bootstrap-static/")
        store = Store(db, fpl, config.league_id)
        await store.sync_bootstrap(bootstrap)
        await store.sync_fixtures(await fpl.get("fixtures/"))
        target = latest_available(bootstrap) if args.command == "sync" else (2 if args.command == "backfill" else args.gw)
        await store.ensure_through(target, bootstrap)
        if args.command == "preview":
            print(json.dumps(await calculate(db, target), ensure_ascii=False, indent=2))
        else:
            print(f"Gameweeks through GW{target} synced. No messages sent.")
    finally:
        await fpl.client.aclose()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    sub.add_parser("backfill")
    sub.add_parser("sync")
    sub.add_parser("status")
    sub.add_parser("cup-status", help="Show cup publication progress and uncertain sends")
    cup_resolve = sub.add_parser("cup-resolve", help="Resolve an uncertain cup send after inspecting the chat")
    cup_resolve.add_argument("cup_id", type=int)
    cup_resolve.add_argument("stage", type=int, help="-1 for draw, 0 for first round, etc.")
    cup_resolve.add_argument("count", type=int, help="Total items visibly delivered for this post")
    preview = sub.add_parser("preview")
    preview.add_argument("gw", type=int, choices=range(1, 39))
    deadline = sub.add_parser("deadline-preview", help="Print the post-deadline transfer report without sending")
    deadline.add_argument("gw", nargs="?", type=int, choices=range(1, 39), help="Default: latest completed gameweek")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("gw", type=int, choices=range(1, 39))
    resolve.add_argument("count", type=int, choices=range(3), help="Messages visibly delivered (0, 1, or 2)")
    args = parser.parse_args()
    args.command = args.command or "run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = Config.load(require_secrets=args.command == "run")
    if args.command == "deadline-preview":
        asyncio.run(deadline_preview(config, args.gw))
        return
    # VPS/Linux and macOS: hold an advisory lock for the full process lifetime.
    with open(config.database + ".lock", "a") as process_lock:
        try:
            fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another bot/CLI process is using this database. Stop it first.\n")
        if args.command != "run":
            asyncio.run(cli(args, config))
            return
        app = (Application.builder().token(config.token).post_init(post_init)
               .post_stop(post_stop).post_shutdown(post_shutdown).build())
        app.bot_data["config"] = config
        app.add_handler(CommandHandler("report", report_command))
        app.add_handler(CommandHandler("transfers", transfers_command, block=False))
        app.add_handler(CommandHandler("graph", graph_command, block=False))
        app.add_handler(CommandHandler("race", race_command, block=False))
        app.add_handler(CommandHandler("cup", cup_command, block=False))
        app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
