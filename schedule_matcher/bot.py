"""Schedule Matcher bot: group scheduling plus NTU library booking.

Scheduling data is stored in SQLite outside OneDrive and keyed by Telegram
user id. The booking commands live in schedule_matcher.booking.handlers.
"""

from __future__ import annotations

import logging
import re
import random
import string
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import (ask, availability_view, config, flows, keyboards, matching,
               storage, webapp)
from .booking import groupbook
from .booking import handlers as booking_handlers

log = logging.getLogger(__name__)

# The bot is two separate tools that happen to share a process. Nothing in
# one menu ever points into the other.
SCHEDULE_MENU = {
    "sm_new": ("➕ New", [
        ("Start a new event", "/create"),
        ("Add MY availability to an event", "/add"),
    ]),
    "sm_view": ("👀 View", [
        ("This chat's events", "/events"),
        ("Who is free when (grid)", "/view"),
        ("Best common times", "/best"),
    ]),
    "sm_edit": ("✏️ Edit", [
        ("Change my availability", "/edit"),
        ("Delete slots or an event", "/delete"),
    ]),
}

LIBRARY_MENU = {
    "lb_book": ("📖 Book", [
        ("Book a space", "/book"),
        ("My favourites", "/fav"),
        ("Check in / save a code", "/checkin"),
    ]),
    "lb_advanced": ("🧩 Advanced", [
        ("Split a long session with friends", "/groupbook"),
        ("Extended session (books, then holds)", "/extendedbooking"),
        ("Repeat a booking every week", "/recurring"),
    ]),
    "lb_view": ("👀 View", [
        ("My bookings", "/bookings"),
        ("Scheduled bookings", "/scheduled"),
        ("What's free right now", "/availability"),
    ]),
    "lb_edit": ("✏️ Edit", [
        ("Cancel / end early", "/cancelbooking"),
        ("Move a booking", "/move"),
    ]),
    "lb_settings": ("⚙️ Settings", [
        ("Set up my NTU login", "/setup"),
        ("Bot inbox for confirmation emails", "/botemail"),
        ("Library rules I know", "/rules"),
        ("Update the room list from the library", "/refreshcatalog"),
        ("How many shortcuts to show", "/mostused"),
        ("Wipe my credentials", "/forgetme"),
    ]),
}

# Only developers ever see these.
DEV_MENU = {
    "dev": ("🛠 Developer", [
        ("Chope (hold without booking)", "/chope"),
        ("Spaces I'm holding", "/holds"),
        ("How long chopes last", "/holdtime"),
        ("Diagnostics", "/developer"),
        ("Developer mode on/off", "/dev"),
    ]),
}

PRODUCTS = {
    "schedule": ("📅 Schedule Matcher", SCHEDULE_MENU),
    "library": ("📚 Library Booking", LIBRARY_MENU),
    "developer": ("🛠 Developer", DEV_MENU),
}

DM_COMMANDS = [
    ("menu", "Everything the bot can do"),
    ("book", "Book an NTU library space"),
    ("bookings", "My bookings and their codes"),
    ("checkin", "Check in, or save a check-in code"),
    ("availability", "See what's free right now"),
    ("create", "Schedule Matcher: start a new event"),
    ("add", "Schedule Matcher: add my availability"),
    ("view", "Schedule Matcher: who is free when"),
    ("setup", "Save my NTU login"),
    ("help", "How this bot works"),
]

# In groups the bot is only the scheduling half.
GROUP_COMMANDS = [
    ("create", "Start a new event"),
    ("add", "Add my availability"),
    ("view", "Who is free when"),
    ("best", "Best common times"),
    ("edit", "Change my availability"),
    ("groupbook", "Split a long library booking across members"),
]

KEYBOARD_LABELS = flows.KEYBOARD_LABELS      # defined there so both halves see it

HELP = (
    "I am two tools in one bot.\n\n"
    "📅 SCHEDULE MATCHER - find a time everyone is free\n"
    "  New:  /create starts an event, /add puts YOUR free times into it\n"
    "  View: /events lists them, /view draws the grid of who is free when,\n"
    "        /best names the best slots\n"
    "  Edit: /edit changes your times, /delete removes them\n\n"
    "📚 LIBRARY BOOKING - reserve an NTU library space (private chat)\n"
    "  Book: /book, /fav, /checkin (also stores your check-in code)\n"
    "  Advanced: /groupbook, /extendedbooking, /recurring\n"
    "  View: /bookings, /scheduled, /availability\n"
    "  Edit: /cancelbooking, /move\n"
    "  Settings: /setup, /botemail, /rules, /refreshcatalog, /mostused,\n"
    "            /forgetme\n\n"
    "Use the two buttons under the message box, or the menu button beside "
    "it - nothing has to be typed."
)


def _sections(user_id: int | None = None) -> dict:
    """Every menu section, including the developer one when allowed."""
    sections = dict(SCHEDULE_MENU)
    sections.update(LIBRARY_MENU)
    if user_id is not None and storage.is_developer(user_id):
        sections.update(DEV_MENU)
    return sections


def main_menu_markup(user_id: int | None = None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(PRODUCTS["schedule"][0],
                                  callback_data="menu|schedule")],
            [InlineKeyboardButton(PRODUCTS["library"][0],
                                  callback_data="menu|library")]]
    if user_id is not None and storage.is_developer(user_id):
        rows.append([InlineKeyboardButton(PRODUCTS["developer"][0],
                                          callback_data="menu|developer")])
    return InlineKeyboardMarkup(rows)


