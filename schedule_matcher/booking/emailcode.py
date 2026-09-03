"""Read a LibFacilities confirmation email.

A real one looks like this:

    The following booking has been confirmed:

    Lee Wee Nam Library
    LIBLWNL-AK-08: 12:45pm - 2:15pm Thursday, September 3, 2026.
    ...
    2. Enter this code: W5XY
    ...
    please cancel it via https://libcalendar.ntu.edu.sg/equipment/cancel?id=c1b9...

so the whole booking is in there: the space, the day, both times, the check-in
code and the cancellation link. Pasting or forwarding it to the bot is
therefore enough to track a booking made on the website - nothing needs typing.
"""

from __future__ import annotations

import re
from datetime import datetime

CODE_RE = re.compile(
    r"(?:check[\s-]*in\s*code|booking\s*code|enter\s*this\s*code|code)"
    r"\s*[:\-]?\s*([A-Z0-9]{4,8})\b", re.I)
CANCEL_RE = re.compile(r"https://libcalendar\.ntu\.edu\.sg/\S*cancel\S*", re.I)
REF_RE = re.compile(r"booking\s*(?:id|reference)\s*[:\-]?\s*([\w-]+)", re.I)

# "LIBLWNL-AK-08: 12:45pm - 2:15pm Thursday, September 3, 2026."
BOOKING_RE = re.compile(
    r"^(?P<space>[^\n:]{2,60}):\s*"
    r"(?P<start>\d{1,2}:\d{2}\s*[ap]m)\s*[-–]\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*[ap]m)\s+"
    r"(?:\w+,\s*)?(?P<date>[A-Z][a-z]+\s+\d{1,2},\s*\d{4})",
    re.I | re.M)
LIBRARY_RE = re.compile(r"^([A-Z][\w&' ]*Library)\s*$", re.M)


def _when(date_text: str, clock: str) -> datetime | None:
    for fmt in ("%B %d, %Y %I:%M%p", "%b %d, %Y %I:%M%p"):
        try:
            return datetime.strptime(
                f"{date_text.strip()} {clock.replace(' ', '').lower()}", fmt)
        except ValueError:
            continue
    return None


def parse_text(text: str) -> dict:
    """Everything the email is willing to tell us; None where it says nothing."""
    code = CODE_RE.search(text)
    cancel = CANCEL_RE.search(text)
    ref = REF_RE.search(text)
    out = {
        "code": code.group(1).upper() if code else None,
        "cancel_link": cancel.group(0).rstrip(".,)>]") if cancel else None,
        "reference": ref.group(1) if ref else None,
        "space": None, "start": None, "end": None, "library": None,
    }
    booking = BOOKING_RE.search(text)
    if booking:
        out["space"] = booking.group("space").strip()
        out["start"] = _when(booking.group("date"), booking.group("start"))
        out["end"] = _when(booking.group("date"), booking.group("end"))
    library = LIBRARY_RE.search(text)
    if library:
        out["library"] = library.group(1).strip()
    return out


def looks_like_confirmation(text: str) -> bool:
    return ("libcalendar.ntu.edu.sg" in text.lower()
            or bool(CODE_RE.search(text)))
