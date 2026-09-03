"""What each library category actually allows: hours, notice, length, spaces.

Everything here was measured or read off the site rather than guessed:

* **Hours** come from the public availability grid - the first and last slot
  it returns for a day is that day's window. Observed: Mon-Fri 08:30-21:00,
  Sat 08:30-16:30, and **Sunday closed** (no category returns any slot).
* **Notice, length and daily caps** come from each category's own Policy
  text ("Reservations are accepted up to N days in advance", "Max duration of
  each booking: 2 hours", ...).
* **Spaces** are the real names harvested from the logged-in grid.

The bot needs this because a scheduled booking is made for a day whose grid
does not exist yet: without it we would offer 08:00-23:30 on a Sunday for a
room that closes at 21:00 and is shut that day.

`refresh()` re-derives it from the site; the cached copy lives in the kv table.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

from .. import storage

log = logging.getLogger(__name__)

CACHE_KEY = "category_meta"
CACHE_HOURS = 24 * 14

# Observed across every category; used until a category's own probe says
# otherwise. Sunday missing = closed.
DEFAULT_HOURS = {
    0: ("08:30", "21:00"),   # Monday
    1: ("08:30", "21:00"),
    2: ("08:30", "21:00"),
    3: ("08:30", "21:00"),
    4: ("08:30", "21:00"),
    5: ("08:30", "16:30"),   # Saturday closes early
    6: None,                 # Sunday: shut
}

# Every category's policy says the same thing, and it is the one time that
# matters for /schedulebook.
WINDOW_OPENS = time(23, 59)


def _meta() -> dict:
    return storage.durable_get(CACHE_KEY, {}) or {}


def get(lid: int, gid: int) -> dict:
    return _meta().get(f"{lid}_{gid}", {})


def save(meta: dict) -> None:
    storage.durable_set(CACHE_KEY, meta)


def hours_for(lid: int, gid: int, day: date) -> tuple[time, time] | None:
    """(open, close) for that weekday, or None when the library is shut."""
    entry = get(lid, gid)
    observed = entry.get("hours") or {}
    key = str(day.weekday())
    # A weekday we recorded as None is genuinely shut (Sunday). A weekday we
    # simply never saw - because its booking window had not opened during the
    # probe - is not evidence of anything, so fall back to the usual hours.
    hours = observed[key] if key in observed else DEFAULT_HOURS[day.weekday()]
    if hours is None:
        return None
    return (datetime.strptime(hours[0], "%H:%M").time(),
            datetime.strptime(hours[1], "%H:%M").time())


def is_open(lid: int, gid: int, day: date) -> bool:
    return hours_for(lid, gid, day) is not None


def advance_days(lid: int, gid: int) -> int:
    """How many days ahead this category can be booked. 0 = day-of only."""
    return int(get(lid, gid).get("advance_days", 1))


def max_each_minutes(lid: int, gid: int) -> int | None:
    value = get(lid, gid).get("max_each")
    return int(value) if value else None


def max_day_minutes(lid: int, gid: int) -> int | None:
    value = get(lid, gid).get("max_day")
    return int(value) if value else None


def spaces(lid: int, gid: int) -> dict[int, str]:
    """item id -> real name, so a scheduled booking can name a table even
    though that day's grid does not exist yet."""
    return {int(k): v for k, v in (get(lid, gid).get("spaces") or {}).items()}


def window_opens_at(lid: int, gid: int, target: date) -> datetime:
    """When the site starts accepting bookings for `target`.

    A category that takes bookings A days ahead rolls its window forward at
    23:59, so the day becomes bookable at 23:59 on (target - A - 1). For the
    day-of categories (A=0) that is 23:59 the night before, which matches
    their policy text word for word.
    """
    opens_on = target - timedelta(days=advance_days(lid, gid) + 1)
    return datetime.combine(opens_on, WINDOW_OPENS)


def describe(lid: int, gid: int) -> str:
    """One-paragraph summary for /rules and the confirm screens."""
    entry = get(lid, gid)
    if not entry:
        return ""
    adv = advance_days(lid, gid)
    when = ("day-of only - opens 23:59 the night before" if adv == 0
            else f"up to {adv} day{'s' if adv != 1 else ''} ahead")
    bits = [f"{entry.get('category', '')}: {when}"]
    if entry.get("max_each"):
        bits.append(f"max {int(entry['max_each']) // 60}h per booking")
    if entry.get("max_day"):
        bits.append(f"{int(entry['max_day']) // 60}h per day")
    if entry.get("spaces"):
        bits.append(f"{len(entry['spaces'])} spaces")
    return ", ".join(bits)


# --- Building it from the site -------------------------------------------

def parse_policy(text: str) -> dict:
    """Pull the numbers out of a category's Policy blurb."""
    out: dict = {}
    if re.search(r"advance booking is\s*not\s*available", text, re.I):
        out["advance_days"] = 0
    else:
        m = re.search(r"accepted up to (\d+)\s*day", text, re.I)
        if m:
            out["advance_days"] = int(m.group(1))
    m = re.search(r"Max duration of each booking:\s*(\d+(?:\.\d+)?)\s*(hour|min)", text, re.I)
    if m:
        n = float(m.group(1))
        out["max_each"] = int(n * 60) if m.group(2).lower().startswith("hour") else int(n)
    m = re.search(r"Max duration of bookings/day[:\s]*(\d+(?:\.\d+)?)\s*(hour|min)", text, re.I)
    if m:
        n = float(m.group(1))
        out["max_day"] = int(n * 60) if m.group(2).lower().startswith("hour") else int(n)
    return out


async def refresh(user_id: int, username: str, password: str) -> dict:
    """Re-read hours, policies and space names for every category."""
    import asyncio

    from . import browser, libcal

    meta = _meta()
    locations = await libcal.fetch_locations(force=True)
    for loc in locations:
        for cat in loc.categories:
            if not cat.gid:
                continue
            key = f"{cat.lid}_{cat.gid}"
            entry = dict(meta.get(key, {}))
            entry.update(library=loc.name, category=cat.label,
                         lid=cat.lid, gid=cat.gid)
            hours: dict[str, list[str] | None] = {}
            for n in range(7):
                day = date.today() + timedelta(days=n)
                try:
                    grid = await libcal.fetch_grid(cat.lid, cat.gid, day)
                except Exception:
                    continue
                if not grid:
                    continue                     # window shut, tells us nothing
                starts = [c.start for cells in grid.values() for c in cells]
                ends = [c.end for cells in grid.values() for c in cells]
                hours[str(day.weekday())] = [f"{min(starts):%H:%M}", f"{max(ends):%H:%M}"]
                entry.setdefault("spaces", {}).update(
                    {str(i): libcal.room_name(i) for i in grid})
            if hours:
                merged = dict(entry.get("hours") or {})
                merged.update(hours)
                entry["hours"] = merged
            names = await asyncio.to_thread(browser.harvest_names, user_id,
                                            username, password, cat.lid, cat.gid)
            if names:
                libcal.remember_room_names(names)
                entry["spaces"] = {str(k): v for k, v in names.items()}
            meta[key] = entry
    save(meta)
    return meta
