"""Interactive, button-driven weekly schedule editor for the starosta and
deputy starosta. Pick a day, pick a pair slot, then a single "hub" card
lets you set type/subject/teacher in any order (previously-used values
become one-tap choices) before moving on to the link and a final
confirmation. Every screen has a real "⬅ Назад" that returns to the
previous step without losing what was already entered, and everything
renders into the single-message panel (see panel.py) instead of piling up
new messages.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import panel
from access import require_staff, require_staff_cb
from database import Database
from keyboards import with_back_to_menu
from scheduler import AttendanceScheduler

router = Router(name="schedule")

CLASS_TYPES = ["Лекция", "Практика", "Лабораторная"]

PanelTarget = Message | CallbackQuery


class ScheduleWizard(StatesGroup):
    entering_class_type = State()
    entering_subject = State()
    entering_teacher = State()
    entering_link = State()


# ----------------------------------------------------------------------
# Day picker (entry point)
# ----------------------------------------------------------------------
def _weekday_picker_kb() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for day in config.WORKING_WEEKDAYS_RU:
        row.append(InlineKeyboardButton(text=config.WEEKDAY_FULL_RU[day], callback_data=f"sw:day:{day}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="📋 Массовая загрузка (весь список)", callback_data="sched:bulk")])
    return with_back_to_menu(buttons)


async def _render_weekday_picker(target: PanelTarget, state: FSMContext) -> None:
    await panel.show(target, state, "🗓 <b>Управление расписанием</b>\n\nВыберите день недели:", _weekday_picker_kb())


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, db: Database, state: FSMContext) -> None:
    if await require_staff(message, db) is None:
        return
    await _render_weekday_picker(message, state)


@router.callback_query(F.data == "menu:schedule")
async def cb_menu_schedule(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await _render_weekday_picker(callback, state)


@router.callback_query(F.data == "sw:days")
async def cb_sw_days(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await _render_weekday_picker(callback, state)


# ----------------------------------------------------------------------
# Day view: 7 pair slots, occupied or empty
# ----------------------------------------------------------------------
async def _day_view(weekday: str, db: Database) -> tuple[str, InlineKeyboardMarkup]:
    entries = {e["pair_number"]: e for e in await db.get_schedule_for_weekday(weekday)}
    lines = [f"🗓 <b>{config.WEEKDAY_FULL_RU[weekday]}</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for pair in range(1, 8):
        start, end = config.BELL_SCHEDULE[pair]
        entry = entries.get(pair)
        if entry:
            lines.append(f"{pair}. {start}–{end} — {entry['subject']} ({entry['class_type']})")
            label = f"{pair}. {entry['subject']}"
        else:
            label = f"{pair}. {start}–{end} — пусто"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"sw:pair:{weekday}:{pair}")])
    if not entries:
        lines.append("Занятий пока нет.")
    buttons.append([InlineKeyboardButton(text="⬅ К дням недели", callback_data="sw:days")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("sw:day:"))
async def cb_sw_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    weekday = callback.data.split(":", 2)[2]
    text, kb = await _day_view(weekday, db)
    await panel.show(callback, state, text, kb)


# ----------------------------------------------------------------------
# Slot detail: view / edit / delete a single pair
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("sw:pair:"))
async def cb_sw_pair(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    _, _, weekday, pair_s = callback.data.split(":")
    pair_number = int(pair_s)
    entry = await db.get_schedule_entry_by_slot(weekday, pair_number)
    start, end = config.BELL_SCHEDULE[pair_number]
    header = f"<b>{config.WEEKDAY_FULL_RU[weekday]}, пара {pair_number}</b> ({start}–{end})"

    if entry:
        text = (
            f"{header}\n\n"
            f"📚 {entry['subject']} — {entry['class_type']}\n"
            f"👤 {entry['teacher']}\n"
            f"🔗 {entry['link']}"
        )
        buttons = [
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"sw:edit:{weekday}:{pair_number}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sw:del:{weekday}:{pair_number}")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data=f"sw:day:{weekday}")],
        ]
    else:
        text = f"{header}\n\nСлот свободен."
        buttons = [
            [InlineKeyboardButton(text="➕ Добавить пару", callback_data=f"sw:edit:{weekday}:{pair_number}")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data=f"sw:day:{weekday}")],
        ]
    await panel.show(callback, state, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("sw:del:"))
async def cb_sw_delete_confirm(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    _, _, weekday, pair_s = callback.data.split(":")
    buttons = [
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"sw:delyes:{weekday}:{pair_s}")],
        [InlineKeyboardButton(text="⬅ Отмена", callback_data=f"sw:pair:{weekday}:{pair_s}")],
    ]
    await panel.show(
        callback, state,
        "Удалить это занятие из расписания?\n"
        "⚠️ История посещаемости, уже накопленная по этой паре, тоже будет удалена.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("sw:delyes:"))
async def cb_sw_delete(callback: CallbackQuery, db: Database, state: FSMContext, sched: AttendanceScheduler) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    _, _, weekday, pair_s = callback.data.split(":")
    entry = await db.get_schedule_entry_by_slot(weekday, int(pair_s))
    if entry:
        await db.delete_schedule_entry(entry["id"])
        await sched.configure_weekly_jobs()
    text, kb = await _day_view(weekday, db)
    await panel.show(callback, state, "🗑 Удалено.\n\n" + text, kb)


# ----------------------------------------------------------------------
# Edit hub: one card, three editable fields (type / subject / teacher) in
# any order, each backed by a picker of previously-used values. Editing an
# existing pair pre-fills the card so a one-field tweak is "open → change
# the one thing → Далее → Далее → Сохранить".
# ----------------------------------------------------------------------
async def _render_hub(target: PanelTarget, state: FSMContext) -> None:
    data = await state.get_data()
    weekday, pair_number = data["weekday"], data["pair_number"]
    start, end = config.BELL_SCHEDULE[pair_number]
    class_type, subject, teacher = data.get("class_type"), data.get("subject"), data.get("teacher")

    lines = [
        f"<b>{config.WEEKDAY_FULL_RU[weekday]}, пара {pair_number}</b> ({start}–{end})\n",
        f"🏷 Тип: {class_type or '—'}",
        f"📚 Предмет: {subject or '—'}",
        f"👤 Преподаватель: {teacher or '—'}",
    ]
    buttons = [
        [InlineKeyboardButton(text="🏷 Изменить тип", callback_data="sw:htype")],
        [InlineKeyboardButton(text="📚 Изменить предмет", callback_data="sw:hsubj")],
        [InlineKeyboardButton(text="👤 Изменить преподавателя", callback_data="sw:hteach")],
    ]
    if class_type and subject and teacher:
        buttons.append([InlineKeyboardButton(text="➡ Далее: ссылка", callback_data="sw:hnext")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data=f"sw:pair:{weekday}:{pair_number}")])

    await state.set_state(None)
    await panel.show(target, state, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("sw:edit:"))
async def cb_sw_edit_start(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    _, _, weekday, pair_s = callback.data.split(":")
    pair_number = int(pair_s)
    entry = await db.get_schedule_entry_by_slot(weekday, pair_number)
    initial = {"weekday": weekday, "pair_number": pair_number}
    if entry:
        initial.update(
            class_type=entry["class_type"], subject=entry["subject"],
            teacher=entry["teacher"], link=entry["link"],
        )
    await state.set_data(initial)
    await _render_hub(callback, state)


@router.callback_query(F.data == "sw:hub")
async def cb_sw_hub(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await _render_hub(callback, state)


# --- type field ---------------------------------------------------------
@router.callback_query(F.data == "sw:htype")
async def cb_sw_htype(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    buttons = [[InlineKeyboardButton(text=t, callback_data=f"sw:htype_set:{i}")] for i, t in enumerate(CLASS_TYPES)]
    buttons.append([InlineKeyboardButton(text="✏️ Другое", callback_data="sw:htype_custom")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")])
    await panel.show(callback, state, "Выберите тип занятия:", InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("sw:htype_set:"))
async def cb_sw_htype_set(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    idx = int(callback.data.split(":", 2)[2])
    await state.update_data(class_type=CLASS_TYPES[idx])
    await _render_hub(callback, state)


@router.callback_query(F.data == "sw:htype_custom")
async def cb_sw_htype_custom(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await state.set_state(ScheduleWizard.entering_class_type)
    await panel.show(
        callback, state, "Введите название типа занятия (например, «Семинар»):",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")]]),
    )


@router.message(ScheduleWizard.entering_class_type)
async def msg_sw_type_custom(message: Message, db: Database, state: FSMContext) -> None:
    class_type = (message.text or "").strip()
    if not class_type:
        await panel.show(message, state, "Тип занятия не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(class_type=class_type)
    await _render_hub(message, state)


# --- subject field -------------------------------------------------------
@router.callback_query(F.data == "sw:hsubj")
async def cb_sw_hsubj(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    subjects = await db.get_distinct_subjects()
    await state.update_data(subject_options=subjects)
    buttons = [[InlineKeyboardButton(text=s, callback_data=f"sw:hsubj_set:{i}")] for i, s in enumerate(subjects)]
    buttons.append([InlineKeyboardButton(text="✏️ Новый предмет", callback_data="sw:hsubj_custom")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")])
    await panel.show(callback, state, "Выберите предмет или введите новый:", InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("sw:hsubj_set:"))
async def cb_sw_hsubj_set(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    idx = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    options = data.get("subject_options", [])
    if idx >= len(options):
        await callback.answer("Список устарел, откройте поле заново.", show_alert=True)
        return
    await state.update_data(subject=options[idx])
    await _render_hub(callback, state)


@router.callback_query(F.data == "sw:hsubj_custom")
async def cb_sw_hsubj_custom(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await state.set_state(ScheduleWizard.entering_subject)
    await panel.show(
        callback, state, "Введите название предмета:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")]]),
    )


@router.message(ScheduleWizard.entering_subject)
async def msg_sw_subject(message: Message, db: Database, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await panel.show(message, state, "Название предмета не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(subject=subject)
    await _render_hub(message, state)


# --- teacher field ---------------------------------------------------------
@router.callback_query(F.data == "sw:hteach")
async def cb_sw_hteach(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    teachers = await db.get_distinct_teachers()
    await state.update_data(teacher_options=teachers)
    buttons = [[InlineKeyboardButton(text=t, callback_data=f"sw:hteach_set:{i}")] for i, t in enumerate(teachers)]
    buttons.append([InlineKeyboardButton(text="✏️ Новый преподаватель", callback_data="sw:hteach_custom")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")])
    await panel.show(
        callback, state, "Выберите преподавателя или введите нового:", InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("sw:hteach_set:"))
async def cb_sw_hteach_set(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    idx = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    options = data.get("teacher_options", [])
    if idx >= len(options):
        await callback.answer("Список устарел, откройте поле заново.", show_alert=True)
        return
    await state.update_data(teacher=options[idx])
    await _render_hub(callback, state)


@router.callback_query(F.data == "sw:hteach_custom")
async def cb_sw_hteach_custom(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await state.set_state(ScheduleWizard.entering_teacher)
    await panel.show(
        callback, state, "Введите ФИО преподавателя:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")]]),
    )


@router.message(ScheduleWizard.entering_teacher)
async def msg_sw_teacher(message: Message, db: Database, state: FSMContext) -> None:
    teacher = (message.text or "").strip()
    if not teacher:
        await panel.show(message, state, "ФИО преподавателя не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(teacher=teacher)
    await _render_hub(message, state)


# ----------------------------------------------------------------------
# Link step, then confirm
# ----------------------------------------------------------------------
async def _prompt_link(target: PanelTarget, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    current_link = data.get("link")
    suggestion = current_link or (
        await db.get_last_link_for_subject(data["subject"]) if data.get("subject") else None
    )
    await state.update_data(_link_suggestion=suggestion)

    buttons = []
    if suggestion:
        shown = suggestion if len(suggestion) <= 40 else suggestion[:37] + "…"
        label = "🔗 Оставить текущую" if current_link else "🔗 Использовать"
        buttons.append([InlineKeyboardButton(text=f"{label}: {shown}", callback_data="sw:link_reuse")])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести новую ссылку", callback_data="sw:link_new")])
    buttons.append([InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hub")])

    await state.set_state(None)
    await panel.show(
        target, state,
        f"Преподаватель: <b>{data['teacher']}</b>\n\nСсылка на занятие (Zoom/Teams и т.п.):",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "sw:hnext")
async def cb_sw_hnext(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await _prompt_link(callback, db, state)


@router.callback_query(F.data == "sw:link_reuse")
async def cb_sw_link_reuse(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    data = await state.get_data()
    suggestion = data.get("_link_suggestion")
    if not suggestion:
        await callback.answer("Ссылка недоступна.", show_alert=True)
        return
    await state.update_data(link=suggestion)
    await _show_confirm(callback, state)


@router.callback_query(F.data == "sw:link_new")
async def cb_sw_link_new(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    await state.set_state(ScheduleWizard.entering_link)
    await panel.show(
        callback, state, "Введите ссылку на занятие (должна начинаться с http:// или https://):",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hnext")]]),
    )


@router.message(ScheduleWizard.entering_link)
async def msg_sw_link(message: Message, db: Database, state: FSMContext) -> None:
    link = (message.text or "").strip()
    if not (link.startswith("http://") or link.startswith("https://")):
        await panel.show(message, state, "Ссылка должна начинаться с http:// или https://. Попробуйте ещё раз:")
        return
    await state.update_data(link=link)
    await _show_confirm(message, state)


async def _show_confirm(target: PanelTarget, state: FSMContext) -> None:
    data = await state.get_data()
    weekday, pair_number = data["weekday"], data["pair_number"]
    start, end = config.BELL_SCHEDULE[pair_number]
    text = (
        "<b>Проверьте данные:</b>\n\n"
        f"📅 {config.WEEKDAY_FULL_RU[weekday]}, пара {pair_number} ({start}–{end})\n"
        f"📚 {data['subject']} — {data['class_type']}\n"
        f"👤 {data['teacher']}\n"
        f"🔗 {data['link']}"
    )
    buttons = [
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="sw:save")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="sw:hnext")],
    ]
    await state.set_state(None)
    await panel.show(target, state, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "sw:save")
async def cb_sw_save(callback: CallbackQuery, db: Database, state: FSMContext, sched: AttendanceScheduler) -> None:
    if await require_staff_cb(callback, db) is None:
        return
    data = await state.get_data()
    await db.upsert_schedule_entry(
        weekday=data["weekday"],
        pair_number=data["pair_number"],
        class_type=data["class_type"],
        subject=data["subject"],
        link=data["link"],
        teacher=data["teacher"],
    )
    await sched.configure_weekly_jobs()
    weekday = data["weekday"]
    await panel.clear_keep_panel(state)
    text, kb = await _day_view(weekday, db)
    await panel.show(callback, state, "✅ Сохранено.\n\n" + text, kb)
