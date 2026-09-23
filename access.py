"""Shared staff-only access-control helpers used by every router that
exposes starosta/deputy-only actions."""
from __future__ import annotations

from maxapi.types import CallbackQuery, Message

from database import Database, Student


async def require_staff(message: Message, db: Database) -> Student | None:
    student = await db.get_student_by_max_user_id(message.from_user.id)
    if student is None or not student.is_staff:
        await message.answer("⛔ Эта команда доступна только старосте и заместителю старосты.")
        return None
    return student


async def require_staff_cb(callback: CallbackQuery, db: Database) -> Student | None:
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student is None or not student.is_staff:
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return None
    return student
