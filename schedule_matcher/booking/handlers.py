"""Telegram flows for NTU library booking.

Flow order (user feedback): Library -> Category -> Day -> Start -> End ->
Space -> Confirm. Only days and spaces that are actually free are ever shown
(the public grid tells us), every screen has Back / Main-menu buttons, and
spaces carry their real names (LIBLWNL-AK-10 ...), harvested once per
category from the logged-in page.

/schedulebook uses the same steps without grid filtering, for day-of
categories whose slots only open at a set hour - the scheduler fires the
booking then.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time as dtime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .. import ask, config, flows, storage, tasks
from . import (botmail, browser, catalog, credstore, emailcode, holds,
               libcal, rules)

log = logging.getLogger(__name__)

# Modes that pick from the live grid (as opposed to /schedulebook, which
# plans slots whose booking window has not opened yet).
LIVE_MODES = {"now", "chope", "ext"}

MODE_TITLES = {
    "now": "Book a library space",
    "chope": "Chope (hold) a space without booking it",
    "ext": "Extended session - book the first leg, chope the rest",
    "sched": "Schedule a booking for when its window opens",
    "recur": "Repeat a booking every week",
}

# The flow is one thing in four modes; the row of buttons at the top of every
# screen switches between them without starting over.
MODE_ORDER = ["now", "sched", "ext", "recur", "chope"]
MODE_SHORT = {"now": "Book", "chope": "Chope", "sched": "Schedule",
              "ext": "Extended", "recur": "Repeat"}


def _mode_rows(bk, user_id: int | None = None) -> list[list[InlineKeyboardButton]]:
    """Book / Schedule / Extended / Repeat - plus Chope for developers only.

    Extended holds the later legs by itself, so choping is not something an
    ordinary user ever needs to ask for. Four buttons is too many for one row
    on a phone, so past three they wrap into pairs.
    """
    row = []
    for mode in MODE_ORDER:
        if mode == "chope" and not (user_id and storage.is_developer(user_id)):
            continue
        label = MODE_SHORT[mode]
        row.append(InlineKeyboardButton(
            f"\u2022 {label} \u2022" if mode == bk.get("mode") else label,
            callback_data=f"bk|mode|{mode}"))
    if len(row) <= 3:
        return [row]
    return [row[i:i + 2] for i in range(0, len(row), 2)]


def _switch_mode(bk, mode: str) -> str:
    """Change mode, keeping as much of the current choice as still makes sense.
    Returns the step to re-render."""
    was, bk["mode"] = bk.get("mode"), mode
    if was == mode:
        return bk.get("step", "home")
    step = bk.get("step", "home")
    # Extended asks for a total longer than one booking; the others don't, so
    # any duration/slot chosen under the old mode may no longer be valid.
    if "ext" in (was, mode) and step in ("range", "space", "confirm", "fire",
                                         "sconfirm", "end"):
        for key in ("dur", "start", "end", "room", "free_spaces"):
            bk.pop(key, None)
        return "dur" if bk.get("day") else "day"
    # Scheduling shows every day/time (its window may not be open yet), so a
    # day picked from the live grid is still fine, but the grid cache is not.
    if "sched" in (was, mode):
        bk.pop("grids", None)
        if step in ("confirm", "sconfirm", "space", "fire"):
            return "space" if bk.get("end") else "dur"
    if step in ("confirm", "sconfirm", "fire"):
        return "space" if bk.get("room") is None else "confirm"
    return step


def _prev_step(bk: dict, step: str) -> str:
    """Back target for each step. The main flow is day -> duration -> range
    (start+end shown together); 'Custom' branches into the old start -> end
    pickers, so Back from the space step depends on the path taken."""
    if step == "space":
        return "end" if bk.get("via_custom") else "range"
    if step == "confirm":
        return "range" if bk.get("only_item") else "space"
    if step == "dur" and bk.get("mode") == "recur":
        return "days"
    return {"cat": "home", "day": "cat", "dur": "day", "range": "dur",
            "start": "dur", "end": "start", "fire": "space",
            "sconfirm": "fire", "days": "cat", "until": "space",
            "rconfirm": "until"}.get(step, "home")


# --- Small helpers --------------------------------------------------------

async def _require_private(update: Update) -> bool:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Booking involves your NTU credentials, so let's do this in a "
            "private chat - message me directly.")
        return False
    return True


def _profile(user_id: int) -> dict | None:
    row = storage.get_user(user_id)
    if not row or not row["ntu_username"] or not row["ntu_password"]:
        return None
    return {
        "username": credstore.decrypt(row["ntu_username"]),
        "password": credstore.decrypt(row["ntu_password"]),
        "email": row["email"] or "",
    }


def _nav_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("« Back", callback_data="bk|back"),
            InlineKeyboardButton("Main menu", callback_data="bk|home")]


def _kb(items: list[tuple[str, str]], per_row: int = 2, nav: bool = True,
        bk: dict | None = None, save: bool = False) -> InlineKeyboardMarkup:
    """items -> buttons, with the mode switcher on top and Back/Menu below."""
    rows = []
    if bk is not None:
        rows.extend(_mode_rows(bk, bk.get("user_id")))
    row = []
    for label, data in items:
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if save:
        rows.append([InlineKeyboardButton("\u2605 Save as favourite",
                                          callback_data="bk|favhere")])
    if nav:
        rows.append(_nav_row())
    return InlineKeyboardMarkup(rows)


def _msg_query(user, msg):
    """Adapter so the step renderers can drive a fresh bot message the same
    way they drive a callback query's message."""
    class Q:
        pass
    q = Q()
    q.from_user = user

    async def edit(text, **kw):
        await msg.edit_text(text, **kw)

    q.edit_message_text = edit
    return q


def _shortlist(bk, items: list[tuple[str, str]],
               rank: list[int] | None = None) -> list[tuple[str, str]]:
    """Trim a long button list to the most-used few, with a toggle to expand.

    `rank` is a parallel list of usage counts; higher sorts first. Ties keep
    the site's own order, so the list never looks shuffled.
    """
    size = storage.shortlist_size(bk.get("user_id") or 0)
    if len(items) <= size + 1:
        return items
    if rank:
        order = sorted(range(len(items)), key=lambda i: (-rank[i], i))
        items = [items[i] for i in order]
    if bk.get("show_all"):
        return items + [("\u25b2 Show fewer", "bk|showall")]
    return items[:size] + [
        (f"\u25bc Show all ({len(items)})", "bk|showall")]


def _space_label(item_id: int) -> str:
    name = libcal.room_name(item_id)
    return name if not name.startswith("Room ") else f"Space {item_id}"


async def _refresh_grid(bk) -> dict:
    """Re-fetch the chosen day's grid so we never promise a stale slot."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    grid = await libcal.fetch_grid(cat.lid, cat.gid, bk["day"])
    bk.setdefault("grids", {})[bk["day"]] = grid
    return grid


def _restrict(grid: dict, only_item) -> dict:
    if only_item:
        return {k: v for k, v in grid.items() if k == only_item}
    return grid


async def _ensure_space_names(query, context, bk, item_ids: list[int]) -> None:
    """Harvest real space names once per category if any id is unnamed."""
    unknown = [i for i in item_ids if libcal.room_name(i).startswith("Room ")]
    if not unknown:
        return
    profile = _profile(query.from_user.id)
    if not profile:
        return
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    fail_key = f"harvest_fail_{cat.lid}_{cat.gid}"
    if storage.cache_get(fail_key, max_age_hours=6):
        return  # tried recently and got nothing - don't make the user wait again
    await query.edit_message_text(
        "Fetching the real space names for this category (one-time, ~30s)...")
    names = await asyncio.to_thread(
        browser.harvest_names, query.from_user.id, profile["username"],
        profile["password"], cat.lid, cat.gid)
    if names:
        libcal.remember_room_names(names)
    else:
        storage.cache_set(fail_key, 1)


# --- /setup ---------------------------------------------------------------

SETUP_STEPS = [
    ("ntu_username", "Your NTU network username (what you type at the NTU login page):"),
    ("ntu_password", "Your NTU network password.\n(I'll delete your message right after reading it; "
                     "it is stored encrypted on this machine only, never in OneDrive.)"),
]


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    context.user_data["setup_step"] = 0
    await update.effective_message.reply_text(
        "Let's set up library booking - just two things. /cancel_setup aborts, "
        "/forgetme wipes everything.\n\n"
        "Honest note: your password is stored encrypted on the machine running "
        "this bot, but whoever HOSTS the bot could technically read it, since "
        "the code runs on their computer. Only continue if you trust the host.\n\n"
        + SETUP_STEPS[0][1])


async def cmd_cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("setup_step", None)
    await update.effective_message.reply_text("Setup cancelled.")


async def cmd_forgetme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await purge_proofs(context.bot, update.effective_user.id)
    storage.clear_user(update.effective_user.id)
    state = browser._state_path(update.effective_user.id)
    if state.exists():
        state.unlink()
    await update.effective_message.reply_text("All your credentials and sessions are deleted from this machine.")


async def _setup_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    step = context.user_data.get("setup_step")
    if step is None:
        return False
    key, _ = SETUP_STEPS[step]
    text = update.effective_message.text.strip()
    user_id = update.effective_user.id
    if key == "ntu_username":
        storage.save_user(user_id, ntu_username=credstore.encrypt(text))
    elif key == "ntu_password":
        storage.save_user(user_id, ntu_password=credstore.encrypt(text))
        try:
            await update.effective_message.delete()
        except Exception:
            pass
    step += 1
    if step >= len(SETUP_STEPS):
        context.user_data.pop("setup_step", None)
        row = storage.get_user(user_id)
        note = ""
        if row and not row["email"] and row["ntu_username"]:
            derived = f"{credstore.decrypt(row['ntu_username']).upper()}@e.ntu.edu.sg"
            storage.save_user(user_id, email=derived)
            note = (f"\n\nFor check-in I'll assume your student email is "
                    f"{derived} - if that's wrong, fix it with /email.")
        await update.effective_chat.send_message(
            "Done - you're set up. /book books now, /schedulebook books at a "
            "set time (for day-of categories like Arrakis)." + note)
    else:
        context.user_data["setup_step"] = step
        await update.effective_chat.send_message(SETUP_STEPS[step][1])
    return True


# --- /book and /schedulebook entry ----------------------------------------

async def _start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if not await _require_private(update):
        return
    if not _profile(update.effective_user.id):
        await update.effective_message.reply_text("First run /setup so I can book under your account.")
        return
    try:
        locations = await libcal.fetch_locations()
    except Exception:
        log.exception("fetch_locations failed")
        await update.effective_message.reply_text("Couldn't reach libcalendar.ntu.edu.sg - try again later.")
        return
    context.user_data["bk"] = {"mode": mode, "locations": locations,
                               "step": "home", "user_id": update.effective_user.id}
    msg = await flows.start(update, context, flows.LIBRARY, "...")
    await _render(_msg_query(update.effective_user, msg), context, "home")


async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_flow(update, context, "now")


async def cmd_schedulebook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_flow(update, context, "sched")


async def cmd_chope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Holding without booking is a developer tool: Extended already holds
    its later legs by itself, and a stray hold blocks the space for others."""
    if not storage.is_developer(update.effective_user.id):
        await update.effective_message.reply_text(
            "Choping is a developer feature. /extendedbooking already holds "
            "the later legs of a long session for you.")
        return
    await _start_flow(update, context, "chope")


async def cmd_extendedbooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_flow(update, context, "ext")


# --- Step renderers -------------------------------------------------------

async def _render(query, context, step: str):
    bk = context.user_data["bk"]
    if step != bk.get("step"):
        bk.pop("show_all", None)      # expansion belongs to one screen only
    bk["step"] = step
    await {"home": _r_home, "cat": _r_cat, "day": _r_day, "dur": _r_dur,
           "range": _r_range, "start": _r_start, "end": _r_end,
           "space": _r_space, "fire": _r_fire, "confirm": _r_confirm,
           "sconfirm": _r_sconfirm, "days": _r_days, "until": _r_until,
           "rconfirm": _r_rconfirm}[step](query, context, bk)


async def _r_home(query, context, bk):
    title = MODE_TITLES.get(bk["mode"], "Book a library space") + " - where?"
    cats, _ = storage.usage_counts(query.from_user.id)
    items, rank = [], []
    for i, loc in enumerate(bk["locations"]):
        items.append((loc.name, f"bk|loc|{i}"))
        rank.append(sum(n for (lid, _gid), n in cats.items()
                        if any(c.lid == lid for c in loc.categories)))
    items = _shortlist(bk, items, rank)
    last = context.user_data.get("bk_last")
    if last:
        items.insert(0, (f"Again: {last['category']}", "bk|again"))
    await query.edit_message_text(title,
                                  reply_markup=_kb(items, per_row=1, nav=False, bk=bk))


async def _r_cat(query, context, bk):
    loc = bk["locations"][bk["loc"]]
    cats, _ = storage.usage_counts(query.from_user.id)
    items = [(c.label, f"bk|cat|{i}") for i, c in enumerate(loc.categories)]
    rank = [cats.get((c.lid, c.gid), 0) for c in loc.categories]
    items = _shortlist(bk, items, rank)
    await query.edit_message_text(f"{loc.name} - pick a category:",
                                  reply_markup=_kb(items, per_row=1, bk=bk))


