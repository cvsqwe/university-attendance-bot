"""
Staff-only handlers (Starosta & Deputy): /set_schedule parser, manual
attendance override, invite-link management and the /report Excel export.

Everything except /set_schedule (a big text paste, not menu navigation)
renders into the single-message panel — see panel.py.
"""
from __future__ import annotations

import datetime as dt
import os

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from magic_filter import F

import config
import panel
from access import (
    require_employee as _require_employee,
    require_employee_cb as _require_employee_cb,
    require_staff as _require_staff,
    require_staff_cb as _require_staff_cb,
)
from database import Database
from keyboards import back_to_menu_kb, with_back_to_menu
from maxapi.client import MaxClient
from maxapi.filters import Command
from maxapi.router import Router
from maxapi.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from reports import build_excel_report
from scheduler import AttendanceScheduler
from utils import (
    MONTHS_RU_GEN,
    ROLE_LABELS,
    STATUS_LABELS,
    STATUS_LABELS_SHORT,
    fmt_date_human,
    today_msk,
    weekday_ru_for,
)

router = Router(name="admin")

PanelTarget = Message | CallbackQuery


# ----------------------------------------------------------------------
# Invite links — the only way a new account gets registered. Each roster
# entry owns a one-time secret token; the starosta/deputy hands out the
# max.ru deep link built from it, and /start binds automatically.
#
# The employee report link (top of this screen) is different: it's a
# single reusable token, not tied to any name — anyone who follows it
# gets employee (report-only) access. See handlers/employee.py.
# ----------------------------------------------------------------------
async def _invite_list_view(db: Database, bot: MaxClient) -> tuple[str, InlineKeyboardMarkup]:
    me = await bot.get_me()
    employee_token = await db.get_or_create_employee_invite_token()
    employee_link = f"https://max.ru/{me.username}?start=staff-{employee_token}"

    lines = [
        "👔 <b>Ссылка для сотрудников</b> (отчёты, многоразовая):",
        f"<code>{employee_link}</code>",
        "",
    ]

    unregistered = await db.get_unregistered_students()
    if unregistered:
        lines.append(f"Не зарегистрированы в группе ({len(unregistered)}):")
        lines.append("Выберите, чтобы получить персональную ссылку для регистрации.")
    else:
        lines.append("🎉 Все участники группы уже зарегистрированы.")

    buttons = [
        [InlineKeyboardButton(text=s.full_name, callback_data=f"inv:show:{s.id}")]
        for s in unregistered
    ]
    buttons.append([InlineKeyboardButton(text="🔄 Перевыпустить ссылку сотрудников", callback_data="emp_link:regen")])
    return "\n".join(lines), with_back_to_menu(buttons)


async def _render_invite_card(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext,
                               student_id: int, toast: str | None = None) -> None:
    target = await db.get_student_by_id(student_id)
    if target is None or target.max_user_id is not None:
        await callback.answer("Этот человек уже зарегистрирован.", show_alert=True)
        return

    me = await bot.get_me()
    link = f"https://max.ru/{me.username}?start={target.invite_token}"

    text = (
        f"👤 <b>{target.full_name}</b>\n\n"
        f"Ссылка для регистрации (одноразовая):\n<code>{link}</code>\n\n"
        "Перешлите её человеку любым способом (нет прямой кнопки «Отправить в чат», как в Telegram, "
        "поэтому ссылку нужно скопировать вручную)."
    )
    buttons = [
        [InlineKeyboardButton(text="🔄 Перевыпустить ссылку", callback_data=f"inv:regen:{student_id}")],
        [InlineKeyboardButton(text="⬅ К списку", callback_data="inv:back")],
    ]
    await panel.show(callback, state, text, InlineKeyboardMarkup(inline_keyboard=buttons), toast=toast)


