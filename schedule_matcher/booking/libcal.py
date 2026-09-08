"""NTU LibCal (libcalendar.ntu.edu.sg) client.

Two layers:

* Public HTTP (httpx): the locations page and the availability-grid AJAX
  endpoint work without login, so browsing categories and free slots in
  Telegram is instant and never touches credentials.

* Playwright: viewing a /spaces page or submitting a booking redirects
  through au.libauth.com to the NTU login, so the actual booking drives a
  real (headless) browser. Sessions are persisted per user in
  %LOCALAPPDATA%\\ScheduleMatcher so later bookings usually skip the login.

Grid cell states (className on each 15-minute slot):
  (none)                -> free
  s-lc-eq-checkout      -> booked by someone
  s-lc-eq-r-unavailable -> the 45-minute buffer LibCal paints before an
                           existing booking. You may still END a booking
                           inside that buffer (booking 13:00-14:00 is fine
                           when 13:15-14:00 is buffer for a 14:00 booking),
                           so buffer cells block starts but not ends.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import config, storage

log = logging.getLogger(__name__)

BASE = config.LIBCAL_BASE
GRID_FMT = "%Y-%m-%d %H:%M:%S"

FREE, BOOKED, BUFFER, UNAVAILABLE = "free", "booked", "buffer", "unavail"


@dataclass
class Category:
    label: str
    lid: int
    gid: int
    url: str


@dataclass
class Location:
    name: str
    categories: list[Category] = field(default_factory=list)


@dataclass
class Cell:
    start: datetime
    end: datetime
    item_id: int
    checksum: str
    state: str


# --- Public data ----------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        headers={"User-Agent": "Mozilla/5.0 (ScheduleMatcher bot)"},
        timeout=25,
        follow_redirects=False,
    )


def _merge_catalogue(locations: list[Location]) -> None:
    """Add the categories the homepage does not name, and use the site's own
    wording for the ones it does.

    The homepage is public but incomplete: a category linked as
    /reserve/collab (Griffin Booth) or /space/52771 (three of the four in the
    Humanities library) carries no lid/gid in its URL, so the parse above
    threw it away and those spaces could not be booked at all. The catalogue
    knows their ids and their real names, and it ships with the code in
    catalog_seed.json, so this fills the gaps without anyone logging in.
    """
    from . import catalog

    by_name = {loc.name: loc for loc in locations}
    for entry in catalog.all_categories():
        lid, gid = entry.get("lid"), entry.get("gid")
        label, library = entry.get("category"), entry.get("library")
        if not (lid and gid and label and library):
            continue
        loc = by_name.get(library)
        if loc is None:
            loc = Location(name=library, categories=[])
            by_name[library] = loc
            locations.append(loc)
        existing = next((c for c in loc.categories
                         if c.lid == lid and c.gid == gid), None)
        if existing is None:
            loc.categories.append(Category(
                label=label, lid=lid, gid=gid,
                url=f"/spaces?lid={lid}&gid={gid}"))
        else:
            existing.label = label      # the site's wording beats the homepage's


def _normalise(locations: list[Location]) -> list[Location]:
    """Whatever the list came from, make it the list we actually offer.

    Both the homepage and the day-old cache can be missing categories or
    carrying "All Categories", which is a view with no grid of its own. This
    runs on every path - including the cached one, which is how Griffin Booth
    stayed invisible for a day after it was first fixed.
    """
    for loc in locations:
        loc.categories = [c for c in loc.categories if c.gid]
    _merge_catalogue(locations)
    return [loc for loc in locations if loc.categories]


async def fetch_locations(force: bool = False) -> list[Location]:
    """Every library and its categories: the homepage, plus the catalogue."""
    if not force:
        cached = storage.cache_get("libcal_locations", max_age_hours=24)
        if cached:
            return _normalise([
                Location(name=loc["name"],
                         categories=[Category(**c) for c in loc["categories"]])
                for loc in cached
            ])
    async with _client() as client:
        resp = await client.get("/")
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    locations: list[Location] = []
    for panel in soup.select(".s-lc-box, .panel"):
        heading = panel.select_one(".s-lc-box-title, .panel-heading")
        if not heading:
            continue
        name = heading.get_text(strip=True)
        cats: list[Category] = []
        for a in panel.select("a[href]"):
            href = a["href"]
            if "/spaces" not in href and "/space/" not in href and "/reserve" not in href:
                continue
            query = parse_qs(urlparse(href).query)
            try:
                lid = int(query.get("lid", ["0"])[0])
                gid = int(query.get("gid", ["0"])[0])
            except ValueError:
                continue
            label = a.get_text(strip=True)
            if not label or lid == 0 or gid == 0:
                # lid=0/gid=0 is the "All Categories" view, which has no grid
                # of its own. Links without ids at all (/space/NNN,
                # /reserve/collab) are picked up from the catalogue below.
                continue
            if any(c.lid == lid and c.gid == gid for c in cats):
                continue  # one <li> sometimes holds two <a>s to the same target
            cats.append(Category(label=label, lid=lid, gid=gid, url=href))
        if cats and "staff only" not in name.lower():
            locations.append(Location(name=name, categories=cats))
    locations = _normalise(locations)
    if locations:
        storage.cache_set(
            "libcal_locations",
            [{"name": l.name, "categories": [vars(c) for c in l.categories]} for l in locations],
        )
    return locations


def is_seat_category(lid: int, gid: int) -> bool:
    """Does this category book individual seats rather than whole spaces?

    The Humanities library works that way: asking for its grid normally
    answers with one item - the room itself, the /space/NNNNN the homepage
    links - and the four Study Pods or ten Window Seats only appear when the
    request says seat=1. Booking the room is not what anyone means, so these
    categories are always fetched seat-wise.
    """
    from . import catalog

    return bool(catalog.get(lid, gid).get("seats"))


async def fetch_grid(lid: int, gid: int, day: date,
                     seat: bool | None = None) -> dict[int, list[Cell]]:
    """15-minute cells per room (itemId) for one day, via the public AJAX grid."""
    if seat is None:
        seat = is_seat_category(lid, gid)
    data = {
        "lid": lid, "gid": gid, "eid": -1, "seat": 1 if seat else 0,
        "seatId": 0, "zone": 0,
        "start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(),
        "pageIndex": 0, "pageSize": 100 if seat else 18,
    }
    async with _client() as client:
        resp = await client.post(
            "/spaces/availability/grid",
            data=data,
            headers={
                "Referer": f"{BASE}/spaces?lid={lid}&gid={gid}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp.raise_for_status()
    payload = resp.json()
    rooms: dict[int, list[Cell]] = {}
    for slot in payload.get("slots", []):
        cls = slot.get("className", "")
        if "checkout" in cls:
            state = BOOKED
        elif "r-unavailable" in cls:
            state = BUFFER
        elif cls:
            state = UNAVAILABLE
        else:
            state = FREE
        cell = Cell(
            start=datetime.strptime(slot["start"], GRID_FMT),
            end=datetime.strptime(slot["end"], GRID_FMT),
            item_id=slot["itemId"],
            checksum=slot.get("checksum", ""),
            state=state,
        )
        rooms.setdefault(cell.item_id, []).append(cell)
    for cells in rooms.values():
        cells.sort(key=lambda c: c.start)
    return rooms


async def days_with_availability(lid: int, gid: int, days: int) -> dict[date, dict[int, list[Cell]]]:
    """Grids for today..today+days, keeping only days with at least one
    bookable start. Day-of categories (e.g. Arrakis) return zero slots for
    days whose booking window hasn't opened, so they drop out here."""
    import asyncio
    day_list = [date.today() + timedelta(days=d) for d in range(days)]
    grids = await asyncio.gather(*(fetch_grid(lid, gid, d) for d in day_list),
                                 return_exceptions=True)
    out: dict[date, dict[int, list[Cell]]] = {}
    for day, grid in zip(day_list, grids):
        if isinstance(grid, Exception):
            log.warning("grid fetch failed for %s: %s", day, grid)
            continue
        if any(bookable_starts(cells) for cells in grid.values()):
            out[day] = grid
    return out


