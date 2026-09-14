#!/usr/bin/env python3
"""Run: python bot.py [run|backfill|preview GW|status|resolve GW COUNT]."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from data import FPL, Store, calculate, connect, latest_completed, latest_available, rows
from formatting import format_messages
from questions import answer, error_text

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
        load_dotenv(Path(__file__).with_name(".env"))
        required = ["FPL_LEAGUE_ID"]
        if require_secrets:
            required += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ADMIN_TELEGRAM_ID", "OPENAI_API_KEY"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        result = cls(os.getenv("TELEGRAM_BOT_TOKEN", ""), int(os.getenv("TELEGRAM_CHAT_ID") or 0),
                     int(os.getenv("ADMIN_TELEGRAM_ID") or 0), int(os.environ["FPL_LEAGUE_ID"]),
                     os.getenv("OPENAI_API_KEY", ""), os.getenv("DATABASE_PATH", "fpl_data.db"))
        if result.league_id <= 0 or (require_secrets and (result.admin_id <= 0 or result.chat_id == 0)):
            raise ValueError("Invalid league/admin/chat ID")
        return result


class Service:
    def __init__(self, config, db, fpl, llm):
        self.config, self.db, self.fpl, self.llm = config, db, fpl, llm
        self.store = Store(db, fpl, config.league_id)
        self.lock = asyncio.Lock()
        self.question_lock = asyncio.Lock()
        self.question_times = {}

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

    async def poll(self, bot):
        if self.lock.locked():
            return
        async with self.lock:
            try:
                bootstrap = await self.fpl.get("bootstrap-static/")
                await self.store.sync_bootstrap(bootstrap)
                latest = latest_completed(bootstrap)
                available = latest_available(bootstrap)
                if available is not None and available != latest:
                    await self.store.ensure_through(available, bootstrap)
                if latest is None:
                    return
                # Resume failed older reports, then publish every newly completed GW.
                completed = [e["id"] for e in bootstrap["events"] if e["finished"] and e["data_checked"]]
                baseline = await rows(self.db, "SELECT value FROM metadata WHERE key='poll_baseline'")
                if not baseline:
                    # Initial deployment backfills history but publishes just the latest.
                    await self.set_metadata("poll_baseline", str(latest))
                    baseline = [{"value": str(latest)}]
                pending = {r["gw_id"] for r in await rows(self.db, "SELECT gw_id FROM reports WHERE sent_count<2")}
                targets = pending | {g for g in completed if g >= int(baseline[0]["value"])}
                for gw in sorted(targets):
                    await self.broadcast(bot, gw, bootstrap)
            except Exception as exc:
                # Avoid logging URLs containing Telegram tokens or response bodies.
                LOG.error("Polling failed (%s). Check connectivity/configuration and pending deliveries.", type(exc).__name__)


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


async def ask_command(update, context):
    await question_message(update, context, command=True)


async def question_message(update, context, command=False):
    service = context.application.bot_data["service"]
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not message or not user or user.is_bot or not chat or chat.id != service.config.chat_id:
        return
    text = message.text or ""
    replied = message.reply_to_message
    replying = bool(replied and replied.from_user and replied.from_user.id == context.bot.id)
    mention = re.compile(r"@" + re.escape(context.bot.username) + r"\b", re.IGNORECASE)
    if not command and not replying and not mention.search(text):
        return
    question = text.split(maxsplit=1)[1] if command and len(text.split(maxsplit=1)) > 1 else ("" if command else mention.sub("", text).strip())
    if not question:
        await message.reply_text("После /ask напишите вопрос.")
        return
    if len(question) > 1500:
        await message.reply_text(error_text('length', question))
        return
    now = time.monotonic()
    service.question_times = {uid: t for uid, t in service.question_times.items() if now - t < 10}
    if user.id in service.question_times:
        await message.reply_text(error_text('cooldown', question))
        return
    if service.question_lock.locked() or service.lock.locked():
        await message.reply_text(error_text('busy', question))
        return
    service.question_times[user.id] = now
    async with service.question_lock:
        result = await answer(service, question, (replied.text or "")[:2500] if replying else "")
        await message.reply_text(result, parse_mode=None)


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


async def cli(args, config):
    db = await connect(config.database)
    fpl = FPL()
    try:
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
    preview = sub.add_parser("preview")
    preview.add_argument("gw", type=int, choices=range(1, 39))
    resolve = sub.add_parser("resolve")
    resolve.add_argument("gw", type=int, choices=range(1, 39))
    resolve.add_argument("count", type=int, choices=range(3), help="Messages visibly delivered (0, 1, or 2)")
    args = parser.parse_args()
    args.command = args.command or "run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = Config.load(require_secrets=args.command == "run")
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
        app.add_handler(CommandHandler("ask", ask_command, block=False))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, question_message, block=False))
        app.run_polling(allowed_updates=["message"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