async def _r_day(query, context, bk):
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    if bk["mode"] in LIVE_MODES:
        # Each category's booking window is a fixed pattern, so which days are
        # open is cached per day - the list renders instantly after the first
        # look. Actual slots are always re-fetched fresh at the next step.
        cache_key = f"day_avail_{cat.lid}_{cat.gid}_{date.today():%Y%m%d}"
        day_strs = storage.cache_get(cache_key, max_age_hours=6)
        if day_strs is None:
            await query.edit_message_text(f"Checking {cat.label} availability...")
            grids = await libcal.days_with_availability(cat.lid, cat.gid,
                                                       config.BOOKING_DAYS_AHEAD)
            bk["grids"] = grids
            day_strs = [f"{d:%Y%m%d}" for d in sorted(grids)]
            storage.cache_set(cache_key, day_strs)
        days = [datetime.strptime(s, "%Y%m%d").date() for s in day_strs]
        if not days:
            await query.edit_message_text(
                f"Nothing bookable for {cat.label} in the next "
                f"{config.BOOKING_DAYS_AHEAD} days.\n\n"
                "This category may only open day-by-day (like Arrakis) - "
                "/schedulebook can fire the booking the moment its window opens.",
                reply_markup=_kb([], nav=True, bk=bk))
            return
        items = [(d.strftime("%a %d %b"), f"bk|day|{d:%Y%m%d}") for d in days]
        await query.edit_message_text(f"{cat.label} - days with free slots:",
                                      reply_markup=_kb(items, per_row=2, bk=bk,
                                                       save=True))
    else:
        # Scheduling still cannot invent hours: a day the library is shut is
        # never offered, and each day says when its booking window opens.
        today = date.today()
        items = []
        for n in range(config.BOOKING_DAYS_AHEAD + 1):
            day = today + timedelta(days=n)
            if not catalog.is_open(cat.lid, cat.gid, day):
                continue
            opens = catalog.window_opens_at(cat.lid, cat.gid, day)
            when = "open now" if opens <= datetime.now() else f"opens {opens:%a %H:%M}"
            items.append((f"{day:%a %d %b} ({when})", f"bk|day|{day:%Y%m%d}"))
        if not items:
            await query.edit_message_text(
                f"{cat.label} has no open days in the next "
                f"{config.BOOKING_DAYS_AHEAD} days.", reply_markup=_kb([], bk=bk))
            return
        await query.edit_message_text(
            f"{cat.label} - which day do you want the booking FOR?\n"
            f"(Sundays are skipped - the library is closed.)",
            reply_markup=_kb(items, per_row=1, bk=bk))


def _availability_strip(grid: dict, day) -> str:
    """Whole-day picture: one emoji per 30-min block, hour labels every 2h.
    Green = at least one space free, red = none."""
    parts, chunk = [], []
    for n in range(32):  # 08:00 .. 23:30
        t = datetime.combine(day, dtime(8, 0)) + timedelta(minutes=30 * n)
        free = any(
            c.state == libcal.FREE for cells in grid.values() for c in cells
            if c.start <= t < c.end or c.start == t)
        chunk.append("🟩" if free else "🟥")
        if len(chunk) == 4:
            parts.append(f"{t - timedelta(minutes=90):%H:%M} " + "".join(chunk))
            chunk = []
    return "\n".join(parts[i] + "  " + parts[i + 1] if i + 1 < len(parts) else parts[i]
                     for i in range(0, len(parts), 2))


DURATIONS = [30, 45, 60, 90, 120, 150, 180, 240]
EXT_DURATIONS = [180, 240, 300, 360, 480]   # /extendedbooking totals


async def _sched_availability(bk) -> str:
    """What the day looks like when scheduling.

    Once a day's window has opened its grid is real, so show the same emoji
    strip as normal booking. Before that there is nothing to show, and saying
    so is better than an empty strip that reads as "everything is taken".
    """
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    opens = catalog.window_opens_at(cat.lid, cat.gid, bk["day"])
    if opens > datetime.now():
        return ("\n\n🔒 Not open yet - this day's slots appear at "
                f"{opens:%a %d %b %H:%M}. I am choosing blind and will fight "
                "for it the moment it opens.")
    try:
        grid = await libcal.fetch_grid(cat.lid, cat.gid, bk["day"])
    except Exception:
        return "\n\n(Couldn't load that day's availability just now.)"
    if not grid:
        return "\n\n(No availability published for that day yet.)"
    free = len([1 for cells in grid.values() if libcal.bookable_starts(cells)])
    return ("\n\n" + _availability_strip(grid, bk["day"])
            + f"\n{free} space(s) still have free slots.")


def _cap_minutes(bk) -> int:
    """How long one booking may run in this category."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    _, hi = libcal.category_limits(cat.lid, cat.gid)
    return catalog.max_each_minutes(cat.lid, cat.gid) or hi


def _hop_plan(grid, bk, start, end):
    """Segments covering [start, end) across desks, or None if impossible."""
    if bk.get("only_item"):
        return None                       # a favourite pins one desk
    return libcal.cover_span(grid, start, end, _cap_minutes(bk))


def _plan_lines(plan) -> str:
    out = []
    for item_id, seg_start, seg_end in plan:
        mins = int((seg_end - seg_start).total_seconds() // 60)
        length = f"{mins // 60}h{mins % 60:02d}" if mins >= 60 else f"{mins}min"
        out.append(f"  {seg_start:%H:%M}-{seg_end:%H:%M}  "
                   f"{_space_label(item_id)}  ({length})")
    return "\n".join(out)


def _moves_sentence(plan) -> str:
    moves = len(plan) - 1
    if moves <= 0:
        return "No moving - one desk the whole time."
    if moves == 1:
        return f"One move, at {plan[1][1]:%H:%M}."
    times = ", ".join(f"{seg[1]:%H:%M}" for seg in plan[1:])
    return f"{moves} moves, at {times}."


def _open_window(bk) -> tuple[datetime, datetime]:
    """The library's real opening and closing time on the chosen day."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    hours = catalog.hours_for(cat.lid, cat.gid, bk["day"])
    if hours is None:                      # shut: give an empty window
        midnight = datetime.combine(bk["day"], dtime(0, 0))
        return midnight, midnight
    return (datetime.combine(bk["day"], hours[0]),
            datetime.combine(bk["day"], hours[1]))


async def _r_dur(query, context, bk):
    bk.pop("via_custom", None)
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    lo, hi = libcal.category_limits(cat.lid, cat.gid)
    # The published policy is the real ceiling; what the cart offered on one
    # day only reflected that day's neighbouring bookings.
    policy_max = catalog.max_each_minutes(cat.lid, cat.gid)
    if policy_max:
        hi = policy_max if bk["mode"] == "sched" else max(hi, policy_max)
    if bk["mode"] == "ext":
        # Totals deliberately longer than one booking allows; the total is
        # split into legs of `hi` minutes each.
        items = [(f"{m // 60}h", f"bk|dur|{m}")
                 for m in EXT_DURATIONS if m > hi]
        items.append(("Custom start & end", "bk|dur|custom"))
        await query.edit_message_text(
            f"{bk['day']:%a %d %b} - how long in TOTAL?\n"
            f"({cat.label} allows {hi} min per booking, so I'll split the "
            f"session into {hi}-min legs: leg 1 booked, the rest choped.)",
            reply_markup=_kb(items, per_row=3, bk=bk))
        return
    items = []
    for m in DURATIONS:
        if lo <= m <= hi:
            label = f"{m // 60}h{m % 60 or ''}" if m >= 60 else f"{m}min"
            items.append((label, f"bk|dur|{m}"))
    if not items:
        items.append((f"{hi}min", f"bk|dur|{hi}"))
    items.append(("Custom start & end", "bk|dur|custom"))
    await query.edit_message_text(
        f"{bk['day']:%a %d %b} - how long do you need?\n"
        f"({cat.label} allows {lo}-{hi} min per booking)",
        reply_markup=_kb(items, per_row=3, bk=bk))


async def _r_range(query, context, bk):
    dur = timedelta(minutes=bk["dur"])
    step = 15 if bk.get("fine") else 30
    if bk["mode"] in LIVE_MODES:
        grid = _restrict(await _refresh_grid(bk), bk.get("only_item"))
        fits = (libcal.spaces_free_span if bk["mode"] == "ext"
                else libcal.spaces_free_for)
        options = []
        hops = {}
        for s in sorted({s for cells in grid.values()
                         for s in libcal.bookable_starts(cells, step=step)}):
            if fits(grid, s, s + dur):
                options.append(s)
            elif bk["mode"] == "ext":
                # Nothing covers it from one desk, but moving might.
                plan = _hop_plan(grid, bk, s, s + dur)
                if plan:
                    options.append(s)
                    hops[s] = len(plan)
        strip = _availability_strip(grid, bk["day"])
        toggle = ("30-min steps" if bk.get("fine") else "15-min steps", "bk|fine")
        if not options:
            await query.edit_message_text(
                f"No free {bk['dur']}-minute slot on {bk['day']:%a %d %b}:\n\n"
                f"{strip}\n\nTry a shorter duration or another day.",
                reply_markup=_kb([toggle], nav=True, bk=bk))
            return
        items = []
        for s in options[:60]:
            label = f"{s:%H:%M}-{(s + dur):%H:%M}"
            if s in hops:
                label += f" ⇄{hops[s]}"      # needs that many desks
            items.append((label, f"bk|rg|{s:%H%M}"))
        items.append(toggle)
        note = ("\n⇄ = no single desk covers it; I'd split it across that many."
                if hops else "")
        await query.edit_message_text(
            f"{bk['day']:%a %d %b}, {bk['dur']} min - pick your slot "
            "(🟩 free / 🟥 taken):\n\n" + strip + note,
            reply_markup=_kb(items, per_row=3, bk=bk))
    else:
        opens, closes = _open_window(bk)
        options = []
        t = opens
        while t + dur <= closes:
            options.append(t)
            t += timedelta(minutes=step)
        if not options:
            await query.edit_message_text(
                f"{bk['dur']} min doesn't fit inside "
                f"{opens:%H:%M}-{closes:%H:%M} on {bk['day']:%a %d %b}.",
                reply_markup=_kb([], bk=bk))
            return
        items = [(f"{s:%H:%M}-{(s + dur):%H:%M}", f"bk|rg|{s:%H%M}")
                 for s in options]
        await query.edit_message_text(
            f"{bk['day']:%a %d %b}, {bk['dur']} min - which slot?\n"
            f"(Open {opens:%H:%M}-{closes:%H:%M}.)"
            + await _sched_availability(bk),
            reply_markup=_kb(items, per_row=3, bk=bk))


async def _r_start(query, context, bk):
    step = 15 if bk.get("fine") else 30
    strip = ""
    if bk["mode"] in LIVE_MODES:
        grid = _restrict(await _refresh_grid(bk), bk.get("only_item"))
        starts = sorted({s for cells in grid.values()
                         for s in libcal.bookable_starts(cells, step=step)})
        strip = "\n\n" + _availability_strip(grid, bk["day"])
        if not starts:
            await query.edit_message_text(
                f"No free start times left that day.{strip}",
                reply_markup=_kb([], nav=True, bk=bk))
            return
    else:
        opens, closes = _open_window(bk)
        starts, t = [], opens
        while t < closes:
            starts.append(t)
            t += timedelta(minutes=step)
    items = [(f"{s:%H:%M}", f"bk|st|{s:%H%M}") for s in starts[:64]]
    items.append(("30-min steps" if bk.get("fine") else "15-min steps", "bk|fine"))
    await query.edit_message_text(
        f"{bk['day']:%a %d %b} - from what time?{strip}",
        reply_markup=_kb(items, per_row=4, bk=bk))


async def _r_end(query, context, bk):
    start = bk["start"]
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    lo, hi = libcal.category_limits(cat.lid, cat.gid)
    step = 15 if bk.get("fine") else 30
    strip = ""
    if bk["mode"] == "ext":
        # An extended session is deliberately longer than one booking allows,
        # so the cap applies to each leg (handled by _ext_legs), not to the
        # total. What matters here is that the whole stretch is free and the
        # library is still open at the end of it.
        grid = _restrict(bk["grids"][bk["day"]], bk.get("only_item"))
        strip = "\n" + "\n" + _availability_strip(grid, bk["day"])
        _, closes = _open_window(bk)
        ceiling = min(start + timedelta(minutes=config.GROUP_MAX_MINUTES), closes)
        ends, t = [], start + timedelta(minutes=step)
        while t <= ceiling:
            if libcal.spaces_free_span(grid, start, t):
                ends.append(t)
            t += timedelta(minutes=step)
        # Only totals worth splitting belong in this mode.
        ends = [e for e in ends if (e - start) >= timedelta(minutes=min(hi, 60))]
        if not ends:
            await query.edit_message_text(
                "Nothing long enough is free from that start - pick another "
                "time, or use Book for a single slot.",
                reply_markup=_kb([], nav=True, bk=bk))
            return
    elif bk["mode"] in LIVE_MODES:
        grid = _restrict(bk["grids"][bk["day"]], bk.get("only_item"))
        strip = "\n\n" + _availability_strip(grid, bk["day"])
        ends = sorted({e for cells in grid.values()
                       if start in libcal.bookable_starts(cells, step=step)
                       for e in libcal.valid_ends(cells, start)})
        ends = [e for e in ends
                if timedelta(minutes=lo) <= e - start <= timedelta(minutes=hi)
                and not (e.minute % step)]
        if not ends:
            await query.edit_message_text(
                "That start time just became unavailable - pick another.",
                reply_markup=_kb([], nav=True, bk=bk))
            return
    else:
        _, closes = _open_window(bk)
        limit = min(start + timedelta(minutes=hi), closes)
        ends, t = [], start + timedelta(minutes=max(lo, step))
        while t <= limit:
            ends.append(t)
            t += timedelta(minutes=step)
    items = [(f"{e:%H:%M}", f"bk|en|{e:%H%M}") for e in ends]
    items.append(("30-min steps" if bk.get("fine") else "15-min steps", "bk|fine"))
    if bk["mode"] == "ext":
        header = (f"From {start:%H:%M} until when in TOTAL?" + "\n" +
                  f"(I split it into {hi}-min legs: leg 1 is booked, the "
                  f"rest are choped.){strip}")
    else:
        header = f"From {start:%H:%M} until when? ({cat.label}: {lo}-{hi} min){strip}"
    await query.edit_message_text(header, reply_markup=_kb(items, per_row=4, bk=bk))