def span_is_free(cells: list[Cell], start: datetime, end: datetime) -> bool:
    """Every cell from start to end is bookable (free, or a buffer we may
    book through). Ignores the per-booking length cap, so it also answers
    'could this whole stretch be covered by back-to-back bookings?'."""
    by_start = {c.start: c for c in cells}
    t = start
    while t < end:
        cell = by_start.get(t)
        if cell is None or cell.state not in (FREE, BUFFER):
            return False
        t = cell.end
    return True


def spaces_free_span(grid: dict[int, list[Cell]], start: datetime,
                     end: datetime) -> list[int]:
    """Spaces whose whole [start, end) stretch is bookable, cap ignored."""
    now = datetime.now()
    return [iid for iid, cells in grid.items()
            if start >= now and span_is_free(cells, start, end)]


def spaces_free_for(grid: dict[int, list[Cell]], start: datetime,
                    end: datetime) -> list[int]:
    """Item ids whose cells are bookable for the whole [start, end) period."""
    free = []
    for item_id, cells in grid.items():
        if (start in bookable_starts(cells, step=15)
                and end in valid_ends(cells, start)):
            free.append(item_id)
    return free


def category_limits(lid: int, gid: int) -> tuple[int, int]:
    """(min, max) booking minutes for a category, from probed/observed data
    (the add-to-cart response updates these on every booking)."""
    limits = storage.durable_get("category_limits", {}) or {}
    entry = limits.get(f"{lid}_{gid}", {})
    return int(entry.get("min", 30)), int(entry.get("max", config.MAX_BOOKING_MINUTES))