def product_markup(product: str, user_id: int | None = None):
    """The sections of one product, never mixing in the other's."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    _, sections = PRODUCTS[product]
    if product == "developer" and not (user_id and storage.is_developer(user_id)):
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            "« Menu", callback_data="menu|root")]])
    rows = [[InlineKeyboardButton(title, callback_data=f"menu|{key}")]
            for key, (title, _) in sections.items()]
    rows.append([InlineKeyboardButton("« Menu", callback_data="menu|root")])
    return InlineKeyboardMarkup(rows)


def submenu_markup(key: str, user_id: int | None = None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    _, entries = _sections(user_id)[key]
    back = ("menu|schedule" if key.startswith("sm_")
            else "menu|developer" if key == "dev" else "menu|library")
    rows = [[InlineKeyboardButton(f"{label}  ({cmd})", callback_data=f"run|{cmd[1:]}")]
            for label, cmd in entries]
    rows.append([InlineKeyboardButton("« Back", callback_data=back),
                 InlineKeyboardButton("Menu", callback_data="menu|root")])
    return InlineKeyboardMarkup(rows)


def persistent_keyboard():
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(
        [[KeyboardButton(k) for k in KEYBOARD_LABELS]],
        resize_keyboard=True, is_persistent=True)


def _identity(update: Update) -> tuple[int, str]:
    user = update.effective_user
    name = user.first_name or user.username or str(user.id)
    return user.id, name


# --- The availability grid: a Mini App, because a chat cannot drag ---------
#
# Telegram keyboards are discrete buttons capped at 8 per row and 100 in
# total, so a paintable week (224 half-hour cells) cannot exist in a message.
# The grid is a web page opened inside Telegram instead.
#
# One rule shapes the whole flow: sendData - the only way a page can answer
# without us running a server - works solely from a reply-keyboard button in a
# PRIVATE chat, while events live in groups. So the group carries a link and
# the painting happens in the DM.

def _deep_link(bot, chat_id: int, code: str) -> str:
    """A t.me link that opens one group's event in the private chat.

    Start payloads allow only letters, digits, _ and -, so the group's
    negative id travels with its sign written as 'n'.
    """
    return (f"https://t.me/{bot.username}?start="
            f"add_{str(chat_id).replace('-', 'n')}_{code}")


def _parse_deep_link(payload: str) -> tuple[int, str] | None:
    if not payload.startswith("add_"):
        return None
    try:
        _, raw_chat, code = payload.split("_", 2)
        return int(raw_chat.replace("n", "-", 1)), code.upper()
    except ValueError:
        return None


TAP_THROUGH = "Tap through a calendar"


def _grid_offer(chat_id: int, code: str, user_id: int):
    """The two ways to answer an event, or None when neither is possible.

    The grid has to be a reply-keyboard button - Telegram only accepts a Mini
    App's answer from one - so the calendar sits beside it in the same
    keyboard rather than as an inline button, and nobody has to leave Telegram
    to add their times.
    """
    from telegram import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

    event = storage.get_event(chat_id, code)
    if not event:
        return None
    days = storage.event_days(event)
    url = webapp.url_for(chat_id, event, days,
                         storage.user_slots(chat_id, code, user_id),
                         storage.availabilities(chat_id, code))
    rows = []
    if url:
        rows.append([KeyboardButton("Open the grid",
                                    web_app=WebAppInfo(url=url))])
    rows.append([KeyboardButton(TAP_THROUGH)])
    how = ("Drag down the strip to paint when you are free, or tap through a "
           "calendar instead - whichever suits you." if url
           else "Tap through a calendar to add your times.")
    text = (f"'{event['name']}' - {days[0]:%a %d %b} to {days[-1]:%a %d %b}.\n\n"
            f"{how} Either way, what you send replaces your previous answer."
            + _slots_summary(chat_id, code, user_id))
    return text, ReplyKeyboardMarkup(rows, resize_keyboard=True,
                                     one_time_keyboard=True)


async def _open_grid(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, code: str) -> bool:
    """Offer both ways in a private chat. False if this is not one."""
    if update.effective_chat.type != "private":
        return False
    offer = _grid_offer(chat_id, code, update.effective_user.id)
    if offer is None:
        return False
    context.user_data["grid_event"] = (chat_id, code)
    await update.effective_message.reply_text(offer[0], reply_markup=offer[1])
    return True


async def on_tap_through(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"Tap through a calendar" - the original flow, still here.

    The event usually belongs to a group while the tapping happens in a
    private chat, so the target is remembered for the callbacks that follow;
    without it they would look the event up in the DM and not find it.
    """
    target = context.user_data.get("grid_event")
    if not target:
        await update.effective_message.reply_text(
            "Which event? /add picks one.", reply_markup=persistent_keyboard())
        return
    chat_id, code = target
    event = storage.get_event(chat_id, code)
    if not event:
        await update.effective_message.reply_text(
            "That event no longer exists.", reply_markup=persistent_keyboard())
        return
    context.chat_data["adding_for"] = {"chat": chat_id, "code": code}
    now = datetime.now()
    await update.effective_message.reply_text(
        f"Pick dates for '{event['name']}'"
        f"{_slots_summary(chat_id, code, update.effective_user.id)}",
        reply_markup=keyboards.month_calendar(now.year, now.month, code))


async def _offer_privately(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                           chat_id: int, rows) -> bool:
    """DM the person their way in. False if Telegram will not let us."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    try:
        if len(rows) == 1:
            offer = _grid_offer(chat_id, rows[0]["code"], user_id)
            if offer is None:
                return False
            context.user_data["grid_event"] = (chat_id, rows[0]["code"])
            await context.bot.send_message(user_id, offer[0], reply_markup=offer[1])
            return True
        await context.bot.send_message(
            user_id, "Which event do you want to add your times to?",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{r['name']} ({r['code']})",
                                       url=_deep_link(context.bot, chat_id, r["code"]))]
                 for r in rows]))
        return True
    except Exception:
        log.info("cannot DM %s yet - answering in the group instead", user_id)
        return False


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A painted grid coming back. Everything in it is a stranger's until checked."""
    payload = update.effective_message.web_app_data.data
    data = webapp.read_reply(payload)
    user_id, user_name = _identity(update)
    if data is None:
        await update.effective_message.reply_text(
            "I couldn't read what the grid sent. Nothing was changed - /add "
            "opens it again.", reply_markup=persistent_keyboard())
        return

    chat_id, code = data["chat_id"], data["code"]
    event = storage.get_event(chat_id, code)
    if not event:
        await update.effective_message.reply_text(
            "That event no longer exists, so I have not saved anything.",
            reply_markup=persistent_keyboard())
        return

    # The page is a public URL and the DM has no idea which group is which, so
    # anyone could name any chat id. Ask Telegram whether they belong there.
    if chat_id != user_id:
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            allowed = member.status not in ("left", "kicked")
        except Exception:
            log.warning("membership check failed for %s in %s", user_id, chat_id)
            allowed = False
        if not allowed:
            await update.effective_message.reply_text(
                "That event belongs to a group you are not in, so I have not "
                "saved anything.", reply_markup=persistent_keyboard())
            return

    storage.replace_slots(chat_id, code, user_id, user_name, data["intervals"])
    merged = matching.merge_intervals(data["intervals"])
    if merged:
        lines = "\n".join(f"  {s:%a %d %b}  {s:%H:%M} - {e:%H:%M}" for s, e in merged)
        body = f"Saved for '{event['name']}':\n{lines}"
    else:
        body = (f"Saved for '{event['name']}': you are not free on any of "
                "those days.")
    await update.effective_message.reply_text(
        f"{body}\n\nSend it again any time to change it - the newest answer "
        f"replaces the last. /best {code} 60 finds the best common time.",
        reply_markup=persistent_keyboard())
    await _update_board(context.bot, chat_id, code)


