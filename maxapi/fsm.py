"""FSM state storage — reuses aiogram's in-memory FSM machinery directly.
It only ever keys state by (bot_id, chat_id, user_id) and never touches a
Telegram type, so it works unchanged for MAX.
"""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

_storage = MemoryStorage()
_BOT_ID = 0  # single bot instance; only used to namespace the storage key


def make_state(chat_id: int, user_id: int) -> FSMContext:
    key = StorageKey(bot_id=_BOT_ID, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=_storage, key=key)
