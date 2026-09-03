"""Settings, and where the bot keeps its private files.

This project lives in a synced OneDrive folder, which is a bad home for the
bot token, NTU credentials and the SQLite database. Secrets and state live in
a local, non-synced directory instead:

    %LOCALAPPDATA%\\ScheduleMatcher\\.env
    %LOCALAPPDATA%\\ScheduleMatcher\\schedule_matcher.db
    %LOCALAPPDATA%\\ScheduleMatcher\\pw_state_<user>.json   (browser sessions)
    %LOCALAPPDATA%\\ScheduleMatcher\\debug\\                (failure screenshots)

Override the directory with SCHEDULE_MATCHER_HOME. A .env next to the code is
still read as a fallback, but `warnings()` will point out that it is sitting
in cloud storage.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

CLOUD_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync")


def _home() -> Path:
    override = os.getenv("SCHEDULE_MATCHER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "ScheduleMatcher"
    return Path.home() / ".schedule-matcher"


HOME = _home()
DEBUG_DIR = HOME / "debug"
# Check-in screenshots live here only until the booking ends.
PROOF_DIR = HOME / "proof"

# The private copy wins: python-dotenv does not overwrite variables that are
# already set, so whichever file is loaded first takes precedence.
ENV_FILE = HOME / ".env"
LEGACY_ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
load_dotenv(LEGACY_ENV_FILE)


def is_cloud_synced(path: Path) -> bool:
    lowered = str(path).lower()
    return any(marker in lowered for marker in CLOUD_MARKERS)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

DB_PATH = Path(os.getenv("DB_PATH", "").strip() or (HOME / "schedule_matcher.db"))

# Old pickle DB locations, migrated into SQLite on first run.
LEGACY_PICKLES = (BASE_DIR / "schedule_bot.pkl", BASE_DIR.parent / "schedule_bot.pkl")

LIBCAL_BASE = os.getenv("LIBCAL_BASE", "https://libcalendar.ntu.edu.sg").rstrip("/")

# All times are naive local time; NTU runs on Asia/Singapore and so should the
# machine hosting this bot.
TIMEZONE = "Asia/Singapore"

# Show the booking browser window instead of running headless. Useful the
# first time (to watch the NTU login) and whenever selectors need debugging.
HEADFUL = os.getenv("HEADFUL", "").strip().lower() in {"1", "true", "yes", "on"}

# Longest booking the bot will offer, in minutes.
MAX_BOOKING_MINUTES = int(os.getenv("MAX_BOOKING_MINUTES", "240"))

# How many days ahead the /book date picker offers.
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "7"))

# How long after a booking the bot-inbox poller keeps looking for the email code.
EMAIL_POLL_MINUTES = int(os.getenv("EMAIL_POLL_MINUTES", "10"))

# Scheduled bookings: how long to keep retrying after the fire time when the
# booking window hasn't opened yet, and the gap between attempts.
SCHED_RETRY_MINUTES = int(os.getenv("SCHED_RETRY_MINUTES", "30"))
SCHED_RETRY_GAP_SECONDS = int(os.getenv("SCHED_RETRY_GAP_SECONDS", "120"))

# Measured: the day-of window opens at 23:59:00 exactly, and contested desks
# go within moments. So for the first few minutes after the fire time the bot
# retries every few seconds rather than every couple of minutes.
SCHED_RUSH_SECONDS = int(os.getenv("SCHED_RUSH_SECONDS", "15"))
SCHED_RUSH_WINDOW_MINUTES = int(os.getenv("SCHED_RUSH_WINDOW_MINUTES", "5"))

# The bot's owner: the only person who may grant developer mode to others.
# Read from .env ONLY - .env lives in %LOCALAPPDATA%, never in the repo, so no
# Telegram id is ever committed to git. Extra developers are added at runtime
# with /dev add <id> and stored in the database, also outside the repo.
def _owner_id() -> int | None:
    raw = os.getenv("OWNER_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


OWNER_ID = _owner_id()

LOG_FILE = HOME / "bot.log"

# Choping (holding a space without booking it). Each hold keeps a headless
# browser parked on the checkout page, so keep the cap modest. The site's own
# hold lasts ~10 min; the bot renews it until HOLD_MAX_MINUTES is reached.
MAX_HOLDS = int(os.getenv("MAX_HOLDS", "6"))
# How long the bot keeps re-taking a slot before it stops on its own.
# This is the bot being polite, not a library rule - set it to 0 for no
# limit, or extend a single hold from /holds when you need longer.
HOLD_MAX_MINUTES = int(os.getenv("HOLD_MAX_MINUTES", "60"))

# A measured hold lapsed after 5.5 min while the page claimed 4.3, so neither
# the page's promise nor a fixed 10 min can be trusted. Re-take the slot every
# few minutes instead: a renewal's gap is sub-second, whereas noticing a lapse
# from the 30 s grid poll can leave the slot open to others for up to 30 s.
HOLD_RENEW_AFTER_SECONDS = int(os.getenv("HOLD_RENEW_AFTER_SECONDS", "200"))
HOLD_POLL_SECONDS = int(os.getenv("HOLD_POLL_SECONDS", "30"))

# Group fan-out booking: length of each member's leg, and the longest total
# session /groupbook will split.
GROUP_LEG_MINUTES = int(os.getenv("GROUP_LEG_MINUTES", "120"))
GROUP_MAX_MINUTES = int(os.getenv("GROUP_MAX_MINUTES", "480"))

# Experimental: put the bot's own inbox address into the booking form's email
# field so confirmation emails arrive directly (no Outlook rule needed). The
# library may require an NTU address - off by default.
USE_BOT_EMAIL_ON_FORM = os.getenv("USE_BOT_EMAIL_ON_FORM", "").strip().lower() in {
    "1", "true", "yes", "on"}


def warnings() -> list[str]:
    notes = []
    if LEGACY_ENV_FILE.exists() and is_cloud_synced(LEGACY_ENV_FILE):
        notes.append(
            f"Your bot token is in {LEGACY_ENV_FILE}, inside a synced cloud folder. "
            f"Move it to {ENV_FILE} so it stays on this machine."
        )
    if is_cloud_synced(DB_PATH):
        notes.append(
            f"The database is at {DB_PATH}, inside a synced cloud folder. It holds "
            f"NTU credentials. Move it to {HOME} or set DB_PATH."
        )
    return notes


def validate() -> None:
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        raise SystemExit(
            f"TELEGRAM_TOKEN is not set. Put it in {ENV_FILE}\n"
            "(that folder is local to this machine, so the token is not synced anywhere)."
        )
    HOME.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