async def _r_space(query, context, bk):
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    if bk["mode"] in LIVE_MODES:
        grid = _restrict(await _refresh_grid(bk), bk.get("only_item"))
        fits = (libcal.spaces_free_span if bk["mode"] == "ext"
                else libcal.spaces_free_for)
        free = fits(grid, bk["start"], bk["end"])
        plan = (_hop_plan(grid, bk, bk["start"], bk["end"])
                if bk["mode"] == "ext" else None)
        if not free:
            if plan:
                bk["plan"] = plan
                await query.edit_message_text(
                    f"No single desk covers {bk['start']:%H:%M}-{bk['end']:%H:%M}, "
                    f"but {len(plan)} together do:\n\n{_plan_lines(plan)}\n\n"
                    f"{_moves_sentence(plan)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"Use these {len(plan)} desks", callback_data="bk|hop")],
                        _nav_row()]))
                return
            await query.edit_message_text(
                "No space is free for that whole period, and I cannot cover it "
                "by moving desks either - try a shorter one.",
                reply_markup=_kb([], nav=True, bk=bk))
            return
        bk["free_spaces"] = free
        await _ensure_space_names(query, context, bk, free)
        verb = {"chope": "hold", "ext": "use"}.get(bk["mode"], "book")
        # Not ranked by history: which spaces are free changes every day, so a
        # "most used" order would be misleading here.
        space_items = [(_space_label(i), f"bk|sp|{i}") for i in free]
        space_items = _shortlist(bk, space_items)
        items = [(f"Any space - just {verb} one", "bk|any")] + space_items
        if plan and len(plan) > 1:
            bk["plan"] = plan
            items.append((f"Or hop between {len(plan)} desks", "bk|hop"))
        await query.edit_message_text(
            f"{cat.label}, {bk['start']:%a %d %b %H:%M}-{bk['end']:%H:%M} - "
            f"{len(free)} space(s) free:",
            reply_markup=_kb(items, per_row=2, bk=bk, save=True))
    else:
        # The target day's grid does not exist yet, so the catalogue supplies
        # the table list - you can still ask for a specific one.
        ids = sorted(catalog.spaces(cat.lid, cat.gid))
        if not ids:
            try:
                ids = sorted(await libcal.fetch_grid(cat.lid, cat.gid, date.today()))
            except Exception:
                ids = []
        bk["free_spaces"] = ids
        items = [("Any space (I take whatever is free)", "bk|any")]
        items += _shortlist(bk, [(_space_label(i), f"bk|sp|{i}") for i in ids])
        await query.edit_message_text(
            f"{cat.label} - which space?\n"
            "'Any' wins contested slots more often; a named table is exact.",
            reply_markup=_kb(items, per_row=2, bk=bk))


async def _r_fire(query, context, bk):
    d = bk["day"]
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    opens = catalog.window_opens_at(cat.lid, cat.gid, d)
    eve = d - timedelta(days=1)
    items = [
        (f"When it opens: {opens:%a %d %b %H:%M}", "bk|fire|auto"),
        (f"11:59pm night before ({eve:%d %b})", "bk|fire|eve"),
        (f"7am that day ({d:%d %b} 07:00)", "bk|fire|0700"),
        ("In 2 minutes (test run)", "bk|fire|now"),
        ("Custom - I'll type it", "bk|fire|custom"),
    ]
    await query.edit_message_text(
        f"When should I FIRE this booking attempt?\n"
        f"(Booking is for {d:%a %d %b} {bk['start']:%H:%M}-{bk['end']:%H:%M}. "
        f"If the window isn't open yet I retry every "
        f"{config.SCHED_RETRY_GAP_SECONDS // 60} min for "
        f"{config.SCHED_RETRY_MINUTES} min.)",
        reply_markup=_kb(items, per_row=1, bk=bk))


def _account_lines(user_id: int) -> str:
    row = storage.get_user(user_id)
    username = credstore.decrypt(row["ntu_username"]) if row and row["ntu_username"] else "-"
    email_addr = (row["email"] if row else None) or "(whatever the site autofills - I'll save it)"
    return f"Booking as:\n  NTU username: {username}\n  Email: {email_addr}"


def _chosen_space_text(bk) -> str:
    return ("any free space" if bk.get("room") in (None, 0)
            else _space_label(bk["room"]))


async def _r_confirm(query, context, bk):
    loc = bk["locations"][bk["loc"]]
    cat = loc.categories[bk["cat"]]

    # Freshness: never show a confirm screen for a slot that just got taken.
    grid = _restrict(await _refresh_grid(bk), bk.get("only_item"))
    fits = libcal.spaces_free_span if bk["mode"] == "ext" else libcal.spaces_free_for
    free = fits(grid, bk["start"], bk["end"])
    if not free:
        await query.edit_message_text(
            "That period just became unavailable - someone got there first. "
            "Pick a different time.", reply_markup=_kb([], nav=True, bk=bk))
        bk["step"] = "end"
        return
    if bk.get("room") and bk["room"] not in free:
        taken = _space_label(bk["room"])
        bk["free_spaces"] = free
        bk["room"] = None
        await query.edit_message_text(
            f"{taken} was just taken - {len(free)} other space(s) are still "
            "free for your period, pick one:")
        await _render(query, context, "space")
        return
    bk["free_spaces"] = free

    warn = rules.precheck(query.from_user.id, bk.get("room"), bk["start"], bk["end"])
    warn_text = ("\n\n⚠ " + "\n⚠ ".join(warn)) if warn else ""
    head = (f"{loc.name} - {cat.label}\n{_chosen_space_text(bk)}\n"
            f"{bk['start']:%a %d %b, %H:%M} - {bk['end']:%H:%M}\n\n"
            f"{_account_lines(query.from_user.id)}{warn_text}")

    if bk["mode"] == "chope":
        await query.edit_message_text(
            f"Chope this (hold, don't book)?\n\n{head}\n\n"
            "I'll take it to the checkout page and stop there, so nobody else "
            f"can take it. The site holds it ~10 min and I keep renewing "
            f"(up to {config.HOLD_MAX_MINUTES} min). Book or release it any "
            "time from /holds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Chope it", callback_data="bk|go")],
                _nav_row()]))
        return

    if bk["mode"] == "ext" and bk.get("plan"):
        segments = bk["plan"]
        await query.edit_message_text(
            f"Extended session across {len(segments)} desks?\n\n{head}\n\n"
            f"{_plan_lines(segments)}\n\n{_moves_sentence(segments)}\n\n"
            "The first is booked properly; the rest are choped so nobody takes "
            "them, and you convert each from /holds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Book the first + chope the rest",
                                      callback_data="bk|go")],
                _nav_row()]))
        return

    if bk["mode"] == "ext":
        legs = _ext_legs(bk)
        plan = "\n".join(
            f"  leg {i}: {s:%H:%M}-{e:%H:%M}" + (" -> BOOK now" if i == 1 else " -> chope")
            for i, (s, e) in enumerate(legs, 1))
        await query.edit_message_text(
            f"Extended session?\n\n{head}\n\n{len(legs)} legs:\n{plan}\n\n"
            "Leg 1 gets booked properly. The rest are choped (held) so nobody "
            "takes them - tap Book on each from /holds when you want it. "
            "The site refuses two back-to-back bookings of the same space, so "
            "expect to submit later legs yourself or from another space.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Book leg 1 + chope the rest",
                                      callback_data="bk|go")],
                _nav_row()]))
        return

    await query.edit_message_text(
        f"Book this?\n\n{head}\n\n"
        "Press Confirm and I'll tick the agreement box and submit. If the "
        "site refuses, I'll show you its exact message.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Confirm booking", callback_data="bk|go")],
            _nav_row(),
        ]))


def _ext_legs(bk) -> list[tuple[datetime, datetime]]:
    """Split the requested total into legs of the category's max length."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    _, cap = libcal.category_limits(cat.lid, cat.gid)
    legs, t = [], bk["start"]
    while t < bk["end"]:
        legs.append((t, min(t + timedelta(minutes=cap), bk["end"])))
        t += timedelta(minutes=cap)
    return legs


WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _rule_days_text(weekdays) -> str:
    days = [WEEKDAY_NAMES[d] for d in sorted(weekdays)]
    if len(days) == 1:
        return days[0]
    return " & ".join([", ".join(days[:-1]), days[-1]])


def _occurrences(lid: int, gid: int, weekdays, until, start: dtime | None = None,
                 limit: int | None = None) -> list[date]:
    """Every day this rule would book.

    Days the library is shut are skipped, and so is today once the session's
    own start time has gone by - counting a slot that already began would
    promise a booking the scheduler is right to refuse.
    """
    out = []
    now = datetime.now()
    day = date.today()
    while day <= until:
        if day.weekday() in weekdays and catalog.is_open(lid, gid, day):
            if start is None or datetime.combine(day, start) > now:
                out.append(day)
                if limit and len(out) >= limit:
                    break
        day += timedelta(days=1)
    return out


async def _r_days(query, context, bk):
    """Which weekdays - the recurring answer to 'which day'."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    chosen = set(bk.get("weekdays") or [])
    items = []
    for n, name in enumerate(WEEKDAY_NAMES):
        if not catalog.is_open(cat.lid, cat.gid, _next_weekday(n)):
            continue                       # Sunday: never offered
        items.append((f"• {name} •" if n in chosen else name, f"bk|wd|{n}"))
    rows = list(items)
    if chosen:
        rows.append((f"Done - {_rule_days_text(chosen)}", "bk|wdone"))
    await query.edit_message_text(
        f"{cat.label} - which days, every week?\n"
        "(Tap each day you want, then Done. Sundays are not offered - the "
        "library is closed.)",
        reply_markup=_kb(rows, per_row=4, bk=bk))


def _after_space(bk) -> str:
    """Where the flow goes once a space is chosen."""
    return {"sched": "fire", "recur": "until"}.get(bk["mode"], "confirm")


def _next_weekday(n: int) -> date:
    """The next date falling on weekday n, for opening-hours questions."""
    today = date.today()
    return today + timedelta(days=(n - today.weekday()) % 7)


async def _r_until(query, context, bk):
    """How long to keep repeating."""
    cat = bk["locations"][bk["loc"]].categories[bk["cat"]]
    today = date.today()
    presets = [(4, "4 weeks"), (8, "8 weeks"), (13, "13 weeks - a semester")]
    items = [(f"{label} (until {today + timedelta(weeks=w):%d %b})",
              f"bk|until|{w}") for w, label in presets]
    items.append(("Custom - I'll type the date", "bk|until|custom"))
    await query.edit_message_text(
        f"{cat.label}, {_rule_days_text(bk['weekdays'])} "
        f"{bk['start']:%H:%M}-{bk['end']:%H:%M}\n\n"
        "Until when should I keep booking this?\n"
        f"(You can stop it any time with /recurring. Longest is "
        f"{config.RECUR_MAX_WEEKS} weeks.)",
        reply_markup=_kb(items, per_row=1, bk=bk))


async def _r_rconfirm(query, context, bk):
    """The last screen: exactly what will be booked, and when I will try."""
    loc = bk["locations"][bk["loc"]]
    cat = loc.categories[bk["cat"]]
    until = bk["until"]
    days = _occurrences(cat.lid, cat.gid, bk["weekdays"], until,
                        bk["start"].time())
    preview = []
    for day in days[:3]:
        fire = catalog.window_opens_at(cat.lid, cat.gid, day)
        preview.append(f"  {day:%a %d %b}   I try at {fire:%a %d %b %H:%M}")
    warn = rules.precheck(query.from_user.id, bk.get("room"),
                          datetime.combine(days[0], bk["start"].time()),
                          datetime.combine(days[0], bk["end"].time())) if days else []
    await query.edit_message_text(
        f"Repeat this booking?\n\n"
        f"{loc.name} - {cat.label}\n"
        f"{_chosen_space_text(bk)}\n"
        f"{_rule_days_text(bk['weekdays'])}, "
        f"{bk['start']:%H:%M} - {bk['end']:%H:%M}\n"
        f"Until {until:%a %d %b} ({len(days)} bookings)\n\n"
        + ("First three:\n" + "\n".join(preview) + "\n\n" if preview else "")
        + ("⚠ " + "\n⚠ ".join(warn) + "\n\n" if warn else "")
        + "I set each one up as its booking window comes near, then race for "
        "it like any scheduled booking. You get a message either way, and "
        "/recurring stops the lot.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Repeat it", callback_data="bk|go")],
            _nav_row(),
        ]))


async def _r_sconfirm(query, context, bk):
    loc = bk["locations"][bk["loc"]]
    cat = loc.categories[bk["cat"]]
    await query.edit_message_text(
        f"Schedule this booking?\n\n"
        f"{loc.name} - {cat.label}\n"
        f"{_chosen_space_text(bk)}\n"
        f"{bk['start']:%a %d %b, %H:%M} - {bk['end']:%H:%M}\n"
        f"Fires at: {bk['fire_at']:%a %d %b %H:%M}\n\n"
        f"{_account_lines(query.from_user.id)}\n\n"
        "The PC running the bot must be on at fire time. I'll DM you the result.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Schedule it", callback_data="bk|go")],
            _nav_row(),
        ]))


# --- Executing ------------------------------------------------------------

async def _bk_go(query, context, user_id: int):
    bk = context.user_data.get("bk") or {}
    if "start" not in bk:
        await query.edit_message_text("Session lost - run /book again.")
        return
    loc = bk["locations"][bk["loc"]]
    cat = loc.categories[bk["cat"]]
    context.user_data["bk_last"] = {"category": cat.label, "loc": bk["loc"], "cat": bk["cat"]}

    if bk["mode"] == "recur":
        mine = storage.list_rules(user_id)
        if len(mine) >= config.MAX_RULES:
            await query.edit_message_text(
                f"You already have {len(mine)} repeating bookings, which is "
                f"the limit ({config.MAX_RULES}). /recurring deletes one.")
            return
        rule_id = storage.add_rule(
            user_id, cat.lid, cat.gid, loc.name, cat.label, bk.get("room"),
            bk["weekdays"], f"{bk['start']:%H:%M}", f"{bk['end']:%H:%M}",
            bk["until"])
        days = _occurrences(cat.lid, cat.gid, bk["weekdays"], bk["until"],
                            bk["start"].time())
        flows.finish(context, flows.LIBRARY)
        await query.edit_message_text(
            f"Repeating (#{rule_id}): {_rule_days_text(bk['weekdays'])} "
            f"{bk['start']:%H:%M}-{bk['end']:%H:%M}, {len(days)} bookings up to "
            f"{bk['until']:%d %b}.\n\n"
            "I set each one up as its window comes near and tell you how it "
            "went. /recurring stops it, /scheduled shows what is queued.")
        return

    if bk["mode"] == "sched":
        fire = bk["fire_at"]
        job_id = storage.add_scheduled(
            user_id, cat.lid, cat.gid, loc.name, cat.label,
            bk.get("room") or None, bk["start"], bk["end"], fire,
            fire + timedelta(minutes=config.SCHED_RETRY_MINUTES))
        await query.edit_message_text(
            f"Scheduled (#{job_id}). I'll try at {fire:%a %d %b %H:%M} and DM you. "
            "/scheduled shows or cancels pending jobs.")
        return

    profile = _profile(user_id)
    if not profile:
        await query.edit_message_text("Run /setup first.")
        return

    if bk["mode"] in ("chope", "ext"):
        await _run_hold_plan(query, context, bk, user_id, profile, loc, cat)
        return

    # Last freshness gate before spending a minute in the browser.
    grid = _restrict(await _refresh_grid(bk), bk.get("only_item"))
    free = libcal.spaces_free_for(grid, bk["start"], bk["end"])
    room_id = bk.get("room") or (bk.get("free_spaces") or [0])[0]
    swapped = None
    if room_id not in free:
        if free:
            swapped, room_id = room_id, free[0]
        else:
            await query.edit_message_text(
                "That slot was taken in the last few seconds - nothing free "
                "for your period any more. /book to pick again.")
            return

    checksum = next((c.checksum for c in grid.get(room_id, [])
                     if c.start == bk["start"] and c.state == libcal.FREE), None)
    if checksum is None:
        await query.edit_message_text(
            "That start just became unavailable - /book to pick again.")
        return
    await query.edit_message_text(
        "Booking now - driving the library site, this can take up to a minute...")
    email_override = None
    if config.USE_BOT_EMAIL_ON_FORM:
        email_override = await botmail.ensure_inbox()
    result = await asyncio.to_thread(
        browser.book, user_id, profile["username"], profile["password"], profile,
        cat.lid, cat.gid, room_id, bk["start"], bk["end"], checksum,
        email_override)
    flows.finish(context, flows.LIBRARY)      # the outcome message stays put
    prefix = (f"(Heads-up: {_space_label(swapped)} got taken, I booked "
              f"{_space_label(room_id)} instead.)\n\n") if swapped and result.ok else ""
    ok = await _report_booking(query.edit_message_text, context, user_id, loc.name,
                               cat.label, cat.lid, cat.gid, room_id,
                               bk["start"], bk["end"], result, prefix=prefix)

    # /move: the new booking is in - now drop the old one.
    move_from = bk.get("move_from")
    if ok and move_from:
        old = storage.get_booking(move_from)
        if old:
            note = await _cancel_any(user_id, old)
            await context.bot.send_message(user_id, f"Old booking: {note}")


