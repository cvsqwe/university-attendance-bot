"""Command-text filters, replicating just the aiogram.filters surface this
codebase uses (Command / CommandStart / CommandObject)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommandObject:
    command: str
    args: str | None = None


class Command:
    def __init__(self, command: str):
        self.command = command

    def __call__(self, message) -> CommandObject | None:
        text = (message.text or "").strip()
        if not text.startswith("/"):
            return None
        head, _, rest = text.partition(" ")
        cmd = head[1:].split("@", 1)[0]
        if cmd != self.command:
            return None
        return CommandObject(command=cmd, args=rest.strip() or None)


def CommandStart() -> Command:
    return Command("start")
