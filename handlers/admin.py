"""
Staff-only handlers (Starosta & Deputy): /set_schedule parser, manual
attendance override, invite-link management and the /report Excel export.

Everything except /set_schedule (a big text paste, not menu navigation)
renders into the single-message panel — see panel.py.
"""
from __future__ import annotations

import datetime as dt
import os
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import config
import panel
from access import require_staff as _require_staff, require_staff_cb as _require_staff_cb
from database import Database
from keyboards import back_to_menu_kb, with_back_to_menu
from reports import build_excel_report
from scheduler import AttendanceScheduler
from utils import STATUS_LABELS, STATUS_LABELS_SHORT, fmt_date_human, today_msk

router = Router(name="admin")

PanelTarget = Message | CallbackQuery


# ----------------------------------------------------------------------
# Invite links — the only way a new account gets registered. Each roster
# entry owns a one-time secret token; the starosta/deputy hands out the
# t.me deep link built from it, and /start binds automatically.
# ----------------------------------------------------------------------
async def _invite_list_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    unregistered = await db.get_unregistered_students()
    if not unregistered:
        return "🎉 Все участники группы уже зарегистрированы.", back_to_menu_kb()
    buttons = [
        [InlineKeyboardButton(text=s.full_name, callback_data=f"inv:show:{s.id}")]
        for s in unregistered
    ]
    text = f"Не зарегистрированы ({len(unregistered)}):\nВыберите, чтобы получить ссылку для регистрации."
    return text, with_back_to_menu(buttons)


async def _render_invite_card(callback: CallbackQuery, db: Database, bot: Bot, state: FSMContext,
                               student_id: int, toast: str | None = None) -> None:
    target = await db.get_student_by_id(student_id)
    if target is None or target.telegram_id is not None:
        await callback.answer("Этот человек уже зарегистрирован.", show_alert=True)
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={target.invite_token}"
    share_text = f"Ссылка для регистрации в боте посещаемости — {target.full_name}"
    share_url = "tg://msg_url?url=" + quote(link, safe="") + "&text=" + quote(share_text, safe="")

    text = (
        f"👤 <b>{target.full_name}</b>\n\n"
        f"Ссылка для регистрации (одноразовая):\n<code>{link}</code>"
    )
    buttons = [
        [InlineKeyboardButton(text="📤 Отправить в чат", url=share_url)],
        [InlineKeyboardButton(text="🔄 Перевыпустить ссылку", callback_data=f"inv:regen:{student_id}")],
        [InlineKeyboardButton(text="⬅ К списку", callback_data="inv:back")],
    ]
    await panel.show(callback, state, text, InlineKeyboardMarkup(inline_keyboard=buttons), toast=toast)