@router.message(Command("invites"))
async def cmd_invites(message: Message, db: Database, bot: MaxClient, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    text, kb = await _invite_list_view(db, bot)
    await panel.show(message, state, text, kb)


@router.callback_query(F.data == "menu:invites")
async def cb_menu_invites(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _invite_list_view(db, bot)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data == "inv:back")
async def cb_invites_back(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _invite_list_view(db, bot)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data == "emp_link:regen")
async def cb_employee_link_regen(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await db.regenerate_employee_invite_token()
    text, kb = await _invite_list_view(db, bot)
    await panel.show(callback, state, text, kb, toast="Ссылка сотрудников перевыпущена")


@router.callback_query(F.data.startswith("inv:show:"))
async def cb_invite_show(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    student_id = int(callback.data.split(":", 2)[2])
    await _render_invite_card(callback, db, bot, state, student_id)


@router.callback_query(F.data.startswith("inv:regen:"))
async def cb_invite_regen(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
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
# Grades — one grade per (session, student), set by staff. Reuses the same
# session→student drill-down as the override flow above.
# ----------------------------------------------------------------------
GRADE_CHOICES = ["5", "4", "3", "2"]


class GradeStates(StatesGroup):
    waiting_custom_grade = State()


async def _grades_session_list(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    today = today_msk()
    date_from = (today - dt.timedelta(days=7)).isoformat()
    sessions = await db.get_sessions_range(date_from, today.isoformat())
    if not sessions:
        return "Нет занятий за последние 7 дней для выставления оценок.", back_to_menu_kb()

    buttons = [
        [InlineKeyboardButton(
            text=f"{s['date']} · Пара {s['pair_number']} · {s['subject']}",
            callback_data=f"gr_sess:{s['id']}",
        )]
        for s in sessions
    ]
    return "Выберите занятие для выставления оценок:", with_back_to_menu(buttons)


@router.message(Command("grades"))
async def cmd_grades(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_employee(message, db) is None:
        return
    text, keyboard = await _grades_session_list(db)
    await panel.show(message, state, text, keyboard)


@router.callback_query(F.data == "menu:grades")
async def cb_menu_grades(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return
    text, keyboard = await _grades_session_list(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data == "gr_back")
async def cb_grades_back(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return
    text, keyboard = await _grades_session_list(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data.startswith("gr_sess:"))
async def cb_grades_session(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return

    session_id = int(callback.data.split(":", 1)[1])
    session = await db.get_session(session_id)
    if session is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return

    students = await db.get_all_registered_students()
    buttons = []
    for st in students:
        grade = await db.get_grade(session_id, st.id)
        grade_label = grade["grade"] if grade else "—"
        buttons.append([InlineKeyboardButton(
            text=f"{st.full_name} [{grade_label}]",
            callback_data=f"gr_student:{session_id}:{st.id}",
        )])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="gr_back")])

    await panel.show(
        callback, state,
        f"Занятие: {session['date']} · Пара {session['pair_number']} · {session['subject']}\n"
        "Выберите студента:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("gr_student:"))
async def cb_grades_student(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)
    target = await db.get_student_by_id(student_id)
    if target is None:
        await callback.answer("Студент не найден.", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=g, callback_data=f"gr_set:{session_id}:{student_id}:{g}") for g in GRADE_CHOICES],
        [InlineKeyboardButton(text="✏️ Другое значение", callback_data=f"gr_custom:{session_id}:{student_id}")],
        [InlineKeyboardButton(text="🗑 Убрать оценку", callback_data=f"gr_clear:{session_id}:{student_id}")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data=f"gr_sess:{session_id}")],
    ]
    await panel.show(
        callback, state,
        f"Студент: {target.full_name}\nВыберите оценку за эту пару:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("gr_set:"))
async def cb_grades_set(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s, grade = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)

    await db.set_grade(session_id, student_id, grade)
    target = await db.get_student_by_id(student_id)

    await panel.show(
        callback, state,
        f"✅ Оценка студента «{target.full_name}» установлена: {grade}",
        back_to_menu_kb(),
        toast="Оценка обновлена",
    )


@router.callback_query(F.data.startswith("gr_clear:"))
async def cb_grades_clear(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)

    await db.delete_grade(session_id, student_id)
    target = await db.get_student_by_id(student_id)

    await panel.show(
        callback, state,
        f"✅ Оценка студента «{target.full_name}» за эту пару удалена",
        back_to_menu_kb(),
        toast="Оценка удалена",
    )


@router.callback_query(F.data.startswith("gr_custom:"))
async def cb_grades_custom(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_employee_cb(callback, db) is None:
        return

    _, session_id_s, student_id_s = callback.data.split(":")
    session_id, student_id = int(session_id_s), int(student_id_s)
    await state.update_data(gr_session_id=session_id, gr_student_id=student_id)
    await state.set_state(GradeStates.waiting_custom_grade)
    await panel.show(
        callback, state, "Отправьте значение оценки одним сообщением (например, «зачёт» или «4+»):",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Назад", callback_data=f"gr_student:{session_id}:{student_id}")]
        ]),
    )


@router.message(GradeStates.waiting_custom_grade)
async def msg_grades_custom(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_employee(message, db) is None:
        return

    data = await state.get_data()
    session_id, student_id = data.get("gr_session_id"), data.get("gr_student_id")
    if session_id is None or student_id is None:
        await panel.show(message, state, "Сессия устарела, начните заново.", back_to_menu_kb())
        await state.clear()
        return

    grade = (message.text or "").strip()
    if not grade:
        await panel.show(message, state, "Пустое значение не подходит, отправьте оценку текстом.")
        return

    await db.set_grade(session_id, student_id, grade)
    target = await db.get_student_by_id(student_id)
    await panel.clear_keep_panel(state)
    await panel.show(
        message, state,
        f"✅ Оценка студента «{target.full_name}» установлена: {grade}",
        back_to_menu_kb(),
        toast="Оценка обновлена",
    )


# ----------------------------------------------------------------------
# Excluded days — a whole date (holiday, cancelled day) removed from
# attendance tracking: no sessions get created for it going forward, and
# any that already exist (with their attendance/grades) are deleted.
# ----------------------------------------------------------------------
class ExcludeDayStates(StatesGroup):
    waiting_date = State()
    waiting_reason = State()


def _parse_excluded_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


async def _exclude_day_list_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    excluded = await db.get_excluded_dates()
    lines = ["🚫 <b>Исключённые дни</b>", "Занятия за эти даты не создаются и не учитываются.", ""]
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Исключить дату", callback_data="excl_add")]
    ]
    if not excluded:
        lines.append("Пока ничего не исключено.")
    else:
        for row in excluded:
            date_obj = dt.date.fromisoformat(row["date"])
            label = fmt_date_human(date_obj)
            if row["reason"]:
                label += f" — {row['reason']}"
            lines.append(f"• {label}")
            buttons.append([InlineKeyboardButton(
                text=f"↩️ Вернуть {date_obj.strftime('%d.%m.%Y')}",
                callback_data=f"excl_remove:{row['date']}",
            )])
    return "\n".join(lines), with_back_to_menu(buttons)


@router.message(Command("exclude_day"))
async def cmd_exclude_day(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    text, keyboard = await _exclude_day_list_view(db)
    await panel.show(message, state, text, keyboard)


@router.callback_query(F.data == "menu:exclude_day")
async def cb_menu_exclude_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, keyboard = await _exclude_day_list_view(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data == "excl_back")
async def cb_exclude_day_back(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, keyboard = await _exclude_day_list_view(db)
    await panel.show(callback, state, text, keyboard)


@router.callback_query(F.data == "excl_add")
async def cb_exclude_day_add(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await state.set_state(ExcludeDayStates.waiting_date)
    await panel.show(
        callback, state,
        "Введите дату, которую нужно исключить, в формате ДД.ММ.ГГГГ (например, 08.03.2027):",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="excl_back")]]),
    )


@router.message(ExcludeDayStates.waiting_date)
async def msg_exclude_day_date(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return

    date_obj = _parse_excluded_date(message.text or "")
    if date_obj is None:
        await panel.show(
            message, state,
            "Не могу разобрать дату. Введите в формате ДД.ММ.ГГГГ, например 08.03.2027:",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="excl_back")]]),
        )
        return

    await state.update_data(excl_date=date_obj.isoformat())
    await state.set_state(ExcludeDayStates.waiting_reason)
    await panel.show(
        message, state,
        f"Дата: {fmt_date_human(date_obj)}\nУкажите причину одним сообщением (например, «Праздник») "
        "или отправьте «-», чтобы оставить без причины:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Отмена", callback_data="excl_back")]]),
    )