def _holds_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    mine = holds.for_user(user_id)
    if not mine:
        return None
    rows = []
    for h in mine:
        rows.append([
            InlineKeyboardButton(f"✅ Book #{h.id} {h.start:%H:%M}",
                                 callback_data=f"bk|hbook|{h.id}"),
            InlineKeyboardButton("⏱ Keep holding", callback_data=f"bk|hext|{h.id}"),
            InlineKeyboardButton("✕ Release", callback_data=f"bk|hrel|{h.id}"),
        ])
    gone = _restorable(user_id)
    if gone:
        rows.append([InlineKeyboardButton(
            f"↩ Take back {len(gone)} released", callback_data="bk|hallback")])
    rows.append([InlineKeyboardButton("⏱ How long chopes last",
                                      callback_data="bk|ht|ask")])
    return InlineKeyboardMarkup(rows)


async def _run_hold_plan(query, context, bk, user_id, profile, loc, cat):
    """/chope: hold one slot. /extendedbooking: book leg 1, hold the rest."""
    if bk["mode"] == "ext" and bk.get("plan"):
        # A desk-hopping plan already says which space each leg belongs to.
        legs = [(seg_start, seg_end) for _, seg_start, seg_end in bk["plan"]]
        leg_rooms = [item_id for item_id, _, _ in bk["plan"]]
    else:
        legs = _ext_legs(bk) if bk["mode"] == "ext" else [(bk["start"], bk["end"])]
        leg_rooms = [bk.get("room")] * len(legs)
    await query.edit_message_text(
        "Working on it - opening the library site" +
        (f" for {len(legs)} legs" if len(legs) > 1 else "") + "...")
    lines: list[str] = []

    for i, (start, end) in enumerate(legs, 1):
        grid = await libcal.fetch_grid(cat.lid, cat.gid, start.date())
        free = libcal.spaces_free_for(grid, start, end)
        wanted = leg_rooms[i - 1]
        room = wanted if wanted in free else (free[0] if free else None)
        if room is None:
            lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: no space free any more")
            continue
        checksum = next((c.checksum for c in grid.get(room, [])
                         if c.start == start and c.state == libcal.FREE), None)
        if checksum is None:
            lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: start just taken")
            continue

        if bk["mode"] == "ext" and i == 1:
            result = await asyncio.to_thread(
                browser.book, user_id, profile["username"], profile["password"],
                profile, cat.lid, cat.gid, room, start, end, checksum)
            if result.ok:
                if result.email_used:
                    storage.save_user(user_id, email=result.email_used)
                bid = storage.add_booking(user_id, loc.name, cat.label,
                                          libcal.room_name(room), room, start, end,
                                          result.reference, lid=cat.lid, gid=cat.gid)
                tasks.spawn(_poll_botmail(context, user_id, bid), bot=context.bot,
                            user_id=user_id, feature="watching the bot inbox",
                            notify="I stopped watching my inbox for your "
                                   "check-in code - paste the confirmation "
                                   "email here instead.")
                lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: BOOKED "
                             f"({_space_label(room)})")
            else:
                rules.record_refusal(cat.label, result.message)
                lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: refused - "
                             f"{result.message[:120]}")
            continue

        try:
            hold = await holds.create(
                user_id, profile["username"], profile["password"], cat.lid, cat.gid,
                room, start, end, checksum, loc.name, cat.label,
                note=f"leg {i} of {len(legs)}" if len(legs) > 1 else "")
            lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: CHOPED "
                         f"({_space_label(room)}, hold #{hold.id}, "
                         f"until {hold.expires_at:%H:%M})"
                         if len(legs) > 1 else
                         f"Choped {_space_label(room)} {start:%H:%M}-{end:%H:%M} "
                         f"as hold #{hold.id}, held until {hold.expires_at:%H:%M}")
        except Exception as e:
            lines.append(f"leg {i} {start:%H:%M}-{end:%H:%M}: hold failed - "
                         f"{str(e)[:120]}")

    context.user_data["bk_last"] = {"category": cat.label, "loc": bk["loc"],
                                    "cat": bk["cat"]}
    flows.finish(context, flows.LIBRARY)      # a result, not a live screen
    tail = ("\n\nHolds renew themselves until you book or release them "
            f"(max {config.HOLD_MAX_MINUTES} min). /holds anytime.")
    await query.edit_message_text("\n".join(lines) + tail,
                                  reply_markup=_holds_keyboard(user_id))


def _restorable(user_id: int) -> list:
    """Slots released recently whose time has not passed."""
    live = {(h.item_id, h.start) for h in holds.for_user(user_id)}
    return [r for r in storage.released_holds(user_id)
            if (r["item_id"],
                datetime.strptime(r["start_ts"], storage.FMT)) not in live]


async def cmd_holds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    if not storage.is_developer(update.effective_user.id):
        await update.effective_message.reply_text(
            "Holds are a developer feature - /bookings shows your bookings.")
        return
    user_id = update.effective_user.id
    mine = holds.for_user(user_id)
    if not mine:
        gone = _restorable(user_id)
        if gone:
            rows = [[InlineKeyboardButton(
                f"↩ Take back all {len(gone)} released slot(s)",
                callback_data="bk|hallback")]]
            for r in gone[:6]:
                label = r["label"].split(" (")[0]
                rows.append([InlineKeyboardButton(
                    f"↩ {label} {r['start_ts'][-5:]}-{r['end_ts'][-5:]}",
                    callback_data=f"bk|hagain|{r['id']}")])
            await flows.start(
                update, context, flows.LIBRARY,
                "You aren't holding anything now, but these were released "
                "recently and their time hasn't passed:",
                reply_markup=InlineKeyboardMarkup(rows))
            return
        await update.effective_message.reply_text(
            "You aren't holding anything. /chope holds a space without "
            "booking it; /extendedbooking books a first leg and chopes the rest.")
        return
    await flows.start(
        update, context, flows.LIBRARY,
        "Your holds:\n" + "\n".join(h.describe() for h in mine),
        reply_markup=_holds_keyboard(update.effective_user.id))


async def _hold_action(query, context, user_id: int, hold_id: int, book_it: bool):
    hold = holds.get(hold_id)
    if not hold or hold.user_id != user_id:
        await query.edit_message_text("That hold is gone (booked, released or expired).")
        return
    flows.finish(context, flows.LIBRARY)
    if book_it:
        await query.edit_message_text(f"Booking hold #{hold_id}...")
        ok, msg = await holds.book(hold)
        if ok:
            await query.edit_message_text(
                f"Booked {hold.label} {hold.start:%a %d %b %H:%M}-{hold.end:%H:%M}.\n\n"
                f"{msg[:200]}\n\nPaste the confirmation email here (or /code) "
                "and I'll auto check-in for you.")
        else:
            rules.record_refusal(hold.category, msg)
            await query.edit_message_text(f"The site refused it:\n\n{msg[:500]}")
    else:
        await holds.release(hold)
        await query.edit_message_text(f"Released hold #{hold_id} ({hold.label} "
                                      f"{hold.start:%H:%M}-{hold.end:%H:%M}).")


async def _cancel_any(user_id: int, booking) -> str:
    """Release a booking any time before it ends.

    On this LibCal install cancelling IS checking out: POST /r/checkout with
    the booking email and the same code used for check-in frees the space.
    It works whether or not you checked in, so you can drop a booking you
    changed your mind about or leave a session early. The cancellation link
    from the confirmation email is kept as a fallback.
    """
    now = datetime.now()
    end_at = datetime.strptime(booking["end_ts"], storage.FMT)
    if now > end_at:
        return "that booking has already ended - nothing to release."

    row = storage.get_user(user_id)
    email_addr = row["email"] if row else None
    tried = []

    if booking["checkin_code"] and email_addr:
        ok, msg = await libcal.checkout(email_addr, booking["checkin_code"])
        if ok:
            storage.update_booking(booking["id"], status="cancelled")
            return "released - the space is free again (" + msg[:150] + ")"
        # "This booking has already been Checked Out." means the space is
        # already free (you left early on the site, or it ended) - that is
        # the outcome we wanted, not a failure.
        if "already been checked out" in msg.lower():
            storage.update_booking(booking["id"], status="checked_out")
            return "already checked out - the space is free, nothing to do."
        tried.append("check-out said: " + msg[:150])

    if booking["cancel_link"]:
        ok, msg = await libcal.cancel_via_link(booking["cancel_link"])
        if ok:
            storage.update_booking(booking["id"], status="cancelled")
            return "cancelled via the email link (" + msg[:150] + ")"
        tried.append("the email link said: " + msg[:150])

    if not booking["checkin_code"]:
        tried.append("I do not have this booking" + chr(39) + "s code yet")
    if not email_addr:
        tried.append("I do not know your email (/email you@e.ntu.edu.sg)")

    return ("I could not release it. " + " | ".join(tried) +
            ". Cancelling here means checking out, so send me its code "
            "(/code ABC123) and I will try again - or paste the "
            "cancellation link from the confirmation email.")

async def _report_booking(send, context, user_id, loc_name, cat_label, lid, gid,
                          room_id, start, end, result: browser.BookingResult,
                          prefix: str = "") -> bool:
    if result.ok:
        if result.email_used:
            storage.save_user(user_id, email=result.email_used)
        booking_id = storage.add_booking(
            user_id, loc_name, cat_label, libcal.room_name(room_id),
            room_id, start, end, result.reference, lid=lid, gid=gid)
        msg = (f"{prefix}Booked: {_space_label(room_id)}, "
               f"{start:%a %d %b %H:%M}-{end:%H:%M}.\n\n"
               f"Site said: {result.message[:200]}\n\n"
               "Forward or paste the confirmation email here (or /code ABC123) "
               "and I'll auto check-in for you at start time.")
        tasks.spawn(_poll_botmail(context, user_id, booking_id), bot=context.bot,
                    user_id=user_id, feature="watching the bot inbox",
                    notify="I stopped watching my inbox for your check-in "
                           "code - paste the confirmation email here instead.")
        await send(msg, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("★ Save as favourite",
                                 callback_data=f"bk|favadd|{booking_id}")]]))
        return True
    rules.record_refusal(cat_label, result.message)
    note = f"The site refused the booking:\n\n{result.message[:600]}"
    if result.window_not_open:
        note += "\n\nThis category's window isn't open yet - /schedulebook can fire it when it opens."
    if result.debug_files:
        note += f"\n\n(Diagnostics saved to {config.DEBUG_DIR})"
    await send(note)
    return False


async def _poll_botmail(context, user_id: int, booking_id: int):
    """Watch the bot's own inbox for the confirmation email."""
    deadline = datetime.now() + timedelta(minutes=config.EMAIL_POLL_MINUTES)
    while datetime.now() < deadline:
        for message in await botmail.fetch_messages():
            found = emailcode.parse_text(message.get("text", ""))
            if found["code"] or found["cancel_link"]:
                fields = {k: v for k, v in (
                    ("checkin_code", found["code"]),
                    ("cancel_link", found["cancel_link"]),
                    ("booking_ref", found["reference"])) if v}
                storage.update_booking(booking_id, **fields)
                text = "Got the confirmation email from my inbox."
                if found["code"]:
                    text += f" Check-in code: {found['code']} - /checkin when you arrive."
                await context.bot.send_message(user_id, text)
                return
        await asyncio.sleep(30)


# --- /botemail, /code, /email, /bookings, /checkin, /cancelbooking --------

async def cmd_botemail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    address = await botmail.ensure_inbox()
    if not address:
        await update.effective_message.reply_text(
            "Couldn't create the bot inbox right now - paste the confirmation "
            "email text here instead, that always works.")
        return
    await update.effective_message.reply_text(
        f"My inbox address:\n{address}\n\n"
        "One-time setup in NTU webmail (outlook.office.com):\n"
        "Settings > Mail > Rules > Add new rule\n"
        "- Condition: From contains the library's sender address\n"
        f"- Action: Forward to {address}\n\n"
        "After that I read check-in codes and cancel links automatically. "
        "Pasting the email text here always works too.")


async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    if not context.args or "@" not in context.args[0]:
        row = storage.get_user(update.effective_user.id)
        known = row["email"] if row else None
        await ask.ask(update, context, "email",
                      "Which email are your bookings under?"
                      + (f"\n(I currently use {known})" if known else ""),
                      suggestion=known, suggestion_label=f"Keep {known}" if known else None)
        return
    await _save_email(update, context.args[0].strip())


async def _save_email(update, address: str) -> None:
    storage.save_user(update.effective_user.id, email=address.strip())
    await update.effective_message.reply_text(
        f"Email saved: {address.strip()} (this machine only).")


async def answer_email(update, context, answer: str, pending: dict) -> None:
    if "@" not in answer:
        await update.effective_message.reply_text(
            "That doesn't look like an email - try /email again.")
        return
    await _save_email(update, answer)


def _pending_booking(user_id: int):
    """Only safe when there is exactly one candidate - otherwise ask."""
    rows = storage.list_bookings(user_id)
    return rows[0] if len(rows) == 1 else None


