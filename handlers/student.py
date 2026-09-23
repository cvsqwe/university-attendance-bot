"""
Student-facing handlers: /start registration via personal invite link,
/today, /absence and the single-click "Я на паре" check-in callback.

Everything below /start renders into a single per-chat "panel" message
(see panel.py) that's edited in place — tapping a button, or typing a
reply where free text is required, never leaves old screens behind. The
per-session "Я на паре" check-in messages sent by the scheduler are the
one deliberate exception: those are timed notifications, not menu
screens, so they stay as their own messages.
"""
from __future__ import annotations

import datetime as dt

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from magic_filter import F

import config
import panel
from database import Database
from keyboards import back_to_menu_kb, main_menu_kb, with_back_to_menu
from maxapi.filters import Command, CommandObject, CommandStart
from maxapi.router import Router
from maxapi.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from utils import (
    ROLE_LABELS,
    STATUS_LABELS,
    fmt_date_human,
    now_msk,
    today_msk,
    weekday_ru_for,
)

router = Router(name="student")

PanelTarget = Message | CallbackQuery


class AbsenceStates(StatesGroup):
    choosing_reason = State()
    waiting_custom_reason = State()


NEEDS_INVITE_TEXT = (
    "🔒 Для регистрации нужна персональная пригласительная ссылка.\n"
    "Обратитесь к старосте или заместителю старосты — они отправят вам вашу ссылку."
)


