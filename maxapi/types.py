"""Light stand-ins for the aiogram/Telegram types this codebase used, kept
API-compatible on purpose (same constructor kwargs, same attribute and
method names) so handlers/keyboards.py/panel.py need only change their
import line, not their logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maxapi.client import MaxClient


# ---------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------
@dataclass
class InlineKeyboardButton:
    text: str
    callback_data: str | None = None
    url: str | None = None

    def to_max(self) -> dict:
        if self.url is not None:
            return {"type": "link", "text": self.text, "url": self.url}
        return {"type": "callback", "text": self.text, "payload": self.callback_data}


@dataclass
class InlineKeyboardMarkup:
    inline_keyboard: list[list[InlineKeyboardButton]]

    def to_max_attachment(self) -> dict:
        return {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[button.to_max() for button in row] for row in self.inline_keyboard]
            },
        }


# ---------------------------------------------------------------------
# Users / chats
# ---------------------------------------------------------------------
@dataclass
class User:
    id: int
    name: str | None = None
    username: str | None = None


@dataclass
class Chat:
    id: int


# ---------------------------------------------------------------------
# Messages / callbacks
# ---------------------------------------------------------------------
@dataclass
class Message:
    message_id: str
    chat: Chat
    from_user: User
    text: str | None
    bot: "MaxClient" = field(repr=False)

    async def answer(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> "Message":
        return await self.bot.send_message(text, user_id=self.from_user.id, keyboard=reply_markup)

    async def edit_text(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.edit_message(self.message_id, text, keyboard=reply_markup)

    async def edit_reply_markup(self, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.edit_message(self.message_id, text=None, keyboard=reply_markup)

    async def delete(self) -> None:
        await self.bot.delete_message(self.message_id)


@dataclass
class CallbackQuery:
    id: str
    data: str | None
    from_user: User
    message: Message | None
    bot: "MaxClient" = field(repr=False)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        # MAX has a single one-time toast concept (`notification`) — no
        # separate modal-alert variant like Telegram's show_alert, so both
        # paths map to the same call.
        await self.bot.answer_callback(self.id, notification=text)


# ---------------------------------------------------------------------
# Wire-format parsing helpers, shared by MaxClient (sendMessage responses)
# and the update dispatcher (embedded message/user objects on updates).
# ---------------------------------------------------------------------
def user_from_dict(data: dict | None) -> User | None:
    if not data:
        return None
    return User(id=data["user_id"], name=data.get("name"), username=data.get("username"))


def message_from_dict(data: dict | None, bot: "MaxClient") -> Message | None:
    if not data:
        return None
    body = data.get("body") or {}
    recipient = data.get("recipient") or {}
    chat_id = recipient.get("chat_id")
    if chat_id is None:
        chat_id = recipient.get("user_id")
    sender = user_from_dict(data.get("sender")) or User(id=chat_id)
    return Message(
        message_id=body.get("mid", ""),
        chat=Chat(id=chat_id),
        from_user=sender,
        text=body.get("text"),
        bot=bot,
    )
