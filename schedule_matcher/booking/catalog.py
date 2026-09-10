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

`refresh()` re-derives it from the site; the answer lives in the durable
table. A fresh install has none of it, and cannot go and get it either -
the policy pages need a login. So the measured copy ships with the code in
`catalog_seed.json` and is loaded on first use: a new laptop knows the hours,
notice periods and desk names before anyone signs in. This is public library
data, identical for every user.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

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


SEED_FILE = Path(__file__).with_name("catalog_seed.json")


def _load_seed() -> dict:
    """Populate an empty catalogue from the copy shipped with the code.

    Without this a fresh install believes every category takes 1 day's
    notice, so a day-of booking would be scheduled to fire a day early.
    """
    try:
        seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    except Exception as exc:                      # missing or corrupt: carry on
        log.warning("no catalogue seed: %s", exc)
        return {}
    meta = seed.get("categories") or {}
    if not meta:
        return {}
    save(meta)
    if seed.get("room_names"):
        from . import libcal
        libcal.remember_room_names({int(k): v
                                    for k, v in seed["room_names"].items()})
    if seed.get("probed_limits"):
        storage.durable_set("category_limits", seed["probed_limits"])
    log.info("catalogue seeded with %d categories", len(meta))
    return meta


def _meta() -> dict:
    return storage.durable_get(CACHE_KEY, {}) or _load_seed()


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


def all_categories() -> list[dict]:
    """Every category the catalogue knows, for the booking picker.

    Each entry carries library, category, lid and gid. This is what lets a
    machine that has never logged in still offer Griffin Booth, whose
    homepage link names no ids.
    """
    return [e for e in _meta().values() if e.get("lid") and e.get("gid")]


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


async def learn(user_id: int, username: str, password: str, library: str,
                label: str, lid: int, gid: int) -> dict:
    """Everything worth knowing about one category, read off the site."""
    import asyncio

    from . import browser, libcal

    entry = {"library": library, "category": label, "lid": lid, "gid": gid}
    hours, grid_ids = {}, set()
    # Today is skipped for opening hours: its grid only shows what is left of
    # it, so learning at 3pm would record the library as opening at 3pm. Any
    # weekday never seen falls back to the usual hours, which is right.
    for n in range(1, 8):
        day = date.today() + timedelta(days=n)
        try:
            grid = await libcal.fetch_grid(lid, gid, day)
        except Exception:
            continue
        if not grid:
            continue
        cells = [c for cs in grid.values() for c in cs]
        hours[str(day.weekday())] = [f"{min(c.start for c in cells):%H:%M}",
                                     f"{max(c.end for c in cells):%H:%M}"]
        grid_ids |= set(grid)
    if not grid_ids:                    # nothing published ahead: today will do
        try:
            grid_ids = set(await libcal.fetch_grid(lid, gid, date.today()))
        except Exception:
            pass
    if hours:
        entry["hours"] = hours
    names = await asyncio.to_thread(browser.harvest_names, user_id, username,
                                    password, lid, gid)
    if names:
        libcal.remember_room_names(names)
        entry["spaces"] = {str(k): v for k, v in names.items()}
        # Real spaces that the public grid never returned means this category
        # books seats, and the grid has to be asked for them by name.
        if grid_ids and not (grid_ids & set(names)):
            entry["seats"] = True
    text = await asyncio.to_thread(browser.harvest_policy, user_id, username,
                                   password, lid, gid)
    if text:
        entry.update(parse_policy(text))
    return entry


async def discover(user_id: int, username: str, password: str) -> list[str]:
    """Find categories the bot does not know yet, and learn them.

    Cheap by design: one browser session reads every library's own category
    list, and only something genuinely new costs the slow per-category work.
    That is what keeps the shipped seed a starting point rather than a thing
    anyone has to maintain by hand - if NTU adds a room, the bot finds it.
    """
    import asyncio

    from . import browser, libcal

    meta = _meta()
    locations = await libcal.fetch_locations()
    lids = sorted({c.lid for loc in locations for c in loc.categories if c.lid})
    names = {c.lid: loc.name for loc in locations for c in loc.categories}
    found = await asyncio.to_thread(browser.harvest_all_categories, user_id,
                                    username, password, lids)
    added = []
    for lid, cats in found.items():
        for gid, label in cats.items():
            key = f"{lid}_{gid}"
            if key in meta:
                meta[key]["category"] = label      # the site's own wording
                continue
            if "staff only" in label.lower():
                continue
            log.info("catalogue: learning %s (lid=%s gid=%s)", label, lid, gid)
            meta[key] = await learn(user_id, username, password,
                                    names.get(lid, ""), label, lid, gid)
            added.append(label)
    save(meta)
    if added:
        storage.cache_clear("libcal_locations")      # the picker must re-read
    return added


async def refresh(user_id: int, username: str, password: str) -> dict:
    """Re-read hours, policies and space names for every category."""
    import asyncio

    from . import browser, libcal

    meta = _meta()
    locations = await libcal.fetch_locations(force=True)

    # The homepage does not name every category - Griffin Booth is linked as
    # /reserve/collab and the Humanities ones as /space/NNNNN, neither of
    # which carries an lid or gid. Each library's own page does list them all.
    for loc in locations:
        lid = next((c.lid for c in loc.categories if c.lid), None)
        if not lid:
            continue
        found = await asyncio.to_thread(browser.harvest_categories, user_id,
                                        username, password, lid)
        for gid, label in found.items():
            if not any(c.lid == lid and c.gid == gid for c in loc.categories):
                log.info("catalogue: %s adds %s (gid=%s)", loc.name, label, gid)
                loc.categories.append(
                    libcal.Category(label=label, lid=lid, gid=gid,
                                    url=f"/spaces?lid={lid}&gid={gid}"))

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
                entry["grid_ids"] = sorted(set(entry.get("grid_ids") or [])
                                           | set(grid))
            if hours:
                merged = dict(entry.get("hours") or {})
                merged.update(hours)
                entry["hours"] = merged
            names = await asyncio.to_thread(browser.harvest_names, user_id,
                                            username, password, cat.lid, cat.gid)
            if names:
                libcal.remember_room_names(names)
                entry["spaces"] = {str(k): v for k, v in names.items()}
                # A category whose real spaces are none of the ids the public
                # grid returned is booking seats, not rooms: the grid answers
                # with the room itself unless the request asks for seats.
                grid_ids = set(entry.get("grid_ids") or [])
                if grid_ids and not (grid_ids & set(names)):
                    entry["seats"] = True
                    log.info("catalogue: %s books seats, not spaces", cat.label)
            # The policy blurb is the only source for the notice period and
            # the length caps, and it needs a login to read.
            text = await asyncio.to_thread(browser.harvest_policy, user_id,
                                           username, password, cat.lid, cat.gid)
            if text:
                entry.update(parse_policy(text))
            meta[key] = entry
    save(meta)
    return meta
