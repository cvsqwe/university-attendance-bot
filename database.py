"""
Async SQLite data layer built on aiosqlite.

Every public method opens its own short-lived connection. The group is
small (≈26 users) and traffic is low, so this keeps the code simple and
avoids any shared-connection concurrency pitfalls, while WAL mode keeps
reads and writes from blocking each other.
"""
from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

import aiosqlite

import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('starosta', 'deputy', 'student')),
    telegram_id INTEGER UNIQUE,
    registered_at TEXT,
    invite_token TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday TEXT NOT NULL CHECK(weekday IN ('ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС')),
    pair_number INTEGER NOT NULL CHECK(pair_number BETWEEN 1 AND 7),
    class_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    link TEXT NOT NULL,
    teacher TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    weekday TEXT NOT NULL,
    pair_number INTEGER NOT NULL,
    class_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    link TEXT NOT NULL,
    teacher TEXT NOT NULL,
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    notify_dt TEXT NOT NULL,
    close_dt TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(schedule_id, date)
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('present', 'absent', 'excused')),
    marked_at TEXT,
    UNIQUE(session_id, student_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    UNIQUE(session_id, student_id)
);

CREATE TABLE IF NOT EXISTS planned_absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(student_id, date)
);
"""


@dataclass
class Student:
    id: int
    full_name: str
    role: str
    telegram_id: int | None
    registered_at: str | None
    invite_token: str | None

    @property
    def is_staff(self) -> bool:
        return self.role in config.STAFF_ROLES


def _row_to_student(row: aiosqlite.Row) -> Student:
    return Student(
        id=row["id"],
        full_name=row["full_name"],
        role=row["role"],
        telegram_id=row["telegram_id"],
        registered_at=row["registered_at"],
        invite_token=row["invite_token"],
    )


class Database:
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path)

    # ------------------------------------------------------------------
    # Init / seeding
    # ------------------------------------------------------------------
    async def init_db(self) -> None:
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.executescript(SCHEMA)
            await db.commit()
            # Migration for databases created before invite_token existed.
            cur = await db.execute("PRAGMA table_info(students)")
            cols = {row[1] for row in await cur.fetchall()}
            if "invite_token" not in cols:
                await db.execute("ALTER TABLE students ADD COLUMN invite_token TEXT")
                await db.commit()
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_students_invite_token'"
            )
            if await cur.fetchone() is None:
                await db.execute("CREATE UNIQUE INDEX idx_students_invite_token ON students(invite_token)")
                await db.commit()
        await self._seed_roster()
        await self._ensure_invite_tokens()

    async def _ensure_invite_tokens(self) -> None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM students WHERE invite_token IS NULL")
            rows = await cur.fetchall()
            for row in rows:
                for _ in range(5):
                    token = secrets.token_urlsafe(6)
                    try:
                        await db.execute(
                            "UPDATE students SET invite_token = ? WHERE id = ?", (token, row["id"])
                        )
                        await db.commit()
                        break
                    except aiosqlite.IntegrityError:
                        continue

    async def _seed_roster(self) -> None:
        async with self._connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM students")
            (count,) = await cur.fetchone()
            if count > 0:
                return
            await db.executemany(
                "INSERT INTO students (full_name, role) VALUES (?, ?)",
                config.ROSTER,
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Students
    # ------------------------------------------------------------------
    async def get_student_by_telegram_id(self, telegram_id: int) -> Student | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM students WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cur.fetchone()
            return _row_to_student(row) if row else None

    async def get_student_by_id(self, student_id: int) -> Student | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM students WHERE id = ?", (student_id,))
            row = await cur.fetchone()
            return _row_to_student(row) if row else None

    async def get_unregistered_students(self) -> list[Student]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM students WHERE telegram_id IS NULL ORDER BY full_name"
            )
            rows = await cur.fetchall()
            return [_row_to_student(r) for r in rows]

    async def get_student_by_invite_token(self, token: str) -> Student | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM students WHERE invite_token = ?", (token,))
            row = await cur.fetchone()
            return _row_to_student(row) if row else None

    async def register_student_by_token(self, token: str, telegram_id: int) -> Student | None:
        """Binds telegram_id to the roster entry that owns this invite token.
        Returns None if the token is unknown, already used, or the
        telegram_id is already bound to a different roster entry."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM students WHERE invite_token = ?", (token,))
            row = await cur.fetchone()
            if row is None or row["telegram_id"] is not None:
                return None
            cur = await db.execute("SELECT id FROM students WHERE telegram_id = ?", (telegram_id,))
            if await cur.fetchone() is not None:
                return None
            await db.execute(
                "UPDATE students SET telegram_id = ?, registered_at = ? WHERE id = ?",
                (telegram_id, dt.datetime.now().isoformat(timespec="seconds"), row["id"]),
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM students WHERE id = ?", (row["id"],))
            return _row_to_student(await cur.fetchone())

    async def regenerate_invite_token(self, student_id: int) -> str:
        async with self._connect() as db:
            for _ in range(5):
                token = secrets.token_urlsafe(6)
                try:
                    await db.execute(
                        "UPDATE students SET invite_token = ? WHERE id = ?", (token, student_id)
                    )
                    await db.commit()
                    return token
                except aiosqlite.IntegrityError:
                    continue
        raise RuntimeError("Could not generate a unique invite token")

    async def get_all_registered_students(self) -> list[Student]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM students WHERE telegram_id IS NOT NULL ORDER BY full_name"
            )
            rows = await cur.fetchall()
            return [_row_to_student(r) for r in rows]

    async def get_staff(self) -> list[Student]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM students WHERE role IN ('starosta','deputy') "
                "AND telegram_id IS NOT NULL"
            )
            rows = await cur.fetchall()
            return [_row_to_student(r) for r in rows]

    async def get_all_students(self) -> list[Student]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM students ORDER BY full_name")
            rows = await cur.fetchall()
            return [_row_to_student(r) for r in rows]

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------
    async def get_full_schedule(self) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedule ORDER BY "
                "CASE weekday WHEN 'ПН' THEN 1 WHEN 'ВТ' THEN 2 WHEN 'СР' THEN 3 "
                "WHEN 'ЧТ' THEN 4 WHEN 'ПТ' THEN 5 WHEN 'СБ' THEN 6 WHEN 'ВС' THEN 7 END, "
                "pair_number"
            )
            return await cur.fetchall()

    async def get_schedule_for_weekday(self, weekday: str) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedule WHERE weekday = ? ORDER BY pair_number", (weekday,)
            )
            return await cur.fetchall()

    async def get_schedule_entry(self, schedule_id: int) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM schedule WHERE id = ?", (schedule_id,))
            return await cur.fetchone()

    async def get_schedule_entry_by_slot(self, weekday: str, pair_number: int) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedule WHERE weekday = ? AND pair_number = ?",
                (weekday, pair_number),
            )
            return await cur.fetchone()

    async def upsert_schedule_entry(self, weekday: str, pair_number: int, class_type: str,
                                     subject: str, link: str, teacher: str) -> int:
        """Creates or updates the single (weekday, pair_number) slot. Updates
        in place when the slot already exists, keeping its schedule id so
        sessions/attendance history already tied to it is preserved."""
        start_time, end_time = config.BELL_SCHEDULE[pair_number]
        existing = await self.get_schedule_entry_by_slot(weekday, pair_number)
        async with self._connect() as db:
            if existing:
                await db.execute(
                    "UPDATE schedule SET class_type = ?, subject = ?, link = ?, teacher = ?, "
                    "start_time = ?, end_time = ? WHERE id = ?",
                    (class_type, subject, link, teacher, start_time, end_time, existing["id"]),
                )
                await db.commit()
                return existing["id"]
            cur = await db.execute(
                "INSERT INTO schedule (weekday, pair_number, class_type, subject, link, teacher, "
                "start_time, end_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (weekday, pair_number, class_type, subject, link, teacher, start_time, end_time),
            )
            await db.commit()
            return cur.lastrowid

    async def delete_schedule_entry(self, schedule_id: int) -> None:
        async with self._connect() as db:
            await db.execute("DELETE FROM schedule WHERE id = ?", (schedule_id,))
            await db.commit()

    async def get_distinct_subjects(self) -> list[str]:
        async with self._connect() as db:
            cur = await db.execute("SELECT DISTINCT subject FROM schedule ORDER BY subject")
            return [row[0] for row in await cur.fetchall()]

    async def get_distinct_teachers(self) -> list[str]:
        async with self._connect() as db:
            cur = await db.execute("SELECT DISTINCT teacher FROM schedule ORDER BY teacher")
            return [row[0] for row in await cur.fetchall()]

    async def get_last_link_for_subject(self, subject: str) -> str | None:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT link FROM schedule WHERE subject = ? ORDER BY id DESC LIMIT 1", (subject,)
            )
            row = await cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------
    # Sessions (a concrete date-stamped occurrence of a schedule entry)
    # ------------------------------------------------------------------
    async def create_session(self, schedule_id: int, date: str, weekday: str, pair_number: int,
                              class_type: str, subject: str, link: str, teacher: str,
                              start_dt: str, end_dt: str, notify_dt: str, close_dt: str) -> int:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM sessions WHERE schedule_id = ? AND date = ?",
                (schedule_id, date),
            )
            existing = await cur.fetchone()
            if existing:
                return existing["id"]
            cur = await db.execute(
                "INSERT INTO sessions (schedule_id, date, weekday, pair_number, class_type, "
                "subject, link, teacher, start_dt, end_dt, notify_dt, close_dt, notified, closed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (schedule_id, date, weekday, pair_number, class_type, subject, link, teacher,
                 start_dt, end_dt, notify_dt, close_dt),
            )
            await db.commit()
            return cur.lastrowid

    async def mark_session_notified(self, session_id: int) -> None:
        async with self._connect() as db:
            await db.execute("UPDATE sessions SET notified = 1 WHERE id = ?", (session_id,))
            await db.commit()

    async def mark_session_closed(self, session_id: int) -> None:
        async with self._connect() as db:
            await db.execute("UPDATE sessions SET closed = 1 WHERE id = ?", (session_id,))
            await db.commit()

    async def get_session(self, session_id: int) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            return await cur.fetchone()

    async def get_session_by_schedule_and_date(self, schedule_id: int, date: str) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE schedule_id = ? AND date = ?", (schedule_id, date)
            )
            return await cur.fetchone()

    async def get_sessions_for_date(self, date: str) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE date = ? ORDER BY pair_number", (date,)
            )
            return await cur.fetchall()

    async def get_unclosed_sessions(self) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE closed = 0")
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Notifications (sent check-in messages, needed to edit them later)
    # ------------------------------------------------------------------
    async def add_notification(self, session_id: int, student_id: int, chat_id: int,
                                message_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO notifications (session_id, student_id, chat_id, message_id) "
                "VALUES (?, ?, ?, ?)",
                (session_id, student_id, chat_id, message_id),
            )
            await db.commit()

    async def get_notifications_for_session(self, session_id: int) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM notifications WHERE session_id = ?", (session_id,)
            )
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Attendance
    # ------------------------------------------------------------------
    async def mark_attendance(self, session_id: int, student_id: int, status: str,
                               marked_at: str | None = None) -> bool:
        """Inserts an attendance record if one doesn't already exist.
        Returns True if inserted, False if a record already existed."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM attendance WHERE session_id = ? AND student_id = ?",
                (session_id, student_id),
            )
            if await cur.fetchone() is not None:
                return False
            await db.execute(
                "INSERT INTO attendance (session_id, student_id, status, marked_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, student_id, status, marked_at or dt.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()
            return True

    async def set_attendance(self, session_id: int, student_id: int, status: str) -> None:
        """Staff override: unconditionally sets/overwrites the status."""
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO attendance (session_id, student_id, status, marked_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, student_id) DO UPDATE SET status = excluded.status, "
                "marked_at = excluded.marked_at",
                (session_id, student_id, status, dt.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def get_attendance(self, session_id: int, student_id: int) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM attendance WHERE session_id = ? AND student_id = ?",
                (session_id, student_id),
            )
            return await cur.fetchone()

    async def get_attendance_for_session(self, session_id: int) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT a.*, s.full_name FROM attendance a "
                "JOIN students s ON s.id = a.student_id WHERE a.session_id = ?",
                (session_id,),
            )
            return await cur.fetchall()

    async def get_attendance_range(self, date_from: str, date_to: str) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT sess.date, sess.pair_number, sess.subject, sess.class_type, "
                "st.id AS student_id, st.full_name, a.status "
                "FROM sessions sess "
                "JOIN attendance a ON a.session_id = sess.id "
                "JOIN students st ON st.id = a.student_id "
                "WHERE sess.date BETWEEN ? AND ? "
                "ORDER BY sess.date, sess.pair_number, st.full_name",
                (date_from, date_to),
            )
            return await cur.fetchall()

    async def get_sessions_range(self, date_from: str, date_to: str) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions WHERE date BETWEEN ? AND ? ORDER BY date, pair_number",
                (date_from, date_to),
            )
            return await cur.fetchall()

    # ------------------------------------------------------------------
    # Planned absences
    # ------------------------------------------------------------------
    async def add_planned_absence(self, student_id: int, date: str, reason: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO planned_absences (student_id, date, reason, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(student_id, date) DO UPDATE SET reason = excluded.reason, "
                "created_at = excluded.created_at",
                (student_id, date, reason, dt.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def excuse_existing_sessions(self, student_id: int, date: str) -> None:
        """Retroactively marks this student "excused" for every session that
        already exists on this date (already notified, open or closed) —
        needed because a planned absence declared mid-day only auto-excuses
        pairs whose notification hasn't fired yet otherwise. A pair the
        student already checked into ("present") is left alone."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM sessions WHERE date = ?", (date,))
            session_ids = [row["id"] for row in await cur.fetchall()]
            for session_id in session_ids:
                cur = await db.execute(
                    "SELECT status FROM attendance WHERE session_id = ? AND student_id = ?",
                    (session_id, student_id),
                )
                row = await cur.fetchone()
                if row and row["status"] == "present":
                    continue
                await db.execute(
                    "INSERT INTO attendance (session_id, student_id, status, marked_at) "
                    "VALUES (?, ?, 'excused', ?) "
                    "ON CONFLICT(session_id, student_id) DO UPDATE SET status = 'excused', "
                    "marked_at = excluded.marked_at",
                    (session_id, student_id, dt.datetime.now().isoformat(timespec="seconds")),
                )
            await db.commit()

    async def get_planned_absence(self, student_id: int, date: str) -> aiosqlite.Row | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM planned_absences WHERE student_id = ? AND date = ?",
                (student_id, date),
            )
            return await cur.fetchone()

    async def get_planned_absences_for_date(self, date: str) -> list[aiosqlite.Row]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT pa.*, s.full_name FROM planned_absences pa "
                "JOIN students s ON s.id = pa.student_id WHERE pa.date = ?",
                (date,),
            )
            return await cur.fetchall()