@router.message(Command("invites"))
async def cmd_invites(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    text, kb = await _invite_list_view(db)
    await panel.show(message, state, text, kb)


@router.callback_query(F.data == "menu:invites")
async def cb_menu_invites(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _invite_list_view(db)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data == "inv:back")
async def cb_invites_back(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _invite_list_view(db)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data.startswith("inv:show:"))
async def cb_invite_show(callback: CallbackQuery, db: Database, bot: Bot, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    student_id = int(callback.data.split(":", 2)[2])
    await _render_invite_card(callback, db, bot, state, student_id)


@router.callback_query(F.data.startswith("inv:regen:"))
async def cb_invite_regen(callback: CallbackQuery, db: Database, bot: Bot, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    student_id = int(callback.data.split(":", 2)[2])
    await db.regenerate_invite_token(student_id)
    await _render_invite_card(callback, db, bot, state, student_id, toast="Ссылка перевыпущена")


# ----------------------------------------------------------------------
# Bulk schedule paste — enter (or replace) several pairs at once, either by
# typing /set_schedule with the lines attached, or via the "📋 Массовая
# загрузка" button (handlers/schedule.py's weekday picker), which waits
# for the next text message. Each line is upserted individually (same as
# the per-slot wizard), so slots not mentioned in the paste are left
# untouched and existing attendance history for unchanged slots survives.
# ----------------------------------------------------------------------
class BulkScheduleStates(StatesGroup):
    waiting_text = State()


TYPE_CODES: dict[str, str] = {"Л": "Лекция", "ПР": "Практика", "ЛР": "Лабораторная"}

BULK_FORMAT_HELP = (
    "Отправьте расписание одним сообщением, по одной паре в строке, в формате:\n\n"
    "<code>ДЕНЬ|ПАРА|ТИП|ПРЕДМЕТ|ПРЕПОДАВАТЕЛЬ|ССЫЛКА</code>\n\n"
    "День — один из ПН, ВТ, СР, ЧТ, ПТ, СБ.\n"
    "Тип — Л (лекция), ПР (практика) или ЛР (лабораторная). Пример:\n"
    "<code>ПН|1|Л|Высшая математика|Иванов И.И.|https://zoom.us/j/123456</code>\n\n"
    "Каждая строка добавит или обновит одну пару — остальное расписание не тронется."
)


def _normalize_link(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("[") and "](" in raw and raw.endswith(")"):
        return raw.split("](", 1)[1][:-1].strip()
    return raw


def parse_schedule_text(text: str) -> tuple[list[dict], list[str]]:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("/set_schedule"):
        lines = lines[1:]

    entries: list[dict] = []
    errors: list[str] = []

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 6:
            errors.append(f"Строка {idx}: ожидается 6 полей через «|» "
                           f"(ДЕНЬ|ПАРА|ТИП|ПРЕДМЕТ|ПРЕПОДАВАТЕЛЬ|ССЫЛКА), получено {len(parts)}.")
            continue

        day, pair_str, type_code, subject, teacher, link = parts

        if day not in config.WORKING_WEEKDAYS_RU:
            errors.append(
                f"Строка {idx}: неизвестный или нерабочий день «{day}». Занятия только "
                "с понедельника по субботу. Допустимые значения: "
                + ", ".join(config.WORKING_WEEKDAYS_RU) + "."
            )
            continue

        if not pair_str.isdigit() or not (1 <= int(pair_str) <= 7):
            errors.append(f"Строка {idx}: номер пары должен быть числом от 1 до 7, получено «{pair_str}».")
            continue
        pair_number = int(pair_str)

        class_type = TYPE_CODES.get(type_code.upper())
        if class_type is None:
            errors.append(
                f"Строка {idx}: неизвестный тип «{type_code}». Допустимые значения: "
                + ", ".join(TYPE_CODES) + "."
            )
            continue

        if not subject:
            errors.append(f"Строка {idx}: не указан предмет.")
            continue

        if not teacher:
            errors.append(f"Строка {idx}: не указан преподаватель.")
            continue

        link = _normalize_link(link)
        if not (link.startswith("http://") or link.startswith("https://")):
            errors.append(f"Строка {idx}: ссылка должна начинаться с http:// или https://, получено «{link}».")
            continue

        start_time, end_time = config.BELL_SCHEDULE[pair_number]
        entries.append({
            "weekday": day,
            "pair_number": pair_number,
            "class_type": class_type,
            "subject": subject,
            "link": link,
            "teacher": teacher,
            "start_time": start_time,
            "end_time": end_time,
        })

    return entries, errors


async def _apply_bulk_schedule(target: PanelTarget, db: Database, state: FSMContext,
                                sched: AttendanceScheduler, entries: list[dict]) -> None:
    for entry in entries:
        await db.upsert_schedule_entry(
            weekday=entry["weekday"], pair_number=entry["pair_number"],
            class_type=entry["class_type"], subject=entry["subject"],
            link=entry["link"], teacher=entry["teacher"],
        )
    await sched.configure_weekly_jobs()
    await panel.clear_keep_panel(state)

    by_day: dict[str, list[dict]] = {}
    for entry in entries:
        by_day.setdefault(entry["weekday"], []).append(entry)

    lines = [f"✅ Загружено/обновлено пар: {len(entries)}.\n"]
    for day in config.WORKING_WEEKDAYS_RU:
        if day not in by_day:
            continue
        lines.append(f"<b>{config.WEEKDAY_FULL_RU[day]}</b>")
        for entry in sorted(by_day[day], key=lambda e: e["pair_number"]):
            lines.append(
                f"  {entry['pair_number']}. {entry['start_time']}–{entry['end_time']} "
                f"{entry['subject']} ({entry['class_type']}) — {entry['teacher']}"
            )
    await panel.show(target, state, "\n".join(lines), back_to_menu_kb())


@router.message(Command("set_schedule"))
async def cmd_set_schedule(message: Message, db: Database, state: FSMContext, sched: AttendanceScheduler) -> None:
    if await _require_staff(message, db) is None:
        return

    entries, errors = parse_schedule_text(message.text or "")

    if errors:
        await panel.show(
            message, state, "❗ Обнаружены ошибки в расписании. Ничего не изменено.\n\n" + "\n".join(errors)
        )
        return
    if not entries:
        await panel.show(message, state, "Не найдено ни одной строки расписания.\n\n" + BULK_FORMAT_HELP)
        return

    await _apply_bulk_schedule(message, db, state, sched, entries)


@router.callback_query(F.data == "sched:bulk")
async def cb_bulk_start(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await state.set_state(BulkScheduleStates.waiting_text)
    await panel.show(
        callback, state, BULK_FORMAT_HELP,
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="menu:schedule")]]),
    )


@router.message(BulkScheduleStates.waiting_text)
async def msg_bulk_text(message: Message, db: Database, state: FSMContext, sched: AttendanceScheduler) -> None:
    entries, errors = parse_schedule_text(message.text or "")

    if errors:
        await panel.show(
            message, state,
            "❗ Обнаружены ошибки. Ничего не изменено, попробуйте ещё раз.\n\n" + "\n".join(errors),
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="menu:schedule")]]),
        )
        return
    if not entries:
        await panel.show(
            message, state, "Не найдено ни одной строки расписания.\n\n" + BULK_FORMAT_HELP,
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="menu:schedule")]]),
        )
        return

    await _apply_bulk_schedule(message, db, state, sched, entries)


