"""What we know about the library's booking rules, and what we keep learning.

Pre-checks run at confirm time so the user is warned BEFORE a minute-long
browser flow ends in a refusal. Every refusal message the site ever returns
is also recorded (kv key 'learned_refusals'), so new rules can be promoted
into KNOWN_RULES once the pattern is clear. /rules prints both.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import config, storage

KNOWN_RULES = [
    ("R1", "No consecutive bookings of the same facility",
     "The site refuses a booking of a space that touches your existing "
     "booking of the same space (back-to-back or overlapping)."),
    ("R2", "45-minute buffer",
     "The 45 minutes before an existing booking show red, but a booking may "
     "END inside that buffer (e.g. 1-2pm is fine when 1:15-2pm is buffer)."),
    ("R3", "Check-in window: 5 min before start until 15 min after",
     "Outside this window check-in fails; no-shows lose the booking. The bot "
     "auto-checks-in at start-2min, at start, and once more at start+5min."),
    ("R4", "Day-of categories open at 11:59 PM the night before",
     "Arrakis-style categories have no advance booking; the site's policy "
     "text says the window opens at 11:59 PM for the following day. "
     "/schedulebook's default fire time matches this."),
    ("R5", "Per-category length limits",
     "Each category has its own min/max per booking (probed table below); "
     "longer sessions need multiple legs - see /groupbook."),
    ("R9", "One active booking session at a time",
     "The confirmation email states each user may hold only one active "
     "session; check out to end one before starting another."),
    ("R8", "Cancelling is Check Out - same code as check-in",
     "There is no 'my bookings' page (/r offers only Reserve, Check In and "
     "Check Out), so releasing a booking means POSTing email + check-in code "
     "to /r/checkout. That frees the space at any point before the booking "
     "ends, whether or not you checked in. The emailed cancellation link is "
     "the fallback when the code isn't known."),
    ("R7", "Choping: reaching checkout holds a slot for ~10 minutes",
     "Adding a slot to the cart blocks nobody, but reaching the checkout page "
     "marks it taken on the public grid until you submit, press Remove, or "
     "~10 min pass. /chope parks there and renews the hold; the renewal has a "
     "sub-second gap where someone else could grab the slot."),
    ("R6", "At most 8 hours of bookings per day",
     "From the site's policy text (Arrakis); refusals will show if other "
     "categories differ."),
]


def precheck(user_id: int, item_id: int | None, start: datetime,
             end: datetime) -> list[str]:
    """Warnings (not blocks) for a booking the user is about to confirm."""
    warnings: list[str] = []
    if item_id:
        for b in storage.list_bookings(user_id):
            if b["item_id"] != item_id or b["status"] not in ("booked", "checked_in"):
                continue
            b_start = datetime.strptime(b["start_ts"], storage.FMT)
            b_end = datetime.strptime(b["end_ts"], storage.FMT)
            if b_end == start or b_start == end:
                warnings.append(
                    f"Rule R1: this touches your existing {b['room_name']} booking "
                    f"({b['start_ts']}-{b['end_ts'][-5:]}) - the site will likely "
                    "refuse consecutive bookings of the same space. Consider a "
                    "different space, or /groupbook with a friend.")
            elif b_start < end and start < b_end:
                warnings.append(
                    f"You already have {b['room_name']} booked "
                    f"{b['start_ts']}-{b['end_ts'][-5:]} overlapping this period.")
    if end - start > timedelta(minutes=config.MAX_BOOKING_MINUTES):
        warnings.append(f"Rule R5: longer than {config.MAX_BOOKING_MINUTES} min - "
                        "the site will likely trim or refuse this.")
    return warnings


def record_refusal(category: str, message: str) -> None:
    seen = storage.durable_get("learned_refusals", []) or []
    entry = {"at": datetime.now().strftime(storage.FMT),
             "category": category, "message": message[:300]}
    if not any(e.get("message") == entry["message"] for e in seen):
        seen.append(entry)
        storage.durable_set("learned_refusals", seen[-50:])


def rules_text() -> str:
    lines = ["Known booking rules:"]
    for rid, title, desc in KNOWN_RULES:
        lines.append(f"\n{rid}. {title}\n   {desc}")
    limits = storage.durable_get("category_limits", {}) or {}
    if limits:
        lines.append("\n\nPer-category booking lengths (probed from the site, "
                     "self-updating with every booking):")
        for entry in sorted(limits.values(), key=lambda e: e.get("label", "")):
            if entry.get("label"):
                lines.append(f"- {entry['label']}: {entry.get('min', '?')}-"
                             f"{entry.get('max', '?')} min")
    learned = storage.durable_get("learned_refusals", []) or []
    if learned:
        lines.append("\n\nRefusal messages I've collected (newest last):")
        for e in learned[-10:]:
            lines.append(f"- [{e['category']}] {e['message']}")
    return "\n".join(lines)
