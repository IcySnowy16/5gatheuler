"""Background work that cannot fail quietly.

`asyncio.create_task` drops exceptions on the floor: the coroutine dies, the
task object is garbage collected, and nothing is ever printed. That is how a
watched inbox, a scheduled booking or a hold recovery could stop working with
no sign at all.

python-telegram-bot's own `Application.create_task` does route failures into
the error handler, but the bot also spawns work from inside handlers and
loops, where only plain asyncio is available. `spawn()` is the wrapper for
those: it logs the failure, records it so `/developer` can show it, and tells
the person who was waiting, naming the feature so the message means something.
"""

from __future__ import annotations

import asyncio
import logging

from . import storage

log = logging.getLogger(__name__)


def spawn(coro, *, bot=None, user_id: int | None = None, feature: str = "task",
          notify: str | None = None) -> asyncio.Task:
    """Run `coro` in the background, reporting it if it raises.

    bot/user_id: who to tell. `notify` overrides the default sentence when the
    feature has something more useful to say ("paste the email instead").
    """
    async def guarded():
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001 - the point
            log.exception("%s failed in the background", feature)
            storage.record_error(feature, f"{type(exc).__name__}: {exc}")
            if bot is not None and user_id is not None:
                text = notify or (
                    f"{feature} hit an error and stopped. Nothing was changed "
                    "on your bookings. It is recorded - /developer shows the "
                    "details.")
                try:
                    await bot.send_message(user_id, text)
                except Exception:
                    log.debug("could not report the %s failure", feature,
                              exc_info=True)
            return None

    return asyncio.create_task(guarded())
