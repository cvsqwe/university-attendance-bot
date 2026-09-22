"""
Configuration module: environment variables, bell schedule, roster and
static lookup tables shared across the whole bot.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Core settings
# --------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
DB_PATH: str = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "attendance.db"))
TIMEZONE: str = "Europe/Moscow"

# How many minutes before a class starts the check-in notification is sent.
NOTIFY_BEFORE_START_MIN: int = 5
# How many minutes before a class ends the check-in window closes.
CLOSE_BEFORE_END_MIN: int = 10

# --------------------------------------------------------------------------
# Bell schedule (MSK). Pair number -> (start "HH:MM", end "HH:MM")
# --------------------------------------------------------------------------
BELL_SCHEDULE: dict[int, tuple[str, str]] = {
    1: ("09:00", "10:30"),
    2: ("10:40", "12:10"),
    3: ("12:40", "14:10"),
    4: ("14:20", "15:50"),
    5: ("16:00", "17:30"),
    6: ("17:40", "19:10"),
    7: ("19:20", "20:50"),
}

# --------------------------------------------------------------------------
# Weekday lookups (Russian short names used everywhere in the UI/DB)
# --------------------------------------------------------------------------
WEEKDAYS_RU: list[str] = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

# Days the group actually has classes on. The schedule editor and the
# /set_schedule bulk parser only allow these — no Sunday classes.
WORKING_WEEKDAYS_RU: list[str] = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ"]

# Maps our RU weekday code to Python's date.weekday() index (Mon=0 ... Sun=6)
WEEKDAY_RU_TO_PY_INDEX: dict[str, int] = {
    "ПН": 0, "ВТ": 1, "СР": 2, "ЧТ": 3, "ПТ": 4, "СБ": 5, "ВС": 6,
}
WEEKDAY_PY_INDEX_TO_RU: dict[int, str] = {v: k for k, v in WEEKDAY_RU_TO_PY_INDEX.items()}

# Maps our RU weekday code to APScheduler's CronTrigger day_of_week value
WEEKDAY_RU_TO_CRON: dict[str, str] = {
    "ПН": "mon", "ВТ": "tue", "СР": "wed", "ЧТ": "thu",
    "ПТ": "fri", "СБ": "sat", "ВС": "sun",
}

WEEKDAY_FULL_RU: dict[str, str] = {
    "ПН": "Понедельник", "ВТ": "Вторник", "СР": "Среда", "ЧТ": "Четверг",
    "ПТ": "Пятница", "СБ": "Суббота", "ВС": "Воскресенье",
}

# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
ROLE_STAROSTA = "starosta"
ROLE_DEPUTY = "deputy"
ROLE_STUDENT = "student"
STAFF_ROLES = (ROLE_STAROSTA, ROLE_DEPUTY)

# --------------------------------------------------------------------------
# Standard reasons offered as quick-reply chips for /absence
# --------------------------------------------------------------------------
ABSENCE_REASON_CHIPS: list[str] = ["Болезнь", "Семейные обстоятельства", "Работа"]

# --------------------------------------------------------------------------
# Group roster (seeded into the database on first run)
# --------------------------------------------------------------------------
STAROSTA_FULL_NAME = "Печерский Сергей Дмитриевич"
DEPUTY_FULL_NAME = "Мухарлямов Иван Михайлович"

STUDENT_FULL_NAMES: list[str] = [
    "Аксёнов Александр Ярославович",
    "Барников Кирилл Артемович",
    "Белоусов Иван Андреевич",
    "Боднарчук Виталий Богданович",
    "Галагуз Вадим Денисович",
    "Гладун Виталий Олегович",
    "Закордонец Никита Сергеевич",
    "Казимиров Даниил Сергеевич",
    "Кожемяцкий Дмитрий Андреевич",
    "Кондратьева Екатерина Ивановна",
    "Куртаджиев Эльдар Шевкетович",
    "Медведь Илья Владимирович",
    "Мулюкин Сергей Егорович",
    "Панков Алексей Игоревич",
    "Половинкин Дмитрий Алексеевич",
    "Саду Амет",
    "Сашенков Даниил Дмитриевич",
    "Тимченко Денис Александрович",
    "Фокин Евгений Сергеевич",
    "Ходус Валентин Вадимович",
    "Цуринов Максим Геннадьевич",
    "Шашанов Владислав Денисович",
    "Шеин Владислав Юрьевич",
    "Юхнин Вадим Владимирович",
]

# Full roster as (full_name, role) tuples, used to seed the `students` table.
ROSTER: list[tuple[str, str]] = (
    [(STAROSTA_FULL_NAME, ROLE_STAROSTA), (DEPUTY_FULL_NAME, ROLE_DEPUTY)]
    + [(name, ROLE_STUDENT) for name in STUDENT_FULL_NAMES]
)
