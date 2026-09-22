"""
Bot entry point: wires the database, scheduler and handlers together and
starts long polling.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import config
from database import Database
from handlers import get_root_router
from scheduler import AttendanceScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def main() -> None:
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Create a .env file (see .env.example) or export it.")
        sys.exit(1)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())

    db = Database()
    await db.init_db()
    logger.info("Database initialised at %s", db.db_path)

    sched = AttendanceScheduler(bot, db)
    await sched.configure_weekly_jobs()
    await sched.startup_recovery()
    sched.start()
    logger.info("Scheduler started")

    dispatcher.include_router(get_root_router())

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dispatcher.start_polling(bot, db=db, sched=sched)
    finally:
        sched.scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
