"""
Bot entry point: wires the database, scheduler and handlers together and
starts long polling against the MAX bot API.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import config
from database import Database
from handlers import get_root_router
from maxapi.client import MaxApiError, MaxClient
from maxapi.dispatcher import dispatch_update
from scheduler import AttendanceScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def main() -> None:
    if not config.MAX_BOT_TOKEN:
        logger.error("MAX_BOT_TOKEN is not set. Create a .env file (see .env.example) or export it.")
        sys.exit(1)

    bot = MaxClient(token=config.MAX_BOT_TOKEN)

    db = Database()
    await db.init_db()
    logger.info("Database initialised at %s", db.db_path)

    sched = AttendanceScheduler(bot, db)
    await sched.configure_weekly_jobs()
    await sched.startup_recovery()
    sched.start()
    logger.info("Scheduler started")

    root_router = get_root_router()
    base_ctx = {"db": db, "sched": sched, "bot": bot}

    me = await bot.get_me()
    logger.info("Long polling for bot @%s (id=%s)", me.username, me.id)

    marker: int | None = None
    try:
        while True:
            try:
                updates, marker = await bot.get_updates(marker=marker, timeout=30)
            except MaxApiError as exc:
                logger.warning("get_updates failed: %s — retrying shortly", exc)
                await asyncio.sleep(5)
                continue

            for update in updates:
                try:
                    await dispatch_update(root_router, update, bot, base_ctx)
                except Exception:
                    logger.exception("Unhandled error while dispatching update: %r", update)
    finally:
        sched.scheduler.shutdown(wait=False)
        await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
