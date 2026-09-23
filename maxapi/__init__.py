"""Minimal MAX messenger bot framework used in place of aiogram/Telegram.

Deliberately small: it only implements the handful of concepts this bot
actually uses (inline keyboards, callback answers, FSM-driven wizards,
long polling) rather than a general-purpose SDK. FSM storage and filter
matching are borrowed from aiogram's `fsm` and `magic_filter` modules,
which are transport-agnostic (they only ever look at plain attributes/ids,
never at Telegram network objects), so behaviour there is unchanged from
the Telegram version.
"""
