"""Runs scheduled bookings at their fire time.

Jobs live in the scheduled_bookings table so they survive restarts. A loop
wakes every 30 s; a due job attempts the booking, and if the category's
window simply isn't open yet ("This time slot is not open for booking right
now...") it re-arms itself every SCHED_RETRY_GAP_SECONDS until retry_until.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime, timedelta

from .. import config, storage, tasks
from . import browser, catalog, holds, libcal
from . import handlers as h

log = logging.getLogger(__name__)

FMT = storage.FMT


async def _materialise_recurring(application) -> None:
    """Turn each rule's next occurrences into ordinary scheduled jobs.

    Everything downstream - racing the 23:59 window, pre-holding, retrying,
    reporting, checking in - then works on a recurring booking without
    knowing that recurrence exists.

    Only occurrences whose window opens within RECUR_LOOKAHEAD_HOURS are
    created. That is not tidiness: _prehold_scan below fetches a grid for
    every pending job on every tick, so a term's worth of jobs created up
    front would hammer the library for weeks.
    """
    today = date.today()
    now = datetime.now()
    horizon = now + timedelta(hours=config.RECUR_LOOKAHEAD_HOURS)
    for rule in storage.active_rules():
        try:
            until = date.fromisoformat(rule["until_date"])
        except ValueError:
            log.warning("rule #%s has a bad end date: %r",
                        rule["id"], rule["until_date"])
            continue
        if until < today:
            storage.update_rule(rule["id"], status="finished")
            await _tell(application, rule["user_id"],
                        f"Your repeating booking for {rule['category']} has "
                        f"reached its end date ({until:%d %b}) and has stopped. "
                        "/recurring sets up another.")
            continue
        weekdays = storage.rule_weekdays(rule)
        if not weekdays:
            continue
        day = today
        while day <= until:
            if day.weekday() not in weekdays:
                day += timedelta(days=1)
                continue
            fire = catalog.window_opens_at(rule["lid"], rule["gid"], day)
            if fire > horizon:
                break                      # later days open later still
            if not catalog.is_open(rule["lid"], rule["gid"], day):
                day += timedelta(days=1)
                continue                   # Sunday, or a closure
            start = datetime.combine(day, dtime.fromisoformat(rule["start_hm"]))
            end = datetime.combine(day, dtime.fromisoformat(rule["end_hm"]))
            if end > now and not storage.rule_job_exists(rule["id"], start):
                job_id = storage.add_scheduled(
                    rule["user_id"], rule["lid"], rule["gid"], rule["location"],
                    rule["category"], rule["item_id"], start, end, fire,
                    fire + timedelta(minutes=config.SCHED_RETRY_MINUTES),
                    rule_id=rule["id"])
                log.info("rule #%s -> scheduled job #%s for %s",
                         rule["id"], job_id, start)
                await _tell(application, rule["user_id"],
                            f"Repeating booking: {rule['category']} on "
                            f"{start:%a %d %b} {start:%H:%M}-{end:%H:%M} is set "
                            f"up. I try at {fire:%a %d %b %H:%M}.")
            day += timedelta(days=1)


async def _tell(application, user_id: int, text: str) -> None:
    try:
        await application.bot.send_message(user_id, text)
    except Exception:
        log.debug("could not DM %s", user_id, exc_info=True)


async def run(application) -> None:
    """Started once from bot.py post_init."""
    # Jobs stuck in 'running' from a crash get another chance or a burial.
    c = storage.conn()
    c.execute(
        "UPDATE scheduled_bookings SET status = CASE WHEN retry_until >="
        " datetime('now','localtime') THEN 'retrying' ELSE 'failed' END"
        " WHERE status='running'")
    c.commit()
    try:
        removed = await h.purge_proofs(application.bot)
        if removed:
            log.info("erased %d check-in photo(s) whose booking had ended", removed)
    except Exception:
        log.exception("startup proof cleanup failed")
    log.info("Scheduled-booking runner started")
    while True:
        # Rules first: an occurrence that is already due then fires on this
        # same tick, and _tick_seconds() below can see it and speed up.
        try:
            await _materialise_recurring(application)
        except Exception:
            log.exception("recurring materialise failed")
        try:
            for job in storage.due_scheduled():
                storage.update_scheduled(job["id"], status="running")
                tasks.spawn(_run_job(application, job["id"]),
                            bot=application.bot, user_id=job["user_id"],
                            feature=f"scheduled booking #{job['id']}")
        except Exception:
            log.exception("scheduler tick failed")
        try:
            await _checkin_scan(application)
        except Exception:
            log.exception("check-in scan failed")
        try:
            await _code_nag_scan(application)
        except Exception:
            log.exception("code reminder failed")
        try:
            await _prehold_scan(application)
        except Exception:
            log.exception("pre-hold scan failed")
        try:
            await h.purge_proofs(application.bot)   # booking over -> photo gone
        except Exception:
            log.exception("proof cleanup failed")
        await asyncio.sleep(_tick_seconds())


# Check-in window (library rule R3): 5 min before start until 15 min after.
# Attempt gates: T-2min, T, T+5min; stop at the first success.
_CHECKIN_GATES = (timedelta(minutes=-2), timedelta(0), timedelta(minutes=5))


async def _code_nag_scan(application) -> None:
    """Ask once, soon, for a code the confirmation email should have brought.

    Waiting until five minutes before the start is too late to be useful: the
    same code is what lets you cancel, so a booking made for next week sits
    unusable until the day. This asks once, a few minutes after booking, when
    the email has had time to arrive and there is still time to act on it.
    """
    if config.CODE_NAG_MINUTES <= 0:
        return
    now = datetime.now()
    for b in storage.bookings_missing_code(config.CODE_NAG_MINUTES):
        start = datetime.strptime(b["start_ts"], FMT)
        # One ask, not two: the check-in scan would otherwise repeat this a few
        # minutes later for anything starting soon.
        storage.update_booking(b["id"], code_nagged=1, checkin_nagged=1)
        soon = start - now
        if soon <= timedelta(minutes=config.CODE_NAG_MINUTES):
            secs = soon.total_seconds()
            # Round up, and only claim it has started when it really has -
            # "already started" about a booking 55 seconds away is a lie.
            urgency = ("It has already started" if secs < 0
                       else f"It starts in {max(1, round(secs / 60))} min")
            await application.bot.send_message(
                b["user_id"],
                f"{urgency} and I have no check-in code for {b['room_name']}.\n\n"
                "Send /code ABC123 from the confirmation email now - check-in "
                "shuts 15 min after the start, and the same code is what "
                "cancels it if you change your mind.")
            continue
        await application.bot.send_message(
            b["user_id"],
            f"I still have no check-in code for {b['room_name']} on "
            f"{start:%a %d %b %H:%M}.\n\n"
            "Paste the confirmation email here, or send /code ABC123. Without "
            "it I cannot check you in - and I cannot cancel it for you either, "
            "since cancelling uses the same code.")


async def _checkin_scan(application) -> None:
    bot = application.bot
    now = datetime.now()
    for b in storage.bookings_needing_checkin():
        start = datetime.strptime(b["start_ts"], FMT)
        user = storage.get_user(b["user_id"])
        email = user["email"] if user else None
        if not b["checkin_code"] or not email:
            if now >= start - timedelta(minutes=5) and not b["checkin_nagged"]:
                storage.update_booking(b["id"], checkin_nagged=1, code_nagged=1)
                missing = "check-in code" if email else "email (/email) and check-in code"
                await bot.send_message(
                    b["user_id"],
                    f"⏰ {b['room_name']} starts at {start:%H:%M} but I'm missing "
                    f"your {missing}! Paste the confirmation email or /code ABC123 "
                    "NOW - the check-in window closes 15 min after start.")
            continue
        attempts = b["checkin_attempts"] or 0
        if attempts >= len(_CHECKIN_GATES) or now < start + _CHECKIN_GATES[attempts]:
            continue
        storage.update_booking(b["id"], checkin_attempts=attempts + 1)
        ok, msg = await h.checkin_booking(bot, b["user_id"], b)
        if ok:
            # The check-in page usually names the space better than we could,
            # and checkin_booking has just filed that, so read the row again
            # rather than repeating "your booking" back at somebody.
            named = storage.get_booking(b["id"]) or b
            await bot.send_message(
                b["user_id"], f"✅ Checked in to {named['room_name']} "
                              f"({start:%H:%M}) automatically.")
        elif attempts + 1 >= len(_CHECKIN_GATES):
            await bot.send_message(
                b["user_id"], f"❌ Auto check-in for {b['room_name']} failed "
                              f"{len(_CHECKIN_GATES)} times. Site said: {msg[:200]}\n"
                              "Try /checkin yourself or check in at the library kiosk.")


async def _hold_until_fire(application, job) -> None:
    """Chope a slot that is already free, so waiting for the fire time does
    not mean losing it.

    A custom fire time usually means "later today", and a slot that is
    bookable now can be gone in minutes. Rather than sit and hope, the bot
    takes the slot immediately and converts that hold into the booking when
    the time comes.
    """
    user_id = job["user_id"]
    profile = h._profile(user_id)
    if not profile:
        return
    start = datetime.strptime(job["start_ts"], FMT)
    end = datetime.strptime(job["end_ts"], FMT)
    try:
        grid = await libcal.fetch_grid(job["lid"], job["gid"], start.date())
    except Exception:
        return
    if not grid:
        return                                   # window not open: nothing to hold
    item_id = job["item_id"]
    if not item_id:
        free = libcal.spaces_free_for(grid, start, end)
        if not free:
            return
        item_id = free[0]
    checksum = next((c.checksum for c in grid.get(item_id, [])
                     if c.start == start and c.state == libcal.FREE), None)
    if checksum is None:
        return                                   # already taken or already held
    try:
        hold = await holds.create(
            user_id, profile["username"], profile["password"], job["lid"],
            job["gid"], item_id, start, end, checksum, job["location"] or "",
            job["category"] or "", note=f"holding for scheduled #{job['id']}")
    except Exception as exc:
        log.info("could not pre-hold job %s: %s", job["id"], exc)
        return
    hold.budget_minutes = 0                      # hold it right up to fire time
    storage.update_scheduled(job["id"], hold_id=hold.id)
    await application.bot.send_message(
        user_id,
        f"Scheduled booking #{job['id']}: that slot is free already, so I am "
        f"holding {hold.label} {start:%H:%M}-{end:%H:%M} until "
        f"{datetime.strptime(job['fire_at'], FMT):%H:%M}, then booking it.")


async def _prehold_scan(application) -> None:
    """Give every waiting job a hold if its slot can be taken now."""
    for job in storage.upcoming_scheduled():
        if job["hold_id"] and holds.get(job["hold_id"]):
            continue                             # already holding it
        fire = datetime.strptime(job["fire_at"], FMT)
        if fire <= datetime.now():
            continue                             # it is about to run anyway
        await _hold_until_fire(application, job)


def _tick_seconds() -> int:
    """Poll fast while a job is racing for a slot, slowly the rest of the time.

    A 15-second retry is pointless if the loop only wakes every 30 seconds.
    """
    soon = datetime.now() + timedelta(seconds=90)
    racing = storage.conn().execute(
        "SELECT 1 FROM scheduled_bookings WHERE status IN ('pending','retrying')"
        " AND fire_at <= ? LIMIT 1", (soon.strftime(FMT),)).fetchone()
    return 5 if racing else 30


async def _run_job(application, job_id: int) -> None:
    job = storage.get_scheduled(job_id)
    if job is None:
        return
    bot = application.bot
    user_id = job["user_id"]
    profile = h._profile(user_id)
    if not profile:
        storage.update_scheduled(job_id, status="failed", last_error="no credentials")
        await bot.send_message(user_id, f"Scheduled booking #{job_id}: no saved "
                                        "credentials - run /setup.")
        return

    start = datetime.strptime(job["start_ts"], FMT)
    end = datetime.strptime(job["end_ts"], FMT)

    # The live grid supplies both the space choice and the cell checksum the
    # booking AJAX needs. An empty grid means the day's window isn't open yet.
    try:
        grid = await libcal.fetch_grid(job["lid"], job["gid"], start.date())
    except Exception as e:
        log.warning("grid fetch in job %s failed: %s", job_id, e)
        grid = {}
    if not grid:
        await _retry_or_fail(bot, job_id, user_id,
                             "the booking window isn't open yet")
        return
    item_id = job["item_id"]
    if not item_id:
        free = libcal.spaces_free_for(grid, start, end)
        if not free:
            storage.update_scheduled(job_id, status="failed",
                                     last_error="no space free for that period")
            await bot.send_message(
                user_id, f"Scheduled booking #{job_id}: no space is free for "
                         f"{start:%a %d %b %H:%M}-{end:%H:%M} - all taken.")
            return
        item_id = free[0]
    checksum = next((c.checksum for c in grid.get(item_id, [])
                     if c.start == start and c.state == libcal.FREE), None)
    if checksum is None:
        storage.update_scheduled(job_id, status="failed",
                                 last_error="start slot already taken")
        await bot.send_message(
            user_id, f"Scheduled booking #{job_id}: "
                     f"{libcal.room_name(item_id)} at {start:%H:%M} was already taken.")
        return

    held = holds.get(job["hold_id"]) if job["hold_id"] else None
    if held is not None:
        # We have been holding this slot: turn the hold straight into a booking.
        ok, message = await holds.book(held)
        storage.update_scheduled(job_id, hold_id=None,
                                 status="done" if ok else "retrying")
        if ok:
            await bot.send_message(
                user_id, f"Scheduled booking #{job_id}: booked "
                         f"{held.label} {start:%H:%M}-{end:%H:%M} from the slot "
                         "I was holding for you.")
            return
        await bot.send_message(
            user_id, f"Scheduled booking #{job_id}: the held slot would not "
                     f"book ({message[:150]}). Trying normally...")

    result = await asyncio.to_thread(
        browser.book, user_id, profile["username"], profile["password"], profile,
        job["lid"], job["gid"], item_id, start, end, checksum)

    if result.ok:
        storage.update_scheduled(job_id, status="done")

        async def send(text, **kwargs):
            await bot.send_message(user_id, f"Scheduled booking #{job_id}: {text}",
                                   **kwargs)

        # Reuse the normal success path (stores booking, watches bot inbox).
        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.bot = bot
        await h._report_booking(send, ctx, user_id, job["location"],
                                job["category"], job["lid"], job["gid"],
                                item_id, start, end, result)
        return

    retryable = result.window_not_open or "No available cell" in result.message
    if retryable:
        await _retry_or_fail(bot, job_id, user_id, result.message[:200])
    else:
        storage.update_scheduled(job_id, status="failed",
                                 last_error=result.message[:400])
        await bot.send_message(
            user_id, f"Scheduled booking #{job_id} failed - the site said:\n\n"
                     f"{result.message[:400]}")


async def _retry_or_fail(bot, job_id: int, user_id: int, reason: str) -> None:
    job = storage.get_scheduled(job_id)
    retry_until = datetime.strptime(job["retry_until"], FMT)
    attempts = (job["attempts"] or 0) + 1
    # The window opens at 23:59:00 exactly (measured) and contested desks go
    # within seconds, so hammer it for the first few minutes before settling
    # into the slow retry.
    first_fire = retry_until - timedelta(minutes=config.SCHED_RETRY_MINUTES)
    rushing = datetime.now() < first_fire + timedelta(
        minutes=config.SCHED_RUSH_WINDOW_MINUTES)
    gap = config.SCHED_RUSH_SECONDS if rushing else config.SCHED_RETRY_GAP_SECONDS
    next_try = datetime.now() + timedelta(seconds=gap)
    if next_try <= retry_until:
        storage.update_scheduled(job_id, status="retrying", attempts=attempts,
                                 last_error=reason[:400],
                                 fire_at=next_try.strftime(FMT))
        if attempts == 1:
            await bot.send_message(
                user_id, f"Scheduled booking #{job_id}: {reason}. Retrying every "
                         f"{config.SCHED_RUSH_SECONDS}s for the first "
                         f"{config.SCHED_RUSH_WINDOW_MINUTES} min, then every "
                         f"{config.SCHED_RETRY_GAP_SECONDS // 60} min, until "
                         f"{retry_until:%H:%M}.")
    else:
        storage.update_scheduled(job_id, status="failed", attempts=attempts,
                                 last_error=reason[:400])
        await bot.send_message(
            user_id, f"Scheduled booking #{job_id}: gave up after {attempts} "
                     f"attempt(s). Last reason: {reason}")
