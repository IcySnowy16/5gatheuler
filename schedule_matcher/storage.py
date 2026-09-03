"""SQLite storage for events, availability, bookings and credentials.

Replaces the old pickle file (which lived wherever the bot happened to be
launched from, inside OneDrive). Existing pickle data is imported once on
startup. Availability is keyed by Telegram user id, so two people with the
same first name no longer collide; rows migrated from the pickle have no user
id and keep their display name as identity.
"""

from __future__ import annotations

import json
import logging
import pickle
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    chat_id    INTEGER NOT NULL,
    code       TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    creator    INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (chat_id, code)
);
CREATE TABLE IF NOT EXISTS slots (
    id           INTEGER PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    code         TEXT    NOT NULL,
    user_id      INTEGER,
    display_name TEXT NOT NULL,
    start_ts     TEXT NOT NULL,
    end_ts       TEXT NOT NULL,
    UNIQUE (chat_id, code, user_id, display_name, start_ts, end_ts)
);
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT,
    ntu_username BLOB,
    ntu_password BLOB,
    email        TEXT,
    imap_host    TEXT,
    imap_user    TEXT,
    imap_pass    BLOB,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    location     TEXT,
    category     TEXT,
    room_name    TEXT,
    item_id      INTEGER,
    start_ts     TEXT NOT NULL,
    end_ts       TEXT NOT NULL,
    booking_ref  TEXT,
    checkin_code TEXT,
    cancel_link  TEXT,
    status       TEXT DEFAULT 'booked',
    created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS scheduled_bookings (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    lid         INTEGER NOT NULL,
    gid         INTEGER NOT NULL,
    location    TEXT,
    category    TEXT,
    item_id     INTEGER,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    fire_at     TEXT NOT NULL,
    retry_until TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',
    attempts    INTEGER DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS favourites (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    label         TEXT NOT NULL,
    lid           INTEGER NOT NULL,
    gid           INTEGER NOT NULL,
    item_id       INTEGER,
    default_start TEXT,
    default_end   TEXT,
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS group_legs (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER,
    lid        INTEGER NOT NULL,
    gid        INTEGER NOT NULL,
    location   TEXT,
    category   TEXT,
    item_id    INTEGER,
    start_ts   TEXT NOT NULL,
    end_ts     TEXT NOT NULL,
    claimed_by INTEGER,
    claimed_name TEXT,
    status     TEXT DEFAULT 'open',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS templates (
    user_id  INTEGER NOT NULL,
    weekday  INTEGER NOT NULL,
    start_hm TEXT NOT NULL,
    end_hm   TEXT NOT NULL,
    tier     INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, weekday, start_hm, end_hm)
);
CREATE TABLE IF NOT EXISTS developers (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS durable (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS holds (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    lid         INTEGER NOT NULL,
    gid         INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    location    TEXT,
    category    TEXT,
    label       TEXT,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    note        TEXT,
    status      TEXT DEFAULT 'held',
    renewals    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    refreshed_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release; applied with try/except so existing
# databases upgrade in place.
_MIGRATIONS = (
    "ALTER TABLE bookings ADD COLUMN checkin_attempts INTEGER DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN checkin_nagged INTEGER DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN lid INTEGER",
    "ALTER TABLE bookings ADD COLUMN gid INTEGER",
    "ALTER TABLE slots ADD COLUMN tier INTEGER DEFAULT 0",
    "ALTER TABLE events ADD COLUMN deadline_ts TEXT",
    "ALTER TABLE events ADD COLUMN reminded INTEGER DEFAULT 0",
    "ALTER TABLE events ADD COLUMN final_start TEXT",
    "ALTER TABLE events ADD COLUMN final_end TEXT",
    "ALTER TABLE group_legs ADD COLUMN hold_id INTEGER",
    "ALTER TABLE group_legs ADD COLUMN held_until TEXT",
    "ALTER TABLE bookings ADD COLUMN proof_path TEXT",
    "ALTER TABLE bookings ADD COLUMN proof_chat_id INTEGER",
    "ALTER TABLE bookings ADD COLUMN proof_msg_id INTEGER",
    "ALTER TABLE scheduled_bookings ADD COLUMN hold_id INTEGER",
)

FMT = "%Y-%m-%d %H:%M"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                _conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn.commit()
        _migrate_pickles(_conn)
        _promote_durable(_conn)
    return _conn


# --- Pickle migration -----------------------------------------------------

def _migrate_pickles(c: sqlite3.Connection) -> None:
    if c.execute("SELECT value FROM kv WHERE key='pickle_migrated'").fetchone():
        return
    for path in config.LEGACY_PICKLES:
        try:
            _import_pickle(c, path)
        except Exception:
            log.exception("Could not import legacy pickle %s", path)
    c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('pickle_migrated', '1')")
    c.commit()


def _promote_durable(c: sqlite3.Connection) -> None:
    """Move facts that must not expire out of the cache table, once."""
    if c.execute("SELECT value FROM kv WHERE key='durable_promoted'").fetchone():
        return
    for key in ("category_meta", "libcal_room_names", "category_limits",
                "learned_refusals", "botmail_account"):
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            continue
        try:
            payload = json.loads(row["value"])
            data = payload.get("data") if isinstance(payload, dict) else payload
        except Exception:
            continue
        if data is None:
            continue
        c.execute("INSERT OR REPLACE INTO durable (key, value, updated_at)"
                  " VALUES (?,?,datetime('now','localtime'))",
                  (key, json.dumps(data)))
        log.info("promoted %s out of the expiring cache", key)
    c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('durable_promoted', '1')")
    c.commit()


def _import_pickle(c: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        return
    with open(path, "rb") as f:
        db = pickle.load(f)
    count = 0
    for chat_id, events in db.items():
        for code, event in events.items():
            c.execute(
                "INSERT OR IGNORE INTO events (chat_id, code, name, creator) VALUES (?,?,?,?)",
                (chat_id, code, event.get("name", code), event.get("creator")),
            )
            for name, pairs in event.get("availabilities", {}).items():
                for start, end in pairs:
                    c.execute(
                        "INSERT OR IGNORE INTO slots (chat_id, code, user_id, display_name, start_ts, end_ts)"
                        " VALUES (?,?,NULL,?,?,?)",
                        (chat_id, code, name, start.strftime(FMT), end.strftime(FMT)),
                    )
                    count += 1
    log.info("Imported %d slots from legacy pickle %s", count, path)


# --- Events / slots -------------------------------------------------------

def create_event(chat_id: int, code: str, name: str, creator: int) -> None:
    c = conn()
    c.execute(
        "INSERT INTO events (chat_id, code, name, creator) VALUES (?,?,?,?)",
        (chat_id, code, name, creator),
    )
    c.commit()


def list_events(chat_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM events WHERE chat_id=? ORDER BY created_at", (chat_id,)
    ).fetchall()


def get_event(chat_id: int, code: str) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM events WHERE chat_id=? AND code=?", (chat_id, code)
    ).fetchone()


def delete_event(chat_id: int, code: str) -> None:
    c = conn()
    c.execute("DELETE FROM slots WHERE chat_id=? AND code=?", (chat_id, code))
    c.execute("DELETE FROM events WHERE chat_id=? AND code=?", (chat_id, code))
    c.commit()


def add_slot(chat_id: int, code: str, user_id: int, display_name: str,
             start: datetime, end: datetime) -> bool:
    c = conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO slots (chat_id, code, user_id, display_name, start_ts, end_ts)"
        " VALUES (?,?,?,?,?,?)",
        (chat_id, code, user_id, display_name, start.strftime(FMT), end.strftime(FMT)),
    )
    c.commit()
    return cur.rowcount > 0


def user_slots(chat_id: int, code: str, user_id: int) -> list[tuple[datetime, datetime]]:
    rows = conn().execute(
        "SELECT start_ts, end_ts FROM slots WHERE chat_id=? AND code=? AND user_id=?"
        " ORDER BY start_ts",
        (chat_id, code, user_id),
    ).fetchall()
    return [(datetime.strptime(r["start_ts"], FMT), datetime.strptime(r["end_ts"], FMT)) for r in rows]


def delete_slot(chat_id: int, code: str, user_id: int, start: datetime) -> None:
    c = conn()
    c.execute(
        "DELETE FROM slots WHERE chat_id=? AND code=? AND user_id=? AND start_ts=?",
        (chat_id, code, user_id, start.strftime(FMT)),
    )
    c.commit()


def availabilities(chat_id: int, code: str) -> dict[str, list[tuple[datetime, datetime]]]:
    """Per-person merged view: key is the display name (unique-ified by user id)."""
    rows = conn().execute(
        "SELECT user_id, display_name, start_ts, end_ts FROM slots WHERE chat_id=? AND code=?",
        (chat_id, code),
    ).fetchall()
    by_identity: dict[tuple, str] = {}
    out: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for r in rows:
        ident = (r["user_id"], r["display_name"] if r["user_id"] is None else "")
        if ident not in by_identity:
            name = r["display_name"]
            existing = set(by_identity.values())
            if name in existing:
                name = f"{name} ({r['user_id']})"
            by_identity[ident] = name
        out[by_identity[ident]].append(
            (datetime.strptime(r["start_ts"], FMT), datetime.strptime(r["end_ts"], FMT))
        )
    return dict(out)


# --- Users / credentials --------------------------------------------------

def save_user(user_id: int, **fields) -> None:
    c = conn()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(
        f"UPDATE users SET {sets}, updated_at=datetime('now','localtime') WHERE user_id=?",
        (*fields.values(), user_id),
    )
    c.commit()


def get_user(user_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def clear_user(user_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    c.commit()


# --- Bookings -------------------------------------------------------------

def add_booking(user_id: int, location: str, category: str, room_name: str,
                item_id: int | None, start: datetime, end: datetime,
                booking_ref: str | None = None, lid: int | None = None,
                gid: int | None = None) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO bookings (user_id, location, category, room_name, item_id,"
        " start_ts, end_ts, booking_ref, lid, gid) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, location, category, room_name, item_id,
         start.strftime(FMT), end.strftime(FMT), booking_ref, lid, gid),
    )
    c.commit()
    return cur.lastrowid


def bookings_with_expired_proof() -> list[sqlite3.Row]:
    """Check-in screenshots whose booking has finished - time to erase them."""
    return conn().execute(
        "SELECT * FROM bookings WHERE proof_path IS NOT NULL"
        " AND end_ts <= datetime('now','localtime')").fetchall()


def bookings_with_proof(user_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM bookings WHERE user_id=? AND proof_path IS NOT NULL",
        (user_id,)).fetchall()


def bookings_needing_checkin() -> list[sqlite3.Row]:
    """Booked bookings whose start is inside the action window (nag from
    start-5min, attempts until start+15min)."""
    now = datetime.now()
    lo = (now - timedelta(minutes=16)).strftime(FMT)
    hi = (now + timedelta(minutes=6)).strftime(FMT)
    return conn().execute(
        "SELECT * FROM bookings WHERE status='booked' AND start_ts BETWEEN ? AND ?",
        (lo, hi),
    ).fetchall()


def update_booking(booking_id: int, **fields) -> None:
    c = conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE bookings SET {sets} WHERE id=?", (*fields.values(), booking_id))
    c.commit()


def get_booking(booking_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()


def list_bookings(user_id: int, active_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM bookings WHERE user_id=?"
    if active_only:
        # checked_in counts as active too: you can still end a session
        # early, and you may want to cancel it right up until it ends.
        q += " AND status IN ('booked','pending','checked_in')"
        q += " AND end_ts >= datetime('now','localtime')"
    q += " ORDER BY start_ts"
    return conn().execute(q, (user_id,)).fetchall()


def usage_counts(user_id: int) -> tuple[dict[tuple[int, int], int], dict[int, int]]:
    """How often this person has booked each category and each space.

    Drives the "most used first" shortlists, so the common choices sit at the
    top instead of every option being a button.
    """
    cats: dict[tuple[int, int], int] = {}
    items: dict[int, int] = {}
    for r in conn().execute(
            "SELECT lid, gid, item_id FROM bookings WHERE user_id=?", (user_id,)):
        if r["lid"] and r["gid"]:
            cats[(r["lid"], r["gid"])] = cats.get((r["lid"], r["gid"]), 0) + 1
        if r["item_id"]:
            items[r["item_id"]] = items.get(r["item_id"], 0) + 1
    for f in conn().execute(
            "SELECT lid, gid, item_id FROM favourites WHERE user_id=?", (user_id,)):
        # A favourite is a deliberate vote, worth more than a single booking.
        cats[(f["lid"], f["gid"])] = cats.get((f["lid"], f["gid"]), 0) + 3
        if f["item_id"]:
            items[f["item_id"]] = items.get(f["item_id"], 0) + 3
    return cats, items


# --- Developer mode / per-user settings -----------------------------------

def is_developer(user_id: int) -> bool:
    """Owner, or someone the owner added with /dev add."""
    from . import config
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    return conn().execute(
        "SELECT 1 FROM developers WHERE user_id=?", (user_id,)).fetchone() is not None


def add_developer(user_id: int, added_by: int) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO developers (user_id, added_by) VALUES (?,?)",
              (user_id, added_by))
    c.commit()


def remove_developer(user_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM developers WHERE user_id=?", (user_id,))
    c.commit()


def list_developers() -> list[int]:
    return [r["user_id"] for r in conn().execute("SELECT user_id FROM developers")]


def get_setting(user_id: int, key: str, default=None):
    row = conn().execute("SELECT value FROM settings WHERE user_id=? AND key=?",
                         (user_id, key)).fetchone()
    return row["value"] if row else default


def set_setting(user_id: int, key: str, value) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?,?,?)",
              (user_id, key, str(value)))
    c.commit()


def shortlist_size(user_id: int) -> int:
    """How many "most used" entries to show before the Show-all toggle."""
    try:
        return max(1, min(15, int(get_setting(user_id, "mostused", 3))))
    except (TypeError, ValueError):
        return 3


# --- Favourites -----------------------------------------------------------

def add_fav(user_id: int, label: str, lid: int, gid: int, item_id: int | None,
            default_start: str | None = None, default_end: str | None = None) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO favourites (user_id, label, lid, gid, item_id, default_start, default_end)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, label, lid, gid, item_id, default_start, default_end))
    c.commit()
    return cur.lastrowid


def list_favs(user_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM favourites WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()


def get_fav(fav_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM favourites WHERE id=?", (fav_id,)).fetchone()


def del_fav(fav_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM favourites WHERE id=?", (fav_id,))
    c.commit()


# --- Group booking legs ---------------------------------------------------

def add_group_legs(chat_id: int, lid: int, gid: int, location: str, category: str,
                   legs: list[tuple[datetime, datetime]]) -> list[int]:
    c = conn()
    ids = []
    for start, end in legs:
        cur = c.execute(
            "INSERT INTO group_legs (chat_id, lid, gid, location, category, start_ts, end_ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (chat_id, lid, gid, location, category, start.strftime(FMT), end.strftime(FMT)))
        ids.append(cur.lastrowid)
    c.commit()
    return ids


def set_group_msg(leg_ids: list[int], msg_id: int) -> None:
    c = conn()
    c.executemany("UPDATE group_legs SET msg_id=? WHERE id=?",
                  [(msg_id, i) for i in leg_ids])
    c.commit()


def get_leg(leg_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM group_legs WHERE id=?", (leg_id,)).fetchone()


def session_legs(chat_id: int, msg_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM group_legs WHERE chat_id=? AND msg_id=? ORDER BY start_ts",
        (chat_id, msg_id)).fetchall()


def update_leg(leg_id: int, **fields) -> None:
    c = conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE group_legs SET {sets} WHERE id=?", (*fields.values(), leg_id))
    c.commit()


# --- Scheduled bookings ---------------------------------------------------

def add_scheduled(user_id: int, lid: int, gid: int, location: str, category: str,
                  item_id: int | None, start: datetime, end: datetime,
                  fire_at: datetime, retry_until: datetime) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO scheduled_bookings (user_id, lid, gid, location, category, item_id,"
        " start_ts, end_ts, fire_at, retry_until) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, lid, gid, location, category, item_id, start.strftime(FMT),
         end.strftime(FMT), fire_at.strftime(FMT), retry_until.strftime(FMT)),
    )
    c.commit()
    return cur.lastrowid


def due_scheduled() -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM scheduled_bookings WHERE status IN ('pending','retrying')"
        " AND fire_at <= datetime('now','localtime') ORDER BY fire_at",
    ).fetchall()


def upcoming_scheduled() -> list[sqlite3.Row]:
    """Jobs that have not fired yet, for the pre-hold pass."""
    return conn().execute(
        "SELECT * FROM scheduled_bookings WHERE status IN ('pending','retrying')"
        " AND start_ts > datetime('now','localtime') ORDER BY fire_at").fetchall()


def list_scheduled(user_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM scheduled_bookings WHERE user_id=? AND status IN ('pending','retrying')"
        " ORDER BY fire_at", (user_id,),
    ).fetchall()


def update_scheduled(job_id: int, **fields) -> None:
    c = conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE scheduled_bookings SET {sets} WHERE id=?", (*fields.values(), job_id))
    c.commit()


def get_scheduled(job_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM scheduled_bookings WHERE id=?", (job_id,)).fetchone()


# --- Durable facts --------------------------------------------------------
#
# The kv cache expires by design, which is right for "what was free at 10am"
# and badly wrong for "which categories are day-of only". Anything the bot
# would answer incorrectly rather than not at all if it vanished lives here,
# with no expiry - only a timestamp so staleness can be reported.

def durable_get(key: str, default=None):
    row = conn().execute("SELECT value FROM durable WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def durable_set(key: str, data) -> None:
    c = conn()
    c.execute(
        "INSERT INTO durable (key, value, updated_at) VALUES (?,?,datetime('now','localtime'))"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=excluded.updated_at",
        (key, json.dumps(data)))
    c.commit()


def record_error(feature: str, message: str, keep: int = 50) -> None:
    """Remember a failure so /developer can show it without reading bot.log."""
    try:
        errors = durable_get("recent_errors", []) or []
        errors.append({"at": datetime.now().strftime(FMT),
                       "feature": feature, "message": str(message)[:300]})
        durable_set("recent_errors", errors[-keep:])
    except Exception:                      # never let reporting break the caller
        log.debug("could not record the error", exc_info=True)


def recent_errors(limit: int = 10) -> list[dict]:
    return list(reversed((durable_get("recent_errors", []) or [])[-limit:]))


def clear_errors() -> None:
    durable_set("recent_errors", [])


def durable_age_days(key: str) -> float | None:
    row = conn().execute("SELECT updated_at FROM durable WHERE key=?", (key,)).fetchone()
    if not row or not row["updated_at"]:
        return None
    stamp = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
    return (datetime.now() - stamp).total_seconds() / 86400


# --- Holds (persisted so a restart can recover them) ----------------------

def add_hold(user_id: int, lid: int, gid: int, item_id: int, location: str,
             category: str, label: str, start: datetime, end: datetime,
             note: str = "") -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO holds (user_id, lid, gid, item_id, location, category, label,"
        " start_ts, end_ts, note, refreshed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
        (user_id, lid, gid, item_id, location, category, label,
         start.strftime(FMT), end.strftime(FMT), note))
    c.commit()
    return cur.lastrowid


def touch_hold(hold_id: int, renewals: int | None = None) -> None:
    c = conn()
    if renewals is None:
        c.execute("UPDATE holds SET refreshed_at=datetime('now','localtime') WHERE id=?",
                  (hold_id,))
    else:
        c.execute("UPDATE holds SET refreshed_at=datetime('now','localtime'),"
                  " renewals=? WHERE id=?", (renewals, hold_id))
    c.commit()


def close_hold(hold_id: int, status: str = "released") -> None:
    c = conn()
    c.execute("UPDATE holds SET status=? WHERE id=?", (status, hold_id))
    c.commit()


def released_holds(user_id: int, within_minutes: int = 240) -> list[sqlite3.Row]:
    """Holds that ended recently but whose slot is still in the future, so
    they are worth offering back."""
    return conn().execute(
        "SELECT * FROM holds WHERE user_id=? AND status IN ('released','lost')"
        " AND end_ts > datetime('now','localtime')"
        " AND created_at > datetime('now','localtime', ?)"
        " ORDER BY start_ts", (user_id, f"-{within_minutes} minutes")).fetchall()


def hold_budget(user_id: int) -> int:
    """Minutes this person's chopes keep renewing for. 0 means no limit."""
    from . import config
    raw = get_setting(user_id, "hold_minutes")
    try:
        return max(0, int(raw)) if raw is not None else config.HOLD_MAX_MINUTES
    except (TypeError, ValueError):
        return config.HOLD_MAX_MINUTES


def get_hold(hold_id: int) -> sqlite3.Row | None:
    return conn().execute("SELECT * FROM holds WHERE id=?", (hold_id,)).fetchone()


def live_holds(user_id: int | None = None) -> list[sqlite3.Row]:
    """Holds that were still active, and whose slot has not passed."""
    q = ("SELECT * FROM holds WHERE status='held'"
         " AND end_ts > datetime('now','localtime')")
    args: tuple = ()
    if user_id is not None:
        q += " AND user_id=?"
        args = (user_id,)
    return conn().execute(q + " ORDER BY start_ts", args).fetchall()


# --- Cache ----------------------------------------------------------------

def cache_get(key: str, max_age_hours: float) -> object | None:
    row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["value"])
        stored = datetime.strptime(payload["at"], FMT)
        if (datetime.now() - stored).total_seconds() > max_age_hours * 3600:
            return None
        return payload["data"]
    except Exception:
        return None


def cache_set(key: str, data: object) -> None:
    c = conn()
    c.execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
        (key, json.dumps({"at": datetime.now().strftime(FMT), "data": data})),
    )
    c.commit()
