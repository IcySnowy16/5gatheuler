"""Drives a real browser through the LibCal booking flow.

Booking cannot be done with plain HTTP: opening any /spaces page bounces
through au.libauth.com to the NTU network login. So this module uses
Playwright (sync API — call it via asyncio.to_thread from the bot):

    spaces grid -> click start cell -> pick end time -> Submit Times
    -> NTU login (if session expired) -> terms checkbox -> booking form
    -> Submit my Booking -> confirmation

Selectors are collected in `SEL` below so a LibCal facelift only needs edits
in one place. On any failure a screenshot + HTML dump lands in
%LOCALAPPDATA%\\ScheduleMatcher\\debug\\ — send those when a step breaks.

Run `python -m schedule_matcher.booking.browser` for a visible browser with
your saved session, to watch or repair the flow manually.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

BASE = config.LIBCAL_BASE

# NTU's ADFS treats Chrome-on-Windows as capable of Kerberos (WIA) and traps
# a headless browser on an empty /adfs/ls/wia negotiation page forever. A
# non-WIA user agent makes it fall back to the normal password form.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) Gecko/20100101 Firefox/130.0"

SEL = {
    "grid_cell": "a[title]",                       # availability cells carry full-text titles
    "cart_region": "#s-lc-eq-my-bookings, .s-lc-eq-mb, form",
    "end_select": "select",
    "submit_times": re.compile(r"submit\s*times", re.I),
    "terms_continue": re.compile(r"continue|i\s*agree|accept|proceed", re.I),
    "form_submit": re.compile(r"submit\s*my\s*booking|submit\s*booking|^submit$", re.I),
    "error_box": ".alert-danger, .alert-warning, #s-lc-eq-errors",
    "resource_rows": "[data-resource-id]",
}


class BookingError(Exception):
    """The site refused the booking; .args[0] is the site's own message."""


class WindowNotOpenError(BookingError):
    """The category's booking window hasn't opened yet (day-of categories
    like Arrakis show 'This time slot is not open for booking right now.
    Please wait for the next available booking window.'). Scheduled jobs
    retry on this."""


_WINDOW_RE = re.compile(r"not open for booking|booking window|not.*open.*right now", re.I)


def _raise_site_error(text: str):
    if _WINDOW_RE.search(text):
        raise WindowNotOpenError(text)
    raise BookingError(text)


@dataclass
class BookingResult:
    ok: bool
    message: str
    reference: str | None = None
    email_used: str | None = None    # what the booking form's email field held
    window_not_open: bool = False    # category's booking window hasn't opened
    debug_files: list[str] = field(default_factory=list)


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()          # 9:00am / 1:30pm


def _fmt_title_day(dt: datetime) -> str:
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"      # Tuesday, August 25, 2026


# Chromium spends 9-50 s shutting down on Windows whatever we do; these cut
# it to about half that, and stop a headless browser doing background work we
# never asked for - which matters on a laptop running other things.
LAUNCH_ARGS = [
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-component-update",
    "--no-first-run",
]


def _state_path(user_id: int) -> Path:
    return config.HOME / f"pw_state_{user_id}.json"