@router.message(ExcludeDayStates.waiting_reason)
async def msg_exclude_day_reason(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return

    data = await state.get_data()
    date_iso = data.get("excl_date")
    if date_iso is None:
        await panel.show(message, state, "Сессия устарела, начните заново.", back_to_menu_kb())
        await state.clear()
        return

    reason = (message.text or "").strip()
    reason = None if reason in ("", "-") else reason

    await db.add_excluded_date(date_iso, reason)
    removed = await db.delete_sessions_for_date(date_iso)
    await panel.clear_keep_panel(state)

    date_obj = dt.date.fromisoformat(date_iso)
    text = f"✅ {fmt_date_human(date_obj)} исключён из учёта."
    if removed:
        text += f"\nУдалено занятий за этот день (с посещаемостью и оценками): {removed}."
    await panel.show(message, state, text, back_to_menu_kb(), toast="День исключён")


@router.callback_query(F.data.startswith("excl_remove:"))
async def cb_exclude_day_remove(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    date_iso = callback.data.split(":", 1)[1]
    await db.remove_excluded_date(date_iso)
    text, keyboard = await _exclude_day_list_view(db)
    await panel.show(callback, state, text, keyboard, toast="Исключение снято")


# ----------------------------------------------------------------------
# /report — Excel export (build_excel_report lives in reports.py, shared
# with the automatic weekly report in scheduler.py)
# ----------------------------------------------------------------------


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

    user_id = target.from_user.id
    file_path = await build_excel_report(db, date_from, date_to)
    try:
        await target.bot.send_document(
            file_path,
            f"Посещаемость_{date_from.strftime('%m.%Y')}.xlsx",
            caption=f"📊 Отчёт по посещаемости за {date_from.strftime('%m.%Y')}",
            user_id=user_id,
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
        name = st.full_name if st.max_user_id is not None else f"{st.full_name} (не зарегистрирован)"
        attendance = await db.get_attendance(session_id, st.id)
        if attendance:
            label = STATUS_LABELS[attendance["status"]]
        elif session["closed"]:
            label = "❌ Отсутствовал"
        elif st.max_user_id is None:
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
            file_path,
            f"Посещаемость_{today.isoformat()}.xlsx",
            caption=f"📊 Посещаемость за сегодня, {fmt_date_human(today)} (включая ещё не закрытые пары)",
            user_id=callback.from_user.id,
        )
    finally:
        os.remove(file_path)

    text, kb = await _today_sessions_view(db)
    await panel.show(callback, state, text, kb)


# ----------------------------------------------------------------------
# Student card ("🗂 Карточка студента") — one person's full attendance
# history and current percentage, for when "Кто на паре" isn't enough.
# ----------------------------------------------------------------------
async def _student_list_view(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    students = await db.get_all_students()
    buttons = [
        [InlineKeyboardButton(text=s.full_name, callback_data=f"card:show:{s.id}")]
        for s in students
    ]
    return "Выберите студента, чтобы посмотреть карточку:", with_back_to_menu(buttons)


async def _render_student_card(target: PanelTarget, db: Database, state: FSMContext, student_id: int) -> None:
    student = await db.get_student_by_id(student_id)
    if student is None:
        return

    rows = await db.get_attendance_for_student(student_id)
    present = sum(1 for r in rows if r["status"] == "present")
    absent = sum(1 for r in rows if r["status"] == "absent")
    excused = sum(1 for r in rows if r["status"] == "excused")
    total = len(rows)
    pct = round(present / total * 100) if total else 0

    reg_line = f"✅ Зарегистрирован {student.registered_at}" if student.max_user_id else "❌ Не зарегистрирован"
    lines = [
        f"🗂 <b>{student.full_name}</b>",
        f"Роль: {ROLE_LABELS[student.role]}",
        reg_line,
        "",
        f"Всего пар: {total}",
        f"✅ Присутствовал: {present}",
        f"❌ Отсутствовал: {absent}",
        f"📝 Уважительная причина: {excused}",
        f"📈 Посещаемость: {pct}%",
    ]

    recent_gaps = [r for r in rows if r["status"] in ("absent", "excused")][:10]
    if recent_gaps:
        lines.append("")
        lines.append("<b>Последние пропуски:</b>")
        for r in recent_gaps:
            icon = "❌" if r["status"] == "absent" else "📝"
            lines.append(f"{icon} {r['date']} · Пара {r['pair_number']} · {r['subject']}")

    buttons = [[InlineKeyboardButton(text="⬅ К списку", callback_data="menu:card")]]
    await panel.show(target, state, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "menu:card")
async def cb_menu_card(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    text, kb = await _student_list_view(db)
    await panel.show(callback, state, text, kb)


@router.callback_query(F.data.startswith("card:show:"))
async def cb_card_show(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    student_id = int(callback.data.split(":", 2)[2])
    await _render_student_card(callback, db, state, student_id)


# ----------------------------------------------------------------------
# Group excuse ("📝➕ Групповой пропуск") — mark several students excused
# for one date in a single flow, instead of one-by-one via "Корректировка".
# ----------------------------------------------------------------------
class BulkExcuseStates(StatesGroup):
    waiting_custom_reason = State()


def _bulk_excuse_day_keyboard() -> InlineKeyboardMarkup:
    today = today_msk()
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(7):
        date = today + dt.timedelta(days=i)
        if i == 0:
            label = "Сегодня"
        elif i == 1:
            label = "Завтра"
        else:
            label = f"{weekday_ru_for(date)} {date.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"bexc:day:{date.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return with_back_to_menu(buttons)


async def _bulk_excuse_student_kb(db: Database, selected: list[int]) -> InlineKeyboardMarkup:
    students = await db.get_all_students()
    buttons = [
        [InlineKeyboardButton(
            text=f"{'☑️' if s.id in selected else '⬜'} {s.full_name}",
            callback_data=f"bexc:toggle:{s.id}",
        )]
        for s in students
    ]
    buttons.append([InlineKeyboardButton(text=f"✅ Готово ({len(selected)})", callback_data="bexc:done")])
    buttons.append([InlineKeyboardButton(text="⬅ Отмена", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _bulk_excuse_reason_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=reason, callback_data=f"bexc:reason:{reason}")]
        for reason in config.ABSENCE_REASON_CHIPS
    ]
    buttons.append([InlineKeyboardButton(text="Другое (ввести текст)", callback_data="bexc:reason_custom")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="bexc:back_students")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "menu:bulk_excuse")
async def cb_menu_bulk_excuse(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await panel.clear_keep_panel(state)
    await panel.show(callback, state, "На какой день оформить групповой пропуск?", _bulk_excuse_day_keyboard())


@router.callback_query(F.data.startswith("bexc:day:"))
async def cb_bulk_excuse_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    date_iso = callback.data.split(":", 2)[2]
    await state.update_data(bexc_date=date_iso, bexc_selected=[])
    kb = await _bulk_excuse_student_kb(db, [])
    date = dt.date.fromisoformat(date_iso)
    await panel.show(callback, state, f"Выберите студентов, отсутствующих {fmt_date_human(date)}:", kb)


async def _rerender_bulk_excuse_picker(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("bexc_selected", [])
    date_iso = data.get("bexc_date")
    if date_iso is None:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        await panel.clear_keep_panel(state)
        await panel.show(callback, state, "На какой день оформить групповой пропуск?", _bulk_excuse_day_keyboard())
        return
    date = dt.date.fromisoformat(date_iso)
    kb = await _bulk_excuse_student_kb(db, selected)
    await panel.show(callback, state, f"Выберите студентов, отсутствующих {fmt_date_human(date)}:", kb)


@router.callback_query(F.data.startswith("bexc:toggle:"))
async def cb_bulk_excuse_toggle(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    student_id = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    selected = list(data.get("bexc_selected", []))
    if student_id in selected:
        selected.remove(student_id)
    else:
        selected.append(student_id)
    await state.update_data(bexc_selected=selected)
    await _rerender_bulk_excuse_picker(callback, db, state)


@router.callback_query(F.data == "bexc:back_students")
async def cb_bulk_excuse_back_students(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await _rerender_bulk_excuse_picker(callback, db, state)


@router.callback_query(F.data == "bexc:done")
async def cb_bulk_excuse_done(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    data = await state.get_data()
    selected = data.get("bexc_selected", [])
    if not selected:
        await callback.answer("Выберите хотя бы одного студента.", show_alert=True)
        return
    await panel.show(callback, state, f"Причина пропуска для {len(selected)} чел.?", _bulk_excuse_reason_keyboard())


async def _apply_bulk_excuse(target: PanelTarget, db: Database, state: FSMContext, reason: str) -> None:
    data = await state.get_data()
    date_iso = data.get("bexc_date")
    selected = data.get("bexc_selected", [])

    names = []
    for student_id in selected:
        student = await db.get_student_by_id(student_id)
        if student is None:
            continue
        await db.add_planned_absence(student.id, date_iso, reason)
        await db.excuse_existing_sessions(student.id, date_iso)
        names.append(student.full_name)

    await panel.clear_keep_panel(state)
    date = dt.date.fromisoformat(date_iso)
    lines = [f"✅ Плановый пропуск на {fmt_date_human(date)} оформлен для {len(names)} чел.:", ""]
    lines.extend(f"• {n}" for n in names)
    lines.append("")
    lines.append(f"Причина: {reason}")
    await panel.show(target, state, "\n".join(lines), back_to_menu_kb())


@router.callback_query(F.data.startswith("bexc:reason:"))
async def cb_bulk_excuse_reason_chip(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    reason = callback.data.split(":", 2)[2]
    await _apply_bulk_excuse(callback, db, state, reason)


@router.callback_query(F.data == "bexc:reason_custom")
async def cb_bulk_excuse_reason_custom(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await _require_staff_cb(callback, db) is None:
        return
    await state.set_state(BulkExcuseStates.waiting_custom_reason)
    await panel.show(
        callback, state, "Опишите причину пропуска одним сообщением:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="bexc:back_students")]]),
    )


@router.message(BulkExcuseStates.waiting_custom_reason)
async def msg_bulk_excuse_custom_reason(message: Message, db: Database, state: FSMContext) -> None:
    if await _require_staff(message, db) is None:
        return
    reason = (message.text or "").strip() or "Без указания причины"
    await _apply_bulk_excuse(message, db, state, reason)
