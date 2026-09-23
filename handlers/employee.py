"""Employee-only report menu: read-only attendance exports for people who
aren't on the student roster at all (e.g. deanery staff) and register via
one shared, reusable invite link (see EMPLOYEE_TOKEN_PREFIX / cmd_start in
handlers/student.py) instead of a personal one-time token.

Every flow here is pure callback navigation, no free-text steps, so it
doesn't need any FSM states of its own.
"""
from __future__ import annotations

import datetime as dt
import os

from aiogram.fsm.context import FSMContext
from magic_filter import F

import panel
from access import require_employee_cb, require_staff_cb
from database import Database
from keyboards import employee_menu_kb
from maxapi.router import Router
from maxapi.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from reports import build_excel_report, build_session_excel_report
from utils import MONTHS_RU_GEN, fmt_date_human, today_msk

router = Router(name="employee")

EMPLOYEE_TOKEN_PREFIX = "staff-"


async def render_employee_menu(target: CallbackQuery, db: Database, state: FSMContext) -> None:
    employee = await db.get_employee_by_max_user_id(target.from_user.id)
    if employee and employee.display_name:
        name = employee.display_name
        heading = "👔 <b>Меню сотрудника</b>"
    else:
        student = await db.get_student_by_max_user_id(target.from_user.id)
        name = student.full_name if student else "Сотрудник"
        # Staff land here via "👔 Меню сотрудника" on their own menu, not
        # real employee registration — label it as the preview it is.
        heading = "👔 <b>Меню сотрудника (просмотр)</b>"
    text = f"{heading}\n\nЗдравствуйте, {name}! Здесь можно выгрузить отчёты по посещаемости."
    await panel.show(target, state, text, employee_menu_kb())


@router.callback_query(F.data == "menu:employee_view")
async def cb_menu_employee_view(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await render_employee_menu(callback, db, state)


# ----------------------------------------------------------------------
# Shared: send a period's Excel report (used by day/week/month/all-time)
# ----------------------------------------------------------------------
async def _send_period_report(target: CallbackQuery, db: Database, state: FSMContext,
                               date_from: dt.date, date_to: dt.date, period_label: str) -> None:
    sessions = await db.get_sessions_range(date_from.isoformat(), date_to.isoformat())
    if not sessions:
        await panel.show(target, state, f"Нет данных о посещаемости {period_label}.", employee_menu_kb())
        return

    file_path = await build_excel_report(db, date_from, date_to)
    try:
        await target.bot.send_document(
            file_path,
            f"Посещаемость_{date_from.isoformat()}_{date_to.isoformat()}.xlsx",
            caption=f"📊 Отчёт по посещаемости {period_label}",
            user_id=target.from_user.id,
        )
    finally:
        os.remove(file_path)

    await panel.show(target, state, f"📊 Отчёт {period_label} отправлен выше.", employee_menu_kb())


async def _date_list_kb(db: Database, prefix: str) -> InlineKeyboardMarkup | None:
    dates = await db.get_distinct_session_dates()
    if not dates:
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for date_iso in dates:
        date = dt.date.fromisoformat(date_iso)
        row.append(InlineKeyboardButton(text=fmt_date_human(date), callback_data=f"{prefix}:{date_iso}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ----------------------------------------------------------------------
# "📅 Отчёт за пару" — pick a day, then a specific pair on it
# ----------------------------------------------------------------------
@router.callback_query(F.data == "emp:pair_day")
async def cb_emp_pair_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    kb = await _date_list_kb(db, "emp:pd")
    if kb is None:
        await panel.show(callback, state, "Пока нет ни одной проведённой пары.", employee_menu_kb())
        return
    await panel.show(callback, state, "Выберите день:", kb)


@router.callback_query(F.data.startswith("emp:pd:"))
async def cb_emp_pick_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    date_iso = callback.data.split(":", 2)[2]
    date = dt.date.fromisoformat(date_iso)
    sessions = await db.get_sessions_for_date(date_iso)
    if not sessions:
        await panel.show(
            callback, state, f"На {fmt_date_human(date)} пар не найдено.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="emp:pair_day")]]),
        )
        return
    buttons = [
        [InlineKeyboardButton(text=f"Пара {s['pair_number']} · {s['subject']}", callback_data=f"emp:pdp:{s['id']}")]
        for s in sessions
    ]
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="emp:pair_day")])
    await panel.show(callback, state, f"Пары {fmt_date_human(date)}:", InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("emp:pdp:"))