async def _update_board(bot, chat_id: int, code: str) -> None:
    """Edit the one group message that tracks who has answered.

    Editing rather than posting is the whole point: a group of five would
    otherwise collect five "X added their times" messages per event.
    """
    event = storage.get_event(chat_id, code)
    if not event:
        return
    try:
        board_chat, board_msg = event["board_chat_id"], event["board_msg_id"]
    except (IndexError, KeyError):
        return
    if not board_chat or not board_msg:
        return
    try:
        await bot.edit_message_text(
            chat_id=board_chat, message_id=board_msg,
            text=_board_text(chat_id, code),
            reply_markup=_paint_markup(bot, chat_id, code))
    except Exception:
        log.debug("could not update the board for %s", code, exc_info=True)


def _board_text(chat_id: int, code: str) -> str:
    event = storage.get_event(chat_id, code)
    days = storage.event_days(event)
    avail = storage.availabilities(chat_id, code)
    who = ", ".join(sorted(avail)) if avail else "nobody yet"
    return (f"{event['name']}  (code {code})\n"
            f"{days[0]:%a %d %b} to {days[-1]:%a %d %b}\n\n"
            f"Answered: {who}\n\n"
            f"/view shows the grid, /best {code} 60 the best times.")


def _paint_markup(bot, chat_id: int, code: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "Paint my availability", url=_deep_link(bot, chat_id, code))]])


async def _post_board(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      code: str) -> None:
    """Put the event's one message in the chat the event belongs to.

    Sent rather than replied to, because the organiser may be setting this up
    from a private chat while the event lives in a group.
    """
    msg = await context.bot.send_message(
        chat_id, _board_text(chat_id, code),
        reply_markup=_paint_markup(context.bot, chat_id, code))
    storage.set_event_board(chat_id, code, msg.chat_id, msg.message_id)


async def _ask_which_chat(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          name: str) -> None:
    """Telegram's own group picker, limited to groups the bot is already in."""
    from telegram import (KeyboardButton, KeyboardButtonRequestChat,
                          ReplyKeyboardMarkup)

    context.user_data["pending_event"] = name
    await update.effective_message.reply_text(
        f"'{name}' - which chat is it for?\n\n"
        "Pick the group and I will post it there, so everyone can paint their "
        "availability. Only groups I am already in will be offered.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(
                "Choose the group",
                request_chat=KeyboardButtonRequestChat(
                    request_id=1, chat_is_channel=False, bot_is_member=True,
                    request_title=True))],
             [KeyboardButton("Just for me")]],
            resize_keyboard=True, one_time_keyboard=True))


async def on_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The group came back from Telegram's picker: create the event there."""
    shared = update.effective_message.chat_shared
    name = context.user_data.pop("pending_event", None)
    if not name:
        await update.effective_message.reply_text(
            "I have lost track of which event that was for - /create starts "
            "again.", reply_markup=persistent_keyboard())
        return
    target = shared.chat_id
    title = getattr(shared, "title", None) or "that group"
    code = _new_code()
    try:
        storage.create_event(target, code, name, update.effective_user.id)
    except Exception:
        log.exception("could not create %s in %s", name, target)
        await update.effective_message.reply_text(
            "I could not create it there. Am I still in that group?",
            reply_markup=persistent_keyboard())
        return
    context.user_data["event_chat_title"] = title
    await _ask_dates(update, context, target, code, name, where=f" in {title}")


async def on_just_for_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The other answer to "which chat": an event nobody else can see."""
    name = context.user_data.pop("pending_event", None)
    if not name:
        return
    code = _new_code()
    storage.create_event(update.effective_chat.id, code, name,
                         update.effective_user.id)
    await update.effective_message.reply_text(
        "Right - this one is just yours.", reply_markup=persistent_keyboard())
    await _ask_dates(update, context, update.effective_chat.id, code, name)


def _new_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# --- Which days is the event about ----------------------------------------

DATE_PRESETS = (("The next 7 days", "7"), ("The next 14 days", "14"),
                ("Next week, Mon-Sun", "mon"))


async def _ask_dates(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, code: str, name: str, where: str = "") -> None:
    """Which days does this event cover?

    The target chat rides in the callback data: this question is often asked
    in a private chat about an event that belongs to a group, so the chat the
    buttons are tapped in is not the chat that matters.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[InlineKeyboardButton(label, callback_data=f"evd|{chat_id}|{code}|{key}")]
            for label, key in DATE_PRESETS]
    rows.append([InlineKeyboardButton("Pick the dates on a calendar",
                                      callback_data=f"evd|{chat_id}|{code}|cal")])
    await update.effective_message.reply_text(
        f"'{name}' created{where}, code {code}.\n\nWhich days is it about? "
        "Everyone paints their availability across these days.",
        reply_markup=InlineKeyboardMarkup(rows))


async def _finish_dates(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                        code: str, name: str, start: date, end: date,
                        trimmed: bool = False) -> None:
    """Record the range, post the event where it belongs, say so."""
    note = (f"\n(Trimmed to {webapp.MAX_DAYS} days - that is as many as the "
            "grid paints at once.)" if trimmed else "")
    try:
        await _post_board(context, chat_id, code)
    except Exception:
        log.exception("could not post the board for %s in %s", code, chat_id)
        await query.edit_message_text(
            f"'{name}' covers {start:%a %d %b} to {end:%a %d %b}, but I could "
            "not post it in that chat. Am I still there, and allowed to send "
            "messages?")
        return
    where = ""
    if query.message and chat_id != query.message.chat_id:
        title = context.user_data.get("event_chat_title", "the group")
        where = f"\n\nPosted in {title} - everyone there can answer it now."
    await query.edit_message_text(
        f"'{name}' covers {start:%a %d %b} to {end:%a %d %b}.{note}{where}")