async def render_main_menu(target: PanelTarget, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(target.from_user.id)
    await panel.show(target, state, "🏠 <b>Главное меню</b>", main_menu_kb(bool(student and student.is_staff)))


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.set_state(None)
    await render_main_menu(callback, db, state)


# ----------------------------------------------------------------------
# Registration: /start <invite_token> (deep link), /register
#
# There's no self-service name picker any more: an outsider who opens the
# bot has no roster to browse. Registration only succeeds via a personal,
# one-time invite link that the starosta/deputy hands out (see
# handlers/admin.py "🔗 Ссылки").
# ----------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    if student:
        role_label = ROLE_LABELS[student.role]
        await panel.show(
            message, state,
            f"Здравствуйте, {student.full_name}!\nВы зарегистрированы как: <b>{role_label}</b>.\n\n"
            "🏠 <b>Главное меню</b>",
            main_menu_kb(student.is_staff),
        )
        return

    token = command.args
    if token:
        bound = await db.register_student_by_token(token, message.from_user.id)
        if bound:
            role_label = ROLE_LABELS[bound.role]
            await panel.show(
                message, state,
                f"✅ Готово! Вы зарегистрированы как <b>{bound.full_name}</b> ({role_label}).\n\n"
                "🏠 <b>Главное меню</b>",
                main_menu_kb(bound.is_staff),
            )
            return
        owner = await db.get_student_by_invite_token(token)
        if owner is not None and owner.max_user_id is not None:
            await panel.show(
                message, state,
                "Эта пригласительная ссылка уже была использована.\n"
                "Если это не вы — сообщите старосте, он перевыпустит ссылку.",
            )
            return

    await panel.show(message, state, NEEDS_INVITE_TEXT)


@router.message(Command("register"))
async def cmd_register(message: Message, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    if student:
        await panel.show(
            message, state, f"Вы уже зарегистрированы как {student.full_name}.", main_menu_kb(student.is_staff)
        )
        return
    await panel.show(message, state, NEEDS_INVITE_TEXT)


# ----------------------------------------------------------------------
# /today — today's schedule and this student's attendance status
# ----------------------------------------------------------------------
async def _render_today(target: PanelTarget, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(target.from_user.id)
    if student is None:
        await panel.show(target, state, NEEDS_INVITE_TEXT)
        return

    today = today_msk()
    weekday_ru = weekday_ru_for(today)
    entries = await db.get_schedule_for_weekday(weekday_ru)

    if not entries:
        await panel.show(target, state, f"📅 Сегодня, {fmt_date_human(today)}, занятий нет.", back_to_menu_kb())
        return

    planned_absence = await db.get_planned_absence(student.id, today.isoformat())

    lines = [f"📅 Расписание на сегодня, {fmt_date_human(today)}:\n"]
    checkin_buttons: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        session = await db.get_session_by_schedule_and_date(entry["id"], today.isoformat())
        status_text = "⏳ ещё не началась"
        if planned_absence:
            status_text = "📝 Уважительная причина (плановый пропуск)"
        elif session:
            attendance = await db.get_attendance(session["id"], student.id)
            if attendance:
                status_text = STATUS_LABELS[attendance["status"]]
            elif session["closed"]:
                status_text = "❌ Отсутствовал"
            elif session["notified"]:
                status_text = "🟡 Идёт отметка"
                checkin_buttons.append([InlineKeyboardButton(
                    text=f"✅ Отметиться на паре {entry['pair_number']}",
                    callback_data=f"checkin:{session['id']}",
                )])

        lines.append(
            f"<b>Пара {entry['pair_number']}</b> ({entry['start_time']}–{entry['end_time']})\n"
            f"{entry['subject']} — {entry['class_type']}\n"
            f"Преподаватель: {entry['teacher']}\n"
            f"Статус: {status_text}\n"
        )

    if checkin_buttons:
        lines.append(
            "Если пропустили уведомление о начале пары (например, из-за связи) — "
            "отметьтесь кнопкой ниже, пока отметка не закрыта."
        )
    kb = with_back_to_menu(checkin_buttons) if checkin_buttons else back_to_menu_kb()
    await panel.show(target, state, "\n".join(lines), kb)


@router.message(Command("today"))
async def cmd_today(message: Message, db: Database, state: FSMContext) -> None:
    await _render_today(message, db, state)


@router.callback_query(F.data == "menu:today")
async def cb_menu_today(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_today(callback, db, state)


# ----------------------------------------------------------------------
# /absence — planned absence for a whole day
# ----------------------------------------------------------------------
def _absence_day_keyboard() -> InlineKeyboardMarkup:
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
        row.append(InlineKeyboardButton(text=label, callback_data=f"absence_day:{date.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return with_back_to_menu(buttons)


def _absence_reason_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=reason, callback_data=f"absence_reason:{reason}")]
        for reason in config.ABSENCE_REASON_CHIPS
    ]
    buttons.append([InlineKeyboardButton(text="Другое (ввести текст)", callback_data="absence_reason_custom")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="absence_back_days")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_absence_start(target: PanelTarget, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(target.from_user.id)
    if student is None:
        await panel.show(target, state, NEEDS_INVITE_TEXT)
        return
    await panel.clear_keep_panel(state)
    await panel.show(target, state, "На какой день оформить плановый пропуск?", _absence_day_keyboard())


@router.message(Command("absence"))
async def cmd_absence(message: Message, db: Database, state: FSMContext) -> None:
    await _render_absence_start(message, db, state)


@router.callback_query(F.data == "menu:absence")
async def cb_menu_absence(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_absence_start(callback, db, state)


@router.callback_query(F.data == "absence_back_days")
async def cb_absence_back_days(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await panel.show(callback, state, "На какой день оформить плановый пропуск?", _absence_day_keyboard())


@router.callback_query(F.data.startswith("absence_day:"))
async def cb_absence_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    date_iso = callback.data.split(":", 1)[1]
    await state.update_data(absence_date=date_iso)
    await state.set_state(AbsenceStates.choosing_reason)

    date = dt.date.fromisoformat(date_iso)
    await panel.show(callback, state, f"Причина пропуска на {fmt_date_human(date)}?", _absence_reason_keyboard())


@router.callback_query(AbsenceStates.choosing_reason, F.data.startswith("absence_reason:"))
async def cb_absence_reason_chip(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    data = await state.get_data()
    date_iso = data.get("absence_date")
    if student is None or date_iso is None:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        await state.clear()
        return

    reason = callback.data.split(":", 1)[1]
    await db.add_planned_absence(student.id, date_iso, reason)
    await db.excuse_existing_sessions(student.id, date_iso)
    await panel.clear_keep_panel(state)

    date = dt.date.fromisoformat(date_iso)
    await panel.show(
        callback, state,
        f"✅ Плановый пропуск оформлен на {fmt_date_human(date)}.\nПричина: {reason}\n\n"
        "В этот день вы автоматически будете отмечены как «Уважительная причина» и не будете "
        "получать уведомления о начале пар.",
        back_to_menu_kb(),
    )


@router.callback_query(AbsenceStates.choosing_reason, F.data == "absence_reason_custom")
async def cb_absence_reason_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AbsenceStates.waiting_custom_reason)
    await panel.show(
        callback, state, "Опишите причину пропуска одним сообщением:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="absence_back_days")]]),
    )


@router.message(AbsenceStates.waiting_custom_reason)
async def msg_absence_custom_reason(message: Message, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    data = await state.get_data()
    date_iso = data.get("absence_date")
    if student is None or date_iso is None:
        await panel.show(message, state, "Сессия устарела, начните заново.", back_to_menu_kb())
        await state.clear()
        return

    reason = (message.text or "").strip() or "Без указания причины"
    await db.add_planned_absence(student.id, date_iso, reason)
    await db.excuse_existing_sessions(student.id, date_iso)
    await panel.clear_keep_panel(state)

    date = dt.date.fromisoformat(date_iso)
    await panel.show(
        message, state,
        f"✅ Плановый пропуск оформлен на {fmt_date_human(date)}.\nПричина: {reason}",
        back_to_menu_kb(),
    )


# ----------------------------------------------------------------------
# Check-in: "🟢 Я на паре" — a scheduler-pushed notification, not a panel
# screen, so it deliberately stays its own message.
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("checkin:"))
async def cb_checkin(callback: CallbackQuery, db: Database) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы. Используйте /start.", show_alert=True)
        return

    session_id = int(callback.data.split(":", 1)[1])
    session = await db.get_session(session_id)
    if session is None:
        await callback.answer("Занятие не найдено.", show_alert=True)
        return
    if session["closed"]:
        await callback.answer("Окно отметки уже закрыто.", show_alert=True)
        return

    now = now_msk()
    inserted = await db.mark_attendance(session_id, student.id, "present", marked_at=now.isoformat(timespec="seconds"))
    if not inserted:
        await callback.answer("Вы уже отмечены на этой паре.", show_alert=True)
        return

    time_str = now.strftime("%H:%M")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подключиться", url=session["link"])],
        [InlineKeyboardButton(text=f"✅ Вы отмечены ({time_str})", callback_data="noop")],
        [InlineKeyboardButton(text="⬅ В меню", callback_data="menu:home")],
    ])
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass
    await callback.answer("Отметка принята!")


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ----------------------------------------------------------------------
# /help
# ----------------------------------------------------------------------
async def _render_help(target: PanelTarget, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(target.from_user.id)
    lines = [
        "<b>Меню бота</b>",
        "📅 Сегодня — расписание и статус на сегодня",
        "📝 Пропуск — оформить плановый пропуск дня",
    ]
    if student and student.is_staff:
        lines += [
            "",
            "<b>Для старосты / заместителя:</b>",
            "🗓 Расписание — добавить/изменить/удалить пары кнопками (только ПН–СБ)",
            "✏️ Корректировка — вручную изменить статус посещения",
            "📊 Отчёт — выгрузить Excel-отчёт по посещаемости за месяц",
            "👥 Кто на паре — статус текущих/сегодняшних пар в реальном времени, "
            "можно смотреть до закрытия отметки, плюс Excel-выгрузка за сегодня",
            "🔗 Ссылки — выдать/перевыпустить пригласительную ссылку для регистрации",
            "",
            "Каждую субботу сразу после последней пары старосте и заместителю "
            "автоматически приходит Excel-отчёт за всю неделю.",
            "",
            "Новые люди регистрируются только по личной ссылке из «🔗 Ссылки» — "
            "открытой формы выбора ФИО из списка больше нет, это защита от посторонних.",
            "",
            "Кнопка «📋 Массовая загрузка» внутри «🗓 Расписание» позволяет вставить "
            "сразу несколько пар одним сообщением: ДЕНЬ|ПАРА|ТИП|ПРЕДМЕТ|ПРЕПОДАВАТЕЛЬ|ССЫЛКА, "
            "где ТИП — Л (лекция), ПР (практика) или ЛР (лабораторная), дни только ПН–СБ. "
            "То же самое делает команда /set_schedule с такими же строками.",
        ]
    await panel.show(target, state, "\n".join(lines), back_to_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message, db: Database, state: FSMContext) -> None:
    await _render_help(message, db, state)


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_help(callback, db, state)
