"""Reminders: SQLite storage + scheduler loop + repeat parsing + lazy TTL promotion."""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dateutil.relativedelta import relativedelta

logger = logging.getLogger("kesha.reminders")

DB_PATH = Path("./storage/reminders.db")
TICK_SECONDS = 30
LAZY_TTL_HOURS = 24
KRSK_TZ = timezone(timedelta(hours=7))

VALID_TYPES = {"plain", "urgent_llm", "lazy_llm"}
INTERVAL_RE = re.compile(r"^(\d+)(m|h|d|w|mo)$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_interval(s: str) -> relativedelta:
    m = INTERVAL_RE.match(s.strip().lower())
    if not m:
        raise ValueError(f"Invalid interval '{s}'. Use formats: 30m, 2h, 1d, 1w, 3mo")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return relativedelta(minutes=n)
    if unit == "h":
        return relativedelta(hours=n)
    if unit == "d":
        return relativedelta(days=n)
    if unit == "w":
        return relativedelta(weeks=n)
    if unit == "mo":
        return relativedelta(months=n)
    raise ValueError(f"Unknown unit '{unit}'")


def align_to_time(dt: datetime, hhmm: str) -> datetime:
    h, mi = [int(x) for x in hhmm.split(":")]
    local = dt.astimezone(KRSK_TZ).replace(hour=h, minute=mi, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def next_occurrence(prev_due: datetime, interval: str, at_time: Optional[str]) -> datetime:
    delta = parse_interval(interval)
    now = utc_now()
    candidate = prev_due + delta
    while candidate <= now:
        candidate = candidate + delta
    if at_time:
        candidate = align_to_time(candidate, at_time)
        while candidate <= now:
            candidate = candidate + delta
            candidate = align_to_time(candidate, at_time)
    return candidate


class ReminderDB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              due_at TEXT NOT NULL,
              type TEXT NOT NULL,
              repeat_interval TEXT,
              repeat_at_time TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              fired_at TEXT,
              delivered INTEGER DEFAULT 0,
              promoted INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pending ON reminders(due_at, fired_at);
            CREATE INDEX IF NOT EXISTS idx_lazy ON reminders(type, fired_at, delivered);
        """)

    def create(self, chat_id: int, text: str, due_at: datetime, type_: str,
               repeat_interval: Optional[str] = None, repeat_at_time: Optional[str] = None) -> int:
        if type_ not in VALID_TYPES:
            raise ValueError(f"Invalid type '{type_}'. Must be one of: {VALID_TYPES}")
        if repeat_interval:
            parse_interval(repeat_interval)
        if repeat_at_time and not re.match(r"^\d{1,2}:\d{2}$", repeat_at_time):
            raise ValueError(f"Invalid repeat_at_time '{repeat_at_time}'. Use HH:MM")
        cur = self.conn.execute(
            "INSERT INTO reminders(chat_id,text,due_at,type,repeat_interval,repeat_at_time) VALUES(?,?,?,?,?,?)",
            (chat_id, text, utc_iso(due_at), type_, repeat_interval, repeat_at_time),
        )
        return cur.lastrowid

    def get(self, id_: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reminders WHERE id=?", (id_,)).fetchone()

    def list_for(self, chat_id: int, include_fired: bool = False) -> list[sqlite3.Row]:
        if include_fired:
            return self.conn.execute("SELECT * FROM reminders WHERE chat_id=? ORDER BY due_at", (chat_id,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM reminders WHERE chat_id=? AND (fired_at IS NULL OR (type='lazy_llm' AND delivered=0)) ORDER BY due_at",
            (chat_id,),
        ).fetchall()

    def cancel(self, id_: int) -> bool:
        cur = self.conn.execute("DELETE FROM reminders WHERE id=?", (id_,))
        return cur.rowcount > 0

    def update(self, id_: int, **fields) -> bool:
        if not fields:
            return False
        allowed = {"text", "due_at", "type", "repeat_interval", "repeat_at_time", "fired_at", "delivered", "promoted"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown fields: {bad}")
        if "type" in fields and fields["type"] not in VALID_TYPES:
            raise ValueError(f"Invalid type '{fields['type']}'")
        if "due_at" in fields and isinstance(fields["due_at"], datetime):
            fields["due_at"] = utc_iso(fields["due_at"])
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self.conn.execute(f"UPDATE reminders SET {sets} WHERE id=?", (*fields.values(), id_))
        return cur.rowcount > 0

    def fetch_pending_due(self, before: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE fired_at IS NULL AND due_at<=? ORDER BY due_at",
            (utc_iso(before),),
        ).fetchall()

    def fetch_lazy_undelivered(self, chat_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE chat_id=? AND type='lazy_llm' AND fired_at IS NOT NULL AND delivered=0 ORDER BY fired_at",
            (chat_id,),
        ).fetchall()

    def fetch_lazy_old(self, older_than_hours: int) -> list[sqlite3.Row]:
        cutoff = utc_iso(utc_now() - timedelta(hours=older_than_hours))
        return self.conn.execute(
            "SELECT * FROM reminders WHERE type='lazy_llm' AND fired_at IS NOT NULL AND delivered=0 AND fired_at<=? AND promoted=0",
            (cutoff,),
        ).fetchall()

    def mark_fired(self, id_: int, delivered: bool = False) -> bool:
        now = utc_iso(utc_now())
        cur = self.conn.execute(
            "UPDATE reminders SET fired_at=?, delivered=? WHERE id=? AND fired_at IS NULL",
            (now, 1 if delivered else 0, id_),
        )
        return cur.rowcount > 0

    def mark_delivered_batch(self, ids: list[int]):
        if not ids:
            return
        qs = ",".join("?" * len(ids))
        self.conn.execute(f"UPDATE reminders SET delivered=1 WHERE id IN ({qs})", tuple(ids))

    def reschedule(self, id_: int, new_due: datetime):
        self.conn.execute(
            "UPDATE reminders SET due_at=?, fired_at=NULL, delivered=0 WHERE id=?",
            (utc_iso(new_due), id_),
        )


_db: Optional[ReminderDB] = None


def get_db() -> ReminderDB:
    global _db
    if _db is None:
        _db = ReminderDB()
    return _db


def format_reminder_line(r: sqlite3.Row) -> str:
    due_local = parse_iso(r["due_at"]).astimezone(KRSK_TZ).strftime("%Y-%m-%d %H:%M")
    rep = ""
    if r["repeat_interval"]:
        rep = f" 🔁{r['repeat_interval']}"
        if r["repeat_at_time"]:
            rep += f"@{r['repeat_at_time']}"
    return f"#{r['id']} [{r['type']}] {due_local}{rep}: {r['text']}"


async def reminder_loop(bot, claude, allowed_chat_ids: set):
    """Main scheduler: fire due reminders, promote stale lazy ones."""
    db = get_db()
    logger.info(f"Reminder loop started (tick={TICK_SECONDS}s, lazy_ttl={LAZY_TTL_HOURS}h)")

    while True:
        try:
            old_lazy = db.fetch_lazy_old(LAZY_TTL_HOURS)
            for r in old_lazy:
                db.update(r["id"], type="urgent_llm", promoted=1, delivered=0, fired_at=None)
                logger.info(f"Reminder #{r['id']} promoted lazy_llm → urgent_llm (TTL {LAZY_TTL_HOURS}h)")

            now = utc_now()
            due = db.fetch_pending_due(now)
            for r in due:
                if allowed_chat_ids and r["chat_id"] not in allowed_chat_ids:
                    continue
                await _fire_reminder(r, bot, claude, db)

        except Exception as e:
            logger.error(f"Reminder loop iteration error: {e}", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)


async def _fire_reminder(r: sqlite3.Row, bot, claude, db: ReminderDB):
    rid = r["id"]
    chat_id = r["chat_id"]
    rtype = r["type"]
    text = r["text"]
    due_local = parse_iso(r["due_at"]).astimezone(KRSK_TZ).strftime("%H:%M")

    try:
        if rtype == "plain":
            await bot.send_message(chat_id, f"⏰ {text}")
            db.mark_fired(rid, delivered=True)
            logger.info(f"Reminder #{rid} plain delivered to {chat_id}")
        elif rtype == "urgent_llm":
            payload = (
                f"[REMINDER FIRED at {due_local}, type=urgent_llm, id={rid}]\n"
                f"Text: {text}\n"
                f"Action: deliver this reminder to the user in your style. Be brief."
            )
            session = claude(chat_id) if callable(claude) else claude
            if session._is_processing:
                ok = await session.inject(payload)
                if ok:
                    db.mark_fired(rid, delivered=True)
                    logger.info(f"Reminder #{rid} urgent_llm injected into running session")
                else:
                    logger.warning(f"Reminder #{rid} inject failed, will retry next tick")
            else:
                db.mark_fired(rid, delivered=True)
                asyncio.create_task(_run_urgent_llm(payload, chat_id, claude, bot))
                logger.info(f"Reminder #{rid} urgent_llm started new turn")
        elif rtype == "lazy_llm":
            db.mark_fired(rid, delivered=False)
            logger.info(f"Reminder #{rid} lazy_llm fired (waiting for user activity)")

        if r["repeat_interval"]:
            new_due = next_occurrence(parse_iso(r["due_at"]), r["repeat_interval"], r["repeat_at_time"])
            db.reschedule(rid, new_due)
            logger.info(f"Reminder #{rid} rescheduled to {utc_iso(new_due)}")

    except Exception as e:
        logger.error(f"Failed to fire reminder #{rid}: {e}", exc_info=True)


_urgent_llm_handler = None


def set_urgent_llm_handler(handler):
    """Set callback: async handler(chat_id, prompt) that runs through normal _ask pipeline."""
    global _urgent_llm_handler
    _urgent_llm_handler = handler


async def _run_urgent_llm(payload: str, chat_id: int, claude, bot):
    """Trigger Claude via normal bot pipeline (enqueue-like) so response goes to TG."""
    if _urgent_llm_handler:
        try:
            await _urgent_llm_handler(chat_id, payload)
            return
        except Exception as e:
            logger.error(f"urgent_llm handler failed, falling back to plain: {e}")
    # Fallback: send raw text
    try:
        await bot.send_message(chat_id, f"⏰ {payload}")
    except Exception:
        pass


def get_lazy_block_for_prompt(chat_id: int) -> str:
    """Get fired-undelivered lazy reminders, mark them delivered, return formatted block for injection."""
    db = get_db()
    fired = db.fetch_lazy_undelivered(chat_id)
    if not fired:
        return ""
    lines = []
    for r in fired:
        fired_local = parse_iso(r["fired_at"]).astimezone(KRSK_TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(f"[REMINDER fired at {fired_local}, id={r['id']}]: {r['text']}")
    db.mark_delivered_batch([r["id"] for r in fired])
    return "\n".join(lines) + "\n\n"


async def deliver_missed_on_startup(bot, claude, allowed: set):
    """Process missed reminders that fired while bot was down."""
    db = get_db()
    now = utc_now()
    missed = db.fetch_pending_due(now)
    if not missed:
        return

    for chat_id in allowed:
        chat_missed = [r for r in missed if r["chat_id"] == chat_id]
        if not chat_missed:
            continue

        plain_missed = [r for r in chat_missed if r["type"] == "plain"]
        urgent_missed = [r for r in chat_missed if r["type"] == "urgent_llm"]
        lazy_missed = [r for r in chat_missed if r["type"] == "lazy_llm"]

        if plain_missed:
            try:
                lines = [f"⏰ {r['text']}" for r in plain_missed]
                msg = "📋 *Пропущенные напоминания (plain):*\n" + "\n".join(lines)
                await bot.send_message(chat_id, msg[:4000])
                for r in plain_missed:
                    db.mark_fired(r["id"], delivered=True)
                    if r["repeat_interval"]:
                        new_due = next_occurrence(parse_iso(r["due_at"]), r["repeat_interval"], r["repeat_at_time"])
                        db.reschedule(r["id"], new_due)
                logger.info(f"Delivered {len(plain_missed)} missed plain reminders to {chat_id}")
            except Exception as e:
                logger.error(f"Failed missed plain digest: {e}")

        for r in lazy_missed:
            db.mark_fired(r["id"], delivered=False)
            if r["repeat_interval"]:
                new_due = next_occurrence(parse_iso(r["due_at"]), r["repeat_interval"], r["repeat_at_time"])
                db.reschedule(r["id"], new_due)
        if lazy_missed:
            logger.info(f"Marked {len(lazy_missed)} missed lazy_llm reminders for {chat_id}")

        if urgent_missed:
            payload_lines = [
                f"- id={r['id']} due {parse_iso(r['due_at']).astimezone(KRSK_TZ).strftime('%Y-%m-%d %H:%M')}: {r['text']}"
                for r in urgent_missed
            ]
            payload = (
                f"[MISSED REMINDERS while bot was down, type=urgent_llm]\n"
                + "\n".join(payload_lines)
                + "\nAction: deliver these to the user briefly."
            )
            asyncio.create_task(_run_urgent_llm(payload, chat_id, claude, bot))
            for r in urgent_missed:
                db.mark_fired(r["id"], delivered=True)
                if r["repeat_interval"]:
                    new_due = next_occurrence(parse_iso(r["due_at"]), r["repeat_interval"], r["repeat_at_time"])
                    db.reschedule(r["id"], new_due)
            logger.info(f"Triggered urgent_llm digest for {len(urgent_missed)} missed for {chat_id}")
