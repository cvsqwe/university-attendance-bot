"""Small shared helpers: MSK time handling and Russian display labels."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.TIMEZONE)

ROLE_LABELS = {
    "starosta": "Староста",
    "deputy": "Заместитель старосты",
    "student": "Студент",
}

STATUS_LABELS = {
    "present": "✅ Присутствовал",
    "absent": "❌ Отсутствовал",
    "excused": "📝 Уважительная причина",
}

STATUS_LABELS_SHORT = {
    "present": "Присутствовал",
    "absent": "Отсутствовал",
    "excused": "Уважительная причина",
}

MONTHS_RU_GEN: dict[int, str] = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def now_msk() -> dt.datetime:
    """Current MSK time as a naive datetime (the whole app operates in MSK only)."""
    return dt.datetime.now(TZ).replace(tzinfo=None)


def today_msk() -> dt.date:
    return now_msk().date()


def combine_dt(date_iso: str, hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, date_iso.split("-"))
    return dt.datetime(y, mo, d, h, m, 0)


def fmt_dt(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def aware(naive: dt.datetime) -> dt.datetime:
    return naive.replace(tzinfo=TZ)


def weekday_ru_for(date: dt.date) -> str:
    return config.WEEKDAY_PY_INDEX_TO_RU[date.weekday()]


def fmt_date_human(date: dt.date) -> str:
    return f"{date.strftime('%d.%m')} ({weekday_ru_for(date)})"
