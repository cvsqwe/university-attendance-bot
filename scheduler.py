"""
APScheduler wiring: sends the "Я на паре" check-in notification 5 minutes
before each class and closes the attendance window 10 minutes before each
class ends, then reports to the Starosta/Deputy.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import config
from database import Database
from maxapi.client import MaxApiError, MaxClient
from maxapi.types import InlineKeyboardButton, InlineKeyboardMarkup
from reports import build_excel_report, build_session_excel_report
from utils import aware, combine_dt, fmt_date_human, fmt_dt, now_msk, parse_dt

logger = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)

_combine = combine_dt
_fmt_dt = fmt_dt
_parse_dt = parse_dt
_now_naive = now_msk
_aware = aware


class AttendanceScheduler:
    def __init__(self, bot: MaxClient, db: Database):
        self.bot = bot
        self.db = db
        self.scheduler = AsyncIOScheduler(timezone=TZ)

    def start(self) -> None:
        self.scheduler.start()

    # ------------------------------------------------------------------
    # Weekly cron wiring, driven by the `schedule` table
    # ------------------------------------------------------------------
    async def configure_weekly_jobs(self) -> None:
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith("notify_"):
                self.scheduler.remove_job(job.id)

        entries = await self.db.get_full_schedule()
        for entry in entries:
            start_h, start_m = map(int, entry["start_time"].split(":"))
            notify_dummy = dt.datetime(2000, 1, 1, start_h, start_m) - dt.timedelta(
                minutes=config.NOTIFY_BEFORE_START_MIN
            )
            cron_day = config.WEEKDAY_RU_TO_CRON[entry["weekday"]]
            self.scheduler.add_job(
                self._notify_job,
                trigger=CronTrigger(
                    day_of_week=cron_day,
                    hour=notify_dummy.hour,
                    minute=notify_dummy.minute,
                    timezone=TZ,
                ),
                id=f"notify_{entry['id']}",
                args=[entry["id"]],
                replace_existing=True,
                misfire_grace_time=300,
            )
        logger.info("Configured %d weekly notification jobs", len(entries))
        await self._configure_weekly_report_job()
        await self._configure_unregistered_reminder_job()

    async def _configure_weekly_report_job(self) -> None:
        """(Re)schedules the automatic weekly report to fire right after
        Saturday's last pair closes, based on whatever is currently
        scheduled for Saturday. Falls back to a fixed evening time if
        Saturday has no classes at all."""
        saturday_entries = await self.db.get_schedule_for_weekday("СБ")
        if saturday_entries:
            last_pair = max(saturday_entries, key=lambda e: e["pair_number"])
            end_h, end_m = map(int, last_pair["end_time"].split(":"))
            close_dummy = dt.datetime(2000, 1, 1, end_h, end_m) - dt.timedelta(
                minutes=config.CLOSE_BEFORE_END_MIN
            )
            report_dummy = close_dummy + dt.timedelta(minutes=1)
        else:
            report_dummy = dt.datetime(2000, 1, 1, 21, 0)

        self.scheduler.add_job(
            self._weekly_report_job,
            trigger=CronTrigger(
                day_of_week="sat", hour=report_dummy.hour, minute=report_dummy.minute, timezone=TZ
            ),
            id="weekly_report",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    # ------------------------------------------------------------------
    # Startup recovery — reschedules/close-catches-up sessions that were
    # in flight when the bot was last stopped.
    # ------------------------------------------------------------------
    async def startup_recovery(self) -> None:
        now = _now_naive()

        for row in await self.db.get_unclosed_sessions():
            close_dt = _parse_dt(row["close_dt"])
            if row["notified"] == 0:
                # Session row exists but notifications were never sent —
                # treat it like a fresh notify if we're still before the end.
                end_dt = _parse_dt(row["end_dt"])
                if now < end_dt:
                    await self._notify_job(row["schedule_id"], target_date=row["date"])
                else:
                    await self.db.mark_session_closed(row["id"])
                continue
            if close_dt <= now:
                await self._close_job(row["id"])
                continue

            if row["reminded"] == 0:
                reminder_dt = close_dt - dt.timedelta(minutes=config.REMINDER_BEFORE_CLOSE_MIN)
                if reminder_dt <= now:
                    await self._reminder_job(row["id"])
                else:
                    self.scheduler.add_job(
                        self._reminder_job,
                        trigger=DateTrigger(run_date=_aware(reminder_dt)),
                        id=f"remind_{row['id']}",
                        args=[row["id"]],
                        replace_existing=True,
                        misfire_grace_time=300,
                    )

            self.scheduler.add_job(
                self._close_job,
                trigger=DateTrigger(run_date=_aware(close_dt)),
                id=f"close_{row['id']}",
                args=[row["id"]],
                replace_existing=True,
                misfire_grace_time=3600,
            )

        today = now.date()
        today_ru = config.WEEKDAY_PY_INDEX_TO_RU[today.weekday()]
        for entry in await self.db.get_schedule_for_weekday(today_ru):
            existing = await self.db.get_session_by_schedule_and_date(entry["id"], today.isoformat())
            if existing:
                continue
            start_dt = _combine(today.isoformat(), entry["start_time"])
            end_dt = _combine(today.isoformat(), entry["end_time"])
            notify_dt = start_dt - dt.timedelta(minutes=config.NOTIFY_BEFORE_START_MIN)
            if notify_dt <= now < end_dt:
                await self._notify_job(entry["id"], target_date=today.isoformat())

    # ------------------------------------------------------------------
    # Notify job — fires 5 minutes before class start
    # ------------------------------------------------------------------
    async def _notify_job(self, schedule_id: int, target_date: str | None = None) -> None:
        entry = await self.db.get_schedule_entry(schedule_id)
        if entry is None:
            return

        date_iso = target_date or _now_naive().date().isoformat()
        if await self.db.is_date_excluded(date_iso):
            return
        start_dt = _combine(date_iso, entry["start_time"])
        end_dt = _combine(date_iso, entry["end_time"])
        notify_dt = start_dt - dt.timedelta(minutes=config.NOTIFY_BEFORE_START_MIN)
        close_dt = end_dt - dt.timedelta(minutes=config.CLOSE_BEFORE_END_MIN)

        session_id = await self.db.create_session(
            schedule_id=schedule_id,
            date=date_iso,
            weekday=entry["weekday"],
            pair_number=entry["pair_number"],
            class_type=entry["class_type"],
            subject=entry["subject"],
            link=entry["link"],
            teacher=entry["teacher"],
            start_dt=_fmt_dt(start_dt),
            end_dt=_fmt_dt(end_dt),
            notify_dt=_fmt_dt(notify_dt),
            close_dt=_fmt_dt(close_dt),
        )
        session = await self.db.get_session(session_id)

        if session["notified"] == 0:
            await self._send_checkin_notifications(session_id, entry, date_iso, start_dt)
            await self.db.mark_session_notified(session_id)

        reminder_dt = close_dt - dt.timedelta(minutes=config.REMINDER_BEFORE_CLOSE_MIN)
        if reminder_dt > notify_dt:
            self.scheduler.add_job(
                self._reminder_job,
                trigger=DateTrigger(run_date=_aware(reminder_dt)),
                id=f"remind_{session_id}",
                args=[session_id],
                replace_existing=True,
                misfire_grace_time=300,
            )

        self.scheduler.add_job(
            self._close_job,
            trigger=DateTrigger(run_date=_aware(close_dt)),
            id=f"close_{session_id}",
            args=[session_id],
            replace_existing=True,
            misfire_grace_time=3600,
        )

    async def _send_checkin_notifications(self, session_id: int, entry, date_iso: str,
                                           start_dt: dt.datetime) -> None:
        students = await self.db.get_all_registered_students()
        text = (
            f"🔔 <b>Пара №{entry['pair_number']}</b> начинается в {entry['start_time']} (МСК)\n\n"
            f"📚 Предмет: <b>{entry['subject']}</b>\n"
            f"🏷 Тип: {entry['class_type']}\n"
            f"👤 Преподаватель: {entry['teacher']}\n\n"
            f"Отметьтесь на паре кнопкой ниже."
        )
        for student in students:
            planned = await self.db.get_planned_absence(student.id, date_iso)
            planned_pair = await self.db.get_planned_absence_pair(student.id, date_iso, entry["pair_number"])
            if planned or planned_pair:
                await self.db.mark_attendance(session_id, student.id, "excused")
                continue

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключиться", url=entry["link"])],
                [InlineKeyboardButton(text="🟢 Я на паре", callback_data=f"checkin:{session_id}")],
            ])
            try:
                message = await self.bot.send_message(text, user_id=student.max_user_id, keyboard=keyboard)
            except MaxApiError as exc:
                logger.warning("Could not notify student %s: %s", student.full_name, exc)
                continue
            await self.db.add_notification(session_id, student.id, student.max_user_id, message.message_id)

    # ------------------------------------------------------------------
    # Reminder job — fires a couple minutes before the check-in window
    # closes, nudging whoever hasn't tapped "Я на паре" yet.
    # ------------------------------------------------------------------
    async def _reminder_job(self, session_id: int) -> None:
        session = await self.db.get_session(session_id)
        if session is None or session["closed"] == 1 or session["reminded"] == 1:
            return

        notifications = await self.db.get_notifications_for_session(session_id)
        for notif in notifications:
            attendance = await self.db.get_attendance(session_id, notif["student_id"])
            if attendance is not None:
                continue  # already checked in, or excused
            try:
                await self.bot.send_message(
                    "⏰ Через пару минут закроется отметка на паре — не забудьте нажать «🟢 Я на паре».",
                    user_id=notif["user_id"],
                )
            except MaxApiError as exc:
                logger.warning("Could not send reminder for session %s: %s", session_id, exc)

        await self.db.mark_session_reminded(session_id)

    # ------------------------------------------------------------------
    # Close job — fires 10 minutes before class end
    # ------------------------------------------------------------------
    async def _close_job(self, session_id: int) -> None:
        session = await self.db.get_session(session_id)
        if session is None or session["closed"] == 1:
            return

        notifications = await self.db.get_notifications_for_session(session_id)
        present_count = 0
        absent_names: list[str] = []

        for notif in notifications:
            attendance = await self.db.get_attendance(session_id, notif["student_id"])
            if attendance and attendance["status"] == "present":
                present_count += 1
                continue
            if attendance and attendance["status"] == "excused":
                # A planned absence declared after the notification was
                # already sent excuses this pair retroactively (see
                # Database.excuse_existing_sessions) — leave it as is,
                # don't relabel it absent in the summary.
                try:
                    await self.bot.edit_message(
                        notif["message_id"], "📝 Оформлен плановый пропуск — отметка не требуется."
                    )
                except MaxApiError:
                    pass
                continue

            student = await self.db.get_student_by_id(notif["student_id"])
            await self.db.mark_attendance(session_id, notif["student_id"], "absent")
            if student:
                absent_names.append(student.full_name)
            try:
                await self.bot.edit_message(notif["message_id"], "❌ Отметка закрыта (пропуск)")
            except MaxApiError:
                pass

        # Students who never registered can't have been notified or have
        # checked in — until they register, every pair counts as an
        # unexcused absence for them too, same as a no-show who is
        # registered. Once they register, this stops applying and they go
        # through the normal notify/check-in flow above like everyone else.
        for student in await self.db.get_all_students():
            if student.max_user_id is not None:
                continue
            await self.db.mark_attendance(session_id, student.id, "absent")
            absent_names.append(f"{student.full_name} (не зарегистрирован)")

        excused_rows = await self.db.get_excused_students_for_session(session_id)
        excused_names = [row["full_name"] for row in excused_rows]

        await self.db.mark_session_closed(session_id)
        await self._send_summary_report(session, present_count, absent_names, excused_names)

    async def _send_summary_report(self, session, present_count: int,
                                    absent_names: list[str], excused_names: list[str]) -> None:
        staff = await self.db.get_staff()
        if not staff:
            return

        caption = (
            f"📊 <b>Итоги пары №{session['pair_number']}</b> ({session['subject']})\n"
            f"📅 {session['date']}, {session['weekday']}\n\n"
            f"✅ Присутствовало: {present_count}\n"
            f"❌ Отсутствовало: {len(absent_names)}\n"
            f"📝 По уважительной причине: {len(excused_names)}\n\n"
            "Полный список — в приложенном файле."
        )

        file_path = await build_session_excel_report(self.db, session)
        try:
            for person in staff:
                try:
                    await self.bot.send_document(
                        file_path,
                        f"Посещаемость_{session['date']}_пара{session['pair_number']}.xlsx",
                        caption=caption,
                        user_id=person.max_user_id,
                    )
                except MaxApiError as exc:
                    logger.warning("Could not send report to %s: %s", person.full_name, exc)
        finally:
            os.remove(file_path)

    # ------------------------------------------------------------------
    # Weekly report — fires right after Saturday's last pair closes
    # (see _configure_weekly_report_job), covers Monday through Saturday.
    # ------------------------------------------------------------------
    async def _weekly_report_job(self) -> None:
        staff = await self.db.get_staff()
        if not staff:
            return

        today = _now_naive().date()
        monday = today - dt.timedelta(days=today.weekday())
        saturday = monday + dt.timedelta(days=5)

        sessions = await self.db.get_sessions_range(monday.isoformat(), saturday.isoformat())
        period_label = f"{fmt_date_human(monday)} – {fmt_date_human(saturday)}"
        if not sessions:
            text = f"📊 Итоги за неделю {period_label}: занятий не было."
            for person in staff:
                try:
                    await self.bot.send_message(text, user_id=person.max_user_id)
                except MaxApiError as exc:
                    logger.warning("Could not send weekly report to %s: %s", person.full_name, exc)
            return

        file_path = await build_excel_report(self.db, monday, saturday)
        try:
            for person in staff:
                try:
                    await self.bot.send_document(
                        file_path,
                        f"Посещаемость_{monday.isoformat()}_{saturday.isoformat()}.xlsx",
                        caption=f"📊 Итоговый отчёт за неделю {period_label}",
                        user_id=person.max_user_id,
                    )
                except MaxApiError as exc:
                    logger.warning("Could not send weekly report to %s: %s", person.full_name, exc)
        finally:
            os.remove(file_path)

    # ------------------------------------------------------------------
    # Unregistered reminder — weekly nudge to staff listing anyone who
    # still hasn't followed their invite link.
    # ------------------------------------------------------------------
    async def _configure_unregistered_reminder_job(self) -> None:
        self.scheduler.add_job(
            self._unregistered_reminder_job,
            trigger=CronTrigger(
                day_of_week=config.UNREGISTERED_REMINDER_WEEKDAY_CRON,
                hour=config.UNREGISTERED_REMINDER_HOUR,
                minute=config.UNREGISTERED_REMINDER_MINUTE,
                timezone=TZ,
            ),
            id="unregistered_reminder",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    async def _unregistered_reminder_job(self) -> None:
        staff = await self.db.get_staff()
        if not staff:
            return
        unregistered = await self.db.get_unregistered_students()
        if not unregistered:
            return

        lines = [f"🔔 <b>Ещё не зарегистрированы в боте</b> ({len(unregistered)}):", ""]
        lines.extend(f"• {s.full_name}" for s in unregistered)
        lines.append("")
        lines.append("Пригласительные ссылки — в «🔗 Ссылки».")
        text = "\n".join(lines)

        for person in staff:
            try:
                await self.bot.send_message(text, user_id=person.max_user_id)
            except MaxApiError as exc:
                logger.warning("Could not send unregistered reminder to %s: %s", person.full_name, exc)