def _latest_booking(user_id: int):
    rows = storage.list_bookings(user_id)
    return rows[0] if rows else None


def _booking_button_label(row) -> str:
    code = row["checkin_code"]
    mark = f" [{code}]" if code else ""
    return f"{row['room_name'].split(' (')[0]} {row['start_ts'][5:]}{mark}"


def _apply_capture(booking_id: int, fields: dict) -> None:
    """Attach a code / cancel link, and let auto check-in start over so a
    corrected code is actually retried."""
    storage.update_booking(booking_id, checkin_attempts=0, checkin_nagged=0,
                           **fields)


def _describe_capture(fields: dict) -> str:
    bits = []
    if fields.get("checkin_code"):
        bits.append(f"code {fields['checkin_code']}")
    if fields.get("cancel_link"):
        bits.append("cancellation link")
    return " and ".join(bits) or "details"


async def _ask_which_booking(message, context, fields: dict, rows) -> None:
    """More than one booking could own this code - let the user say which."""
    context.user_data["pending_capture"] = fields
    kb = [[InlineKeyboardButton(_booking_button_label(r),
                                callback_data=f"bk|setcode|{r['id']}")]
          for r in rows]
    await message.reply_text(
        f"Which booking is {_describe_capture(fields)} for?",
        reply_markup=InlineKeyboardMarkup(kb))


async def _proof_only(bot, user_id: int, booking, code: str) -> None:
    """Photograph a check-in that has already happened."""
    row = storage.get_user(user_id)
    email = row["email"] if row else None
    if not email:
        return
    ok, _msg, path = await browser.checkin_now(email, code, str(booking["id"]))
    if path and ok:
        await _send_checkin_proof(bot, user_id, booking, path)
    elif path:
        try:
            os.remove(path)
        except OSError:
            pass


def _space_matches(row, space: str) -> bool:
    """Same desk, ignoring the '(Capacity 1)' tail and case."""
    def bare(name):
        return (name or "").split(" (")[0].strip().lower()
    return bool(space) and bare(row["room_name"]) == bare(space)


async def _file_code(send, context, user_id: int, code: str, found: dict,
                     match) -> None:
    """Save the code against `match`, or as a new booking when it is None.

    Only what the site actually said is written. It often knows nothing but
    the start time - the refusal before a check-in window opens names that and
    nothing else - and overwriting a real desk name with "your booking" and a
    real end time with a guess loses information the bot already had.
    """
    start, end = found["start"], found["end"]
    if match is None:
        booking_id = storage.add_booking(
            user_id, found["location"] or "(booked by you)", "your own booking",
            found["space"] or "your booking", None,
            start, end or (start + timedelta(hours=2)))
        lead = "I didn't know about this booking, so I've added it"
    else:
        booking_id = match["id"]
        fields = {}
        if found["space"]:
            fields["room_name"] = found["space"]
        if found["location"]:
            fields["location"] = found["location"]
        # Times are only rewritten when the site gave a complete, sane pair -
        # which happens on a successful check-in. Writing just the start would
        # take a booking you picked yourself and leave it ending before it
        # begins, and the local times came from a real confirmation anyway.
        if end and end > start:
            fields["start_ts"] = start.strftime(storage.FMT)
            fields["end_ts"] = end.strftime(storage.FMT)
        if fields:
            storage.update_booking(booking_id, **fields)
        lead = "Matched your booking"

    _apply_capture(booking_id, {"checkin_code": code})
    booking = storage.get_booking(booking_id)
    shown_start = datetime.strptime(booking["start_ts"], storage.FMT)
    shown_end = datetime.strptime(booking["end_ts"], storage.FMT)
    details = (f"{booking['room_name']}\n{booking['location']}\n"
               f"{shown_start:%a %d %b, %H:%M} - {shown_end:%H:%M}")

    if found["checked_in"]:
        storage.update_booking(booking_id, status="checked_in")
        tasks.spawn(_proof_only(context.bot, user_id, booking, code),
                    bot=context.bot, user_id=user_id,
                    feature="the check-in photo", notify=None)
        await send(f"✅ Checked in.\n\n{details}\n\n"
                   f"Code {code} saved. Check out with /cancelbooking when you "
                   f"leave - the space is yours until {shown_end:%H:%M}.")
    else:
        await send(f"{lead}:\n\n{details}\n\nCode {code} saved. I'll check you "
                   "in automatically from 2 minutes before it starts.")


async def _ask_which_booking_for_code(message, context, code: str, found: dict,
                                      candidates) -> None:
    """The site gave a time but no desk, so it cannot say which booking this
    is. Offer the ones it could be, and the option of a separate booking."""
    context.user_data["code_pending"] = {"code": code, "found": found}
    start = found["start"]
    # Closest to the time the site gave first: that is almost always the one.
    ordered = sorted(candidates, key=lambda r: abs(
        (datetime.strptime(r["start_ts"], storage.FMT) - start).total_seconds()))
    kb = []
    for r in ordered:
        taken = " - already has a code" if r["checkin_code"] else ""
        kb.append([InlineKeyboardButton(
            f"{r['room_name'].split(' (')[0]} {r['start_ts'][-5:]}"
            f"-{r['end_ts'][-5:]}{taken}",
            callback_data=f"bk|codeto|{r['id']}")])
    kb.append([InlineKeyboardButton("A separate booking I made myself",
                                    callback_data="bk|codenew")])
    await message.reply_text(
        f"Code {code} is for a booking starting {start:%H:%M} on "
        f"{start:%a %d %b}, but the library did not say which space.\n\n"
        "Is it one of these, or a booking of its own?",
        reply_markup=InlineKeyboardMarkup(kb))


async def _attach_code(update, context, code: str, rows) -> bool:
    """Ask the site what this code is for, and file it correctly.

    A check-in that succeeds comes back with the whole booking - space,
    library and both times - so the reply describes the real booking instead
    of a generic "code saved", and the record is corrected to match.
    """
    user_id = update.effective_user.id
    row = storage.get_user(user_id)
    email = row["email"] if row else None
    if not email:
        return False                       # fall back to the old behaviour

    try:
        found = await libcal.probe_code(email, code)
    except Exception as exc:
        log.warning("probe_code failed: %s", exc)
        storage.record_error("looking up a check-in code", str(exc))
        return False

    if not found["known"]:
        await update.effective_message.reply_text(
            f"Wrong code - the library has no booking for {code}.\n\n"
            "Check it against your confirmation email. I have not saved it and "
            "I will not retry: a code the site does not know will not start "
            "working later.\n\n"
            "(The same answer comes back for a booking that is not live on the "
            "site yet, so if you have just made it, give it a minute.)")
        return True

    if found["finished"]:
        await update.effective_message.reply_text(
            f"{code} is a real code, but that booking is already checked out - "
            "it is over, so there is nothing to check in to.")
        return True

    start = found["start"]
    if not start:
        return False                       # knew the code but not the time

    # Anything of yours that day could be the one - the site's own answer
    # decides when it can, and you decide when it cannot.
    candidates = [b for b in rows
                  if datetime.strptime(b["start_ts"], storage.FMT).date()
                  == start.date()]
    send = update.effective_message.reply_text

    if found["space"]:
        # The site named the desk, so there is nothing to guess.
        match = next((b for b in candidates
                      if _space_matches(b, found["space"])), None)
        if match is None:
            match = next((b for b in candidates
                          if abs((datetime.strptime(b["start_ts"], storage.FMT)
                                  - start).total_seconds()) <= 15 * 60), None)
        await _file_code(send, context, user_id, code, found, match)
        return True

    if candidates:
        await _ask_which_booking_for_code(update.effective_message, context,
                                          code, found, candidates)
        return True

    await _file_code(send, context, user_id, code, found, None)
    return True

