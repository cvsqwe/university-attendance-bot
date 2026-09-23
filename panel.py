"""Single-message navigation: every bot-initiated screen is rendered into
ONE message per chat that gets edited in place, instead of a new message
per tap. The message id is remembered in FSM data (`panel_message_id`),
which aiogram keeps per (chat, user) regardless of FSM state, so it's
visible to every handler in every router.

Free-text replies (e.g. typing a custom subject) are deleted right after
being read — bots are allowed to delete incoming messages in private
chats — so a screen that needs typed input doesn't leave a trail either.
"""
from __future__ import annotations

from aiogram.fsm.context import FSMContext

from maxapi.client import MaxApiError
from maxapi.types import CallbackQuery, InlineKeyboardMarkup, Message

PanelTarget = Message | CallbackQuery


async def show(target: PanelTarget, state: FSMContext, text: str,
                keyboard: InlineKeyboardMarkup | None = None, toast: str | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
            await state.update_data(panel_message_id=target.message.message_id)
        except MaxApiError:
            pass
        try:
            await (target.answer(toast) if toast else target.answer())
        except MaxApiError:
            pass
        return

    message = target
    try:
        await message.delete()
    except MaxApiError:
        pass

    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    if panel_id:
        try:
            await message.bot.edit_message(panel_id, text, keyboard=keyboard)
            return
        except MaxApiError:
            pass  # panel message gone or too old — fall through to sending a new one

    sent = await message.answer(text, reply_markup=keyboard)
    await state.update_data(panel_message_id=sent.message_id)


async def clear_keep_panel(state: FSMContext) -> None:
    """Like state.clear(), but keeps panel_message_id so a subsequent
    Message-triggered show() still edits the existing panel instead of
    losing track of it and sending a new one."""
    data = await state.get_data()
    panel_id = data.get("panel_message_id")
    await state.clear()
    if panel_id:
        await state.update_data(panel_message_id=panel_id)
