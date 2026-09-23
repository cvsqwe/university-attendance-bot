"""Walks incoming MAX updates through a Router tree: builds the
Message/CallbackQuery-equivalent event, resolves its FSM state, finds the
first handler whose filters all pass, and calls it with whatever
subset of (event, db, state, sched, bot, command) it declared by name.
"""
from __future__ import annotations

import inspect
import logging

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from magic_filter import MagicFilter

from maxapi.client import MaxClient
from maxapi.filters import Command, CommandObject
from maxapi.fsm import make_state
from maxapi.router import Router
from maxapi.types import CallbackQuery, Chat, Message, message_from_dict, user_from_dict

logger = logging.getLogger(__name__)


async def _match_filters(filters: tuple, event, state: FSMContext) -> dict | None:
    """Evaluates every filter against the event. Returns the extra kwargs
    to inject into the handler call (possibly empty) if all filters pass,
    or None if any filter rejects the event."""
    extra: dict = {}
    state_filters = [f for f in filters if isinstance(f, State)]
    if state_filters:
        current_state = await state.get_state()
        for f in state_filters:
            if current_state != f.state:
                return None

    for f in filters:
        if isinstance(f, State):
            continue
        if isinstance(f, MagicFilter):
            if not f.resolve(event):
                return None
            continue
        result = f(event)
        if result is False or result is None:
            return None
        if isinstance(result, CommandObject):
            extra["command"] = result

    return extra


async def _call_handler(handler, event, event_kwarg: str, ctx: dict):
    sig = inspect.signature(handler)
    kwargs = {}
    for name in sig.parameters:
        if name == event_kwarg:
            kwargs[name] = event
        elif name in ctx:
            kwargs[name] = ctx[name]
    return await handler(**kwargs)


async def _dispatch_message(root: Router, message: Message, base_ctx: dict) -> None:
    state = make_state(message.chat.id, message.from_user.id)
    for filters, handler in root.iter_message_handlers():
        extra = await _match_filters(filters, message, state)
        if extra is None:
            continue
        await _call_handler(handler, message, "message", {**base_ctx, "state": state, **extra})
        return
    logger.debug("No handler matched message: %r", message.text)


async def _dispatch_callback(root: Router, callback: CallbackQuery, base_ctx: dict) -> None:
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    state = make_state(chat_id, callback.from_user.id)
    for filters, handler in root.iter_callback_handlers():
        extra = await _match_filters(filters, callback, state)
        if extra is None:
            continue
        await _call_handler(handler, callback, "callback", {**base_ctx, "state": state, **extra})
        return
    logger.debug("No handler matched callback: %r", callback.data)


async def dispatch_update(root: Router, update: dict, bot: MaxClient, base_ctx: dict) -> None:
    """base_ctx carries whatever handlers may ask for by name besides the
    event/state (currently: db, sched)."""
    update_type = update.get("update_type")

    if update_type == "message_created":
        raw = update.get("message") or {}
        if not raw.get("sender"):
            return  # channel posts / system messages — nothing to route to
        message = message_from_dict(raw, bot)
        await _dispatch_message(root, message, base_ctx)

    elif update_type == "message_callback":
        callback_raw = update["callback"]
        user = user_from_dict(callback_raw.get("user"))
        message = message_from_dict(update.get("message"), bot)
        callback = CallbackQuery(
            id=callback_raw["callback_id"],
            data=callback_raw.get("payload"),
            from_user=user,
            message=message,
            bot=bot,
        )
        await _dispatch_callback(root, callback, base_ctx)

    elif update_type == "bot_started":
        # MAX's equivalent of Telegram's /start deep link: fired once, the
        # first time a user opens the bot via a start link, carrying the
        # payload out-of-band instead of as message text. Synthesize a
        # "/start <payload>" message so it flows through the existing
        # CommandStart()-filtered handler unchanged.
        user = user_from_dict(update["user"])
        chat_id = update["chat_id"]
        payload = update.get("payload")
        text = f"/start {payload}" if payload else "/start"
        message = Message(message_id="", chat=Chat(id=chat_id), from_user=user, text=text, bot=bot)
        await _dispatch_message(root, message, base_ctx)

    else:
        logger.debug("Ignoring unhandled update type: %s", update_type)