async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/code            - show each booking and its code
    /code ABC123     - attach it (asks which booking when there's more than one)
    /code ABC123 4   - attach it to booking #4 straight away
    """
    if not await _require_private(update):
        return
    user_id = update.effective_user.id
    rows = storage.list_bookings(user_id)
    if not context.args:
        if not rows:
            await update.effective_message.reply_text("No upcoming bookings.")
            return
        lines = [f"#{r['id']} {r['room_name']} {r['start_ts']} - "
                 f"code {r['checkin_code'] or 'not set yet'}" for r in rows]
        await update.effective_message.reply_text(
            "Your upcoming bookings:\n" + "\n".join(lines) +
            "\n\nSend /code ABC123 to attach a code - I'll ask which "
            "booking it belongs to. Sending a new code for a booking replaces "
            "the old one, so a wrong code is easy to fix.")
        return

    code = context.args[0].upper()
    fields = {"checkin_code": code}

    # The code itself tells us which booking it belongs to - including
    # bookings made on the website that the bot has never seen.
    if not (len(context.args) > 1 and context.args[1].lstrip("#").isdigit()):
        if await _attach_code(update, context, code, rows):
            return

    if not rows:
        await update.effective_message.reply_text(
            "I couldn't match that code to a booking, and I don't know of any "
            "active ones. Book first, or send the confirmation email.")
        return

    if len(context.args) > 1 and context.args[1].lstrip("#").isdigit():
        target = storage.get_booking(int(context.args[1].lstrip("#")))
        if not target or target["user_id"] != user_id:
            await update.effective_message.reply_text("I don't have a booking with that number.")
            return
        _apply_capture(target["id"], fields)
        await update.effective_message.reply_text(
            f"Code {code} saved for {target['room_name']} {target['start_ts']}.")
        return

    if len(rows) == 1:
        _apply_capture(rows[0]["id"], fields)
        await update.effective_message.reply_text(
            f"Code {code} saved for {rows[0]['room_name']} {rows[0]['start_ts']}. "
            "Use /checkin when you arrive.")
        return

    await _ask_which_booking(update.message, context, fields, rows)


NO_LISTING_HINT = (
    "The library has no page that lists your bookings, so I only know the "
    "ones I made or that you told me about. Booked on the website? Send me "
    "the code - /checkin ABC123 14:30 - or just paste the confirmation "
    "email here, and I will look it up and manage it from then on.")


async def _live_bookings(user_id: int) -> tuple[list, list]:
    """Active bookings, checked against the site rather than trusted.

    Returns (still there, disappeared). Anything the site says is free again
    was cancelled somewhere else, so the record is corrected here instead of
    being offered as something to cancel or move.
    """
    rows = storage.list_bookings(user_id)
    if not rows:
        return [], []
    checks = await asyncio.gather(*[
        libcal.confirm_booking(
            r["lid"], r["gid"], r["item_id"],
            datetime.strptime(r["start_ts"], storage.FMT),
            datetime.strptime(r["end_ts"], storage.FMT))
        for r in rows], return_exceptions=True)
    live, gone = [], []
    for row, verdict in zip(rows, checks):
        if verdict == "gone":
            storage.update_booking(row["id"], status="cancelled")
            gone.append(row)
        else:                       # 'held', 'unknown', or the check failed
            live.append(row)
    return live, gone


def _gone_note(gone) -> str:
    if not gone:
        return ""
    which = ", ".join(f"{r['room_name']} {r['start_ts'][-5:]}" for r in gone)
    return (f"\n\n(The library no longer has {which} - cancelled elsewhere, "
            "so I have removed it.)")


async def cmd_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    live, gone = await _live_bookings(user_id)          # correct the record first
    live_ids = {r["id"] for r in live}
    rows = storage.list_bookings(user_id, active_only=False)[-10:]
    if not rows:
        await update.effective_message.reply_text(
            "No bookings recorded yet. Try /book.\n\n" + NO_LISTING_HINT)
        return
    lines = []
    for r in rows:
        code = f" - code {r['checkin_code']}" if r["checkin_code"] else " - no code yet"
        mark = " - confirmed on the site" if r["id"] in live_ids else ""
        lines.append(f"#{r['id']} {r['room_name']} ({r['location']}) "
                     f"{r['start_ts']} to {r['end_ts'][-5:]} [{r['status']}]{code}{mark}")
    await update.effective_message.reply_text("\n".join(lines) + _gone_note(gone))


async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    rows = storage.list_scheduled(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No scheduled bookings. /schedulebook creates one.")
        return
    kb = [[InlineKeyboardButton(
        f"Cancel #{r['id']} {r['category']} {r['start_ts']} (fires {r['fire_at'][-11:]})",
        callback_data=f"bk|scancel|{r['id']}")] for r in rows]
    await flows.start(update, context, flows.LIBRARY,
                      "Pending scheduled bookings - tap to cancel one:",
                      reply_markup=InlineKeyboardMarkup(kb))


async def _pick_booking(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        action: str, verb: str) -> None:
    rows, gone = await _live_bookings(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text(
            ("Nothing to " + verb + " - the library no longer has any of the "
             "bookings I knew about." if gone else "No active bookings.")
            + "\n\n" + NO_LISTING_HINT)
        return
    kb = [[InlineKeyboardButton(f"{r['room_name']} {r['start_ts']}",
                                callback_data=f"bk|{action}|{r['id']}")] for r in rows]
    await flows.start(update, context, flows.LIBRARY,
                      f"Which booking do you want to {verb}?" + _gone_note(gone),
                      reply_markup=InlineKeyboardMarkup(kb))


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Codes and checking in are one thing, so this is one command.

    /checkin                  - your bookings, their codes, and buttons
    /checkin ABC123           - save the code and check in if the window is
                                open (I ask which booking when several fit)
    /checkin ABC123 14:30     - a booking you made on the website yourself
    /checkin ABC123 14:30 3/9 - ... on another day
    """
    if not await _require_private(update):
        return
    user_id = update.effective_user.id
    args = context.args or []
    rows = storage.list_bookings(user_id)

    if not args:
        if not rows:
            await update.effective_message.reply_text(
                "No upcoming bookings. /book makes one, or send me the code of "
                "a booking you made yourself: /checkin ABC123 14:30")
            return
        lines = [f"#{r['id']} {r['room_name']} {r['start_ts']} - "
                 f"code {r['checkin_code'] or 'not saved yet'}" for r in rows]
        kb = [[InlineKeyboardButton(f"Check in: {_booking_button_label(r)}",
                                    callback_data=f"bk|ci|{r['id']}")]
              for r in rows if r["checkin_code"]]
        await update.effective_message.reply_text(
            "Your bookings:\n" + "\n".join(lines) +
            "\n\nSend /checkin ABC123 to save a code (I'll ask which booking "
            "when there's more than one). Sending it again replaces a wrong one.",
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
        return

    code = args[0].upper()
    row = storage.get_user(user_id)
    email_addr = row["email"] if row else None
    if not email_addr:
        await update.effective_message.reply_text(
            "I don't know the email your bookings use yet - it's captured on "
            "your first booking, or set it with /email you@e.ntu.edu.sg")
        return

    # A time means "this is my own booking from the website".
    if len(args) > 1:
        try:
            t = datetime.strptime(args[1], "%H:%M").time()
        except ValueError:
            await update.effective_message.reply_text(
                "Time should look like 14:30 - e.g. /checkin ABC123 14:30")
            return
        day = date.today()
        if len(args) > 2:
            try:
                d, m = args[2].split("/")[:2]
                day = date(day.year, int(m), int(d))
            except Exception:
                await update.effective_message.reply_text("Date should look like 3/9.")
                return
        start = datetime.combine(day, t)
        existing = next((b for b in rows
                         if b["start_ts"] == start.strftime(storage.FMT)), None)
        if existing:
            _apply_capture(existing["id"], {"checkin_code": code})
            where = existing["room_name"]
        else:
            booking_id = storage.add_booking(
                user_id, "(booked by you)", "your own booking", "your booking",
                None, start, start + timedelta(hours=2))
            _apply_capture(booking_id, {"checkin_code": code})
            where = "your booking"
        await update.effective_message.reply_text(
            f"Got it - {where}, {start:%a %d %b %H:%M}, code {code}.\n"
            f"I'll check in at {start - timedelta(minutes=2):%H:%M}, retrying at "
            f"{start:%H:%M} and {start + timedelta(minutes=5):%H:%M} "
            f"(the window shuts at {start + timedelta(minutes=15):%H:%M}).")
        return

    # Just a code: ask the site what it belongs to BEFORE saving anything.
    # Saving first and guessing "too early?" was wrong twice over - it filed a
    # rejected code against an unrelated booking, and promised retries for a
    # code the site had just said it did not know. The site names the space
    # and the times when the code is real, so let it do the talking.
    if await _attach_code(update, context, code, rows):
        return

    # The site knew the code but told us nothing useful about it - fall back.
    if len(rows) > 1:
        await _ask_which_booking(update.effective_message, context,
                                 {"checkin_code": code}, rows)
        return
    if rows:
        _apply_capture(rows[0]["id"], {"checkin_code": code})
        ok, msg = await checkin_booking(context.bot, update.effective_user.id,
                                        rows[0], code)
    else:
        ok, msg = await libcal.checkin(email_addr, code)
    text = ("Checked in. " if ok else "Saved the code, but check-in failed: ") + msg
    if not ok:
        text += ("\n\nIf the booking has not started, I will check you in "
                 "automatically from 2 minutes before it does.")
    await update.effective_message.reply_text(text)


async def cmd_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    await _pick_booking(update, context, "cx", "cancel or end early")


async def _send_checkin_proof(bot, user_id: int, booking, path: str) -> None:
    """Send the photo of the check-in and remember it, so it can be erased
    when the booking ends."""
    start = datetime.strptime(booking["start_ts"], storage.FMT)
    end = datetime.strptime(booking["end_ts"], storage.FMT)
    try:
        with open(path, "rb") as photo:
            msg = await bot.send_photo(
                user_id, photo,
                caption=(f"Checked in: {booking['room_name']} "
                         f"{start:%a %d %b %H:%M}-{end:%H:%M}.\n"
                         f"I delete this photo and the file when the booking "
                         f"ends at {end:%H:%M}."))
        storage.update_booking(booking["id"], proof_path=path,
                               proof_chat_id=msg.chat_id,
                               proof_msg_id=msg.message_id)
    except Exception:
        log.warning("could not send the check-in photo", exc_info=True)


async def checkin_booking(bot, user_id: int, booking, code: str | None = None
                          ) -> tuple[bool, str]:
    """Check in and photograph the result.

    The browser route is preferred because it produces proof; if the page
    cannot be driven for any reason it falls back to the plain HTTP check-in,
    which is what matters most.
    """
    row = storage.get_user(user_id)
    email = row["email"] if row else None
    code = (code or booking["checkin_code"] or "").strip().upper()
    if not email or not code:
        return False, "I need both your email and the booking's code first."

    ok, message, path = await browser.checkin_now(email, code, str(booking["id"]))
    if path is None:                      # browser unavailable - do it plainly
        ok, message = await libcal.checkin(email, code)

    if ok:
        storage.update_booking(booking["id"], status="checked_in")
        if path:
            await _send_checkin_proof(bot, user_id, booking, path)
    elif path:
        try:
            os.remove(path)               # a failed attempt is not proof
        except OSError:
            pass
    return ok, message


async def purge_proofs(bot, user_id: int | None = None, rows=None) -> int:
    """Erase check-in photos - the file and the message in the chat."""
    rows = (rows if rows is not None
            else storage.bookings_with_proof(user_id) if user_id is not None
            else storage.bookings_with_expired_proof())
    gone = 0
    for b in rows:
        if b["proof_msg_id"] and b["proof_chat_id"]:
            try:
                await bot.delete_message(chat_id=b["proof_chat_id"],
                                         message_id=b["proof_msg_id"])
            except Exception:
                pass                      # older than 48 h, or already deleted
        try:
            if b["proof_path"]:
                os.remove(b["proof_path"])
        except OSError:
            pass
        storage.update_booking(b["id"], proof_path=None, proof_chat_id=None,
                               proof_msg_id=None)
        gone += 1
    return gone


async def _do_checkin(query, context, user_id: int, booking_id: int):
    booking = storage.get_booking(booking_id)
    row = storage.get_user(user_id)
    if not booking or not row or not row["email"]:
        await query.edit_message_text(
            "I don't have the email this booking was made under - set it with "
            "/email you@e.ntu.edu.sg and try again.")
        return
    if not booking["checkin_code"]:
        await query.edit_message_text(
            "No check-in code stored yet - paste the confirmation email here, "
            "or send /code ABC123.")
        return
    flows.finish(context, flows.LIBRARY)
    await query.edit_message_text("Checking you in...")
    ok, msg = await checkin_booking(context.bot, user_id, booking)
    await query.edit_message_text(("Checked in. " if ok else "Check-in failed: ") + msg)


async def _do_cancel(query, context, user_id: int, booking_id: int):
    booking = storage.get_booking(booking_id)
    if not booking:
        await query.edit_message_text("Booking not found.")
        return
    flows.finish(context, flows.LIBRARY)
    await query.edit_message_text("Releasing that booking, give me a moment...")
    note = await _cancel_any(user_id, booking)
    if "paste the cancellation link" in note:
        context.user_data["awaiting_cancel_link"] = booking_id
    await query.edit_message_text(f"{booking['room_name']} {booking['start_ts']}: {note}")


# --- /fav, /move, /rules --------------------------------------------------

def _find_cat(locations, lid: int, gid: int):
    for i, loc in enumerate(locations):
        for j, cat in enumerate(loc.categories):
            if cat.lid == lid and cat.gid == gid:
                return i, j
    return None, None


async def _flow_from_lid_gid(update, context, lid: int, gid: int,
                             extra: dict) -> bool:
    """Set up a /book-style flow state pointed at one category."""
    try:
        locations = await libcal.fetch_locations()
    except Exception:
        await update.effective_message.reply_text(
            "Couldn't reach libcalendar.ntu.edu.sg - try again later.")
        return False
    i, j = _find_cat(locations, lid, gid)
    if i is None:
        await update.effective_message.reply_text(
            "That category no longer exists on the site.")
        return False
    context.user_data["bk"] = {"mode": "now", "locations": locations,
                               "loc": i, "cat": j, "step": "day",
                               "user_id": update.effective_user.id, **extra}
    return True


async def _save_fav_here(query, context, user_id: int, bk) -> None:
    """Save whatever has been chosen so far - library+category, plus the
    space if one is picked. Time is deliberately never saved: it has to be
    chosen accurately each time."""
    if bk.get("loc") is None or bk.get("cat") is None:
        await query.answer("Pick a category first, then save.", show_alert=True)
        return
    loc = bk["locations"][bk["loc"]]
    cat = loc.categories[bk["cat"]]
    # A specific table (and time) is only worth saving for scheduled bookings,
    # where the window is usually wide open. For a normal booking you take
    # what is free, so the favourite keeps just the library and the type.
    scheduling = bk.get("mode") == "sched"
    item_id = (bk.get("room") or bk.get("only_item")) if scheduling else None
    start = bk.get("start") if scheduling else None
    end = bk.get("end") if scheduling else None
    label = (f"{_space_label(item_id)} @ {cat.label}" if item_id
             else f"{cat.label} @ {loc.name}")
    existing = [f for f in storage.list_favs(user_id)
                if f["lid"] == cat.lid and f["gid"] == cat.gid
                and (f["item_id"] or None) == (item_id or None)]
    if existing:
        await query.answer(f"{label} is already a favourite.", show_alert=True)
        return
    storage.add_fav(user_id, label, cat.lid, cat.gid, item_id or None,
                    f"{start:%H:%M}" if start else None,
                    f"{end:%H:%M}" if end else None)
    await query.answer(f"Saved: {label}. /fav books it later.", show_alert=True)


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    favs = storage.list_favs(update.effective_user.id)
    kb = []
    for f in favs:
        kb.append([InlineKeyboardButton(f"⚡ {f['label']}", callback_data=f"bk|fav|{f['id']}"),
                   InlineKeyboardButton("✕", callback_data=f"bk|favdel|{f['id']}")])
    kb.append([InlineKeyboardButton("➕ Add favourite", callback_data="bk|fadd")])
    await flows.start(
        update, context, flows.LIBRARY,
        "Favourites - tap to book (today), ✕ deletes:" if favs
        else "No favourites yet - add one (no booking needed):",
        reply_markup=InlineKeyboardMarkup(kb))


async def _fadd_libraries(query, context):
    locations = await libcal.fetch_locations()
    context.user_data["fadd"] = {"locations": locations}
    items = [(loc.name, f"bk|fal|{i}") for i, loc in enumerate(locations)]
    await query.edit_message_text(
        "New favourite - which library?",
        reply_markup=_kb(items, per_row=1, nav=False))


async def _fadd_cats(query, context, i: int):
    fa = context.user_data.get("fadd")
    if not fa:
        await query.edit_message_text("Session expired - /fav again.")
        return
    fa["loc"] = i
    loc = fa["locations"][i]
    items = [(c.label, f"bk|fac|{j}") for j, c in enumerate(loc.categories)]
    await query.edit_message_text(f"{loc.name} - which category?",
                                  reply_markup=_kb(items, per_row=1, nav=False))


async def _fadd_spaces(query, context, j: int):
    fa = context.user_data.get("fadd")
    if not fa:
        await query.edit_message_text("Session expired - /fav again.")
        return
    fa["cat"] = j
    cat = fa["locations"][fa["loc"]].categories[j]
    try:
        grid = await libcal.fetch_grid(cat.lid, cat.gid, date.today())
    except Exception:
        grid = {}
    items = [(f"Any space in {cat.label}", "bk|fas|0")]
    items += [(_space_label(i), f"bk|fas|{i}") for i in sorted(grid)]
    await query.edit_message_text(
        f"{cat.label} - favourite a specific space, or the whole category?",
        reply_markup=_kb(items, per_row=2, nav=False))


async def _fadd_save(query, context, user_id: int, item_id: int):
    fa = context.user_data.pop("fadd", None)
    if not fa:
        await query.edit_message_text("Session expired - /fav again.")
        return
    cat = fa["locations"][fa["loc"]].categories[fa["cat"]]
    label = (f"{_space_label(item_id)} @ {cat.label}" if item_id
             else f"Any @ {cat.label}")
    storage.add_fav(user_id, label, cat.lid, cat.gid, item_id or None)
    await query.edit_message_text(
        f"Favourite saved: {label}\n/fav books it - I'll only ask for the time.")


async def _fav_start(query, update, context, fav_id: int):
    fav = storage.get_fav(fav_id)
    if not fav:
        await query.edit_message_text("Favourite not found.")
        return
    # A favourite that names a table and a time can only have come from a
    # scheduled booking, so reopen it that way; a plain one is a library +
    # space type and the day, time and table are picked fresh.
    scheduled_fav = bool(fav["item_id"] and fav["default_start"])
    if not await _flow_from_lid_gid(update, context, fav["lid"], fav["gid"],
                                    {"only_item": fav["item_id"], "fav": dict(fav)}):
        return
    bk = context.user_data["bk"]
    if scheduled_fav:
        bk["mode"] = "sched"
    bk["day"] = date.today()
    bk["room"] = fav["item_id"]
    grid = _restrict(await _refresh_grid(bk), fav["item_id"])
    if not any(libcal.bookable_starts(c) for c in grid.values()):
        await query.edit_message_text(
            f"{fav['label']}: nothing free today. /book to try other spaces or days.")
        return
    # One-tap path: the saved usual timing, if it's still free today.
    if fav["default_start"] and fav["default_end"]:
        s = datetime.combine(date.today(), datetime.strptime(fav["default_start"], "%H:%M").time())
        e = datetime.combine(date.today(), datetime.strptime(fav["default_end"], "%H:%M").time())
        if libcal.spaces_free_for(grid, s, e):
            bk["start"], bk["end"] = s, e
            await query.edit_message_text(
                f"{fav['label']} - your usual {s:%H:%M}-{e:%H:%M} is FREE today.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Book usual {s:%H:%M}-{e:%H:%M}",
                                          callback_data="bk|favu")],
                    [InlineKeyboardButton("Different time", callback_data="bk|favt")],
                    _nav_row()]))
            return
    await _render(query, context, "dur")


async def cmd_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_private(update):
        return
    await _pick_booking(update, context, "mv", "move to a new time")


async def _move_start(query, update, context, booking_id: int):
    booking = storage.get_booking(booking_id)
    if not booking:
        await query.edit_message_text("Booking not found.")
        return
    if not booking["lid"]:
        await query.edit_message_text(
            "This booking predates /move (no category stored) - cancel and "
            "rebook manually this one time.")
        return
    if not await _flow_from_lid_gid(update, context, booking["lid"], booking["gid"],
                                    {"move_from": booking_id}):
        return
    await query.edit_message_text(
        f"Moving {booking['room_name']} {booking['start_ts']}. "
        "Pick the NEW time - the old booking is only cancelled after the new "
        "one succeeds.")
    msg_q = query
    await _render(msg_q, context, "day")


HOLDTIME_CHOICES = ((30, "30 min"), (60, "1 h"), (120, "2 h"), (240, "4 h"),
                    (480, "8 h"), (0, "No limit"))


