"""One live screen per tool, in private chats.

Starting a booking screen while an older booking screen is still sitting in
the chat leaves two sets of buttons that both look current - and pressing the
stale one does something you did not mean. So in a DM a new screen closes the
previous screen **of the same tool**.

The two tools are independent: planning a meeting and booking a library space
at the same time is perfectly reasonable, so a Schedule Matcher screen never
closes a Library Booking one, or the other way round.

Group chats are left alone: several people share the message history there and
deleting each other's screens would be baffling.
"""

from __future__ import annotations

import logging

LIBRARY = "library"
SCHEDULE = "schedule"

# The two buttons that are always under the message box. They live here
# because both halves need to recognise them: tapping one means "take me
# somewhere else", which has to end any question the other half was waiting
# to have answered.
KEYBOARD_LABELS = {
    "📅 Schedule": SCHEDULE,
    "📚 Library": LIBRARY,
}

TOOL_NAMES = {LIBRARY: "library booking", SCHEDULE: "schedule matcher"}

log = logging.getLogger(__name__)


def _key(product: str) -> str:
    return f"flow_msg_{product}"


def remember(context, product: str, chat_id: int, message_id: int) -> None:
    """Note which message is this tool's live screen."""
    context.user_data[_key(product)] = (chat_id, message_id)


def forget(context, product: str) -> None:
    context.user_data.pop(_key(product), None)


async def close_previous(context, product: str, keep: int | None = None) -> bool:
    """Delete this tool's older screen. True when one was actually closed."""
    stored = context.user_data.get(_key(product))
    if not stored:
        return False
    chat_id, message_id = stored
    forget(context, product)
    if keep is not None and message_id == keep:
        return False
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception:
        # Too old to delete, or already gone: strike it through instead so it
        # is obvious which screen is live.
        try:
            await context.bot.edit_message_text(
                "(closed - you started something newer)",
                chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            log.debug("could not close old %s screen", product, exc_info=True)
    return False


def finish(context, product: str) -> None:
    """This screen has reached its end (a booking made, a code checked in),
    so it stops being 'the live screen' and nothing will ever delete it."""
    forget(context, product)


async def start(update, context, product: str, text: str, reply_markup=None):
    """Send a new screen for `product`, closing that tool's previous one."""
    chat = update.effective_chat
    private = chat.type == "private"
    closed = await close_previous(context, product) if private else False
    if closed:
        text = (f"(Closed your earlier {TOOL_NAMES[product]} screen - one at a "
                f"time.)\n\n{text}")
    msg = await update.effective_message.reply_text(text, reply_markup=reply_markup)
    if private:
        remember(context, product, chat.id, msg.message_id)
    return msg
