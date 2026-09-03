"""Asking a question instead of printing "Usage: /command <thing>".

A button in a menu cannot carry arguments, so `/create` launched from the menu
used to answer "Usage: /create <Event Name>" - which is a dead end for anyone
driving the bot by tapping.

Here a command with a missing argument asks for it instead, and usually offers
a sensible default as a button so nothing has to be typed at all.

Replies are matched with Telegram's ForceReply rather than by swallowing every
message: the question is tied to its answer by `reply_to_message`, so this is
safe in a busy group chat where most messages are not for the bot.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# kind -> coroutine(update, context, answer, question) -> None
_ANSWERERS: dict[str, Callable[..., Awaitable[None]]] = {}


def register(kind: str, answerer: Callable[..., Awaitable[None]]) -> None:
    _ANSWERERS[kind] = answerer


def _store(context) -> dict:
    return context.chat_data.setdefault("asked", {})


async def ask(update, context, kind: str, question: str,
              suggestion: str | None = None, suggestion_label: str | None = None,
              **extra):
    """Ask for one missing value. Returns the question message."""
    markup = None
    if suggestion:
        label = suggestion_label or f"Use '{suggestion}'"
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data="ask|default")]])
        question = f"{question}\n\nOr tap the button for: {suggestion}"
    msg = await update.effective_message.reply_text(
        question, reply_markup=markup or ForceReply(selective=True))
    _store(context)[str(msg.message_id)] = {
        "kind": kind, "user_id": update.effective_user.id,
        "suggestion": suggestion, **extra,
    }
    # Keep only the last few questions per chat.
    asked = _store(context)
    for stale in list(asked)[:-5]:
        asked.pop(stale, None)
    return msg


async def on_reply(update, context) -> None:
    """A reply to one of our questions carries the answer."""
    message = update.effective_message
    target = message.reply_to_message
    if not target:
        return
    pending = _store(context).get(str(target.message_id))
    if not pending:
        return
    if pending.get("user_id") and update.effective_user.id != pending["user_id"]:
        return                      # someone else replied to your prompt
    _store(context).pop(str(target.message_id), None)
    answerer = _ANSWERERS.get(pending["kind"])
    if answerer:
        await answerer(update, context, message.text.strip(), pending)


async def on_default_button(update, context) -> None:
    """The 'use this default' button under a question."""
    query = update.callback_query
    await query.answer()
    pending = _store(context).get(str(query.message.message_id))
    if not pending or not pending.get("suggestion"):
        await query.edit_message_text("That question has expired - run the command again.")
        return
    if pending.get("user_id") and update.effective_user.id != pending["user_id"]:
        return
    _store(context).pop(str(query.message.message_id), None)
    answerer = _ANSWERERS.get(pending["kind"])
    if answerer:
        await answerer(update, context, pending["suggestion"], pending)
