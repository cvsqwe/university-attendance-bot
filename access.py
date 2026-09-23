"""Shared access-control helpers used by every router that exposes
starosta/deputy-only or employee-only actions."""
from __future__ import annotations

from maxapi.types import CallbackQuery, Message

from database import Database, Employee, Student


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


async def require_employee(message: Message, db: Database) -> Employee | Student | None:
    """Real employees pass, but so does staff (starosta/deputy) — they
    already see all attendance data through their own menu, so there's no
    reason to lock them out of the employee report views too; this is also
    what lets them preview/demo the employee menu themselves."""
    employee = await db.get_employee_by_max_user_id(message.from_user.id)
    if employee:
        return employee
    student = await db.get_student_by_max_user_id(message.from_user.id)
    if student and student.is_staff:
        return student
    await message.answer("⛔ Эта команда доступна только сотрудникам с доступом к отчётам.")
    return None


async def require_employee_cb(callback: CallbackQuery, db: Database) -> Employee | Student | None:
    employee = await db.get_employee_by_max_user_id(callback.from_user.id)
    if employee:
        return employee
    student = await db.get_student_by_max_user_id(callback.from_user.id)
    if student and student.is_staff:
        return student
    await callback.answer("⛔ Недостаточно прав.", show_alert=True)
    return None
