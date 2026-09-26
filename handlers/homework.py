"""Homework sharing: anyone registered (student or staff) can upload a file
for a subject, and anyone can browse and re-download what's been shared —
files live on MAX's servers and are only referenced by upload token (see
Database.homework / MaxClient.send_file_by_token).
"""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from magic_filter import F

import panel
from database import Database
from keyboards import back_to_menu_kb, with_back_to_menu
from maxapi.client import MaxApiError
from maxapi.filters import Command
from maxapi.router import Router
from maxapi.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .student import NEEDS_INVITE_TEXT

router = Router(name="homework")

PanelTarget = Message | CallbackQuery

FILE_TYPE_ICONS = {"file": "📄", "image": "🖼", "video": "🎥", "audio": "🎵"}


class HomeworkStates(StatesGroup):
    waiting_file = State()


async def _render_homework_menu(target: PanelTarget, db: Database, state: FSMContext) -> None:
    subjects = await db.get_distinct_subjects()
    if not subjects:
        await panel.show(
            target, state, "🗓 Расписание пока не заполнено, поэтому список предметов пуст.", back_to_menu_kb()
        )
        return

    counts = dict(await db.get_homework_subject_counts())
    buttons = [
        [InlineKeyboardButton(
            text=f"{subject} ({counts[subject]})" if counts.get(subject) else subject,
            callback_data=f"hw:subj:{subject}",
        )]
        for subject in subjects
    ]
    await panel.show(target, state, "📚 <b>Домашки</b>\nВыберите предмет:", with_back_to_menu(buttons))


async def _render_homework_subject(target: PanelTarget, db: Database, state: FSMContext, subject: str) -> None:
    items = await db.get_homework_for_subject(subject)
    lines = [f"📚 <b>{subject}</b>"]
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📤 Загрузить домашку", callback_data=f"hw:upload:{subject}")]
    ]
    if not items:
        lines.append("\nПока никто ничего не загружал.")
    else:
        lines.append("")
        for item in items:
            date_str = item["uploaded_at"][:10]
            icon = FILE_TYPE_ICONS.get(item["file_type"], "📄")
            buttons.append([InlineKeyboardButton(
                text=f"{icon} {item['file_name']} · {item['full_name']} · {date_str}",
                callback_data=f"hw:get:{item['id']}",
            )])
    buttons.append([InlineKeyboardButton(text="⬅ К предметам", callback_data="menu:homework")])
    await panel.show(target, state, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("homework"))
async def cmd_homework(message: Message, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    if student is None:
        await panel.show(message, state, NEEDS_INVITE_TEXT)
        return
    await _render_homework_menu(message, db, state)


@router.callback_query(F.data == "menu:homework")
async def cb_menu_homework(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await panel.show(callback, state, NEEDS_INVITE_TEXT)
        return
    await state.set_state(None)
    await _render_homework_menu(callback, db, state)


@router.callback_query(F.data.startswith("hw:subj:"))
async def cb_homework_subject(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return
    subject = callback.data.split(":", 2)[2]
    await state.set_state(None)
    await _render_homework_subject(callback, db, state, subject)


@router.callback_query(F.data.startswith("hw:upload:"))
async def cb_homework_upload(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    subject = callback.data.split(":", 2)[2]
    await state.update_data(hw_subject=subject)
    await state.set_state(HomeworkStates.waiting_file)
    await panel.show(
        callback, state,
        f"Отправьте домашку по предмету «{subject}» одним сообщением — подойдёт документ, "
        "фото, видео или аудио, в любом формате.\n"
        "Подпись к вложению (если добавите) сохранится как описание.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Отмена", callback_data=f"hw:subj:{subject}")]
        ]),
    )


@router.message(HomeworkStates.waiting_file)
async def msg_homework_file(message: Message, db: Database, state: FSMContext) -> None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    data = await state.get_data()
    subject = data.get("hw_subject")
    if student is None or subject is None:
        await panel.show(message, state, "Сессия устарела, начните заново.", back_to_menu_kb())
        await state.clear()
        return

    attachment = message.get_file_attachment()
    if attachment is None:
        await panel.show(
            message, state,
            "Не вижу вложения в сообщении. Отправьте домашку как документ, фото, видео или "
            "аудио, а не текстом.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Отмена", callback_data=f"hw:subj:{subject}")]
            ]),
        )
        return

    description = (message.text or "").strip() or None
    await db.add_homework(
        student.id, subject, description, attachment["token"], attachment["filename"], attachment["type"]
    )
    await panel.clear_keep_panel(state)
    await _render_homework_subject(message, db, state, subject)


@router.callback_query(F.data.startswith("hw:get:"))
async def cb_homework_get(callback: CallbackQuery, db: Database) -> None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None:
        await callback.answer("Вы не зарегистрированы.", show_alert=True)
        return

    homework_id = int(callback.data.split(":", 2)[2])
    item = await db.get_homework_by_id(homework_id)
    if item is None:
        await callback.answer("Файл не найден.", show_alert=True)
        return

    caption = f"📚 {item['subject']} — {item['file_name']}\nЗагрузил(а): {item['full_name']}"
    if item["description"]:
        caption += f"\n\n{item['description']}"

    try:
        await callback.bot.send_file_by_token(
            item["file_token"], caption=caption, user_id=callback.from_user.id,
            attachment_type=item["file_type"],
        )
    except MaxApiError:
        await callback.answer("Не удалось отправить файл — попробуйте позже.", show_alert=True)
        return
    await callback.answer("Файл отправлен выше.")
