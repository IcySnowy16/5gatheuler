"""Overlap search over everyone's availability.

Fixes two flaws of the original sweep: adjacent/overlapping slots from the
same person are merged first (14:00-15:00 plus 15:00-16:00 now satisfies a
90-minute meeting), and runs of equally-good start times are compressed into
one "start anywhere in this window" line instead of five 15-minute shifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

Interval = tuple[datetime, datetime]

STEP = timedelta(minutes=15)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass
class Suggestion:
    earliest_start: datetime
    latest_start: datetime
    duration: timedelta
    people: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.people)


def best_slots(avail: dict[str, list[Interval]], duration_minutes: int,
               top: int = 5) -> tuple[list[Suggestion], list[str]]:
    """Returns (suggestions sorted best-first, all participant names)."""
    merged = {name: merge_intervals(slots) for name, slots in avail.items() if slots}
    everyone = sorted(merged)
    if not merged:
        return [], []

    need = timedelta(minutes=duration_minutes)
    lo = min(s for slots in merged.values() for s, _ in slots)
    hi = max(e for slots in merged.values() for _, e in slots)
    lo -= timedelta(minutes=lo.minute % 15, seconds=lo.second, microseconds=lo.microsecond)

    # Every candidate start time with the exact set of people free for it.
    windows: list[tuple[datetime, tuple[str, ...]]] = []
    current = lo
    while current + need <= hi:
        free = tuple(
            name for name, slots in merged.items()
            if any(s <= current and current + need <= e for s, e in slots)
        )
        if free:
            windows.append((current, free))
        current += STEP

    # Compress consecutive starts with the identical group into one range.
    suggestions: list[Suggestion] = []
    for start, people in windows:
        last = suggestions[-1] if suggestions else None
        if last and last.people == people and start - last.latest_start <= STEP:
            last.latest_start = start
        else:
            suggestions.append(Suggestion(start, start, need, people))

    suggestions.sort(key=lambda s: (-s.count, s.earliest_start))
    return suggestions[:top], everyone


def format_suggestions(suggestions: list[Suggestion], everyone: list[str],
                       duration_minutes: int) -> str:
    if not suggestions:
        return "No time works for anyone yet. Add availability with /add."
    lines = [f"Best {duration_minutes}-minute slots ({len(everyone)} people responded):\n"]
    for i, s in enumerate(suggestions, 1):
        day = s.earliest_start.strftime("%a %d %b")
        if s.earliest_start == s.latest_start:
            when = f"{day}, {s.earliest_start:%H:%M}-{(s.earliest_start + s.duration):%H:%M}"
        else:
            when = (f"{day}, start {s.earliest_start:%H:%M}-{s.latest_start:%H:%M} "
                    f"(any {duration_minutes} min)")
        missing = [p for p in everyone if p not in s.people]
        line = f"{i}. {when} - {s.count}/{len(everyone)} free"
        if missing and len(missing) <= 5:
            line += f" (missing: {', '.join(missing)})"
        lines.append(line)
    return "\n".join(lines)