async def cb_emp_pick_pair(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    session_id = int(callback.data.split(":", 2)[2])
    session = await db.get_session(session_id)
    if session is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return

    file_path = await build_session_excel_report(db, session)
    try:
        await callback.bot.send_document(
            file_path,
            f"Посещаемость_{session['date']}_пара{session['pair_number']}.xlsx",
            caption=f"📊 {session['subject']} · {session['date']} · Пара {session['pair_number']}",
            user_id=callback.from_user.id,
        )
    finally:
        os.remove(file_path)

    await panel.show(callback, state, "✅ Отчёт по паре отправлен выше.", employee_menu_kb())


# ----------------------------------------------------------------------
# "🗓 Отчёт за день"
# ----------------------------------------------------------------------
@router.callback_query(F.data == "emp:day")
async def cb_emp_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    kb = await _date_list_kb(db, "emp:d")
    if kb is None:
        await panel.show(callback, state, "Пока нет ни одной проведённой пары.", employee_menu_kb())
        return
    await panel.show(callback, state, "За какой день выгрузить отчёт?", kb)


@router.callback_query(F.data.startswith("emp:d:"))
async def cb_emp_day_pick(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    date = dt.date.fromisoformat(callback.data.split(":", 2)[2])
    await _send_period_report(callback, db, state, date, date, f"за {fmt_date_human(date)}")


# ----------------------------------------------------------------------
# "📆 Отчёт за неделю"
# ----------------------------------------------------------------------
@router.callback_query(F.data == "emp:week")
async def cb_emp_week(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    today = today_msk()
    this_monday = today - dt.timedelta(days=today.weekday())
    last_monday = this_monday - dt.timedelta(days=7)
    buttons = [
        [InlineKeyboardButton(text="Эта неделя", callback_data=f"emp:w:{this_monday.isoformat()}")],
        [InlineKeyboardButton(text="Прошлая неделя", callback_data=f"emp:w:{last_monday.isoformat()}")],
        [InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")],
    ]
    await panel.show(callback, state, "За какую неделю выгрузить отчёт?", InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("emp:w:"))
async def cb_emp_week_pick(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    monday = dt.date.fromisoformat(callback.data.split(":", 2)[2])
    saturday = monday + dt.timedelta(days=5)
    label = f"за неделю {fmt_date_human(monday)} – {fmt_date_human(saturday)}"
    await _send_period_report(callback, db, state, monday, saturday, label)


# ----------------------------------------------------------------------
# "🈷 Отчёт за месяц"
# ----------------------------------------------------------------------
@router.callback_query(F.data == "emp:month")
async def cb_emp_month(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    today = today_msk()
    prev_last_day = dt.date(today.year, today.month, 1) - dt.timedelta(days=1)
    periods = [(today.year, today.month), (prev_last_day.year, prev_last_day.month)]
    buttons = [
        [InlineKeyboardButton(text=f"📊 {MONTHS_RU_GEN[m]} {y}", callback_data=f"emp:m:{y}-{m:02d}")]
        for y, m in periods
    ]
    buttons.append([InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")])
    await panel.show(callback, state, "За какой месяц выгрузить отчёт?", InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("emp:m:"))
async def cb_emp_month_pick(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    year_s, month_s = callback.data.split(":", 2)[2].split("-")
    year, month = int(year_s), int(month_s)
    date_from = dt.date(year, month, 1)
    date_to = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    await _send_period_report(callback, db, state, date_from, date_to, f"за {MONTHS_RU_GEN[month]} {year}")


# ----------------------------------------------------------------------
# "📚 Отчёт за всё время"
# ----------------------------------------------------------------------
@router.callback_query(F.data == "emp:all")
async def cb_emp_all(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_employee_cb(callback, db) is None:
        return
    bounds = await db.get_session_date_bounds()
    if bounds is None:
        await callback.answer("Пока нет данных.", show_alert=True)
        return
    date_from, date_to = (dt.date.fromisoformat(b) for b in bounds)
    await _send_period_report(callback, db, state, date_from, date_to, "за всё время")