# ----------------------------------------------------------------------
# Manual override
# ----------------------------------------------------------------------
async def _override_session_list(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    today = today_msk()
    date_from = (today - dt.timedelta(days=7)).isoformat()
    sessions = await db.get_sessions_range(date_from, today.isoformat())
    if not sessions:
        return "Нет занятий за последние 7 дней для редактирования.", back_to_menu_kb()

    buttons = [
        [InlineKeyboardButton(
            text=f"{s['date']} · Пара {s['pair_number']} · {s['subject']}",
            callback_data=f"ov_sess:{s['id']}",
        )]
        for s in sessions
    ]
    return "Выберите занятие для корректировки:", with_back_to_menu(buttons)


@router.message(Command("override"))
async def cmd_override(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    text, keyboard = await _override_session_list(db)
    await panel.show(message, state, text, keyboard)


@router.callback_query(F.data == "menu:override")
async def cb_menu_override(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, keyboard = await _override_session_list(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data == "ov_back")
async def cb_override_back(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, keyboard = await _override_session_list(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data.startswith("ov_sess:"))
async def cb_override_session(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return

    session_id = int(callback.data.split(":", 1)[1])
    session = await db.get_session(session_id)
    if session is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return

    students = await db.get_all_registered_students()
    buttons = []
    for st in students:
        attendance = await db.get_attendance(session_id, st.id)
        status_label = STATUS_LABELS_SHORT.get(attendance["status"], "—") if attendance else "—"
        buttons.append([InlineKeyboardButton(
            text=f"{st.full_name} [{status_label}]",
            callback_data=f"ov_student:{session_id}:{st.id}",
        )])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="ov_back")])

    await panel.show(
        callback, state,
        f"Занятие: {session['date']} · Пара {session['pair_number']} · {session['subject']}\n"
        "Выберите студента:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("ov_student:"))
async def cb_override_student(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)
    target = await db.get_student_by_id(student_id)
    if target is None:
        await callback.answer("Студент не найден.", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text="✅ Присутствовал", callback_data=f"ov_set:{session_id}:{student_id}:present")],
        [InlineKeyboardButton(text="❌ Отсутствовал", callback_data=f"ov_set:{session_id}:{student_id}:absent")],
        [InlineKeyboardButton(text="📝 Уважительная причина", callback_data=f"ov_set:{session_id}:{student_id}:excused")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data=f"ov_sess:{session_id}")],
    ]
    await panel.show(
        callback, state,
        f"Студент: {target.full_name}\nВыберите статус на этой паре:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("ov_set:"))
async def cb_override_set(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s, status = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)

    await db.set_attendance(session_id, student_id, status)
    target = await db.get_student_by_id(student_id)

    await panel.show(
        callback, state,
        f"✅ Статус студента «{target.full_name}» изменён на: {STATUS_LABELS_SHORT[status]}",
        back_to_menu_kb(),
        toast="Статус обновлён",
    )


# ----------------------------------------------------------------------
# /report — Excel export (build_excel_report lives in reports.py, shared
# with the automatic weekly report in scheduler.py)
# ----------------------------------------------------------------------
MONTHS_RU_GEN: dict[int, str] = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _report_period_keyboard() -> InlineKeyboardMarkup:
    today = today_msk()
    prev_last_day = dt.date(today.year, today.month, 1) - dt.timedelta(days=1)
    periods = [(today.year, today.month), (prev_last_day.year, prev_last_day.month)]
    buttons = [
        [InlineKeyboardButton(
            text=f"📊 {MONTHS_RU_GEN[month]} {year}",
            callback_data=f"rep:{year}-{month:02d}",
        )]
        for year, month in periods
    ]
    return with_back_to_menu(buttons)


async def _send_report_for_period(target: PanelTarget, db: Database, state: FSMContext, year: int, month: int) -> None:
    date_from = dt.date(year, month, 1)
    date_to = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)

    sessions = await db.get_sessions_range(date_from.isoformat(), date_to.isoformat())
    if not sessions:
        await panel.show(
            target, state, f"Нет данных о посещаемости за {date_from.strftime('%m.%Y')}.", back_to_menu_kb()
        )
        return

    chat_id = target.message.chat.id if isinstance(target, CallbackQuery) else target.chat.id
    file_path = await build_excel_report(db, date_from, date_to)
    try:
        await target.bot.send_document(
            chat_id,
            FSInputFile(file_path, filename=f"Посещаемость_{date_from.strftime('%m.%Y')}.xlsx"),
            caption=f"📊 Отчёт по посещаемости за {date_from.strftime('%m.%Y')}",
        )
    finally:
        os.remove(file_path)

    await panel.show(
        target, state, f"📊 Отчёт за {MONTHS_RU_GEN[month]} {year} отправлен выше.", back_to_menu_kb()
    )