def _dump(page, tag: str) -> list[str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    files = []
    try:
        png = config.DEBUG_DIR / f"{stamp}-{tag}.png"
        page.screenshot(path=str(png), full_page=True)
        files.append(str(png))
        html = config.DEBUG_DIR / f"{stamp}-{tag}.html"
        html.write_text(page.content(), encoding="utf-8")
        files.append(str(html))
    except Exception:
        log.exception("debug dump failed")
    config.trim_debug_dir()          # keep the newest few, not every failure
    return files


def _visible_error(page) -> str | None:
    try:
        box = page.locator(SEL["error_box"]).first
        if box.count() and box.is_visible():
            text = re.sub(r"\s+", " ", box.inner_text()).strip()
            if text:
                return text
    except Exception:
        pass
    return None


def _maybe_login(page, username: str, password: str, wait_ms: int = 12000) -> bool:
    """Fill the NTU network login if we landed on it. Returns True if we did.
    Handles both the libauth linker form and ADFS (#userNameInput etc.)."""
    try:
        pw = page.locator("input[type='password']").first
        pw.wait_for(state="visible", timeout=wait_ms)
    except Exception:
        return False
    log.info("Login page detected at %s", page.url)
    user_box = page.locator(
        "#userNameInput, input[type='text']:visible, input[type='email']:visible, "
        "input[name*='user' i]:visible, input[id*='user' i]:visible"
    ).first
    user_box.fill(username)
    pw.fill(password)
    kmsi = page.locator("#kmsiInput")   # ADFS "keep me signed in" -> longer sessions
    try:
        if kmsi.count() and kmsi.first.is_visible() and not kmsi.first.is_checked():
            kmsi.first.check()
    except Exception:
        pass
    try:
        page.locator(
            "#submitButton, button[type='submit'], input[type='submit']"
        ).first.click(timeout=4000)
    except Exception:
        pw.press("Enter")
    page.wait_for_load_state("networkidle", timeout=30000)
    if page.locator("input[type='password']").count() and _visible_error(page):
        raise BookingError(f"NTU login failed: {_visible_error(page)}")
    return True


def _harvest_room_names(page) -> dict[int, str]:
    """Space names from the grid's resource rows. The id attribute looks like
    data-resource-id="eid_46005" and the text like "InfoLIBLWNL-AK-03
    (Capacity 1)" (the 'Info' is an icon's accessible text)."""
    try:
        rows = page.eval_on_selector_all(
            SEL["resource_rows"],
            "els => els.map(e => [e.getAttribute('data-resource-id'), e.innerText.trim()])",
        )
        names: dict[int, str] = {}
        for rid, txt in rows:
            m = re.search(r"(\d+)$", rid or "")
            name = re.sub(r"^Info\s*", "", (txt or "").split("\n")[0]).strip()
            if m and name:
                names[int(m.group(1))] = name
        return names
    except Exception:
        return {}


def harvest_categories(user_id: int, username: str, password: str,
                       lid: int) -> dict[int, str]:
    """The library's own category list, read off its page.

    The homepage links some categories without ids in the URL - Griffin Booth
    is /reserve/collab, the Humanities ones are /space/NNNNN - so scraping it
    misses them entirely. This dropdown is the authoritative list.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not config.HEADFUL, args=LAUNCH_ARGS)
        state = _state_path(user_id)
        context = browser.new_context(
            storage_state=str(state) if state.exists() else None, user_agent=UA)
        page = context.new_page()
        try:
            page.goto(f"{BASE}/spaces?lid={lid}", wait_until="domcontentloaded",
                      timeout=45000)
            _maybe_login(page, username, password)
            page.wait_for_selector("select#gid", timeout=30000)
            opts = page.eval_on_selector_all(
                "select#gid option",
                "els => els.map(e => [e.innerText.trim(), e.value])")
            context.storage_state(path=str(state))
            return {int(v): t for t, v in opts
                    if v and v.isdigit() and int(v) > 0 and t}
        except Exception:
            log.exception("category list harvest failed for lid=%s", lid)
            _dump(page, f"cats-{lid}")
            return {}
        finally:
            context.close()
            browser.close()


def harvest_policy(user_id: int, username: str, password: str,
                   lid: int, gid: int) -> str:
    """The category's Policy blurb - notice period and length caps."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not config.HEADFUL, args=LAUNCH_ARGS)
        state = _state_path(user_id)
        context = browser.new_context(
            storage_state=str(state) if state.exists() else None, user_agent=UA)
        page = context.new_page()
        try:
            page.goto(f"{BASE}/spaces?lid={lid}&gid={gid}",
                      wait_until="domcontentloaded", timeout=45000)
            _maybe_login(page, username, password)
            page.wait_for_timeout(1200)
            text = re.sub(r"[ 	]+", " ", page.inner_text("body"))
            context.storage_state(path=str(state))
            return text
        except Exception:
            log.exception("policy read failed for lid=%s gid=%s", lid, gid)
            return ""
        finally:
            context.close()
            browser.close()


def harvest_names(user_id: int, username: str, password: str,
                  lid: int, gid: int, on_date: datetime | None = None) -> dict[int, str]:
    """Log in, open the category page, and read the real space names
    (e.g. 'LIBLWNL-AK-10 (Capacity 1)') off the availability grid rows."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not config.HEADFUL, args=LAUNCH_ARGS)
        state = _state_path(user_id)
        context = browser.new_context(storage_state=str(state) if state.exists() else None,
                                      user_agent=UA)
        page = context.new_page()
        try:
            url = f"{BASE}/spaces?lid={lid}&gid={gid}"
            if on_date:
                url += f"&date={on_date:%Y-%m-%d}"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _maybe_login(page, username, password)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_selector(SEL["resource_rows"], timeout=30000)
            names = _harvest_room_names(page)
            context.storage_state(path=str(state))
            return names
        except Exception:
            log.exception("Space-name harvest failed")
            _dump(page, "harvest")
            return {}
        finally:
            context.close()
            browser.close()


_AJAX_JS = """async (args) => {
    const r = await fetch(args.url, {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                 'X-Requested-With':'XMLHttpRequest'},
        body: args.body, credentials:'same-origin'});
    return JSON.stringify({status: r.status, text: await r.text()});
}"""


def _ajax(page, url: str, fields: dict) -> tuple[int, str]:
    from urllib.parse import urlencode
    raw = page.evaluate(_AJAX_JS, {"url": url, "body": urlencode(fields)})
    import json as _json
    data = _json.loads(raw)
    return data["status"], data["text"]


def accept_terms(page) -> None:
    """Open the booking form on the checkout page.

    On arrival only "Continue" (#terms_accept) is visible; the agreement
    checkbox and "Submit my Booking" (#btn-form-submit) are hidden until it
    is pressed. Order matters: Continue, then tick, then submit.
    """
    cont = page.locator("#terms_accept")
    if not cont.count():
        cont = page.get_by_role("button", name=SEL["terms_continue"])
    try:
        if cont.count() and cont.first.is_visible():
            cont.first.click()
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(400)
    except Exception:
        log.warning("terms Continue click failed", exc_info=True)
    boxes = page.locator("input[type='checkbox']:visible")
    for i in range(boxes.count()):
        box = boxes.nth(i)
        try:
            if not box.is_checked():
                box.check()
        except Exception:
            pass


# The real confirmation says "Booking Confirmed / Your booking has been
# submitted. You will receive an email confirmation at <address>." Matching
# anything looser (e.g. the word "booking") wrongly passed the CHECKOUT page,
# which is why the bot once reported success for a booking never submitted.
CONFIRMED_RE = re.compile(
    r"booking (?:has been submitted|is confirmed)|booking confirmed", re.I)
HOLD_RE = re.compile(r"held for you until", re.I)
CONFIRM_EMAIL_RE = re.compile(r"email confirmation at\s+([\w.+-]+@[\w.-]+)", re.I)


def parse_confirmation(body: str) -> tuple[bool, str | None]:
    """(booking really went through, address the confirmation was sent to)."""
    ok = bool(CONFIRMED_RE.search(body)) and not HOLD_RE.search(body)
    m = CONFIRM_EMAIL_RE.search(body)
    # The sentence ends in a full stop, which the address pattern swallows.
    return ok, m.group(1).rstrip(".") if m else None


def submit_booking_form(page) -> str:
    """Press 'Submit my Booking' and return the resulting page text."""
    submit = page.locator("#btn-form-submit")
    if not submit.count():
        submit = page.get_by_role("button", name=SEL["form_submit"])
    if submit.count():
        submit.first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
    return re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()


def _record_limits(lid: int, gid: int, start: datetime, options: list[str]) -> None:
    """The add response's end options are the site's own truth about limits;
    keep the recorded per-category max fresh with every booking."""
    try:
        from .. import storage
        ends = [datetime.strptime(o, "%Y-%m-%d %H:%M:%S") for o in options]
        mins = [(e - start).total_seconds() / 60 for e in ends]
        limits = storage.durable_get("category_limits", {}) or {}
        key = f"{lid}_{gid}"
        entry = limits.get(key, {})
        entry["min"] = int(min(min(mins), entry.get("min", min(mins))))
        entry["max"] = int(max(max(mins), entry.get("max", 0)))
        limits[key] = entry
        storage.durable_set("category_limits", limits)
    except Exception:
        pass


def book(user_id: int, username: str, password: str, profile: dict,
         lid: int, gid: int, item_id: int, start: datetime, end: datetime,
         checksum: str, email_override: str | None = None,
         dry_run: bool = False) -> BookingResult:
    """Books item_id from start to end. `checksum` is the public grid's
    checksum for the 15-minute cell at `start` - the same token the site's
    own JS sends, so the flow is: add slot to cart (AJAX), adjust the end
    (AJAX update), submit times (AJAX -> auth URL), then the checkout form
    (agreement checkbox + Submit my Booking) in the page."""
    from playwright.sync_api import sync_playwright

    debug: list[str] = []
    trail: list[str] = []

    def note(msg: str) -> None:
        trail.append(f"{datetime.now():%H:%M:%S} {msg}")
        log.info("book: %s", msg)

    def dump_trail() -> None:
        try:
            path = config.DEBUG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-steps.txt"
            path.write_text("\n".join(trail), encoding="utf-8")
            debug.append(str(path))
        except Exception:
            pass

    note(f"book {item_id} {start:%Y-%m-%d %H:%M}-{end:%H:%M} lid={lid} gid={gid}"
         + (" [dry run]" if dry_run else ""))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not config.HEADFUL, args=LAUNCH_ARGS)
        state = _state_path(user_id)
        context = browser.new_context(
            storage_state=str(state) if state.exists() else None, user_agent=UA)
        page = context.new_page()
        try:
            url = f"{BASE}/spaces?lid={lid}&gid={gid}&date={start:%Y-%m-%d}"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if _maybe_login(page, username, password, wait_ms=6000):
                note("logged in at spaces page")
            page.wait_for_load_state("networkidle", timeout=30000)
            note(f"spaces page ready: {page.url[:80]}")

            day = start.date()
            # 1. Add the start slot to the cart.
            status, text = _ajax(page, "/spaces/availability/booking/add", {
                "add[eid]": item_id, "add[gid]": gid, "add[lid]": lid,
                "add[start]": f"{start:%Y-%m-%d %H:%M}", "add[checksum]": checksum,
                "lid": lid, "gid": gid, "start": f"{day}",
                "end": f"{day + timedelta(days=1)}"})
            import json as _json
            try:
                cart = _json.loads(text)["bookings"][0]
            except Exception:
                _raise_site_error(re.sub(r"<[^>]+>", " ", text)[:300]
                                  or f"add failed (HTTP {status})")
            options = cart.get("options", [])
            note(f"cart added id={cart['id']} default_end={cart['end']} "
                 f"{len(options)} end options")
            _record_limits(lid, gid, start, options)
            end_str = f"{end:%Y-%m-%d %H:%M:%S}"
            # 2. Adjust the end time if it isn't the default.
            if end_str != cart["end"]:
                if end_str not in options:
                    allowed = ", ".join(o[11:16] for o in options)
                    raise BookingError(
                        f"The site only allows this booking to end at: {allowed}. "
                        "Pick one of those durations.")
                idx = options.index(end_str)
                # The bookings[] echo must describe the cart AS IT IS (old
                # end + old checksum); only update[...] carries the change.
                status, text = _ajax(page, "/spaces/availability/booking/add", {
                    "update[id]": cart["id"],
                    "update[checksum]": cart["optionChecksums"][idx],
                    "update[end]": end_str,
                    "lid": lid, "gid": gid, "start": f"{day}",
                    "end": f"{day + timedelta(days=1)}",
                    "bookings[0][id]": cart["id"], "bookings[0][eid]": item_id,
                    "bookings[0][seat_id]": 0, "bookings[0][gid]": gid,
                    "bookings[0][lid]": lid,
                    "bookings[0][start]": f"{start:%Y-%m-%d %H:%M}",
                    "bookings[0][end]": cart["end"][:16],
                    "bookings[0][checksum]": cart["checksum"]})
                try:
                    cart = _json.loads(text)["bookings"][0]
                except Exception:
                    _raise_site_error(re.sub(r"<[^>]+>", " ", text)[:300]
                                      or f"end-time update failed (HTTP {status})")
                note(f"end updated to {cart['end']}")

            # 3. Submit times -> the response carries the checkout URL.
            status, text = _ajax(page, "/ajax/space/times", {
                "patron": "", "patronHash": "",
                "returnUrl": f"/spaces?lid={lid}&gid={gid}",
                "bookings[0][id]": cart["id"], "bookings[0][eid]": item_id,
                "bookings[0][seat_id]": 0, "bookings[0][gid]": gid,
                "bookings[0][lid]": lid,
                "bookings[0][start]": f"{start:%Y-%m-%d %H:%M}",
                "bookings[0][end]": f"{end:%Y-%m-%d %H:%M}",
                "bookings[0][checksum]": cart["checksum"], "method": 11})
            next_url = None
            try:
                payload = _json.loads(text)
                if isinstance(payload, dict):
                    next_url = payload.get("url") or payload.get("redirect")
            except Exception:
                pass
            if not next_url:
                m = re.search(r"https?://libcalendar\.ntu\.edu\.sg[^\s\"'<>]+", text)
                next_url = m.group(0) if m else None
            if status >= 400 or not next_url:
                _raise_site_error(re.sub(r"<[^>]+>", " ", text)[:300]
                                  or f"submit times failed (HTTP {status})")
            if next_url.startswith("/"):
                next_url = BASE + next_url
            note(f"times accepted, going to {next_url[:80]}")
            try:
                page.goto(next_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # Transient network blips (ERR_NETWORK_CHANGED etc.) - the
                # cart is held server-side, so one retry is safe.
                time.sleep(2)
                page.goto(next_url, wait_until="domcontentloaded", timeout=45000)
            if _maybe_login(page, username, password):
                note("completed NTU/ADFS login at checkout")
            page.wait_for_load_state("networkidle", timeout=30000)
            note(f"landed on {page.url[:90]}")
            if "libcalendar" not in page.url:
                raise BookingError(
                    "Stuck at the NTU login page - the login did not complete. "
                    "Check your /setup credentials.")

            err = _visible_error(page)
            if err:
                _raise_site_error(err)

            # 4. Terms: the agreement checkbox and the submit button are
            # hidden until "Continue" is pressed, so Continue comes FIRST.
            accept_terms(page)
            note("terms accepted, booking form open")

            # 5. Booking form: NTU autofills it; only fill fields left empty,
            # and remember the email the site used (check-in needs it later).
            email_used = None
            for fid, key in (("#fname", "first_name"), ("#lname", "last_name"),
                             ("#email", "email")):
                loc = page.locator(fid)
                if loc.count() and loc.first.is_visible() and not loc.first.input_value():
                    value = profile.get(key, "")
                    if value:
                        loc.first.fill(value)
            email_box = page.locator("#email")
            if email_box.count():
                if email_override and email_box.first.is_visible():
                    email_box.first.fill(email_override)
                email_used = email_box.first.input_value() or None

            if dry_run:
                body = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
                ok = "checkout" in body.lower() or "booking details" in body.lower()
                context.storage_state(path=str(state))
                note(f"dry run end: checkout_page={ok}")
                dump_trail()
                return BookingResult(
                    ok=ok, message=f"DRY RUN at {page.url[:80]} | {body[:300]}",
                    debug_files=debug)

            body = submit_booking_form(page)
            ok, confirmed_email = parse_confirmation(body)
            note(f"submitted booking form -> confirmed={ok}")
            if confirmed_email:
                email_used = confirmed_email

            err = _visible_error(page)
            if err:
                _raise_site_error(err)
            ref = None
            m = re.search(r"booking (?:id|reference)[:\s]*([\w-]+)", body, re.I)
            if m:
                ref = m.group(1)
            if not ok:
                debug += _dump(page, "unconfirmed")
            context.storage_state(path=str(state))
            snippet = body[:500]
            return BookingResult(ok=ok, message=snippet, reference=ref,
                                 email_used=email_used, debug_files=debug)

        except BookingError as e:
            note(f"REFUSED: {e}")
            debug += _dump(page, "refused")
            dump_trail()
            try:
                context.storage_state(path=str(state))
            except Exception:
                pass
            return BookingResult(ok=False, message=str(e),
                                 window_not_open=isinstance(e, WindowNotOpenError),
                                 debug_files=debug)
        except Exception as e:
            log.exception("Booking flow crashed")
            note(f"CRASH: {type(e).__name__}: {str(e)[:200]}")
            debug += _dump(page, "crash")
            dump_trail()
            return BookingResult(
                ok=False,
                message=f"The booking flow hit an unexpected page ({type(e).__name__}). "
                        f"Debug files saved locally.",
                debug_files=debug)
        finally:
            context.close()
            browser.close()


# --- Check-in with a photo of the result ---------------------------------
#
# /r/checkin needs no login (email + code only), so this is a plain page in a
# throwaway context. Checking in HERE rather than over HTTP means the
# confirmation the library itself renders can be photographed as proof.

CHECKED_IN_RE = re.compile(
    r"checked[\s-]*in|check[\s-]*in successful|you are now checked", re.I)
CHECKIN_FAIL_RE = re.compile(
    r"unable to|invalid|not found|no booking|cannot|expired", re.I)


def checkin_with_proof(email: str, code: str, tag: str = "",
                       sink=None) -> tuple[bool, str, str | None]:
    """(checked in, what the site said, path to the screenshot or None).

    `sink` is a queue that receives the answer the instant it is known, before
    the browser is closed. Closing a headless Chromium on this machine takes
    9-50 s (measured, and it is Chromium's shutdown, not ours - no ordering of
    page/context/browser close avoids it). Waiting for that made a check-in
    take 74 s, against a window only minutes wide.
    """
    from playwright.sync_api import sync_playwright

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    png = config.PROOF_DIR / f"checkin-{tag or code}-{stamp}.png"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not config.HEADFUL, args=LAUNCH_ARGS)
            context = browser.new_context(user_agent=UA)
            page = context.new_page()
            try:
                page.goto(f"{BASE}/r/checkin", wait_until="domcontentloaded",
                          timeout=45000)
                # This install only shows the code box; the email field is
                # present but hidden, so it is set directly.
                page.wait_for_selector("#s-lc-code", timeout=20000)
                page.fill("#s-lc-code", code.strip().upper())
                try:
                    page.eval_on_selector("#s-lc-email", "(e, v) => { e.value = v; }",
                                          email)
                except Exception:
                    pass
                # Wait for the check-in POST itself, not for the page to go
                # quiet: this page never reaches "networkidle", so waiting for
                # it burned the full 30 s timeout on every check-in - 74 s in
                # total, against a check-in window only 15 minutes wide.
                try:
                    with page.expect_response(
                            lambda r: "/r/checkin" in r.url
                            and r.request.method == "POST", timeout=20000):
                        page.click("#s-lc-checkin-button")
                except Exception:
                    pass                     # the click landed; read the page
                page.wait_for_timeout(800)   # let the answer render
                body = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
                failed = bool(CHECKIN_FAIL_RE.search(body))
                ok = bool(CHECKED_IN_RE.search(body)) and not failed
                config.PROOF_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(png), full_page=True)
                answer = (ok, body[:400], str(png))
                if sink is not None:
                    sink.put(answer)          # the caller can stop waiting now
                return answer
            finally:
                context.close()
                browser.close()
    except Exception as e:
        log.warning("check-in screenshot failed: %s", e)
        answer = (False,
                  f"could not open the check-in page ({type(e).__name__})", None)
        if sink is not None:
            sink.put(answer)
        return answer


async def checkin_now(email: str, code: str, tag: str = "",
                      timeout: float = 90) -> tuple[bool, str, str | None]:
    """Check in and come back as soon as the library has answered.

    The browser is left to shut down in its own thread afterwards; nobody is
    waiting on it, and the process reaps it.
    """
    import queue
    import threading

    sink: queue.Queue = queue.Queue(maxsize=1)
    threading.Thread(
        target=checkin_with_proof, args=(email, code, tag), kwargs={"sink": sink},
        daemon=True, name=f"checkin-{tag or code}").start()
    try:
        return await asyncio.to_thread(sink.get, True, timeout)
    except Exception:
        return False, "the check-in page did not answer in time", None


def probe() -> None:
    """Open a visible browser with the saved session for manual inspection."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=LAUNCH_ARGS)
        state = _state_path(0)
        context = browser.new_context(storage_state=str(state) if state.exists() else None,
                                      user_agent=UA)
        page = context.new_page()
        page.on("request", lambda r: r.method == "POST" and print(f"POST {r.url}"))
        page.goto(BASE)
        print("Browser open. Requests with POST are printed here. Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            context.storage_state(path=str(state))
            browser.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    probe()