def _preset_range(key: str) -> tuple[date, date]:
    today = date.today()
    if key == "14":
        return today, today + timedelta(days=13)
    if key == "mon":
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return monday, monday + timedelta(days=6)
    return today, today + timedelta(days=6)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greet with the help text, the persistent keyboard and the menu.

    A deep link from a group - "open the grid for this event" - arrives here
    as the start payload. That indirection exists because a Mini App may only
    send its answer back from a private chat, while events live in groups.
    """
    target = _parse_deep_link((context.args or [""])[0])
    if target and await _open_grid(update, context, *target):
        return
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            HELP, reply_markup=persistent_keyboard())
        await update.effective_message.reply_text(
            "Which one?", reply_markup=main_menu_markup(update.effective_user.id))
    else:
        await update.effective_message.reply_text(HELP)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Which one?", reply_markup=main_menu_markup(update.effective_user.id))


async def cmd_dev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Developer mode. Only the owner (OWNER_ID in .env) may grant it."""
    user_id = update.effective_user.id
    args = context.args or []
    is_owner = config.OWNER_ID is not None and user_id == config.OWNER_ID
    if not is_owner and not storage.is_developer(user_id):
        return                       # silent for everyone else
    if not args:
        extras = ", ".join(f"/{c}" for c in ("chope", "holds", "developer"))
        await update.effective_message.reply_text(
            f"Developer mode is ON for you - {extras} are available and the "
            "Developer section shows in /menu."
            + ("\n\nOwner commands: /dev add <id>, /dev remove <id>, /dev list"
               if is_owner else ""))
        return
    if not is_owner:
        await update.effective_message.reply_text("Only the owner can change this.")
        return
    action = args[0].lower()
    if action == "list":
        ids = storage.list_developers()
        await update.effective_message.reply_text(
            "Developers: " + (", ".join(str(i) for i in ids) if ids else "just you"))
        return
    if action in ("add", "remove") and len(args) > 1 and args[1].lstrip("-").isdigit():
        target = int(args[1])
        if action == "add":
            storage.add_developer(target, user_id)
            await update.effective_message.reply_text(f"{target} can now use developer mode.")
        else:
            storage.remove_developer(target)
            await update.effective_message.reply_text(f"{target} no longer has developer mode.")
        return
    await update.effective_message.reply_text(
        "Usage: /dev, /dev add <telegram id>, /dev remove <id>, /dev list")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP)


async def on_keyboard_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The two persistent buttons open one product each."""
    product = KEYBOARD_LABELS.get((update.effective_message.text or "").strip())
    if not product:
        return
    user_id = update.effective_user.id
    msg = await update.effective_message.reply_text(
        PRODUCTS[product][0], reply_markup=product_markup(product, user_id))
    if product in (flows.LIBRARY, flows.SCHEDULE):
        flows.remember(context, product, msg.chat_id, msg.message_id)


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """menu|root -> the two products; menu|<product> -> its sections;
    menu|<section> -> that section's commands."""
    query = update.callback_query
    await query.answer()
    key = query.data.split("|", 1)[1]
    user_id = query.from_user.id
    if key == "root":
        await query.edit_message_text("Which one?",
                                      reply_markup=main_menu_markup(user_id))
        return
    if key in PRODUCTS:
        await query.edit_message_text(
            PRODUCTS[key][0], reply_markup=product_markup(key, user_id))
        # The product menu is that tool's live screen, so the command you pick
        # from it replaces it instead of leaving two sets of buttons.
        if key in (flows.LIBRARY, flows.SCHEDULE) and query.message:
            flows.remember(context, key, query.message.chat_id,
                           query.message.message_id)
        return
    sections = _sections(user_id)
    if key not in sections:
        await query.edit_message_text("Which one?",
                                      reply_markup=main_menu_markup(user_id))
        return
    await query.edit_message_text(sections[key][0],
                                  reply_markup=submenu_markup(key, user_id))


async def on_run_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """run|<command> - a menu entry was tapped, so run that command."""
    query = update.callback_query
    await query.answer()
    name = query.data.split("|", 1)[1]
    handler = MENU_ACTIONS.get(name)
    if handler is None:
        await query.edit_message_text(f"/{name} - type it and I'll run it.")
        return
    # The command handlers reply to a message, so hand them the menu message
    # with empty args, exactly as if the user had typed the bare command.
    context.args = []
    await handler(update, context)