def room_name(item_id: int) -> str:
    names = storage.durable_get("libcal_room_names", {}) or {}
    return names.get(str(item_id), f"Room {item_id}")


def remember_room_names(mapping: dict[int, str]) -> None:
    names = storage.durable_get("libcal_room_names", {}) or {}
    names.update({str(k): v for k, v in mapping.items()})
    storage.durable_set("libcal_room_names", names)


# --- Slot maths -----------------------------------------------------------

def bookable_starts(cells: list[Cell], now: datetime | None = None,
                    step: int = 30) -> list[datetime]:
    """Starts (on `step`-minute boundaries) with >=30 bookable minutes after."""
    now = now or datetime.now()
    by_start = {c.start: c for c in cells}
    starts = []
    for c in cells:
        if c.state != FREE or c.start < now or c.start.minute % step:
            continue
        nxt = by_start.get(c.start + timedelta(minutes=15))
        if nxt and nxt.state in (FREE, BUFFER):
            starts.append(c.start)
    return starts


def valid_ends(cells: list[Cell], start: datetime) -> list[datetime]:
    """End times reachable from `start`.

    Walk forward through cells; FREE cells extend normally. BUFFER cells can
    be booked *through* (they only exist because of a later booking), but a
    BOOKED/UNAVAILABLE cell stops the walk. Ends every 30 min, capped by
    MAX_BOOKING_MINUTES.
    """
    by_start = {c.start: c for c in cells}
    ends = []
    t = start
    limit = start + timedelta(minutes=config.MAX_BOOKING_MINUTES)
    while t < limit:
        cell = by_start.get(t)
        if cell is None or cell.state not in (FREE, BUFFER):
            break
        t = cell.end
        if (t - start) >= timedelta(minutes=30):
            ends.append(t)  # every 15-min boundary; callers filter for display
    return ends


