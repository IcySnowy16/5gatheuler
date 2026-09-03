"""Group fan-out booking: one long session split across members' accounts.

The library caps each person's booking, so groups book back-to-back legs
under different accounts. /groupbook (in a group chat) lets the organizer
pick category, day and TOTAL period; the bot splits it into legs and posts
one claim button per leg. Claiming DMs that member a confirm card, and the
booking runs under THEIR credentials in THEIR private chat - passwords never
touch the group.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .. import config, storage
from . import browser, holds, libcal
from . import handlers as h

log = logging.getLogger(__name__)

PREV = {"cat": "loc", "day": "cat", "start": "day", "end": "start", "post": "end"}


async def cmd_groupbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "/groupbook is for group chats - it splits a long session across "
            "several members' accounts. For yourself, use /book.")
        return
    try:
        locations = await libcal.fetch_locations()
    except Exception:
        await update.effective_message.reply_text("Couldn't reach libcalendar.ntu.edu.sg.")
        return
    context.chat_data["gb"] = {
        "owner": update.effective_user.id,
        "owner_name": update.effective_user.first_name,
        "locations": locations,
        "step": "loc",
    }
    msg = await update.effective_message.reply_text("...")
    context.chat_data["gb"]["msg_id"] = msg.message_id
    await _render(context.bot, update.effective_chat.id, context, "loc")


async def _render(bot, chat_id: int, context, step: str):
    gb = context.chat_data["gb"]
    gb["step"] = step
    text, kb = await {"loc": _r_loc, "cat": _r_cat, "day": _r_day,
                      "start": _r_start, "end": _r_end, "post": _r_post}[step](gb)
    await bot.edit_message_text(text, chat_id=chat_id, message_id=gb["msg_id"],
                                reply_markup=kb)


def _nav():
    return [InlineKeyboardButton("« Back", callback_data="gb|back"),
            InlineKeyboardButton("Cancel", callback_data="gb|stop")]


def _kb(items, per_row=2, nav=True):
    rows, row = [], []
    for label, data in items:
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if nav:
        rows.append(_nav())
    return InlineKeyboardMarkup(rows)


async def _r_loc(gb):
    items = [(loc.name, f"gb|loc|{i}") for i, loc in enumerate(gb["locations"])]
    return (f"Group booking (set up by {gb['owner_name']}) - which library?",
            _kb(items, per_row=1, nav=False))


async def _r_cat(gb):
    loc = gb["locations"][gb["loc"]]
    items = [(c.label, f"gb|cat|{i}") for i, c in enumerate(loc.categories)]
    return f"{loc.name} - category?", _kb(items, per_row=1)


async def _r_day(gb):
    cat = gb["locations"][gb["loc"]].categories[gb["cat"]]
    grids = await libcal.days_with_availability(cat.lid, cat.gid,
                                               config.BOOKING_DAYS_AHEAD)
    gb["grids"] = grids
    if not grids:
        return (f"Nothing bookable for {cat.label} in the next days.",
                _kb([], nav=True))
    items = [(d.strftime("%a %d %b"), f"gb|day|{d:%Y%m%d}") for d in sorted(grids)]
    return f"{cat.label} - which day?", _kb(items)


async def _r_start(gb):
    grid = gb["grids"][gb["day"]]
    starts = sorted({s for cells in grid.values() for s in libcal.bookable_starts(cells)})
    items = [(f"{s:%H:%M}", f"gb|st|{s:%H%M}") for s in starts[:48]]
    return f"{gb['day']:%a %d %b} - session starts at?", _kb(items, per_row=4)


async def _r_end(gb):
    start = gb["start"]
    limit = start + timedelta(minutes=config.GROUP_MAX_MINUTES)
    ends, t = [], start + timedelta(minutes=_leg_minutes(gb))
    while t <= limit and (t.date() == start.date() or t.time() == datetime.min.time()):
        ends.append(t)
        if t.time() == datetime.min.time():
            break  # midnight is the last offered end
        t += timedelta(minutes=30)
    items = [(f"{e:%H:%M}", f"gb|en|{e:%H%M}") for e in ends]
    return (f"Session {start:%H:%M} until? (split into "
            f"{config.GROUP_LEG_MINUTES // 60}h legs)"), _kb(items, per_row=4)


def _split_legs(start: datetime, end: datetime,
                leg_minutes: int | None = None) -> list[tuple[datetime, datetime]]:
    legs, t = [], start
    step = timedelta(minutes=leg_minutes or config.GROUP_LEG_MINUTES)
    while t < end:
        legs.append((t, min(t + step, end)))
        t += step
    return legs


def _leg_minutes(gb) -> int:
    cat = gb["locations"][gb["loc"]].categories[gb["cat"]]
    _, cat_max = libcal.category_limits(cat.lid, cat.gid)
    return min(config.GROUP_LEG_MINUTES, cat_max)


async def _r_post(gb):
    legs = _split_legs(gb["start"], gb["end"], _leg_minutes(gb))
    lines = [f"{s:%H:%M}-{e:%H:%M}" for s, e in legs]
    return (f"Post this plan? {gb['day']:%a %d %b}, "
            f"{gb['start']:%H:%M}-{gb['end']:%H:%M} -> {len(legs)} leg(s):\n"
            + "\n".join(lines) +
            "\n\nEach leg is claimed and booked by a different member under "
            "their own account (they need /setup with me in DM first).\n\n"
            "Choping holds every leg under YOUR account while people claim, so "
            "the session can't be taken from under you - each hold is released "
            "the moment its claimer books it.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Post + chope all legs", callback_data="gb|golive|hold")],
                [InlineKeyboardButton("Post without holding", callback_data="gb|golive|plain")],
                _nav()]))


def _session_text(legs) -> str:
    first = legs[0]
    lines = [f"📚 Group booking: {first['category']} ({first['location']})",
             f"{datetime.strptime(first['start_ts'], storage.FMT):%a %d %b}",
             ""]
    for leg in legs:
        t = f"{leg['start_ts'][-5:]}-{leg['end_ts'][-5:]}"
        lock = ""
        if leg["status"] == "open" and leg["hold_id"] and holds.get(leg["hold_id"]):
            lock = f" 🔒 held till {holds.get(leg['hold_id']).expires_at:%H:%M}"
        if leg["status"] == "open":
            lines.append(f"⬜ {t} - unclaimed{lock}")
        elif leg["status"] == "claimed":
            lines.append(f"🕐 {t} - {leg['claimed_name']} (confirming in DM){lock}")
        elif leg["status"] == "booked":
            lines.append(f"✅ {t} - {leg['claimed_name']}")
        else:
            lines.append(f"❌ {t} - {leg['claimed_name']} ({leg['status']})")
    lines.append("\nTap a leg to claim it - I'll DM you to confirm.")
    if any(l["hold_id"] and holds.get(l["hold_id"]) for l in legs):
        lines.append("🔒 = choped for the group; released the moment someone books it.")
    return "\n".join(lines)


def _session_kb(legs) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(
        f"Claim {leg['start_ts'][-5:]}-{leg['end_ts'][-5:]}",
        callback_data=f"gb|claim|{leg['id']}")]
        for leg in legs if leg["status"] == "open"]
    return InlineKeyboardMarkup(rows) if rows else None


async def _refresh_session_msg(bot, chat_id: int, msg_id: int):
    legs = storage.session_legs(chat_id, msg_id)
    if legs:
        try:
            await bot.edit_message_text(_session_text(legs), chat_id=chat_id,
                                        message_id=msg_id,
                                        reply_markup=_session_kb(legs))
        except Exception:
            pass  # unchanged text raises; ignore


async def _chope_legs(bot, chat_id: int, msg_id: int, leg_ids: list[int],
                      owner_id: int) -> None:
    """Hold every leg under the organizer's account while people claim."""
    profile = h._profile(owner_id)
    if not profile:
        await bot.send_message(chat_id, "Legs posted, but I can't chope them - "
                                        "the organizer hasn't run /setup with me.")
        return
    held, failed = 0, 0
    for leg_id in leg_ids:
        leg = storage.get_leg(leg_id)
        if not leg or leg["status"] != "open":
            continue
        start = datetime.strptime(leg["start_ts"], storage.FMT)
        end = datetime.strptime(leg["end_ts"], storage.FMT)
        try:
            grid = await libcal.fetch_grid(leg["lid"], leg["gid"], start.date())
            free = libcal.spaces_free_for(grid, start, end)
            if not free:
                failed += 1
                continue
            item_id = free[0]
            checksum = next((c.checksum for c in grid.get(item_id, [])
                             if c.start == start and c.state == libcal.FREE), None)
            if checksum is None:
                failed += 1
                continue
            hold = await holds.create(
                owner_id, profile["username"], profile["password"],
                leg["lid"], leg["gid"], item_id, start, end, checksum,
                leg["location"], leg["category"],
                note=f"group leg {leg['start_ts'][-5:]}")
            storage.update_leg(leg_id, hold_id=hold.id,
                               held_until=f"{hold.expires_at:%H:%M}")
            held += 1
        except Exception as e:
            log.warning("chope of leg %s failed: %s", leg_id, e)
            failed += 1
    await _refresh_session_msg(bot, chat_id, msg_id)
    note = f"Choped {held} leg(s) for the group."
    if failed:
        note += f" {failed} couldn't be held (already taken or hold limit)."
    note += (f" Holds renew themselves for up to {config.HOLD_MAX_MINUTES} min - "
             "claim before then.")
    await bot.send_message(chat_id, note)