async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start an event. Tapped from a menu there are no arguments, so ask."""
    if context.args and update.effective_chat.type == "private":
        # An event is owned by a chat, and a private chat is only ever the
        # organiser's own. Ask which group this one is for, using Telegram's
        # own picker so nobody has to know a chat id.
        await _ask_which_chat(update, context, " ".join(context.args))
        return
    if not context.args:
        await ask.ask(update, context, "event_name",
                      "What should the event be called?",
                      suggestion=_default_event_name(update.effective_chat.id))
        return
    await _create_event(update, context, " ".join(context.args))


def _default_event_name(chat_id: int) -> str:
    """Something usable in one tap: 'Meeting 2' if there is already a meeting."""
    existing = len(storage.list_events(chat_id))
    return f"Meeting {existing + 1}" if existing else "Meeting"


async def _create_event(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        name: str) -> None:
    name = name.strip()[:80] or _default_event_name(update.effective_chat.id)
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    storage.create_event(update.effective_chat.id, code, name, update.effective_user.id)
    if config.WEBAPP_URL:
        await _ask_dates(update, context, update.effective_chat.id, code, name)
        return
    await update.effective_message.reply_text(
        f"Event '{name}' created!\nCode: {code}\nEveryone: use /add to enter availability.")


async def _answer_event_name(update, context, answer: str, pending: dict) -> None:
    await _create_event(update, context, answer)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = storage.list_events(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("No events yet - /create <Name> to start one.")
        return
    await update.effective_message.reply_text(
        "\n".join(f"{r['name']} - code {r['code']}" for r in rows))


def _events_keyboard(chat_id: int, prefix: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = storage.list_events(chat_id)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{r['name']} ({r['code']})", callback_data=f"{prefix}|{r['code']}")]
         for r in rows])


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    chat_id = update.effective_chat.id
    rows = storage.list_events(chat_id)
    if not rows:
        await update.effective_message.reply_text("No events yet - /create <Name> first.")
        return
    # Answering happens privately - a Mini App may only reply from a DM, and
    # a group does not want everybody's screens in it. So the reply goes to
    # the person, and the group hears nothing at all.
    if update.effective_chat.type != "private":
        if await _offer_privately(context, update.effective_user.id, chat_id, rows):
            return
        # Telegram forbids a bot from messaging someone who has never started
        # it, and for them a silent group is the same as a broken bot.
        await update.effective_message.reply_text(
            "Tap to add your times - it opens our private chat:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"{r['name']} ({r['code']})",
                                       url=_deep_link(context.bot, chat_id, r["code"]))]
                 for r in rows]))
        return
    if len(rows) == 1 and await _open_grid(update, context, chat_id, rows[0]["code"]):
        return
    await flows.start(update, context, flows.SCHEDULE, "Which event?",
                      reply_markup=_events_keyboard(chat_id, "evt"))


async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The when2meet-style grid: who is free when."""
    kb = _events_keyboard(update.effective_chat.id, "view")
    if not kb:
        await update.effective_message.reply_text(
            "No events yet - /create <name> starts one.")
        return
    await flows.start(update, context, flows.SCHEDULE,
                      "Show the availability grid for which event?",
                      reply_markup=kb)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change my own availability: add more, or drop slots I no longer have."""
    kb = _events_keyboard(update.effective_chat.id, "edit_evt")
    if not kb:
        await update.effective_message.reply_text("No events yet.")
        return
    await flows.start(update, context, flows.SCHEDULE,
                      "Change your availability in which event?",
                      reply_markup=kb)


def _grid_markup(code: str, picking: bool, names: list[str],
                 chosen: set[str]):
    """Buttons under the grid: everyone / pick people / image."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    if picking:
        for name in names:
            mark = "\u2705" if name in chosen else "\u2b1c"
            rows.append([InlineKeyboardButton(
                f"{mark} {name}", callback_data=f"gpick|{code}|{name[:24]}")])
        rows.append([InlineKeyboardButton("Show grid for these",
                                          callback_data=f"gshow|{code}"),
                     InlineKeyboardButton("Everyone",
                                          callback_data=f"gall|{code}")])
    else:
        rows.append([InlineKeyboardButton("Pick people\u2026",
                                          callback_data=f"gpickmode|{code}"),
                     InlineKeyboardButton("\U0001f5bc Send as image",
                                          callback_data=f"gimg|{code}")])
        rows.append([InlineKeyboardButton("Best times",
                                          callback_data=f"gbest|{code}")])
    return InlineKeyboardMarkup(rows)


async def _send_grid(query, context, chat_id: int, code: str, picking=False):
    event = storage.get_event(chat_id, code)
    avail = storage.availabilities(chat_id, code)
    if not avail:
        await query.edit_message_text(
            f"'{event['name']}': nobody has added availability yet - /add does that.")
        return
    names = sorted(avail)
    chosen = set(context.chat_data.get(f"gpick_{code}", names))
    subset = None if chosen == set(names) else sorted(chosen)
    text = availability_view.emoji_grid(
        avail, people=subset, title=f"{event['name']} (code {code})")
    await query.edit_message_text(
        text[:4000], reply_markup=_grid_markup(code, picking, names, chosen))


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = _events_keyboard(update.effective_chat.id, "del_evt")
    if not kb:
        await update.effective_message.reply_text("No events yet.")
        return
    await flows.start(update, context, flows.SCHEDULE,
                      "Delete your slots from which event?", reply_markup=kb)


BEST_DURATIONS = (30, 60, 90, 120, 180)


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Best common times. With no code, offer the chat's events as buttons."""
    if not context.args:
        kb = _events_keyboard(update.effective_chat.id, "best")
        if not kb:
            await update.effective_message.reply_text(
                "No events yet - /create starts one.")
            return
        await flows.start(update, context, flows.SCHEDULE,
                          "Best times for which event?", reply_markup=kb)
        return
    code = context.args[0].upper()
    try:
        minutes = int(context.args[1]) if len(context.args) > 1 else 60
    except ValueError:
        await update.effective_message.reply_text("Duration must be a number of minutes.")
        return
    event = storage.get_event(update.effective_chat.id, code)
    if not event:
        await update.effective_message.reply_text("Event not found in this chat.")
        return
    avail = storage.availabilities(update.effective_chat.id, code)
    if not avail:
        await update.effective_message.reply_text("Nobody has added availability yet - use /add.")
        return
    suggestions, everyone = matching.best_slots(avail, minutes)
    await update.effective_message.reply_text(matching.format_suggestions(suggestions, everyone, minutes))


async def _best_duration_prompt(query, chat_id: int, code: str) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    event = storage.get_event(chat_id, code)
    rows = [[InlineKeyboardButton(f"{m} min" if m < 60 else f"{m // 60}h{m % 60 or ''}",
                                  callback_data=f"bestdur|{code}|{m}")
             for m in BEST_DURATIONS[i:i + 3]] for i in (0, 3)]
    await query.edit_message_text(
        f"'{event['name'] if event else code}' - how long does it need to be?",
        reply_markup=InlineKeyboardMarkup([r for r in rows if r]))


async def _best_result(query, chat_id: int, code: str, minutes: int) -> None:
    event = storage.get_event(chat_id, code)
    if not event:
        await query.edit_message_text("That event no longer exists.")
        return
    avail = storage.availabilities(chat_id, code)
    if not avail:
        await query.edit_message_text("Nobody has added availability yet - use /add.")
        return
    suggestions, everyone = matching.best_slots(avail, minutes)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    await query.edit_message_text(
        matching.format_suggestions(suggestions, everyone, minutes),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "See the full grid", callback_data=f"view|{code}")]]))