@router.message(Command("report"))
async def cmd_report(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        try:
            year_s, month_s = args[1].strip().split("-")
            year, month = int(year_s), int(month_s)
            if not (1 <= month <= 12):
                raise ValueError
        except ValueError:
            await panel.show(
                message, state,
                "Неверный формат периода. Используйте: /report ГГГГ-ММ, например /report 2026-09",
                back_to_menu_kb(),
            )
            return
        await _send_report_for_period(message, db, state, year, month)
        return

    await panel.show(message, state, "За какой период выгрузить отчёт?", _report_period_keyboard())


@router.callback_query(F.data == "menu:report")
async def cb_menu_report(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await panel.show(callback, state, "За какой период выгрузить отчёт?", _report_period_keyboard())


@router.callback_query(F.data.startswith("rep:"))
async def cb_report_period(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    year_s, month_s = callback.data.split(":", 1)[1].split("-")
    await _send_report_for_period(callback, db, state, int(year_s), int(month_s))


# ----------------------------------------------------------------------
# Live status ("👥 Кто на паре") — read-only view of today's sessions,
# including ones that haven't closed yet, plus an Excel snapshot of the
# current day at any time (before or after a pair closes).
# ----------------------------------------------------------------------
async def _today_sessions_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    today = today_msk()
    sessions = await db.get_sessions_for_date(today.isoformat())
    buttons: list[list[InlineKeyboardButton]] = []
    if not sessions:
        text = (
            f"Сегодня, {fmt_date_human(today)}: пока нет пар с начавшейся отметкой.\n"
            "Статус появится за 5 минут до начала пары."
        )
    else:
        text = f"Пары сегодня, {fmt_date_human(today)}:"
        for s in sessions:
            state_icon = "⚪ закрыта" if s["closed"] else "🔴 идёт"
            buttons.append([InlineKeyboardButton(
                text=f"Пара {s['pair_number']} · {s['subject']} — {state_icon}",
                callback_data=f"live:sess:{s['id']}",
            )])
    buttons.append([InlineKeyboardButton(text="📊 Excel за сегодня", callback_data="live:excel_today")])
    return text, with_back_to_menu(buttons)


@router.message(Command("live"))
async def cmd_live(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    text, kb = await _today_sessions_view(db)
    await panel.show(message, state, text, kb)


@router.callback_query(F.data == "menu:live")
async def cb_menu_live(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _today_sessions_view(db)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data == "live:today")
async def cb_live_today(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _today_sessions_view(db)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data.startswith("live:sess:"))
async def cb_live_session(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    session_id = int(callback.data.split(":", 2)[2])
    session = await db.get_session(session_id)
    if session is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return

    students = await db.get_all_students()
    lines = [
        f"<b>Пара {session['pair_number']} · {session['subject']}</b>",
        "🔴 Отметка идёт" if not session["closed"] else "⚪ Отметка закрыта",
        "",
    ]
    for st in students:
        name = st.full_name if st.telegram_id is not None else f"{st.full_name} (не зарегистрирован)"
        attendance = await db.get_attendance(session_id, st.id)
        if attendance:
            label = STATUS_LABELS[attendance["status"]]
        elif session["closed"]:
            label = "❌ Отсутствовал"
        elif st.telegram_id is None:
            label = "❌ Будет отмечен как отсутствующий при закрытии"
        else:
            label = "⏳ Ещё не отметился"
        lines.append(f"{name} — {label}")

    buttons = [[InlineKeyboardButton(text="⬅ Назад", callback_data="live:today")]]
    await panel.show(callback, state, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "live:excel_today")
async def cb_live_excel_today(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    today = today_msk()
    sessions = await db.get_sessions_for_date(today.isoformat())
    if not sessions:
        await callback.answer("Сегодня пока нет пар с начавшейся отметкой.", show_alert=True)
        return

    file_path = await build_excel_report(db, today, today)
    try:
        await callback.bot.send_document(
            callback.message.chat.id,
            FSInputFile(file_path, filename=f"Посещаемость_{today.isoformat()}.xlsx"),
            caption=f"📊 Посещаемость за сегодня, {fmt_date_human(today)} (включая ещё не закрытые пары)",
        )
    finally:
        os.remove(file_path)

    text, kb = await _today_sessions_view(db)
    await panel.show(callback, state, text, kb)