async def _release_session_holds(chat_id: int, msg_id: int) -> None:
    for leg in storage.session_legs(chat_id, msg_id):
        if leg["hold_id"]:
            hold = holds.get(leg["hold_id"])
            if hold:
                await holds.release(hold)
            storage.update_leg(leg["id"], hold_id=None)


async def _claim(bot, query, chat_id: int, leg_id: int):
    leg = storage.get_leg(leg_id)
    user = query.from_user
    if not leg or leg["status"] != "open":
        await query.answer("Already claimed.", show_alert=True)
        return
    row = storage.get_user(user.id)
    if not row or not row["ntu_username"]:
        await query.answer(
            "You need to set up first: DM me /setup (30 seconds).", show_alert=True)
        return
    storage.update_leg(leg_id, status="claimed", claimed_by=user.id,
                       claimed_name=user.first_name)
    await _refresh_session_msg(bot, chat_id, leg["msg_id"])
    try:
        await bot.send_message(
            user.id,
            f"You claimed the {leg['start_ts'][-5:]}-{leg['end_ts'][-5:]} leg of "
            f"{leg['category']} on {leg['start_ts'][:10]}.\n"
            "Confirm and I'll book it under YOUR account (agreement box ticked "
            "for you):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirm & book my leg", callback_data=f"gb|go|{leg_id}"),
                InlineKeyboardButton("Release leg", callback_data=f"gb|rel|{leg_id}"),
            ]]))
        await query.answer("Check your DM to confirm!")
    except Exception:
        storage.update_leg(leg_id, status="open", claimed_by=None, claimed_name=None)
        await _refresh_session_msg(bot, chat_id, leg["msg_id"])
        await query.answer("I can't DM you - press Start in my DM first, then reclaim.",
                          show_alert=True)