def _slots_summary(chat_id: int, code: str, user_id: int) -> str:
    slots = storage.user_slots(chat_id, code, user_id)
    if not slots:
        return ""
    merged = matching.merge_intervals(slots)
    lines = "\n".join(f"- {s:%a %d %b %H:%M} to {e:%H:%M}" for s, e in merged)
    return f"\n\nYour availability:\n{lines}"


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id, user_name = _identity(update)
    parts = query.data.split("|")
    action = parts[0]

    if action == "ignore":
        return

    if action == "evd":
        target, code, key = int(parts[1]), parts[2], parts[3]
        event = storage.get_event(target, code)
        if not event:
            await query.edit_message_text("That event no longer exists.")
            return
        if key == "cal":
            # Reuse the month calendar: the first tap is the first day, the
            # second the last. A note on chat_data tells the date| branch that
            # this calendar is choosing a range, not a day to add times to -
            # and which chat's event it belongs to, which may not be this one.
            context.chat_data["range_pick"] = {"code": code, "chat": target,
                                               "first": None}
            now = datetime.now()
            await query.edit_message_text(
                f"'{event['name']}' - tap the FIRST day it covers:",
                reply_markup=keyboards.month_calendar(now.year, now.month, code))
            return
        start, end = _preset_range(key)
        storage.set_event_dates(target, code, start, end)
        await _finish_dates(query, context, target, code, event["name"], start, end)
        return

    # A range being picked has to be recognised before the guard below, which
    # looks the event up in the chat the buttons are in - wrong when a group's
    # event is being set up from a private chat.
    picking = context.chat_data.get("range_pick")
    if picking and action in ("date", "nav") and parts[-1] == picking.get("code"):
        target, code = picking["chat"], picking["code"]
        event = storage.get_event(target, code)
        if not event:
            context.chat_data.pop("range_pick", None)
            await query.edit_message_text("That event no longer exists.")
            return
        if action == "nav":
            await query.edit_message_text(
                query.message.text or "Pick a day:",
                reply_markup=keyboards.month_calendar(int(parts[1]), int(parts[2]), code))
            return
        chosen = date(int(parts[1]), int(parts[2]), int(parts[3]))
        if picking["first"] is None:
            picking["first"] = chosen.isoformat()
            await query.edit_message_text(
                f"First day {chosen:%a %d %b}. Now tap the LAST day:",
                reply_markup=keyboards.month_calendar(chosen.year, chosen.month, code))
            return
        context.chat_data.pop("range_pick", None)
        first = date.fromisoformat(picking["first"])
        start, end = min(first, chosen), max(first, chosen)
        span = (end - start).days + 1
        if span > webapp.MAX_DAYS:
            end = start + timedelta(days=webapp.MAX_DAYS - 1)
        storage.set_event_dates(target, code, start, end)
        await _finish_dates(query, context, target, code, event["name"], start, end,
                            trimmed=span > webapp.MAX_DAYS)
        return

    if action == "best":
        await _best_duration_prompt(query, chat_id, parts[1])
        return
    if action == "bestdur":
        await _best_result(query, chat_id, parts[1], int(parts[2]))
        return

    if action in ("gpickmode", "gpick", "gshow", "gall", "gimg", "gbest"):
        code = parts[1]
        key = f"gpick_{code}"
        avail = storage.availabilities(chat_id, code)
        names = sorted(avail)
        if action == "gpickmode":
            context.chat_data.setdefault(key, list(names))
            await _send_grid(query, context, chat_id, code, picking=True)
        elif action == "gpick":
            chosen = set(context.chat_data.get(key, names))
            wanted = parts[2]
            match = next((n for n in names if n[:24] == wanted), None)
            if match:
                chosen.symmetric_difference_update({match})
            context.chat_data[key] = sorted(chosen) or list(names)
            await _send_grid(query, context, chat_id, code, picking=True)
        elif action == "gall":
            context.chat_data[key] = list(names)
            await _send_grid(query, context, chat_id, code)
        elif action == "gshow":
            await _send_grid(query, context, chat_id, code)
        elif action == "gbest":
            suggestions, everyone = matching.best_slots(avail, 60)
            await query.edit_message_text(
                matching.format_suggestions(suggestions, everyone, 60))
        elif action == "gimg":
            chosen = set(context.chat_data.get(key, names))
            subset = None if chosen == set(names) else sorted(chosen)
            event = storage.get_event(chat_id, code)
            png = availability_view.png_grid(
                avail, people=subset, title=f"{event['name']} ({code})")
            if png is None:
                await query.answer("Image needs Pillow installed.", show_alert=True)
            else:
                await context.bot.send_photo(
                    chat_id, png, caption=f"{event['name']} - who is free when")
        return

    if action in ("evt", "nav", "date", "t_start", "t_save", "done", "view",
                  "del_evt", "del_slot", "edit_evt"):
        code = parts[-1] if action != "del_slot" else parts[1]
        if action in ("view", "del_evt", "done") :
            code = parts[1]
        # Tapping through a calendar in a private chat for a group's event:
        # the event lives in the group, not in the chat the buttons are in.
        adding = context.chat_data.get("adding_for")
        if adding and adding.get("code") == code:
            chat_id = adding["chat"]
        event = storage.get_event(chat_id, code)
        if not event:
            await query.edit_message_text("That event no longer exists.")
            return

    if action == "evt":
        code = parts[1]
        if await _open_grid(update, context, chat_id, code):
            await query.edit_message_text(
                f"Opening the grid for '{event['name']}'.\n\n"
                "Prefer tapping through a calendar? /edit still does that.")
            return
        now = datetime.now()
        await query.edit_message_text(
            f"Pick dates for '{event['name']}'{_slots_summary(chat_id, code, user_id)}",
            reply_markup=keyboards.month_calendar(now.year, now.month, code))

    elif action == "nav":
        year, month, code = int(parts[1]), int(parts[2]), parts[3]
        await query.edit_message_text(
            f"Pick dates for '{event['name']}'{_slots_summary(chat_id, code, user_id)}",
            reply_markup=keyboards.month_calendar(year, month, code))

    elif action == "date":
        year, month, day, code = int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
        await query.edit_message_text(
            f"Start time on {year}-{month:02d}-{day:02d}:{_slots_summary(chat_id, code, user_id)}",
            reply_markup=keyboards.start_time_picker(year, month, day, code))

    elif action == "t_start":
        year, month, day, h, m, code = (int(parts[1]), int(parts[2]), int(parts[3]),
                                        int(parts[4]), int(parts[5]), parts[6])
        await query.edit_message_text(
            f"Start {h:02d}:{m:02d} - pick an end time:{_slots_summary(chat_id, code, user_id)}",
            reply_markup=keyboards.end_time_picker(year, month, day, h, m, code))

    elif action == "t_save":
        year, month, day = int(parts[1]), int(parts[2]), int(parts[3])
        sh, sm, eh, em, code = int(parts[4]), int(parts[5]), int(parts[6]), int(parts[7]), parts[8]
        day_start = datetime(year, month, day)
        added = storage.add_slot(chat_id, code, user_id, user_name,
                                 day_start + timedelta(hours=sh, minutes=sm),
                                 day_start + timedelta(hours=eh, minutes=em))
        await query.answer("Added!" if added else "Already added.", show_alert=not added)
        await query.edit_message_text(
            f"Add another slot, or Done:{_slots_summary(chat_id, code, user_id)}",
            reply_markup=keyboards.start_time_picker(year, month, day, code))

    elif action == "done":
        code = parts[1]
        context.chat_data.pop("adding_for", None)
        flows.finish(context, flows.SCHEDULE)   # a result screen, keep it
        await query.edit_message_text(
            f"Saved for '{event['name']}'.{_slots_summary(chat_id, code, user_id)}\n\n"
            f"/best {code} 60 finds the best common time.")

    elif action == "view":
        await _send_grid(query, context, chat_id, parts[1])

    elif action == "edit_evt":
        # Editing is "add more" and "remove", so offer both entry points.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        code = parts[1]
        await query.edit_message_text(
            f"'{event['name']}' - your availability"
            f"{_slots_summary(chat_id, code, user_id)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Add more times", callback_data=f"evt|{code}")],
                [InlineKeyboardButton("Remove a slot", callback_data=f"del_evt|{code}")],
            ]))

    elif action == "del_evt":
        code = parts[1]
        slots = storage.user_slots(chat_id, code, user_id)
        if not slots:
            await query.edit_message_text("You have no slots in this event.")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = [[InlineKeyboardButton(f"Delete {s:%a %d %b %H:%M}-{e:%H:%M}",
                                    callback_data=f"del_slot|{code}|{int(s.timestamp())}")]
              for s, e in slots]
        kb.append([InlineKeyboardButton("Done", callback_data=f"done|{code}")])
        await query.edit_message_text("Tap a slot to delete it:",
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "del_slot":
        code, ts = parts[1], int(parts[2])
        storage.delete_slot(chat_id, code, user_id, datetime.fromtimestamp(ts))
        slots = storage.user_slots(chat_id, code, user_id)
        if not slots:
            await query.edit_message_text("All your slots are deleted.")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = [[InlineKeyboardButton(f"Delete {s:%a %d %b %H:%M}-{e:%H:%M}",
                                    callback_data=f"del_slot|{code}|{int(s.timestamp())}")]
              for s, e in slots]
        kb.append([InlineKeyboardButton("Done", callback_data=f"done|{code}")])
        await query.edit_message_text("Tap a slot to delete it:",
                                      reply_markup=InlineKeyboardMarkup(kb))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Never fail silently.

    Without this, an exception in a handler only reached the log ("No error
    handlers are registered") and the user saw nothing at all happen - which
    is exactly how a missing argument in the cancel/move picker went unnoticed.
    """
    log.exception("handler failed", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return
    detail = ""
    user = getattr(update, "effective_user", None)
    if user is not None and storage.is_developer(user.id):
        detail = (f"\n\n{type(context.error).__name__}: "
                  f"{context.error}")
    try:
        await context.bot.send_message(
            chat.id,
            "Something went wrong on my side - it is written to the log and "
            "nothing was changed. Try again, or /menu to start over." + detail)
    except Exception:
        pass


def _menu_actions():
    """command name -> handler, for the buttons in the menu."""
    from .booking import groupbook
    from .booking import handlers as bh
    return {
        "book": bh.cmd_book, "fav": bh.cmd_fav, "bookings": bh.cmd_bookings,
        "holds": bh.cmd_holds, "scheduled": bh.cmd_scheduled,
        "recurring": bh.cmd_recurring,
        "checkin": bh.cmd_checkin, "code": bh.cmd_code,
        "cancelbooking": bh.cmd_cancel_booking, "move": bh.cmd_move,
        "setup": bh.cmd_setup, "email": bh.cmd_email,
        "botemail": bh.cmd_botemail, "rules": bh.cmd_rules,
        "forgetme": bh.cmd_forgetme, "groupbook": groupbook.cmd_groupbook,
        "create": cmd_create, "add": cmd_add, "view": cmd_view,
        "best": cmd_best, "events": cmd_events, "delete": cmd_delete,
        "edit": cmd_edit, "dev": cmd_dev,
        "availability": bh.cmd_availability, "mostused": bh.cmd_mostused,
        "chope": bh.cmd_chope, "holdtime": bh.cmd_holdtime,
        "developer": bh.cmd_developer,
        "refreshcatalog": bh.cmd_refreshcatalog,
        "extendedbooking": bh.cmd_extendedbooking,
    }


MENU_ACTIONS = {}


class _RedactToken(logging.Filter):
    """Keep the bot token out of the log file.

    python-telegram-bot talks to api.telegram.org/bot<TOKEN>/method, and httpx
    logs that URL at INFO. Anyone handed a log - or a /developer dump - would
    have had the token, which is enough to take over the bot.
    """

    _PAT = re.compile(r"bot\d+:[A-Za-z0-9_\-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "bot" in msg and ":" in msg:
            cleaned = self._PAT.sub("bot<token hidden>", msg)
            if cleaned != msg:
                record.msg, record.args = cleaned, ()
        return True


async def _catalogue_watch(app) -> None:
    """Go and look at the library now and then, instead of being told.

    The site publishes no category list without a login, which is why a
    measured copy ships with the code - but a shipped copy is a starting
    point, not a thing anyone should have to maintain. Once somebody has
    signed in, the bot checks for itself and learns whatever is new.
    """
    from .booking import catalog, credstore

    if config.CATALOG_MAX_AGE_DAYS <= 0:
        return
    # A machine that is only on a few days a week must not be the machine with
    # last month's room names, so the cheap check happens every start: six
    # page loads in one browser session. The expensive pass - every desk name
    # and policy re-read - still waits for CATALOG_MAX_AGE_DAYS.
    # Never let a browse for room names compete with a booking race: the check
    # costs a browser and a minute or two, and a 23:59 window does not wait.
    racing = storage.conn().execute(
        "SELECT 1 FROM scheduled_bookings WHERE status IN ('pending','retrying')"
        " AND fire_at <= ? LIMIT 1",
        ((datetime.now() + timedelta(minutes=15)).strftime(storage.FMT),)).fetchone()
    if racing:
        log.info("Catalogue: a booking is about to fire, so the look at the "
                 "library waits for the next start.")
        return
    # Opening hours first: the grid is public, so this needs no login and no
    # browser, and it is the only way to learn a weekday the site had not
    # published last time anyone looked.
    try:
        learned = await catalog.refresh_hours()
        if learned:
            log.info("Catalogue: %d more opening times observed.", learned)
    except Exception:
        log.warning("could not observe opening hours", exc_info=True)

    age = storage.durable_age_days("category_meta")
    deep = age is None or age >= config.CATALOG_MAX_AGE_DAYS
    row = storage.conn().execute(
        "SELECT user_id, ntu_username, ntu_password FROM users"
        " WHERE ntu_username IS NOT NULL AND ntu_password IS NOT NULL"
        " LIMIT 1").fetchone()
    if row is None:
        log.info("Catalogue: nobody has signed in yet, so the shipped copy "
                 "stands. It gains anything new the first time someone does.")
        return
    try:
        changed = await catalog.discover(row["user_id"],
                                         credstore.decrypt(row["ntu_username"]),
                                         credstore.decrypt(row["ntu_password"]),
                                         deep=deep)
    except Exception:
        log.warning("catalogue check failed", exc_info=True)
        return
    if not changed:
        log.info("Catalogue: checked%s, nothing has changed at the library.",
                 " thoroughly" if deep else "")
        return
    log.info("Catalogue: %s", "; ".join(changed))
    if config.OWNER_ID:
        try:
            await app.bot.send_message(
                config.OWNER_ID,
                "The library has changed since I last looked:\n  "
                + "\n  ".join(changed) + "\n\n/book has it now.")
        except Exception:
            log.debug("could not tell the owner", exc_info=True)


async def _check_grid_published() -> None:
    """A working grid or none at all - never a button that goes nowhere.

    The Mini App has to be reachable by Telegram on someone's phone, which a
    synced folder cannot do: it needs a public HTTPS address. If the page is
    not published, the bot quietly falls back to the tap-through calendar
    rather than offering a button that opens a 404.
    """
    import httpx

    url = config.WEBAPP_URL
    if not url:
        log.info("Availability grid: off (WEBAPP_URL is empty), using the "
                 "calendar pickers.")
        return
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            resp = await client.get(url)
        published = (resp.status_code == 200
                     and "telegram-web-app.js" in resp.text)
        why = f"HTTP {resp.status_code}"
    except Exception as exc:                                  # offline, DNS, TLS
        published, why = False, f"{type(exc).__name__}: {exc}"
    if published:
        log.info("Availability grid: live at %s", url)
        return
    config.WEBAPP_URL = ""
    log.warning(
        "Availability grid at %s is not published (%s), so /create and /add "
        "will use the tap-through calendar instead. To switch the grid on, "
        "enable GitHub Pages for the repo: Settings > Pages > Deploy from a "
        "branch > main > /docs.", url, why)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    config.validate()
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000,
                                       backupCount=2, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler.addFilter(_RedactToken())
    logging.getLogger().addHandler(file_handler)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactToken())
    # Every poll logged the full API URL, token and all - 9,692 lines of it in
    # three log files, and /developer prints log tails to developers. The
    # filter above catches any that slip through; this stops the flood.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for note in config.warnings():
        log.warning(note)
    storage.conn()  # opens DB, runs schema + pickle migration

    from .booking import holds, scheduler

    async def _post_init(app):
        from telegram import BotCommand, BotCommandScopeAllGroupChats
        try:
            await app.bot.set_my_commands(
                [BotCommand(c, d) for c, d in DM_COMMANDS])
            await app.bot.set_my_commands(
                [BotCommand(c, d) for c, d in GROUP_COMMANDS],
                scope=BotCommandScopeAllGroupChats())
        except Exception:
            log.warning("could not publish the command menu", exc_info=True)
        await _check_grid_published()
        app.create_task(_catalogue_watch(app))
        app.create_task(scheduler.run(app))
        app.create_task(holds.watcher(app))
        app.create_task(holds.restore(app))     # re-take holds a restart dropped

    async def _post_shutdown(app):
        # Give back any choped slots instead of leaving them blocked.
        for hold in list(holds._holds.values()):
            await holds.release(hold)

    application = (ApplicationBuilder().token(config.TELEGRAM_TOKEN)
                   .post_init(_post_init).post_shutdown(_post_shutdown).build())
    MENU_ACTIONS.update(_menu_actions())
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("dev", cmd_dev))
    application.add_handler(CommandHandler("create", cmd_create))
    application.add_handler(CommandHandler("events", cmd_events))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("view", cmd_view))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CommandHandler("best", cmd_best))
    application.add_handler(CommandHandler("edit", cmd_edit))
    booking_handlers.register(application)
    groupbook.register(application)
    ask.register("event_name", _answer_event_name)
    ask.register("email", booking_handlers.answer_email)
    application.add_handler(CallbackQueryHandler(ask.on_default_button,
                                                 pattern=r"^ask\|default$"))
    application.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND, ask.on_reply), group=-1)
    application.add_handler(CallbackQueryHandler(on_menu_callback, pattern=r"^menu\|"))
    application.add_handler(CallbackQueryHandler(on_run_callback, pattern=r"^run\|"))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE
        & filters.Text(list(KEYBOARD_LABELS)), on_keyboard_label), group=-1)
    application.add_handler(MessageHandler(
        filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    application.add_handler(MessageHandler(
        filters.StatusUpdate.CHAT_SHARED, on_chat_shared))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & filters.Text(["Just for me"]),
        on_just_for_me), group=-1)
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & filters.Text([TAP_THROUGH]),
        on_tap_through), group=-1)
    application.add_handler(CallbackQueryHandler(on_callback))

    application.add_error_handler(on_error)
    log.info("Bot is running. Data dir: %s", config.HOME)
    application.run_polling()
