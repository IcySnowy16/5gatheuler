"""Choping: holding a space without booking it.

Verified mechanics on libcalendar.ntu.edu.sg:

* Adding a slot to the cart alone does NOT block anyone - the public grid
  still shows it free.
* Reaching the CHECKOUT page does: the grid flips to `s-lc-eq-checkout` and
  the page says "held for you until <time>" - about 10 minutes.
* The hold survives closing the browser, but you cannot get back into that
  checkout page afterwards (its LibAuth token is single-use, and re-posting
  the times request is refused because the site now sees the slot as taken
  by our own hold). So a hold must live in a browser session we keep open.
* Pressing "Remove" on the checkout page releases the slot immediately, and
  re-adding straight away gives a fresh 10-minute window. That is how a hold
  is renewed - with a sub-second gap where someone else could take it.

Each hold therefore owns a headless browser parked on its checkout page,
which is also the page that later books it (Continue -> tick -> Submit).
Async Playwright is used here so the sessions live in the bot's event loop;
one-shot bookings elsewhere use the sync API in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from .. import config, storage, tasks
from . import libcal
from . import browser as browser_mod
from .browser import BASE, SEL, UA, _state_path

log = logging.getLogger(__name__)

_pw = None            # async playwright driver, started on first use
_holds: dict[int, "Hold"] = {}
_next_id = 1

_AJAX_JS = """async (args) => {
    const r = await fetch(args.url, {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                 'X-Requested-With':'XMLHttpRequest'},
        body: args.body, credentials:'same-origin'});
    return JSON.stringify({status: r.status, text: await r.text()});
}"""


class HoldError(Exception):
    pass


@dataclass
class Hold:
    id: int
    user_id: int
    lid: int
    gid: int
    item_id: int
    start: datetime
    end: datetime
    label: str
    location: str
    category: str
    expires_at: datetime
    created_at: datetime = field(default_factory=datetime.now)
    last_hold_at: datetime = field(default_factory=datetime.now)
    renewals: int = 0
    row_id: int | None = None      # the SQLite row, so a restart can recover it
    auto_renew: bool = True
    # Minutes this hold may keep being re-taken. 0 means "no limit". Starts at
    # the configured default and grows when you extend from /holds.
    budget_minutes: int = 0
    note: str = ""          # e.g. "leg 2 of 3" for /extendedbooking
    _browser: object = None
    _context: object = None
    _page: object = None
    # Serialises renew/release/book. Without it, a renewal interrupted
    # between "Remove" and re-taking leaves a hold orphaned on the server
    # (it lapses by itself, but blocks the slot for minutes meanwhile).
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def held_minutes(self) -> float:
        return (datetime.now() - self.created_at).total_seconds() / 60

    def describe(self) -> str:
        extra = f" [{self.note}]" if self.note else ""
        if self.budget_minutes:
            left = max(0, self.budget_minutes - self.held_minutes)
            budget = f"stops in {int(left)} min unless extended"
        else:
            budget = "holding until you say otherwise"
        return (f"#{self.id} {self.label} {self.start:%a %d %b %H:%M}-{self.end:%H:%M}"
                f"{extra} - {budget}")


async def _driver():
    global _pw
    if _pw is None:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
    return _pw


async def _goto(page, url: str, tries: int = 3) -> None:
    """Navigate, tolerating races with a navigation the page started itself
    (clicking Remove reloads the page, which aborts an immediate goto)."""
    for attempt in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(1.5)


async def _ajax(page, url: str, fields: dict) -> tuple[int, str]:
    raw = await page.evaluate(_AJAX_JS, {"url": url, "body": urlencode(fields)})
    data = json.loads(raw)
    return data["status"], data["text"]


async def _login_if_needed(page, username: str, password: str) -> bool:
    try:
        pw_box = page.locator("input[type='password']").first
        await pw_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False
    user_box = page.locator(
        "#userNameInput, input[type='text']:visible, input[type='email']:visible, "
        "input[name*='user' i]:visible, input[id*='user' i]:visible").first
    await user_box.fill(username)
    await pw_box.fill(password)
    kmsi = page.locator("#kmsiInput")
    try:
        if await kmsi.count() and not await kmsi.first.is_checked():
            await kmsi.first.check()
    except Exception:
        pass
    try:
        await page.locator(
            "#submitButton, button[type='submit'], input[type='submit']").first.click(timeout=4000)
    except Exception:
        await pw_box.press("Enter")
    await page.wait_for_load_state("networkidle", timeout=30000)
    return True


def _parse_expiry(body: str) -> datetime:
    """'held for you until 12:56pm Thursday, August 27, 2026' -> datetime."""
    m = re.search(r"held for you until\s+(\d{1,2}:\d{2}\s*[ap]m)", body, re.I)
    if not m:
        return datetime.now() + timedelta(minutes=10)
    try:
        t = datetime.strptime(m.group(1).replace(" ", "").lower(), "%I:%M%p").time()
    except ValueError:
        return datetime.now() + timedelta(minutes=10)
    when = datetime.combine(date.today(), t)
    if when < datetime.now() - timedelta(hours=1):
        when += timedelta(days=1)      # crossed midnight
    return when


async def _reach_checkout(page, lid: int, gid: int, item_id: int,
                          start: datetime, end: datetime, checksum: str) -> str:
    """add -> (adjust end) -> submit times -> checkout page. Returns body text."""
    day = start.date()
    status, text = await _ajax(page, "/spaces/availability/booking/add", {
        "add[eid]": item_id, "add[gid]": gid, "add[lid]": lid,
        "add[start]": f"{start:%Y-%m-%d %H:%M}", "add[checksum]": checksum,
        "lid": lid, "gid": gid, "start": f"{day}", "end": f"{day + timedelta(days=1)}"})
    try:
        cart = json.loads(text)["bookings"][0]
    except Exception:
        raise HoldError(re.sub(r"<[^>]+>", " ", text)[:200] or f"add failed ({status})")

    end_str = f"{end:%Y-%m-%d %H:%M:%S}"
    if end_str != cart["end"]:
        options = cart.get("options", [])
        if end_str not in options:
            raise HoldError("The site only allows this to end at: "
                            + ", ".join(o[11:16] for o in options))
        idx = options.index(end_str)
        status, text = await _ajax(page, "/spaces/availability/booking/add", {
            "update[id]": cart["id"], "update[checksum]": cart["optionChecksums"][idx],
            "update[end]": end_str,
            "lid": lid, "gid": gid, "start": f"{day}",
            "end": f"{day + timedelta(days=1)}",
            "bookings[0][id]": cart["id"], "bookings[0][eid]": item_id,
            "bookings[0][seat_id]": 0, "bookings[0][gid]": gid, "bookings[0][lid]": lid,
            "bookings[0][start]": f"{start:%Y-%m-%d %H:%M}",
            "bookings[0][end]": cart["end"][:16],
            "bookings[0][checksum]": cart["checksum"]})
        try:
            cart = json.loads(text)["bookings"][0]
        except Exception:
            raise HoldError(re.sub(r"<[^>]+>", " ", text)[:200]
                            or f"end update failed ({status})")

    status, text = await _ajax(page, "/ajax/space/times", {
        "patron": "", "patronHash": "", "returnUrl": f"/spaces?lid={lid}&gid={gid}",
        "bookings[0][id]": cart["id"], "bookings[0][eid]": item_id,
        "bookings[0][seat_id]": 0, "bookings[0][gid]": gid, "bookings[0][lid]": lid,
        "bookings[0][start]": f"{start:%Y-%m-%d %H:%M}",
        "bookings[0][end]": f"{end:%Y-%m-%d %H:%M}",
        "bookings[0][checksum]": cart["checksum"], "method": 11})
    redirect = None
    try:
        redirect = json.loads(text).get("redirect")
    except Exception:
        pass
    if not redirect:
        raise HoldError(re.sub(r"<[^>]+>", " ", text)[:200] or f"times failed ({status})")
    await _goto(page, BASE + redirect if redirect.startswith("/") else redirect)
    await page.wait_for_load_state("networkidle", timeout=30000)
    body = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
    if "Booking Details" not in body:
        raise HoldError(body[:200] or "checkout page did not load")
    return body


async def create(user_id: int, username: str, password: str, lid: int, gid: int,
                 item_id: int, start: datetime, end: datetime, checksum: str,
                 location: str, category: str, note: str = "") -> Hold:
    """Hold one slot: park a live session on its checkout page."""
    global _next_id
    mine = [h for h in _holds.values() if h.user_id == user_id]
    if len(mine) >= config.MAX_HOLDS:
        raise HoldError(f"You already hold {len(mine)} slots "
                        f"(limit {config.MAX_HOLDS}). Release one first: /holds")
    # Each hold is a live headless browser (~270 MB). Refusing here is far
    # better than launching one on a machine with no room and having Windows
    # kill the bot - which would drop every other hold with it.
    free = config.free_ram_mb()
    if free is not None and free < config.MIN_FREE_RAM_MB:
        raise HoldError(
            f"This machine only has {free} MB of memory free, and a hold needs "
            f"about 270 MB. Close something, or release a hold: /holds")
    pw = await _driver()
    browser = await pw.chromium.launch(headless=not config.HEADFUL,
                                   args=browser_mod.LAUNCH_ARGS)
    state = _state_path(user_id)
    context = await browser.new_context(
        storage_state=str(state) if state.exists() else None, user_agent=UA)
    page = await context.new_page()
    try:
        await _goto(page, f"{BASE}/spaces?lid={lid}&gid={gid}&date={start:%Y-%m-%d}")
        await _login_if_needed(page, username, password)
        await page.wait_for_load_state("networkidle", timeout=30000)
        body = await _reach_checkout(page, lid, gid, item_id, start, end, checksum)
        await context.storage_state(path=str(state))
        label = libcal.room_name(item_id)
        row_id = storage.add_hold(user_id, lid, gid, item_id, location, category,
                                  label, start, end, note)
        hold = Hold(id=_next_id, user_id=user_id, lid=lid, gid=gid, item_id=item_id,
                    start=start, end=end, label=label,
                    location=location, category=category,
                    expires_at=_parse_expiry(body), note=note, row_id=row_id,
                    budget_minutes=storage.hold_budget(user_id),
                    _browser=browser, _context=context, _page=page)
        _holds[_next_id] = hold
        _next_id += 1
        log.info("hold #%s created: %s %s-%s until %s", hold.id, hold.label,
                 f"{start:%H:%M}", f"{end:%H:%M}", f"{hold.expires_at:%H:%M}")
        return hold
    except Exception:
        await _shutdown(browser, context)
        raise


async def _shutdown(browser, context) -> None:
    for closer in (context, browser):
        try:
            await closer.close()
        except Exception:
            pass


async def _remove_pending(hold: Hold) -> bool:
    """Click Remove on the checkout page. Caller must hold the lock."""
    try:
        page = hold._page
        for sel in ("button:has-text('Remove')", "a:has-text('Remove')"):
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_timeout(600)   # let its own reload settle
                return True
    except Exception:
        log.warning("hold #%s release failed", hold.id, exc_info=True)
    return False


async def release(hold: Hold, keep_open: bool = False) -> bool:
    """Give the slot back (Remove on the checkout page)."""
    async with hold._lock:
        ok = await _remove_pending(hold)
        if not keep_open:
            _holds.pop(hold.id, None)
            if hold.row_id:
                storage.close_hold(hold.row_id, "released")
            await _shutdown(hold._browser, hold._context)
        return ok


async def renew(hold: Hold) -> bool:
    """Release and immediately re-take the slot, for a fresh window."""
    async with hold._lock:
        return await _renew_locked(hold)


async def _renew_locked(hold: Hold) -> bool:
    try:
        await _remove_pending(hold)
        page = hold._page
        await _goto(page, f"{BASE}/spaces?lid={hold.lid}&gid={hold.gid}"
                          f"&date={hold.start:%Y-%m-%d}")
        await page.wait_for_load_state("networkidle", timeout=30000)
        grid = await libcal.fetch_grid(hold.lid, hold.gid, hold.start.date())
        checksum = next((c.checksum for c in grid.get(hold.item_id, [])
                         if c.start == hold.start and c.state == libcal.FREE), None)
        if checksum is None:
            raise HoldError("someone took the slot during the renewal gap")
        body = await _reach_checkout(page, hold.lid, hold.gid, hold.item_id,
                                     hold.start, hold.end, checksum)
        hold.expires_at = _parse_expiry(body)
        hold.last_hold_at = datetime.now()
        hold.renewals += 1
        if hold.row_id:
            storage.touch_hold(hold.row_id, hold.renewals)
        log.info("hold #%s renewed until %s", hold.id, f"{hold.expires_at:%H:%M}")
        return True
    except Exception as e:
        log.warning("hold #%s renew failed: %s", hold.id, e)
        _holds.pop(hold.id, None)
        if hold.row_id:
            storage.close_hold(hold.row_id, "lost")
        await _shutdown(hold._browser, hold._context)
        return False


async def book(hold: Hold) -> tuple[bool, str]:
    """Turn a hold into a real booking: Continue -> tick -> Submit."""
    async with hold._lock:
        return await _book_locked(hold)


async def _book_locked(hold: Hold) -> tuple[bool, str]:
    page = hold._page
    try:
        cont = page.locator("#terms_accept")
        if await cont.count() and await cont.first.is_visible():
            await cont.first.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(400)
        boxes = page.locator("input[type='checkbox']:visible")
        for i in range(await boxes.count()):
            box = boxes.nth(i)
            try:
                if not await box.is_checked():
                    await box.check()
            except Exception:
                pass
        submit = page.locator("#btn-form-submit")
        if not await submit.count():
            submit = page.get_by_role("button", name=SEL["form_submit"])
        if not await submit.count():
            raise HoldError("couldn't find the Submit my Booking button")
        await submit.first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        body = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
        from .browser import parse_confirmation
        ok, confirmed_email = parse_confirmation(body)
        if ok:
            if confirmed_email:
                storage.save_user(hold.user_id, email=confirmed_email)
            storage.add_booking(hold.user_id, hold.location, hold.category, hold.label,
                                hold.item_id, hold.start, hold.end,
                                lid=hold.lid, gid=hold.gid)
            if hold.row_id:
                storage.close_hold(hold.row_id, "booked")
        return ok, body[:400]
    except Exception as e:
        return False, f"Booking the held slot failed: {e}"
    finally:
        _holds.pop(hold.id, None)
        await _shutdown(hold._browser, hold._context)


def get(hold_id: int) -> Hold | None:
    return _holds.get(hold_id)


def for_user(user_id: int) -> list[Hold]:
    return sorted((h for h in _holds.values() if h.user_id == user_id),
                  key=lambda h: h.start)


async def _still_held(hold: Hold) -> bool:
    """Ask the PUBLIC grid whether the slot is still ours.

    The checkout page's "held until" is not reliable - observed windows have
    ranged from ~4 to ~10 minutes - so the grid is the source of truth.
    """
    try:
        grid = await libcal.fetch_grid(hold.lid, hold.gid, hold.start.date())
        state = next((c.state for c in grid.get(hold.item_id, [])
                      if c.start == hold.start), None)
        return state is not None and state != libcal.FREE
    except Exception:
        return True     # network wobble: assume still held, check again next tick


async def watcher(application) -> None:
    """Keep holds alive.

    Every HOLD_POLL_SECONDS the public grid is checked (the page's stated
    expiry proved unreliable). A hold is re-taken every
    HOLD_RENEW_AFTER_SECONDS regardless, so it never actually lapses; if one
    lapses early anyway, the grid check catches it within one poll.
    """
    while True:
        try:
            for hold in list(_holds.values()):
                capped = (hold.budget_minutes
                          and hold.held_minutes >= hold.budget_minutes)
                age = (datetime.now() - hold.last_hold_at).total_seconds()
                lapsed = not await _still_held(hold)

                if capped or not hold.auto_renew:
                    if lapsed or age >= config.HOLD_RENEW_AFTER_SECONDS:
                        await release(hold)
                        why = (f"I have been holding it {int(hold.held_minutes)} "
                               f"min, my own limit" if capped
                               else "auto-renew is off")
                        from telegram import (InlineKeyboardButton,
                                              InlineKeyboardMarkup)
                        again = InlineKeyboardMarkup([[InlineKeyboardButton(
                            "Hold it again", callback_data=f"bk|hagain|{hold.row_id}")]]
                        ) if hold.row_id else None
                        await application.bot.send_message(
                            hold.user_id,
                            f"Hold #{hold.id} ({hold.label} "
                            f"{hold.start:%H:%M}-{hold.end:%H:%M}) released - {why}. "
                            "Tap below to take it again, or raise "
                            "HOLD_MAX_MINUTES (0 = no limit) so this never "
                            "interrupts you.", reply_markup=again)
                    continue

                if not lapsed and age < config.HOLD_RENEW_AFTER_SECONDS:
                    continue

                if lapsed:
                    log.info("hold #%s lapsed after %.1f min - re-taking",
                             hold.id, age / 60)
                if not await renew(hold):
                    await application.bot.send_message(
                        hold.user_id,
                        f"Hold #{hold.id} ({hold.label} "
                        f"{hold.start:%H:%M}-{hold.end:%H:%M}) is gone - someone "
                        "else took the slot while I was re-taking it.")
        except Exception:
            log.exception("hold watcher tick failed")
        await asyncio.sleep(config.HOLD_POLL_SECONDS)


async def restore(application) -> None:
    """Re-take holds that a restart dropped.

    A hold cannot be resumed: its checkout page is single-use and the site
    refuses to re-issue one while it still sees its own hold. So recovery
    means grabbing the slot again from scratch. The dead session's hold only
    lapses after ~5.5 minutes, so the attempt is retried for a little longer
    than that before giving up - and the user is always told either way.
    """
    rows = storage.live_holds()
    if not rows:
        return
    log.info("recovering %d hold(s) after restart", len(rows))
    for row in rows:
        storage.close_hold(row["id"], "recovering")
        tasks.spawn(_recover_one(application, dict(row)),
                    bot=application.bot, user_id=row["user_id"],
                    feature="taking a hold back after the restart")


async def _recover_one(application, row: dict) -> None:
    profile = None
    try:
        from . import handlers as h
        profile = h._profile(row["user_id"])
    except Exception:
        pass
    label = row.get("label") or libcal.room_name(row["item_id"])
    start = datetime.strptime(row["start_ts"], storage.FMT)
    end = datetime.strptime(row["end_ts"], storage.FMT)
    if not profile:
        await application.bot.send_message(
            row["user_id"],
            f"I lost the hold on {label} {start:%a %d %b %H:%M}-{end:%H:%M} when I "
            "restarted and cannot retake it - no saved credentials (/setup).")
        return

    deadline = datetime.now() + timedelta(minutes=8)
    while datetime.now() < deadline:
        try:
            grid = await libcal.fetch_grid(row["lid"], row["gid"], start.date())
            checksum = next((c.checksum for c in grid.get(row["item_id"], [])
                             if c.start == start and c.state == libcal.FREE), None)
            if checksum:
                hold = await create(row["user_id"], profile["username"],
                                    profile["password"], row["lid"], row["gid"],
                                    row["item_id"], start, end, checksum,
                                    row.get("location") or "", row.get("category") or "",
                                    note=row.get("note") or "")
                await application.bot.send_message(
                    row["user_id"],
                    f"Took back your hold on {label} "
                    f"{start:%a %d %b %H:%M}-{end:%H:%M} after the restart "
                    f"(hold #{hold.id}).")
                return
        except Exception as e:
            log.warning("hold recovery attempt failed: %s", e)
        await asyncio.sleep(30)

    await application.bot.send_message(
        row["user_id"],
        f"I could not get your hold on {label} {start:%a %d %b %H:%M}-{end:%H:%M} "
        "back after the restart - someone else has the slot. Nothing you had "
        "already BOOKED is affected.")


def extend(hold: Hold, minutes: int | None = None) -> str:
    """Give a live hold more time before the bot stops re-taking it."""
    if minutes is None:
        minutes = storage.hold_budget(hold.user_id) or 60
    if minutes <= 0:
        hold.budget_minutes = 0
        return "will keep holding until you book or release it"
    hold.budget_minutes = int(hold.held_minutes) + minutes
    return f"will keep holding for another {minutes} min"


async def reacquire(user_id: int, row_id: int, username: str, password: str,
                    attempts: int = 3) -> tuple["Hold | None", str]:
    """Take a released slot back, if it is still free.

    A lapsed hold cannot be resumed - the checkout page is single-use - so
    this grabs the slot again from scratch.
    """
    row = storage.get_hold(row_id)
    if not row:
        return None, "I no longer have a record of that hold."
    start = datetime.strptime(row["start_ts"], storage.FMT)
    end = datetime.strptime(row["end_ts"], storage.FMT)
    if end <= datetime.now():
        return None, "that booking slot has already passed."
    for attempt in range(attempts):
        try:
            grid = await libcal.fetch_grid(row["lid"], row["gid"], start.date())
            checksum = next((c.checksum for c in grid.get(row["item_id"], [])
                             if c.start == start and c.state == libcal.FREE), None)
            if checksum:
                hold = await create(user_id, username, password, row["lid"],
                                    row["gid"], row["item_id"], start, end,
                                    checksum, row["location"] or "",
                                    row["category"] or "", note=row["note"] or "")
                return hold, "held again"
        except HoldError as e:
            return None, str(e)
        except Exception as e:
            log.warning("reacquire failed: %s", e)
        if attempt < attempts - 1:
            await asyncio.sleep(20)
    return None, "someone else has the slot now."
