"""Minimal Router: registers message/callback handlers behind filter lists
and exposes them for the dispatcher to walk in registration order — the
same "first fully-matching handler wins" semantics aiogram's Dispatcher
gives this codebase, without aiogram's Telegram-bound event machinery.
"""
from __future__ import annotations

from typing import Callable

Filters = tuple
HandlerEntry = tuple[Filters, Callable]


class Router:
    def __init__(self, name: str = ""):
        self.name = name
        self._message_handlers: list[HandlerEntry] = []
        self._callback_handlers: list[HandlerEntry] = []
        self._sub_routers: list["Router"] = []

    def include_router(self, router: "Router") -> None:
        self._sub_routers.append(router)

    def message(self, *filters):
        def decorator(handler: Callable) -> Callable:
            self._message_handlers.append((filters, handler))
            return handler
        return decorator

    def callback_query(self, *filters):
        def decorator(handler: Callable) -> Callable:
            self._callback_handlers.append((filters, handler))
            return handler
        return decorator

    def iter_message_handlers(self):
        yield from self._message_handlers
        for sub in self._sub_routers:
            yield from sub.iter_message_handlers()

    def iter_callback_handlers(self):
        yield from self._callback_handlers
        for sub in self._sub_routers:
            yield from sub.iter_callback_handlers()