async def _book_leg(bot, query, context, leg_id: int):
    leg = storage.get_leg(leg_id)
    user_id = query.from_user.id
    if not leg or leg["claimed_by"] != user_id or leg["status"] != "claimed":
        await query.edit_message_text("This leg isn't yours to book any more.")
        return
    profile = h._profile(user_id)
    if not profile:
        await query.edit_message_text("Run /setup first, then reclaim the leg.")
        return
    start = datetime.strptime(leg["start_ts"], storage.FMT)
    end = datetime.strptime(leg["end_ts"], storage.FMT)

    # A chope blocks EVERYONE, including this member. If the group is holding
    # this leg, hand it back a moment before booking it - the slot is then
    # free for exactly as long as it takes us to grab it.
    held_item = None
    if leg["hold_id"]:
        hold = holds.get(leg["hold_id"])
        if hold:
            held_item = hold.item_id
            await holds.release(hold)
            storage.update_leg(leg_id, hold_id=None)
            await asyncio.sleep(0.5)

    # Prefer the space the group already holds or booked for a neighbouring
    # leg, so nobody has to change desks mid-session.
    grid = await libcal.fetch_grid(leg["lid"], leg["gid"], start.date())
    free = libcal.spaces_free_for(grid, start, end)
    if not free:
        await query.edit_message_text(
            "No space is free for your leg any more - the group may need a "
            "different time.")
        storage.update_leg(leg_id, status="no space")
        await _refresh_session_msg(bot, leg["chat_id"], leg["msg_id"])
        return
    siblings = storage.session_legs(leg["chat_id"], leg["msg_id"])
    preferred = [s["item_id"] for s in siblings
                 if s["status"] == "booked" and s["item_id"] in free]
    if held_item in free:
        preferred.insert(0, held_item)
    item_id = preferred[0] if preferred else free[0]
    checksum = next((c.checksum for c in grid.get(item_id, [])
                     if c.start == start and c.state == libcal.FREE), None)
    if checksum is None:
        await query.edit_message_text("Your leg's start was just taken - reclaim later.")
        return

    await query.edit_message_text("Booking your leg - up to a minute...")
    result = await asyncio.to_thread(
        browser.book, user_id, profile["username"], profile["password"], profile,
        leg["lid"], leg["gid"], item_id, start, end, checksum)

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.bot = bot
    ok = await h._report_booking(
        query.edit_message_text, ctx, user_id, leg["location"], leg["category"],
        leg["lid"], leg["gid"], item_id, start, end, result)
    storage.update_leg(leg_id, status="booked" if ok else "failed",
                       item_id=item_id if ok else None)
    await _refresh_session_msg(bot, leg["chat_id"], leg["msg_id"])


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    action = parts[1]
    bot = context.bot
    chat_id = update.effective_chat.id

    # Claim / DM actions work for anyone, independent of setup state.
    if action == "claim":
        await _claim(bot, query, chat_id, int(parts[2]))
        return
    await query.answer()
    if action == "go":
        await _book_leg(bot, query, context, int(parts[2]))
        return
    if action == "rel":
        leg = storage.get_leg(int(parts[2]))
        if leg and leg["claimed_by"] == query.from_user.id:
            storage.update_leg(leg["id"], status="open", claimed_by=None,
                               claimed_name=None)
            await _refresh_session_msg(bot, leg["chat_id"], leg["msg_id"])
            await query.edit_message_text("Leg released.")
        return

    gb = context.chat_data.get("gb")
    if gb is None:
        await query.answer("Session expired - /groupbook again.", show_alert=True)
        return
    if query.from_user.id != gb["owner"]:
        await query.answer(f"Only {gb['owner_name']} drives the setup.", show_alert=True)
        return
    try:
        if action == "loc":
            gb["loc"] = int(parts[2])
            await _render(bot, chat_id, context, "cat")
        elif action == "cat":
            gb["cat"] = int(parts[2])
            await _render(bot, chat_id, context, "day")
        elif action == "day":
            gb["day"] = datetime.strptime(parts[2], "%Y%m%d").date()
            await _render(bot, chat_id, context, "start")
        elif action == "st":
            gb["start"] = datetime.combine(
                gb["day"], datetime.strptime(parts[2], "%H%M").time())
            await _render(bot, chat_id, context, "end")
        elif action == "en":
            t = datetime.strptime(parts[2], "%H%M").time()
            end = datetime.combine(gb["day"], t)
            if t == datetime.min.time():
                end += timedelta(days=1)
            gb["end"] = end
            await _render(bot, chat_id, context, "post")
        elif action == "golive":
            cat = gb["locations"][gb["loc"]].categories[gb["cat"]]
            loc = gb["locations"][gb["loc"]]
            leg_ids = storage.add_group_legs(
                chat_id, cat.lid, cat.gid, loc.name, cat.label,
                _split_legs(gb["start"], gb["end"], _leg_minutes(gb)))
            storage.set_group_msg(leg_ids, gb["msg_id"])
            owner = gb["owner"]
            context.chat_data.pop("gb", None)
            await _refresh_session_msg(bot, chat_id, gb["msg_id"])
            if len(parts) > 2 and parts[2] == "hold":
                await _chope_legs(bot, chat_id, gb["msg_id"], leg_ids, owner)
        elif action == "back":
            await _render(bot, chat_id, context, PREV.get(gb["step"], "loc"))
        elif action == "stop":
            context.chat_data.pop("gb", None)
            await bot.edit_message_text("Group booking cancelled.", chat_id=chat_id,
                                        message_id=gb["msg_id"])
    except Exception as exc:
        log.exception("groupbook callback failed")
        storage.record_error("group booking", f"{type(exc).__name__}: {exc}")
        try:
            await query.edit_message_text(
                "Something went wrong with that tap - nothing was booked or "
                "released. Try again, or /groupbook to start a new session.")
        except Exception:
            log.debug("could not report the groupbook failure", exc_info=True)


async def cmd_groupcancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Release every leg this chat is still holding."""
    chat_id = update.effective_chat.id
    rows = storage.conn().execute(
        "SELECT DISTINCT msg_id FROM group_legs WHERE chat_id=? AND hold_id IS NOT NULL",
        (chat_id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("Nothing held for this chat.")
        return
    for row in rows:
        await _release_session_holds(chat_id, row["msg_id"])
        await _refresh_session_msg(context.bot, chat_id, row["msg_id"])
    await update.effective_message.reply_text(
        "Released the group's holds. Unclaimed legs are free for anyone again.")


def register(application) -> None:
    application.add_handler(CommandHandler("groupbook", cmd_groupbook))
    application.add_handler(CommandHandler("groupcancel", cmd_groupcancel))
    application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^gb\|"))