def cells_between(cells: list[Cell], start: datetime, end: datetime) -> list[Cell]:
    return [c for c in cells if start <= c.start < end]


# --- Check-in / cancel (plain HTTP, no login needed) ----------------------

async def checkin(email: str, code: str) -> tuple[bool, str]:
    async with _client() as client:
        resp = await client.post(
            "/r/checkin",
            data={"email": email, "code": code.strip().upper(), "latitude": "", "longitude": ""},
            headers={"Referer": f"{BASE}/r/checkin", "X-Requested-With": "XMLHttpRequest"},
        )
    return _ajax_outcome(resp)


async def checkout(email: str, code: str) -> tuple[bool, str]:
    """End a booking you're currently checked in to, freeing the space.

    POST /r/checkout, the mirror of check-in. This is the only self-service
    way to release a booking without the emailed cancellation link - and it
    only applies once the booking has started.
    """
    async with _client() as client:
        resp = await client.post(
            "/r/checkout",
            data={"email": email, "code": code.strip().upper(),
                  "latitude": "", "longitude": ""},
            headers={"Referer": f"{BASE}/r/checkout",
                     "X-Requested-With": "XMLHttpRequest"},
        )
    return _ajax_outcome(resp)


# --- What a check-in code can tell us about its booking ------------------
#
# The check-in endpoint is chatty in a useful way:
#   too early  -> 400 "Unable to Check In for this booking until 10:25am
#                 (booking starts at 10:30am)."  - and it does NOT check in
#   wrong code -> 400 "Unable to find booking matching code"
#   in window  -> 200 JSON whose `html` describes the booking, and you are
#                 now checked in
# So a code alone is enough to learn when a booking starts, which lets the
# bot track bookings made on the website without being told the time.

# The refusal before a check-in window opens names the whole moment:
# "booking starts at 12:30pm Wednesday, September 9, 2026". Reading only the
# clock and assuming today put every future booking on the wrong day.
_STARTS_AT_RE = re.compile(
    r"booking starts at\s*(\d{1,2}:\d{2}\s*[ap]m)"
    r"(?:\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s*"
    r"([a-z]+\s+\d{1,2},?\s*\d{4}))?", re.I)
_UNTIL_RE = re.compile(r"until\s*(\d{1,2}:\d{2}\s*[ap]m)", re.I)
_TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[ap]m)", re.I)
_UNKNOWN_CODE_RE = re.compile(r"find booking matching code|invalid code", re.I)
_ALREADY_RE = re.compile(r"already been checked in", re.I)


def _clock(text: str) -> time | None:
    try:
        return datetime.strptime(text.replace(" ", "").lower(), "%I:%M%p").time()
    except ValueError:
        return None


