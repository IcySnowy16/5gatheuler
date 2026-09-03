"""When2meet-style views of who is free when.

`/best` answers "when should we meet"; this answers "show me the picture so I
can decide myself". Two ways to look:

* everyone who has responded, or
* a subset you tick off, for when the meeting only needs three of the five.

Rendered as an emoji grid in the message (no dependencies, instant) with a
button to send the same thing as a PNG when the grid gets big.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

from . import matching

Interval = tuple[datetime, datetime]

# Half-hour rows, 08:00 to 24:00, matching the availability pickers.
DAY_START_HOUR = 8
DAY_END_HOUR = 24
SLOT_MINUTES = 30
ROWS = (DAY_END_HOUR - DAY_START_HOUR) * 60 // SLOT_MINUTES

# Five shades from "nobody" to "everybody".
SHADES = ["⬜", "\U0001f7e5", "\U0001f7e7", "\U0001f7e8", "\U0001f7e9"]


def _free_at(slots: list[Interval], moment: datetime) -> bool:
    return any(s <= moment < e for s, e in slots)


def counts(avail: dict[str, list[Interval]],
           people: list[str] | None = None) -> tuple[list[date], list[list[int]], int]:
    """(days, grid[row][day] = how many free, number of people counted).

    `people` limits the count to a chosen subset; None means everyone.
    """
    chosen = {n: matching.merge_intervals(s) for n, s in avail.items()
              if people is None or n in people}
    days = sorted({s.date() for slots in chosen.values() for s, _ in slots})
    grid = [[0] * len(days) for _ in range(ROWS)]
    for col, day in enumerate(days):
        for row in range(ROWS):
            moment = (datetime.combine(day, datetime.min.time())
                      + timedelta(hours=DAY_START_HOUR, minutes=row * SLOT_MINUTES))
            grid[row][col] = sum(1 for slots in chosen.values()
                                 if _free_at(slots, moment))
    return days, grid, len(chosen)


def _level(n: int, total: int) -> int:
    """0 nobody free .. 4 everybody free, so the colour rises with the count."""
    if total <= 0 or n <= 0:
        return 0
    if n >= total:
        return 4
    ratio = n / total
    if ratio < 1 / 3:
        return 1
    if ratio < 2 / 3:
        return 2
    return 3


def _shade(n: int, total: int) -> str:
    return SHADES[_level(n, total)]


def emoji_grid(avail: dict[str, list[Interval]],
               people: list[str] | None = None, title: str = "") -> str:
    """The grid as text: one row per half hour, one column per day."""
    days, grid, total = counts(avail, people)
    if not days or not total:
        return "Nobody has added availability yet."

    names = sorted(people if people is not None else avail)
    lines = [title] if title else []
    lines.append("Counting: " + ", ".join(names) + f"  ({total})")
    lines.append("      " + " ".join(f"{d:%a}" for d in days))
    lines.append("      " + " ".join(f"{d.day:2d} " for d in days))

    for row in range(ROWS):
        if not any(grid[row]):
            continue                      # skip hours nobody ever picked
        moment = timedelta(hours=DAY_START_HOUR, minutes=row * SLOT_MINUTES)
        label = f"{moment.seconds // 3600:02d}:{(moment.seconds // 60) % 60:02d}"
        cells = " ".join(_shade(grid[row][c], total) for c in range(len(days)))
        best = max(grid[row])
        lines.append(f"{label} {cells}" + ("  ← all free" if best == total else ""))

    lines.append("")
    lines.append(f"{SHADES[4]} everyone  {SHADES[2]} some  {SHADES[0]} nobody")
    return "\n".join(lines)


def png_grid(avail: dict[str, list[Interval]],
             people: list[str] | None = None, title: str = "") -> io.BytesIO | None:
    """The same grid as a picture. None when Pillow isn't installed."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    days, grid, total = counts(avail, people)
    if not days or not total:
        return None

    rows = [r for r in range(ROWS) if any(grid[r])] or list(range(ROWS))
    cell_w, cell_h, left, top = 96, 22, 70, 54
    width = left + cell_w * len(days) + 20
    height = top + cell_h * len(rows) + 40

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((10, 16), title or "Availability", fill="black")
    d.text((10, 32), f"{total} people", fill="#666666")

    for col, day in enumerate(days):
        d.text((left + col * cell_w + 8, 36), f"{day:%a %d %b}", fill="black")

    palette = ["#f0f0f0", "#ffd6d6", "#ffe9c7", "#e6f4b8", "#7ac943"]
    for i, row in enumerate(rows):
        y = top + i * cell_h
        moment = timedelta(hours=DAY_START_HOUR, minutes=row * SLOT_MINUTES)
        d.text((10, y + 4),
               f"{moment.seconds // 3600:02d}:{(moment.seconds // 60) % 60:02d}",
               fill="#333333")
        for col in range(len(days)):
            n = grid[row][col]
            shade = _level(n, total)
            x = left + col * cell_w
            d.rectangle([x, y, x + cell_w - 3, y + cell_h - 3],
                        fill=palette[shade], outline="#dddddd")
            if n:
                d.text((x + cell_w // 2 - 4, y + 4), str(n), fill="#222222")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "availability.png"
    return buf