async def _show_holdtime(send, user_id: int) -> None:
    current = storage.hold_budget(user_id)
    row = [InlineKeyboardButton(("• " if n == current else "") + label,
                                callback_data=f"bk|ht|{n}")
           for n, label in HOLDTIME_CHOICES]
    now = "until you book or release it" if not current else f"{current} min"
    await send(
        f"A chope is re-taken every few minutes and kept {now}, then released."
        "\n\nEach hold keeps a headless browser open, so long holds cost "
        "memory - fine on a server, heavier on a laptop. You can also extend "
        "one hold at a time from /holds.",
        reply_markup=InlineKeyboardMarkup([row[:3], row[3:]]))


async def cmd_holdtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """How long chopes keep renewing, set from Telegram instead of the .env."""
    if not await _require_private(update):
        return
    user_id = update.effective_user.id
    if not storage.is_developer(user_id):
        await update.effective_message.reply_text(
            "Holds are a developer feature.")
        return
    args = context.args or []
    if args and args[0].isdigit():
        storage.set_setting(user_id, "hold_minutes", max(0, min(1440, int(args[0]))))
    await _show_holdtime(update.effective_message.reply_text, user_id)

async def cmd_mostused(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """How many shortcuts to show before the 'Show all' button."""
    if not await _require_private(update):
        return
    user_id = update.effective_user.id
    args = context.args or []
    if args and args[0].isdigit():
        storage.set_setting(user_id, "mostused", max(1, min(15, int(args[0]))))
    await _show_mostused(update.effective_message.reply_text, user_id)


MOSTUSED_CHOICES = (1, 3, 5, 8, 10)


async def _show_mostused(send, user_id: int) -> None:
    """Tapped from a menu there is no number to type, so offer buttons."""
    size = storage.shortlist_size(user_id)
    row = [InlineKeyboardButton(
        ("• " if n == size else "") + str(n),
        callback_data=f"bk|mu|{n}") for n in MOSTUSED_CHOICES]
    await send(
        f"I show your {size} most-used libraries and space types first, "
        "then a Show-all button. Ranking uses the library and the type of "
        "space only - never a specific table or time, since what is free "
        "changes daily."
        + "\n" + "\n" + "How many shortcuts?",
        reply_markup=InlineKeyboardMarkup([row]))


async def cmd_availability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Look at what's free without starting a booking."""
    if not await _require_private(update):
        return
    try:
        locations = await libcal.fetch_locations()
    except Exception:
        await update.effective_message.reply_text(
            "Couldn't reach libcalendar.ntu.edu.sg - try again later.")
        return
    context.user_data["av"] = {"locations": locations}
    items = [(loc.name, f"av|loc|{i}") for i, loc in enumerate(locations)]
    await flows.start(
        update, context, flows.LIBRARY, "What's free - which library?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(l, callback_data=d)] for l, d in items]))


async def on_availability_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    av = context.user_data.get("av")
    if not av:
        await query.edit_message_text("Session expired - /availability again.")
        return
    if parts[1] == "loc":
        av["loc"] = int(parts[2])
        loc = av["locations"][av["loc"]]
        rows = [[InlineKeyboardButton(c.label, callback_data=f"av|cat|{i}")]
                for i, c in enumerate(loc.categories)]
        await query.edit_message_text(f"{loc.name} - which type?",
                                      reply_markup=InlineKeyboardMarkup(rows))
        return
    if parts[1] == "cat":
        av["cat"] = int(parts[2])
        today = date.today()
        rows = [[InlineKeyboardButton((today + timedelta(days=n)).strftime("%a %d %b"),
                                      callback_data=f"av|day|{n}")]
                for n in range(config.BOOKING_DAYS_AHEAD)]
        await query.edit_message_text("Which day?",
                                      reply_markup=InlineKeyboardMarkup(rows))
        return
    if parts[1] == "day":
        day = date.today() + timedelta(days=int(parts[2]))
        cat = av["locations"][av["loc"]].categories[av["cat"]]
        await query.edit_message_text("Checking...")
        try:
            grid = await libcal.fetch_grid(cat.lid, cat.gid, day)
        except Exception:
            await query.edit_message_text("Couldn't load that grid - try again.")
            return
        free_now = sum(1 for cells in grid.values() if libcal.bookable_starts(cells))
        strip = _availability_strip(grid, day) if grid else "(nothing listed)"
        await query.edit_message_text(
            f"{cat.label}, {day:%a %d %b}\n"
            f"{free_now} of {len(grid)} spaces have free time\n\n{strip}\n\n"
            "/book when you want one.")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(rules.rules_text()[:4000])


async def cmd_refreshcatalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-read hours, notice periods and desk names from the site.

    The catalogue normally comes from the copy shipped with the code, which is
    fine because it rarely changes. Run this when the library alters its hours
    or renames desks - and commit the refreshed `catalog_seed.json` so every
    other machine gets the correction too.
    """
    if (update.effective_chat.type != "private"
            or not storage.is_developer(update.effective_user.id)):
        return
    profile = _profile(update.effective_user.id)
    if not profile:
        await update.effective_message.reply_text(
            "I need your NTU login to read the policy pages - run /setup first.")
        return
    await update.effective_message.reply_text(
        "Re-reading every category from the site. This drives a browser "
        "through all of them, so give it a few minutes.")

    async def work():
        meta = await catalog.refresh(update.effective_user.id,
                                     profile["username"], profile["password"])
        spaces = sum(len(e.get("spaces") or {}) for e in meta.values())
        note = _write_seed(meta)
        await update.effective_message.reply_text(
            f"Catalogue refreshed: {len(meta)} categories, {spaces} spaces."
            f"\n{note}")

    tasks.spawn(work(), bot=context.bot, user_id=update.effective_user.id,
                feature="refreshing the catalogue")


def _write_seed(meta: dict) -> str:
    """Save the refreshed catalogue over the copy that ships with the code."""
    import json

    try:
        seed = {"categories": meta,
                "room_names": storage.durable_get("libcal_room_names", {}) or {},
                "probed_limits": storage.durable_get("category_limits", {}) or {}}
        catalog.SEED_FILE.write_text(
            json.dumps(seed, indent=1, sort_keys=True), encoding="utf-8")
        return ("catalog_seed.json updated - commit it so other machines "
                "start from the same numbers.")
    except Exception as exc:                       # read-only install: no matter
        log.warning("could not update the catalogue seed: %s", exc)
        return "(Saved here, but I could not update catalog_seed.json.)"


async def cmd_developer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only diagnostics. Silently ignored for anyone not on the list."""
    if (update.effective_chat.type != "private"
            or not storage.is_developer(update.effective_user.id)):
        return
    lines = [f"Data dir: {config.HOME}"]
    jobs = storage.conn().execute(
        "SELECT id, category, start_ts, fire_at, status, attempts, last_error"
        " FROM scheduled_bookings ORDER BY id DESC LIMIT 5").fetchall()
    if jobs:
        lines.append("\nRecent scheduled jobs:")
        lines += [f"#{j['id']} {j['category']} {j['start_ts']} fire {j['fire_at']}"
                  f" [{j['status']} x{j['attempts']}]"
                  + (f" err: {j['last_error'][:80]}" if j["last_error"] else "")
                  for j in jobs]
    dumps = sorted(config.DEBUG_DIR.glob("*.png"))[-5:]
    if dumps:
        lines.append("\nRecent debug dumps:")
        lines += [f"  {d.name}" for d in dumps]
    errors = storage.recent_errors(8)
    if errors:
        lines.append("\nRecent failures (newest first):")
        lines += [f"  {e['at'][5:]} [{e['feature']}] {e['message'][:110]}"
                  for e in errors]
    else:
        lines.append("\nNo failures recorded.")
    try:
        tail = config.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        errors = [ln for ln in tail if " ERROR " in ln or " WARNING " in ln][-15:]
        lines.append("\nRecent warnings/errors:" if errors else "\nNo recent errors in log.")
        lines += [ln[:160] for ln in errors]
    except FileNotFoundError:
        lines.append("\n(no log file yet - restart the bot to enable file logging)")
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await update.effective_message.reply_text(text[i:i + 3900])


# --- Free-text inputs (private chat) --------------------------------------

async def _cancel_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    booking_id = context.user_data.get("awaiting_cancel_link")
    if booking_id is None:
        return False
    found = emailcode.parse_text(update.effective_message.text)
    link = found["cancel_link"]
    if not link and "libcalendar.ntu.edu.sg" in update.effective_message.text:
        link = update.effective_message.text.strip()
    if not link:
        await update.effective_message.reply_text(
            "I couldn't find a libcalendar cancellation link in that - paste the full URL.")
        return True
    context.user_data.pop("awaiting_cancel_link", None)
    storage.update_booking(booking_id, cancel_link=link)
    ok, msg = await libcal.cancel_via_link(link)
    if ok:
        storage.update_booking(booking_id, status="cancelled")
    await update.effective_message.reply_text(("Cancelled. " if ok else "Cancellation failed: ") + msg)
    return True


async def _until_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """'Until when?' typed rather than tapped."""
    if not context.user_data.get("awaiting_until_date"):
        return False
    bk = context.user_data.get("bk") or {}
    if "weekdays" not in bk:
        context.user_data.pop("awaiting_until_date", None)
        return False
    text = update.effective_message.text.strip()
    until = None
    for fmt in ("%d/%m/%Y", "%d/%m", "%d %b %Y", "%d %b"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            until = (parsed if "%Y" in fmt
                     else parsed.replace(year=date.today().year))
            break
        except ValueError:
            continue
    if until is None:
        await update.effective_message.reply_text(
            "Couldn't read that - send the last date as DD/MM, e.g. 12/12.")
        return True
    today = date.today()
    if until <= today:
        # A date already past is almost always next year's, typed short.
        until = until.replace(year=today.year + 1)
    longest = today + timedelta(weeks=config.RECUR_MAX_WEEKS)
    if until > longest:
        until = longest
        await update.effective_message.reply_text(
            f"That is further ahead than I keep rules for, so I've set the end "
            f"to {until:%d %b} ({config.RECUR_MAX_WEEKS} weeks). /recurring can "
            "extend it later.")
    context.user_data.pop("awaiting_until_date", None)
    bk["until"] = until
    msg = await update.effective_message.reply_text("...")
    await _render(_msg_query(update.effective_user, msg), context, "rconfirm")
    return True


def _rule_line(rule) -> str:
    """One rule, in a sentence."""
    days = _rule_days_text(storage.rule_weekdays(rule))
    until = date.fromisoformat(rule["until_date"])
    paused = " (paused)" if rule["status"] == "paused" else ""
    where = _space_label(rule["item_id"]) if rule["item_id"] else "any space"
    return (f"{days} {rule['start_hm']}-{rule['end_hm']} {rule['category']}, "
            f"{where}, until {until:%d %b}{paused}")


async def cmd_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The repeating bookings you have, and the buttons to stop them."""
    if not await _require_private(update):
        return
    rules_ = storage.list_rules(update.effective_user.id)
    kb = []
    for r in rules_:
        resume = r["status"] == "paused"
        kb.append([
            InlineKeyboardButton(("▶" if resume else "⏸") + f" {_rule_line(r)}",
                                 callback_data=f"bk|rpause|{r['id']}"),
            InlineKeyboardButton("✕", callback_data=f"bk|rdel|{r['id']}"),
        ])
    kb.append([InlineKeyboardButton("➕ New repeating booking",
                                    callback_data="bk|rnew")])
    await flows.start(
        update, context, flows.LIBRARY,
        ("Repeating bookings - tap one to pause or resume it, ✕ deletes:"
         if rules_ else
         "No repeating bookings yet. I book the same slot every week and race "
         "for it the moment each week's window opens."),
        reply_markup=InlineKeyboardMarkup(kb))


async def _fire_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_fire_time"):
        return False
    bk = context.user_data.get("bk") or {}
    if "day" not in bk:
        context.user_data.pop("awaiting_fire_time", None)
        return False
    text = update.effective_message.text.strip()
    fire = None
    for fmt, base in (("%H:%M", bk["day"]), ("%d/%m %H:%M", None)):
        try:
            parsed = datetime.strptime(text, fmt)
            if base is not None:
                fire = datetime.combine(base, parsed.time())
            else:
                fire = parsed.replace(year=date.today().year)
            break
        except ValueError:
            continue
    if fire is None:
        await update.effective_message.reply_text(
            "Couldn't read that - send HH:MM (on the booking day) or DD/MM HH:MM.")
        return True
    context.user_data.pop("awaiting_fire_time", None)
    bk["fire_at"] = fire
    msg = await update.effective_message.reply_text("...")
    await _render(_msg_query(update.effective_user, msg), context, "sconfirm")
    return True


async def _pasted_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    text = update.effective_message.text
    if not emailcode.looks_like_confirmation(text):
        return False
    found = emailcode.parse_text(text)
    if not (found["code"] or found["cancel_link"]):
        return False
    fields = {k: v for k, v in (
        ("checkin_code", found["code"]),
        ("cancel_link", found["cancel_link"]),
        ("booking_ref", found["reference"])) if v}
    user_id = update.effective_user.id
    rows = storage.list_bookings(user_id)

    # The email names the space and both times, so a booking made on the
    # website can be filed exactly - no guessing, no asking.
    if found["start"] and found["end"]:
        match = next((b for b in rows
                      if abs((datetime.strptime(b["start_ts"], storage.FMT)
                              - found["start"]).total_seconds()) <= 15 * 60), None)
        if match is None:
            booking_id = storage.add_booking(
                user_id, found["library"] or "(booked by you)",
                "your own booking", found["space"] or "your booking", None,
                found["start"], found["end"])
            match = storage.get_booking(booking_id)
            lead = "Added the booking from that email"
        else:
            lead = f"Matched your {match['room_name']} booking"
        _apply_capture(match["id"], fields)
        await update.effective_message.reply_text(
            f"{lead}: {match['room_name']}, "
            f"{found['start']:%a %d %b %H:%M}-{found['end']:%H:%M}.\n"
            f"Captured {_describe_capture(fields)}. I'll check you in "
            "automatically from 2 minutes before it starts.")
        return True

    if not rows:
        await update.effective_message.reply_text(
            "That looks like a library email, but it doesn't say which booking "
            "it is for and I have none on file.")
        return True
    if len(rows) > 1:
        await _ask_which_booking(update.effective_message, context, fields, rows)
        return True
    booking = rows[0]
    _apply_capture(booking["id"], fields)
    await update.effective_message.reply_text(
        f"Captured {_describe_capture(fields)} for {booking['room_name']} "
        f"{booking['start_ts']}. /checkin when you arrive.")
    return True
    if len(rows) > 1:
        await _ask_which_booking(update.message, context, fields, rows)
        return True
    booking = rows[0]
    _apply_capture(booking["id"], fields)
    await update.effective_message.reply_text(
        f"Captured {_describe_capture(fields)} for {booking['room_name']} "
        f"{booking['start_ts']}. /checkin when you arrive.")
    return True


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for handler in (_setup_input, _fire_time_input, _until_date_input,
                    _cancel_link_input, _pasted_email_input):
        if await handler(update, context):
            return


# --- Callback dispatch ----------------------------------------------------

async def on_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    action = parts[1]
    user_id = update.effective_user.id
    bk = context.user_data.get("bk")

    try:
        if action == "rnew":
            await _start_flow(update, context, "recur")
            return
        if action in ("rpause", "rdel"):
            rule = storage.get_rule(int(parts[2]))
            if rule is None or rule["user_id"] != query.from_user.id:
                await query.edit_message_text(
                    "That repeating booking is gone already.")
                return
            if action == "rdel":
                storage.update_rule(rule["id"], status="cancelled")
                await query.edit_message_text(
                    f"Stopped: {_rule_line(rule)}\n\n"
                    "Bookings it already made stay - /bookings shows them, "
                    "/scheduled cancels any that have not run yet.")
                return
            pausing = rule["status"] == "active"
            storage.update_rule(rule["id"],
                                status="paused" if pausing else "active")
            await query.edit_message_text(
                ("Paused" if pausing else "Running again")
                + f": {_rule_line(rule)}"
                + ("\n\nI will not set up any more weeks until you resume it."
                   if pausing else ""))
            return

        if action in ("codeto", "codenew"):
            pending = context.user_data.pop("code_pending", None)
            if not pending:
                await query.edit_message_text(
                    "That question expired - send the code again and I'll ask "
                    "once more.")
                return
            match = None
            if action == "codeto":
                match = storage.get_booking(int(parts[2]))
                if match is None or match["user_id"] != query.from_user.id:
                    await query.edit_message_text("That booking is gone already.")
                    return
            await _file_code(query.edit_message_text, context,
                             query.from_user.id, pending["code"],
                             pending["found"], match)
            return

        if action == "scancel":
            storage.update_scheduled(int(parts[2]), status="cancelled")
            await query.edit_message_text(f"Scheduled booking #{parts[2]} cancelled.")
            return
        if action == "ci":
            await _do_checkin(query, context, user_id, int(parts[2]))
            return
        if action == "cx":
            await _do_cancel(query, context, user_id, int(parts[2]))
            return
        if action == "favadd":
            b = storage.get_booking(int(parts[2]))
            if b and b["lid"]:
                storage.add_fav(user_id, f"{b['room_name']} @ {b['category']}",
                                b["lid"], b["gid"], b["item_id"],
                                b["start_ts"][-5:], b["end_ts"][-5:])
                await context.bot.send_message(
                    user_id, "Saved as favourite - /fav books it in two taps.")
            return
        if action == "favdel":
            storage.del_fav(int(parts[2]))
            await query.edit_message_text("Favourite deleted. /fav shows the rest.")
            return
        if action == "fav":
            await _fav_start(query, update, context, int(parts[2]))
            return
        if action == "fadd":
            await _fadd_libraries(query, context)
            return
        if action == "fal":
            await _fadd_cats(query, context, int(parts[2]))
            return
        if action == "fac":
            await _fadd_spaces(query, context, int(parts[2]))
            return
        if action == "fas":
            await _fadd_save(query, context, user_id, int(parts[2]))
            return
        if action == "mv":
            await _move_start(query, update, context, int(parts[2]))
            return
        if action == "mu":
            storage.set_setting(user_id, "mostused",
                                max(1, min(15, int(parts[2]))))
            await _show_mostused(query.edit_message_text, user_id)
            return
        if action == "setcode":
            fields = context.user_data.pop("pending_capture", None)
            target = storage.get_booking(int(parts[2]))
            if not fields or not target or target["user_id"] != user_id:
                await query.edit_message_text(
                    "That code request expired - send /code ABC123 again.")
                return
            _apply_capture(target["id"], fields)
            await query.edit_message_text(
                f"Saved {_describe_capture(fields)} for {target['room_name']} "
                f"{target['start_ts']}.\nWrong one? Just send the code again "
                "and pick another booking.")
            return
        if action == "hext":
            hold = holds.get(int(parts[2]))
            if not hold or hold.user_id != user_id:
                await query.edit_message_text("That hold has already ended.")
                return
            note = holds.extend(hold)
            await query.edit_message_text(
                f"#{hold.id} {hold.label} {hold.start:%H:%M}-{hold.end:%H:%M}: "
                f"{note}.", reply_markup=_holds_keyboard(user_id))
            return
        if action == "hallback":
            profile = _profile(user_id)
            gone = _restorable(user_id)
            if not profile or not gone:
                await query.edit_message_text("Nothing to take back.")
                return
            await query.edit_message_text(
                f"Taking back {len(gone)} slot(s) - this can take a moment...")
            done, failed = [], []
            for row in gone:
                hold, note = await holds.reacquire(
                    user_id, row["id"], profile["username"], profile["password"],
                    attempts=1)
                label = row["label"].split(" (")[0]
                when = f"{row['start_ts'][-5:]}-{row['end_ts'][-5:]}"
                if hold:
                    done.append(f"{label} {when}")
                else:
                    failed.append(f"{label} {when} ({note})")
            lines = []
            if done:
                lines.append("Holding again: " + ", ".join(done))
            if failed:
                lines.append("Could not get back: " + "; ".join(failed))
            await query.edit_message_text("\n".join(lines) or "Nothing to do.",
                                          reply_markup=_holds_keyboard(user_id))
            return
        if action == "ht":
            if parts[2] == "ask":
                await _show_holdtime(query.edit_message_text, user_id)
            else:
                storage.set_setting(user_id, "hold_minutes",
                                    max(0, min(1440, int(parts[2]))))
                # live holds pick up the new budget immediately
                for h in holds.for_user(user_id):
                    h.budget_minutes = storage.hold_budget(user_id)
                await _show_holdtime(query.edit_message_text, user_id)
            return
        if action == "hagain":
            profile = _profile(user_id)
            if not profile:
                await query.edit_message_text("Run /setup first.")
                return
            await query.edit_message_text("Trying to take that slot back...")
            hold, note = await holds.reacquire(user_id, int(parts[2]),
                                               profile["username"],
                                               profile["password"])
            if hold:
                await query.edit_message_text(
                    f"Holding {hold.label} {hold.start:%H:%M}-{hold.end:%H:%M} "
                    f"again (#{hold.id}).", reply_markup=_holds_keyboard(user_id))
            else:
                await query.edit_message_text(f"Couldn't take it back - {note}")
            return
        if action in ("hbook", "hrel"):
            await _hold_action(query, context, user_id, int(parts[2]),
                               book_it=(action == "hbook"))
            return

        if bk is None:
            await query.edit_message_text("Session expired - run /book again.")
            return

        if action == "loc":
            bk["loc"] = int(parts[2])
            await _render(query, context, "cat")
        elif action == "cat":
            bk["cat"] = int(parts[2])
            await _render(query, context,
                          "days" if bk["mode"] == "recur" else "day")
        elif action == "again":
            last = context.user_data.get("bk_last")
            if not last or last.get("loc") is None:
                await _render(query, context, "home")
            else:
                # Deliberately only the library and category: the day, time
                # and exact space are always picked fresh.
                bk["loc"], bk["cat"] = last["loc"], last["cat"]
                for key in ("day", "dur", "start", "end", "room", "free_spaces",
                            "grids", "only_item"):
                    bk.pop(key, None)
                await _render(query, context, "day")
        elif action == "day":
            bk["day"] = datetime.strptime(parts[2], "%Y%m%d").date()
            await _render(query, context, "dur")
        elif action == "dur":
            if parts[2] == "custom":
                bk["via_custom"] = True
                await _render(query, context, "start")
            else:
                bk["dur"] = int(parts[2])
                await _render(query, context, "range")
        elif action == "rg":
            start = datetime.combine(bk["day"],
                                     datetime.strptime(parts[2], "%H%M").time())
            bk["start"] = start
            bk["end"] = start + timedelta(minutes=bk["dur"])
            if bk.get("only_item"):
                bk["room"] = bk["only_item"]
                await _render(query, context, "confirm")
            else:
                await _render(query, context, "space")
        elif action == "st":
            bk["start"] = datetime.combine(bk["day"],
                                           datetime.strptime(parts[2], "%H%M").time())
            await _render(query, context, "end")
        elif action == "en":
            t = datetime.strptime(parts[2], "%H%M").time()
            end = datetime.combine(bk["day"], t)
            if t == dtime(0, 0):
                end += timedelta(days=1)
            bk["end"] = end
            if bk.get("only_item"):
                # A single-space favourite: no space to choose, go straight on.
                bk["room"] = bk["only_item"]
                await _render(query, context, "confirm")
            else:
                await _render(query, context, "space")
        elif action == "sp":
            bk.pop("plan", None)          # a named desk, not a hop plan
            bk["room"] = int(parts[2])
            await _render(query, context, _after_space(bk))
        elif action == "hop":
            if not bk.get("plan"):
                await query.edit_message_text("That plan expired - pick the time again.")
                return
            bk["room"] = bk["plan"][0][0]
            await _render(query, context, "confirm")
        elif action == "any":
            if bk["mode"] in LIVE_MODES:
                bk["room"] = (bk.get("free_spaces") or [0])[0]
                await _render(query, context, "confirm")
            else:
                bk["room"] = None
                await _render(query, context, _after_space(bk))
        elif action == "fine":
            bk["fine"] = not bk.get("fine")
            await _render(query, context, bk.get("step", "range"))
        elif action == "fire":
            if parts[2] == "custom":
                context.user_data["awaiting_fire_time"] = True
                await query.edit_message_text(
                    "Type the fire time: HH:MM (on the booking day) or DD/MM HH:MM.")
            else:
                if parts[2] == "now":
                    bk["fire_at"] = datetime.now() + timedelta(minutes=2)
                elif parts[2] == "auto":
                    cat_ = bk["locations"][bk["loc"]].categories[bk["cat"]]
                    bk["fire_at"] = catalog.window_opens_at(cat_.lid, cat_.gid,
                                                            bk["day"])
                elif parts[2] == "eve":
                    bk["fire_at"] = datetime.combine(
                        bk["day"] - timedelta(days=1), dtime(23, 59))
                else:
                    t = datetime.strptime(parts[2], "%H%M").time()
                    bk["fire_at"] = datetime.combine(bk["day"], t)
                await _render(query, context, "sconfirm")
        elif action == "wd":
            chosen = set(bk.get("weekdays") or [])
            chosen ^= {int(parts[2])}          # tap to add, tap again to drop
            bk["weekdays"] = sorted(chosen)
            await _render(query, context, "days")
        elif action == "wdone":
            if not bk.get("weekdays"):
                await query.answer("Pick at least one day first.", show_alert=True)
                return
            # The rest of the flow asks about one day's times and spaces, so
            # give it the first day this rule will actually book.
            cat_ = bk["locations"][bk["loc"]].categories[bk["cat"]]
            days = _occurrences(cat_.lid, cat_.gid, bk["weekdays"],
                                date.today() + timedelta(days=14), limit=1)
            bk["day"] = days[0] if days else date.today()
            await _render(query, context, "dur")
        elif action == "until":
            if parts[2] == "custom":
                context.user_data["awaiting_until_date"] = True
                await query.edit_message_text(
                    "Type the last date to book: DD/MM, or DD/MM/YYYY.")
            else:
                weeks = min(int(parts[2]), config.RECUR_MAX_WEEKS)
                bk["until"] = date.today() + timedelta(weeks=weeks)
                await _render(query, context, "rconfirm")
        elif action == "favu":
            await _render(query, context, "confirm")
        elif action == "favt":
            await _render(query, context, "dur")
        elif action == "go":
            await _bk_go(query, context, user_id)
        elif action == "showall":
            bk["show_all"] = not bk.get("show_all")
            await _render(query, context, bk.get("step", "home"))
        elif action == "mode":
            await _render(query, context, _switch_mode(bk, parts[2]))
        elif action == "favhere":
            await _save_fav_here(query, context, user_id, bk)
        elif action == "back":
            await _render(query, context, _prev_step(bk, bk.get("step", "home")))
        elif action == "home":
            await _render(query, context, "home")
        elif action == "abort":
            context.user_data.pop("bk", None)
            await query.edit_message_text("Aborted - nothing was submitted.")
    except Exception:
        log.exception("booking callback failed")
        await query.edit_message_text(
            "Something went wrong - /book or /schedulebook to start over.")


def register(application) -> None:
    application.add_handler(CommandHandler("setup", cmd_setup))
    application.add_handler(CommandHandler("cancel_setup", cmd_cancel_setup))
    application.add_handler(CommandHandler("forgetme", cmd_forgetme))
    application.add_handler(CommandHandler("book", cmd_book))
    application.add_handler(CommandHandler("schedulebook", cmd_schedulebook))
    application.add_handler(CommandHandler("chope", cmd_chope))
    application.add_handler(CommandHandler("extendedbooking", cmd_extendedbooking))
    application.add_handler(CommandHandler("holds", cmd_holds))
    application.add_handler(CommandHandler("holdtime", cmd_holdtime))
    application.add_handler(CommandHandler("scheduled", cmd_scheduled))
    application.add_handler(CommandHandler("recurring", cmd_recurring))
    application.add_handler(CommandHandler("botemail", cmd_botemail))
    application.add_handler(CommandHandler("code", cmd_code))
    application.add_handler(CommandHandler("email", cmd_email))
    application.add_handler(CommandHandler("bookings", cmd_bookings))
    application.add_handler(CommandHandler("checkin", cmd_checkin))
    application.add_handler(CommandHandler("cancelbooking", cmd_cancel_booking))
    application.add_handler(CommandHandler("fav", cmd_fav))
    application.add_handler(CommandHandler("move", cmd_move))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(CommandHandler("mostused", cmd_mostused))
    application.add_handler(CommandHandler("availability", cmd_availability))
    application.add_handler(CallbackQueryHandler(on_availability_callback,
                                                 pattern=r"^av\|"))
    application.add_handler(CommandHandler("developer", cmd_developer))
    application.add_handler(CommandHandler("refreshcatalog", cmd_refreshcatalog))
    application.add_handler(CallbackQueryHandler(on_booking_callback, pattern=r"^bk\|"))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_private_text))
