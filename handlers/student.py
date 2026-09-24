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
from database import Database, Student
from keyboards import back_to_menu_kb, employee_menu_kb, main_menu_kb, with_back_to_menu
from maxapi.client import MaxApiError, MaxClient
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

from .employee import EMPLOYEE_TOKEN_PREFIX, render_employee_menu

router = Router(name="student")

PanelTarget = Message | CallbackQuery


class AbsenceStates(StatesGroup):
    choosing_pairs = State()
    choosing_reason = State()
    waiting_custom_reason = State()


async def _notify_staff_of_absence(db: Database, bot: MaxClient, student: Student, text: str) -> None:
    """Lets the starosta/deputy know a propusk was just filed — skips the
    filer themselves if they happen to be staff."""
    for person in await db.get_staff():
        if person.id == student.id:
            continue
        try:
            await bot.send_message(text, user_id=person.max_user_id)
        except MaxApiError:
            pass


NEEDS_INVITE_TEXT = (
    "🔒 Для регистрации нужна персональная пригласительная ссылка.\n"
    "Обратитесь к старосте или заместителю старосты — они отправят вам вашу ссылку."
)


async def render_main_menu(target: PanelTarget, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(target.from_user.id)
    if student:
        await panel.show(target, state, "🏠 <b>Главное меню</b>", main_menu_kb(student.is_staff))
        return
    employee = await db.get_employee_by_max_user_id(target.from_user.id)
    if employee:
        await render_employee_menu(target, db, state)
        return
    await panel.show(target, state, NEEDS_INVITE_TEXT)


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.set_state(None)
    await render_main_menu(callback, db, state)


@router.message(Command("menu"))
async def cmd_menu(message: Message, db: Database, state: FSMContext) -> None:
    # Escape hatch if the panel message got lost/deleted or a wizard state
    # got stuck — resets state and re-renders (or re-sends) the menu.
    await state.set_state(None)
    await render_main_menu(message, db, state)


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

    employee = await db.get_employee_by_max_user_id(message.from_user.id)
    if employee:
        await render_employee_menu(message, db, state)
        return

    token = command.args
    if token:
        if token.startswith(EMPLOYEE_TOKEN_PREFIX):
            plain_token = token[len(EMPLOYEE_TOKEN_PREFIX):]
            bound_employee = await db.register_employee_by_token(
                plain_token, message.from_user.id, message.from_user.name
            )
            if bound_employee:
                await panel.show(
                    message, state,
                    "✅ Готово! Вам открыт доступ сотрудника к отчётам по посещаемости.",
                    employee_menu_kb(),
                )
                return
            await panel.show(message, state, "Ссылка недействительна или устарела. Обратитесь к старосте за новой.")
            return

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
    employee = await db.get_employee_by_max_user_id(message.from_user.id)
    if employee:
        await panel.show(message, state, "У вас уже есть доступ сотрудника к отчётам.", employee_menu_kb())
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
    today_iso = today.isoformat()
    weekday_ru = weekday_ru_for(today)
    entries = await db.get_schedule_for_weekday(weekday_ru)

    if not entries:
        await panel.show(target, state, f"📅 Сегодня, {fmt_date_human(today)}, занятий нет.", back_to_menu_kb())
        return

    day_planned = await db.get_planned_absence(student.id, today_iso)

    lines = [f"📅 Расписание на сегодня, {fmt_date_human(today)}:\n"]
    checkin_buttons: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        session = await db.get_session_by_schedule_and_date(entry["id"], today_iso)
        pair_planned = await db.get_planned_absence_pair(student.id, today_iso, entry["pair_number"])
        status_text = "⏳ ещё не началась"

        if session:
            attendance = await db.get_attendance(session["id"], student.id)
            if attendance:
                status_text = STATUS_LABELS[attendance["status"]]
                # Filed a propusk but actually made it — let them flip
                # themselves back to "present" while the window is open.
                if attendance["status"] == "excused" and not session["closed"]:
                    checkin_buttons.append([InlineKeyboardButton(
                        text=f"✅ Всё-таки пришёл на пару {entry['pair_number']}",
                        callback_data=f"checkin:{session['id']}",
                    )])
            elif session["closed"]:
                status_text = "❌ Отсутствовал"
            elif session["notified"]:
                status_text = "🟡 Идёт отметка"
                checkin_buttons.append([InlineKeyboardButton(
                    text=f"✅ Отметиться на паре {entry['pair_number']}",
                    callback_data=f"checkin:{session['id']}",
                )])
        elif day_planned or pair_planned:
            # Session doesn't exist yet (before the notify window) — the
            # only way back is to cancel the propusk itself.
            status_text = "📝 Уважительная причина (плановый пропуск)"
            checkin_buttons.append([InlineKeyboardButton(
                text=f"↩️ Отменить пропуск на пару {entry['pair_number']}",
                callback_data=f"absence_cancel_pair:{today_iso}:{entry['pair_number']}",
            )])

        lines.append(
            f"<b>Пара {entry['pair_number']}</b> ({entry['start_time']}–{entry['end_time']})\n"
            f"{entry['subject']} — {entry['class_type']}\n"
            f"Преподаватель: {entry['teacher']}\n"
            f"Статус: {status_text}\n"
        )

    if checkin_buttons:
        lines.append(
            "Если пропустили уведомление о начале пары, всё-таки пришли после того как оформили "
            "пропуск, или передумали — используйте кнопки ниже."
        )
    kb = with_back_to_menu(checkin_buttons) if checkin_buttons else back_to_menu_kb()
    await panel.show(target, state, "\n".join(lines), kb)


@router.callback_query(F.data.startswith("absence_cancel_pair:"))
async def cb_absence_cancel_pair(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    _, date_iso, pair_s = callback.data.split(":")
    pair_number = int(pair_s)

    day_planned = await db.get_planned_absence(student.id, date_iso)
    if day_planned:
        await db.remove_planned_absence(student.id, date_iso)
        await db.revert_excused_for_open_sessions(student.id, date_iso)
        toast = "Плановый пропуск на весь день отменён."
    else:
        await db.remove_planned_absence_pair(student.id, date_iso, pair_number)
        await db.revert_excused_for_open_session_pair(student.id, date_iso, pair_number)
        toast = "Пропуск на эту пару отменён."

    await callback.answer(toast)
    await _render_today(callback, db, state)


@router.message(Command("today"))
async def cmd_today(message: Message, db: Database, state: FSMContext) -> None:
    await _render_today(message, db, state)


@router.callback_query(F.data == "menu:today")
async def cb_menu_today(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_today(callback, db, state)


# ----------------------------------------------------------------------
# /week — read-only view of the whole week's schedule, open to everyone
# (as opposed to "🗓 Расписание", the staff-only editor).
# ----------------------------------------------------------------------
async def _render_week(target: PanelTarget, db: Database, state: FSMContext) -> None:
    entries = await db.get_full_schedule()
    if not entries:
        await panel.show(target, state, "🗓 Расписание пока не заполнено.", back_to_menu_kb())
        return

    by_day: dict[str, list] = {}
    for entry in entries:
        by_day.setdefault(entry["weekday"], []).append(entry)

    lines = ["🗓 <b>Расписание на неделю</b>"]
    for day in config.WORKING_WEEKDAYS_RU:
        if day not in by_day:
            continue
        lines.append("")
        lines.append(f"<b>{config.WEEKDAY_FULL_RU[day]}</b>")
        for entry in sorted(by_day[day], key=lambda e: e["pair_number"]):
            lines.append(
                f"{entry['pair_number']}. {entry['start_time']}–{entry['end_time']} "
                f"{entry['subject']} ({entry['class_type']}) — {entry['teacher']}"
            )

    await panel.show(target, state, "\n".join(lines), back_to_menu_kb())


@router.message(Command("week"))
async def cmd_week(message: Message, db: Database, state: FSMContext) -> None:
    await _render_week(message, db, state)


@router.callback_query(F.data == "menu:week")
async def cb_menu_week(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_week(callback, db, state)


# ----------------------------------------------------------------------
# /absence — planned absence, either for the whole day or for specific
# pairs only. Staff (starosta/deputy) get a heads-up message either way —
# see _notify_staff_of_absence.
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


def _absence_scope_keyboard(date_iso: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Весь день", callback_data=f"absence_scope:day:{date_iso}")],
        [InlineKeyboardButton(text="🎯 Отдельные пары", callback_data=f"absence_scope:pairs:{date_iso}")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="absence_back_days")],
    ])


def _absence_pairs_keyboard(entries, selected: list[int]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{'☑️' if e['pair_number'] in selected else '⬜'} Пара {e['pair_number']} — {e['subject']}",
            callback_data=f"absence_pair_toggle:{e['pair_number']}",
        )]
        for e in entries
    ]
    buttons.append([InlineKeyboardButton(text=f"✅ Готово ({len(selected)})", callback_data="absence_pairs_done")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="absence_back_days")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
    await panel.show(target, state, "На какой день оформить пропуск?", _absence_day_keyboard())


@router.message(Command("absence"))
async def cmd_absence(message: Message, db: Database, state: FSMContext) -> None:
    await _render_absence_start(message, db, state)


@router.callback_query(F.data == "menu:absence")
async def cb_menu_absence(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await _render_absence_start(callback, db, state)


@router.callback_query(F.data == "absence_back_days")
async def cb_absence_back_days(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await panel.show(callback, state, "На какой день оформить пропуск?", _absence_day_keyboard())


@router.callback_query(F.data.startswith("absence_day:"))
async def cb_absence_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    date_iso = callback.data.split(":", 1)[1]
    date = dt.date.fromisoformat(date_iso)
    await state.set_state(None)
    await state.update_data(absence_date=date_iso, absence_pairs=None)

    entries = await db.get_schedule_for_weekday(weekday_ru_for(date))
    if not entries:
        # Nothing on the schedule that day — no pairs to split into,
        # skip straight to a whole-day propusk (which is a no-op anyway).
        await state.set_state(AbsenceStates.choosing_reason)
        await panel.show(callback, state, f"Причина пропуска на {fmt_date_human(date)}?", _absence_reason_keyboard())
        return

    await panel.show(
        callback, state, f"Пропуск на {fmt_date_human(date)} — весь день или отдельные пары?",
        _absence_scope_keyboard(date_iso),
    )


@router.callback_query(F.data.startswith("absence_scope:day:"))
async def cb_absence_scope_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    date_iso = callback.data.split(":", 2)[2]
    await state.update_data(absence_date=date_iso, absence_pairs=None)
    await state.set_state(AbsenceStates.choosing_reason)

    date = dt.date.fromisoformat(date_iso)
    await panel.show(callback, state, f"Причина пропуска на {fmt_date_human(date)}?", _absence_reason_keyboard())


@router.callback_query(F.data.startswith("absence_scope:pairs:"))
async def cb_absence_scope_pairs(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    date_iso = callback.data.split(":", 2)[2]
    date = dt.date.fromisoformat(date_iso)
    entries = await db.get_schedule_for_weekday(weekday_ru_for(date))
    if not entries:
        await callback.answer("На этот день пар нет.", show_alert=True)
        return

    await state.update_data(absence_date=date_iso, absence_pairs=[])
    await state.set_state(AbsenceStates.choosing_pairs)
    await panel.show(callback, state, f"Выберите пары на {fmt_date_human(date)}:", _absence_pairs_keyboard(entries, []))


@router.callback_query(AbsenceStates.choosing_pairs, F.data.startswith("absence_pair_toggle:"))
async def cb_absence_pair_toggle(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    pair_number = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    date_iso = data.get("absence_date")
    if date_iso is None:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        await state.clear()
        return

    selected = list(data.get("absence_pairs") or [])
    if pair_number in selected:
        selected.remove(pair_number)
    else:
        selected.append(pair_number)
    await state.update_data(absence_pairs=selected)

    date = dt.date.fromisoformat(date_iso)
    entries = await db.get_schedule_for_weekday(weekday_ru_for(date))
    await panel.show(callback, state, f"Выберите пары на {fmt_date_human(date)}:", _absence_pairs_keyboard(entries, selected))


@router.callback_query(AbsenceStates.choosing_pairs, F.data == "absence_pairs_done")
async def cb_absence_pairs_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("absence_pairs") or []
    if not selected:
        await callback.answer("Выберите хотя бы одну пару.", show_alert=True)
        return
    await state.set_state(AbsenceStates.choosing_reason)
    await panel.show(callback, state, f"Причина пропуска для {len(selected)} пар(ы)?", _absence_reason_keyboard())


async def _apply_absence(target: PanelTarget, db: Database, bot: MaxClient, state: FSMContext,
                          student: Student, date_iso: str, pairs: list[int] | None, reason: str) -> None:
    date = dt.date.fromisoformat(date_iso)

    if pairs:
        for pair_number in sorted(pairs):
            await db.add_planned_absence_pair(student.id, date_iso, pair_number, reason)
            await db.excuse_existing_session_pair(student.id, date_iso, pair_number)
        pairs_str = ", ".join(str(p) for p in sorted(pairs))
        await _notify_staff_of_absence(
            db, bot, student,
            f"📝 {student.full_name} оформил(а) пропуск пар {pairs_str} на {fmt_date_human(date)}.\n"
            f"Причина: {reason}",
        )
        await panel.clear_keep_panel(state)
        await panel.show(
            target, state,
            f"✅ Пропуск оформлен на {fmt_date_human(date)}, пары: {pairs_str}.\nПричина: {reason}",
            back_to_menu_kb(),
        )
        return

    await db.add_planned_absence(student.id, date_iso, reason)
    await db.excuse_existing_sessions(student.id, date_iso)
    await _notify_staff_of_absence(
        db, bot, student,
        f"📝 {student.full_name} оформил(а) плановый пропуск на весь день {fmt_date_human(date)}.\n"
        f"Причина: {reason}",
    )
    await panel.clear_keep_panel(state)
    await panel.show(
        target, state,
        f"✅ Плановый пропуск оформлен на {fmt_date_human(date)}.\nПричина: {reason}\n\n"
        "В этот день вы автоматически будете отмечены как «Уважительная причина» и не будете "
        "получать уведомления о начале пар.",
        back_to_menu_kb(),
    )


@router.callback_query(AbsenceStates.choosing_reason, F.data.startswith("absence_reason:"))
async def cb_absence_reason_chip(callback: CallbackQuery, db: Database, bot: MaxClient, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    data = await state.get_data()
    date_iso = data.get("absence_date")
    if student is None or date_iso is None:
        await callback.answer("Сессия устарела, начните заново.", show_alert=True)
        await state.clear()
        return

    reason = callback.data.split(":", 1)[1]
    await _apply_absence(callback, db, bot, state, student, date_iso, data.get("absence_pairs"), reason)


@router.callback_query(AbsenceStates.choosing_reason, F.data == "absence_reason_custom")
async def cb_absence_reason_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AbsenceStates.waiting_custom_reason)
    await panel.show(
        callback, state, "Опишите причину пропуска одним сообщением:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="absence_back_days")]]),
    )


@router.message(AbsenceStates.waiting_custom_reason)
async def msg_absence_custom_reason(message: Message, db: Database, bot: MaxClient, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    data = await state.get_data()
    date_iso = data.get("absence_date")
    if student is None or date_iso is None:
        await panel.show(message, state, "Сессия устарела, начните заново.", back_to_menu_kb())
        await state.clear()
        return

    reason = (message.text or "").strip() or "Без указания причины"
    await _apply_absence(message, db, bot, state, student, date_iso, data.get("absence_pairs"), reason)


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

    existing = await db.get_attendance(session_id, student.id)
    if existing and existing["status"] == "present":
        await callback.answer("Вы уже отмечены на этой паре.", show_alert=True)
        return

    # Overwrites whatever was there before (nothing, "excused" from a
    # propusk the student is now walking back, or a stale "absent") —
    # this is also how a self-check-in after filing a propusk works.
    now = now_msk()
    await db.set_attendance(session_id, student.id, "present")

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
    if student is None:
        employee = await db.get_employee_by_max_user_id(target.from_user.id)
        if employee:
            await panel.show(
                target, state,
                "<b>Меню сотрудника</b>\nВыгрузка Excel-отчётов по посещаемости: за конкретную пару, "
                "день, неделю, месяц или за всё время.",
                employee_menu_kb(),
            )
            return
    lines = [
        "<b>Меню бота</b>",
        "📅 Сегодня — расписание и статус на сегодня",
        "🗓 Неделя — расписание на всю неделю (только просмотр)",
        "📝 Пропуск — оформить пропуск на весь день или на отдельные пары (с указанием причины); "
        "о нём автоматически узнают староста и заместитель",
        "📚 Домашки — загрузить домашнее задание по предмету или скачать то, что загрузили другие",
        "",
        "Если оформили пропуск, а всё-таки пришли — откройте «📅 Сегодня»: там появится кнопка "
        "«✅ Всё-таки пришёл», либо «↩️ Отменить пропуск», если пара ещё не началась.",
        "",
        "Команда /menu возвращает это меню, если панель потерялась.",
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
            "🗂 Карточка студента — история посещаемости и % по конкретному человеку",
            "📝➕ Групповой пропуск — отметить сразу нескольких студентов уважительной причиной",
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
