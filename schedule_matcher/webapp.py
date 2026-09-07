"""The contract between the bot and the availability Mini App.

Telegram cannot draw a paintable grid: an inline keyboard is discrete buttons
with no drag gesture, capped at 8 per row and 100 in total, and a week of
half-hours is 224 cells. So the grid is a web page opened inside Telegram, and
this module is the only thing the two sides have to agree on.

One bitmask carries a person's whole answer. Cell `day_index * ROWS + row`,
where a row is 30 minutes from 08:00 - the same grid `availability_view` has
always used, so the emoji picture, the PNG and `/best` keep working on the
slots this produces without knowing where they came from.

The page is static and hosted on GitHub Pages, so everything it needs travels
in the query string, and its answer comes back through `sendData`. Nothing is
hosted by us and no web server is added to the bot.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from . import config
from .availability_view import DAY_START_HOUR, ROWS, SLOT_MINUTES

Interval = tuple[datetime, datetime]

# The page paints at most this many days at once; a longer event pages inside
# the app rather than putting kilobytes in a URL.
MAX_DAYS = 14


def cell_start(day: date, row: int) -> datetime:
    return (datetime.combine(day, datetime.min.time())
            + timedelta(hours=DAY_START_HOUR, minutes=row * SLOT_MINUTES))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def pack(intervals: list[Interval], days: list[date]) -> str:
    """A person's availability as a bitmask over the event's days."""
    total = len(days) * ROWS
    bits = bytearray((total + 7) // 8)
    index = {d: i for i, d in enumerate(days)}
    for start, end in intervals:
        day_i = index.get(start.date())
        if day_i is None:
            continue
        for row in range(ROWS):
            moment = cell_start(days[day_i], row)
            if start <= moment < end:
                cell = day_i * ROWS + row
                bits[cell // 8] |= 1 << (cell % 8)
    return _b64(bytes(bits))


def unpack(packed: str, days: list[date]) -> list[Interval]:
    """The bitmask back into intervals, contiguous cells merged into one.

    Merging here rather than storing 32 rows per day keeps `/view`, `/best`
    and the "your availability" summaries reading exactly as they did when the
    times came from the tap-through pickers.
    """
    if not packed:
        return []
    try:
        bits = _unb64(packed)
    except Exception:
        return []
    out: list[Interval] = []
    for day_i, day in enumerate(days):
        run_start: int | None = None
        for row in range(ROWS + 1):          # one past the end closes a run
            cell = day_i * ROWS + row
            on = (row < ROWS and cell // 8 < len(bits)
                  and bool(bits[cell // 8] & (1 << (cell % 8))))
            if on and run_start is None:
                run_start = row
            elif not on and run_start is not None:
                out.append((cell_start(day, run_start), cell_start(day, row)))
                run_start = None
    return out


def heat(avail: dict[str, list[Interval]], days: list[date]) -> str:
    """How many people are free in each cell, four bits each.

    Four bits caps the count at 15, which is far past the size of any group
    that meets in one Telegram chat, and halves what the URL has to carry.
    """
    counts = [0] * (len(days) * ROWS)
    index = {d: i for i, d in enumerate(days)}
    for slots in avail.values():
        seen: set[int] = set()
        for start, end in slots:
            day_i = index.get(start.date())
            if day_i is None:
                continue
            for row in range(ROWS):
                if start <= cell_start(days[day_i], row) < end:
                    seen.add(day_i * ROWS + row)
        for cell in seen:                    # one person counts once per cell
            counts[cell] = min(15, counts[cell] + 1)
    raw = bytearray((len(counts) + 1) // 2)
    for i, n in enumerate(counts):
        if i % 2 == 0:
            raw[i // 2] |= n << 4
        else:
            raw[i // 2] |= n
    return _b64(bytes(raw))


def url_for(chat_id: int, event, days: list[date], mine: list[Interval],
            avail: dict[str, list[Interval]]) -> str | None:
    """The page's address, with everything it needs to draw itself."""
    if not config.WEBAPP_URL:
        return None
    days = days[:MAX_DAYS]
    params = {
        "v": 1,
        "c": chat_id,
        "e": event["code"],
        "n": event["name"][:60],
        "d0": days[0].isoformat(),
        "nd": len(days),
        "me": pack(mine, days),
        "heat": heat(avail, days),
        "tot": len(avail),
    }
    return f"{config.WEBAPP_URL.rstrip('/')}/?{urlencode(params)}"


def read_reply(payload: str) -> dict | None:
    """What came back from the page, or None if it is not ours.

    The page is public, so this is the boundary where a stranger's JSON has to
    be treated as a stranger's JSON: every field is checked before the caller
    is allowed to touch the database with it.
    """
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    try:
        chat_id = int(data["c"])
        code = str(data["e"]).strip().upper()
        first = date.fromisoformat(str(data["d0"]))
        count = int(data["nd"])
        packed = str(data.get("me") or "")
    except (KeyError, TypeError, ValueError):
        return None
    if not code or not 1 <= count <= MAX_DAYS:
        return None
    days = [first + timedelta(days=n) for n in range(count)]
    return {"chat_id": chat_id, "code": code, "days": days,
            "intervals": unpack(packed, days)}