def _calendar_date(text: str | None) -> date | None:
    """'September 9, 2026' -> a real date. None when the site did not say."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text.replace(",", "")).strip()
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


async def confirm_booking(lid: int, gid: int, item_id: int,
                          start: datetime, end: datetime) -> str:
    """Is this booking still on the site? 'held', 'gone' or 'unknown'.

    There is no page on this LibCal that lists a person's bookings - twenty
    endpoints were probed and every one either 404s or is the ordinary
    availability grid, and the grid's slots carry no owner. What the grid
    does say, reliably, is whether that desk is taken at that time. If the
    slot we believe we hold is FREE, the booking is definitely gone -
    cancelled on the website, or never made. That is the case worth
    catching, because it is the one where our own record lies.

    'held' therefore means "someone has it, consistent with our record", not
    "proved yours". Only the check-in code proves ownership.
    """
    if not (lid and gid and item_id):
        return "unknown"
    try:
        grid = await fetch_grid(lid, gid, start.date())
    except Exception:
        return "unknown"                    # site down: never contradict on a guess
    cells = grid.get(int(item_id))
    if not cells:
        return "unknown"
    covering = [c for c in cells if c.start < end and c.end > start]
    if not covering:
        return "unknown"                    # outside the published day
    if all(c.state == FREE for c in covering):
        return "gone"
    return "held"


async def probe_code(email: str, code: str) -> dict:
    """Ask the site what this code belongs to.

    Returns {known, checked_in, start, end, space, message}. `start` is a
    datetime today (the site only ever talks about the current day's
    bookings). Nothing is guessed: a field stays None when the site did not
    say it.
    """
    out = {"known": False, "checked_in": False, "finished": False,
           "start": None, "end": None, "space": None, "location": None,
           "checked_in_at": None, "message": ""}
    async with _client() as client:
        resp = await client.post(
            "/r/checkin",
            data={"email": email, "code": code.strip().upper(),
                  "latitude": "", "longitude": ""},
            headers={"Referer": f"{BASE}/r/checkin",
                     "X-Requested-With": "XMLHttpRequest"})

    body = resp.text or ""
    details = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            details = str(payload.get("html") or "")
            body = details or body
    except ValueError:
        pass
    text = re.sub(r"\s+", " ", BeautifulSoup(body, "html.parser").get_text(" ")).strip()
    out["message"] = text[:400]

    if _UNKNOWN_CODE_RE.search(text) or re.search(r"invalid value", text, re.I):
        # "Invalid value." is what an empty or malformed code gets. Treating
        # that as a known booking made a typo look like a real reservation.
        return out                                  # code means nothing here
    out["known"] = True
    # "This booking has already been Checked Out" - a real code, but the
    # session is over. Worth saying plainly instead of retrying forever.
    out["finished"] = bool(re.search(r"already been Checked ?Out", text, re.I))

    if resp.status_code < 400:
        out["checked_in"] = True
    if _ALREADY_RE.search(text):
        out["checked_in"] = True

    today = date.today()

    # A successful check-in answers with labelled fields:
    #   Check In time: 12:41pm  Name / Email: …  Location: Lee Wee Nam Library
    #   Space: LIBLWNL-AK-08    Start Time: 12:45pm   Check Out time: 2:15pm
    # Read them by name. Guessing "the first two clock times on the page"
    # picked up the check-in clock and called it the booking's start.
    def field(label):
        m = re.search(
            label + r"\s*:\s*(.{1,60}?)(?=\s*(?:Check In time|Check Out time|"
            r"Name / Email|Location|Space|Start Time|End Time|Ready|$))",
            text, re.I)
        return m.group(1).strip(" .:|") if m else None

    out["space"] = field("Space") or out["space"]
    out["location"] = field("Location")

    def clock_in(raw):
        if not raw:
            return None
        m = _TIME_RE.search(raw)
        t = _clock(m.group(1)) if m else None
        return datetime.combine(today, t) if t else None

    out["checked_in_at"] = clock_in(field("Check In time"))
    out["start"] = clock_in(field("Start Time"))
    out["end"] = clock_in(field("Check Out time")) or clock_in(field("End Time"))

    # Not checked in yet: the refusal names the start - "(booking starts at
    # 10:30am)" - which is all we need to file the booking.
    if out["start"] is None:
        m = _STARTS_AT_RE.search(text)
        if m:
            t = _clock(m.group(1))
            if t:
                # The day the site named, not the day we happen to be asking.
                out["start"] = datetime.combine(
                    _calendar_date(m.group(2)) or today, t)

    if out["space"] is None and details:
        for line in (l.strip() for l in
                     BeautifulSoup(details, "html.parser").get_text("\n").split("\n")):
            if line and not _TIME_RE.search(line) and len(line) < 60:
                out["space"] = line
                break
    return out


async def cancel_via_link(link: str) -> tuple[bool, str]:
    """Follow a cancellation link from the confirmation email and confirm it."""
    if not link.startswith(BASE):
        return False, "That link does not point to libcalendar.ntu.edu.sg."
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (ScheduleMatcher bot)"}) as client:
        resp = await client.get(link)
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form")
        if form is None:
            text = _page_text(soup)
            done = any(w in text.lower() for w in ("cancelled", "canceled"))
            return done, text[:400] or "No cancellation form found on that page."
        payload = {
            inp.get("name"): inp.get("value", "")
            for inp in form.find_all("input") if inp.get("name")
        }
        action = form.get("action") or link
        if action.startswith("/"):
            action = BASE + action
        resp = await client.post(action, data=payload,
                                 headers={"Referer": link, "X-Requested-With": "XMLHttpRequest"})
    return _ajax_outcome(resp)


def _ajax_outcome(resp: httpx.Response) -> tuple[bool, str]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            html = data.get("html") or data.get("message") or str(data)
            text = BeautifulSoup(str(html), "html.parser").get_text(" ", strip=True)
            ok = resp.status_code < 400 and not data.get("error")
            return ok, text[:400]
    except ValueError:
        pass
    text = _page_text(BeautifulSoup(resp.text, "html.parser"))
    return resp.status_code < 400, text[:400]


def _page_text(soup: BeautifulSoup) -> str:
    main = soup.select_one("#s-lc-public-main, .container, body") or soup
    return re.sub(r"\s+", " ", main.get_text(" ", strip=True))


# --- Covering a long window with several spaces --------------------------
#
# A session longer than one booking allows does not need one desk to be free
# for all of it. Taking what is free on one table and moving to another is
# what people do by hand; these two functions work out where to move and when.

def free_runs(cells: list[Cell]) -> list[tuple[datetime, datetime]]:
    """Contiguous stretches this space can be booked for.

    BUFFER counts as bookable: the 45 minutes before someone else's booking
    block a start, but a booking may still END inside them.
    """
    runs: list[tuple[datetime, datetime]] = []
    current: list[datetime] | None = None
    for cell in sorted(cells, key=lambda c: c.start):
        if cell.state in (FREE, BUFFER):
            current = [cell.start, cell.end] if current is None else [current[0], cell.end]
        elif current is not None:
            runs.append((current[0], current[1]))
            current = None
    if current is not None:
        runs.append((current[0], current[1]))
    return runs


def cover_span(grid: dict[int, list[Cell]], start: datetime, end: datetime,
               cap_minutes: int) -> list[tuple[int, datetime, datetime]] | None:
    """Tile [start, end) with as few bookings as possible, hopping desks.

    Greedy: at each point take the space whose free run reaches furthest,
    limited by the category's per-booking cap. Reaching furthest each time
    gives the fewest segments, so a window needs only
    ceil(duration / cap) moves when the grid allows it.

    Returns [(item_id, from, to), …] in order, or None if some moment in the
    window is free nowhere - better to say so than to book half a session.
    """
    runs = {iid: free_runs(cells) for iid, cells in grid.items()}
    plan: list[tuple[int, datetime, datetime]] = []
    used: list[int] = []
    moment = start
    while moment < end:
        best: tuple[int, datetime, datetime] | None = None
        for iid in sorted(runs):
            # The site refuses two back-to-back bookings of the same facility,
            # so the desk we are on cannot also take the next segment.
            if plan and iid == plan[-1][0]:
                continue
            for run_start, run_end in runs[iid]:
                if run_start <= moment < run_end:
                    reach = min(run_end, end, moment + timedelta(minutes=cap_minutes))
                    if reach <= moment:
                        continue
                    # Ties: prefer a desk already used (fewer strange seats),
                    # then the lowest id, so the answer never depends on
                    # dictionary order.
                    better = (best is None or reach > best[2]
                              or (reach == best[2] and iid in used
                                  and best[0] not in used))
                    if better:
                        best = (iid, moment, reach)
        if best is None:
            return None
        plan.append(best)
        used.append(best[0])
        moment = best[2]
    return plan
