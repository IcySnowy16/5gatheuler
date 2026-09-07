"""Call every command and every callback branch with mocks.

pyflakes finds undefined names; it cannot find a handler called with the wrong
arguments, an await that was forgotten, or a branch that blows up on empty
data. This drives each one the way Telegram would and reports what breaks.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

os.environ["SCHEDULE_MATCHER_HOME"] = tempfile.mkdtemp()
os.environ["OWNER_ID"] = "42"
os.environ["TELEGRAM_TOKEN"] = "1:FAKE"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_matcher import config                                # noqa: E402
for d in (config.HOME, config.DEBUG_DIR, config.PROOF_DIR):
    d.mkdir(parents=True, exist_ok=True)

from schedule_matcher import ask, bot as B, storage                 # noqa: E402
from schedule_matcher.booking import (credstore, groupbook,         # noqa: E402
                                      handlers as bh)

UID = 42
storage.conn()
storage.save_user(UID, ntu_username=credstore.encrypt("u"),
                  ntu_password=credstore.encrypt("p"),
                  email="tester@e.ntu.edu.sg")
storage.add_developer(UID, UID)
now = datetime.now()
BOOKING = storage.add_booking(UID, "LWN", "Arrakis", "LIBLWNL-AK-01 (Capacity 1)",
                              46002, now + timedelta(hours=1),
                              now + timedelta(hours=3), lid=3368, gid=11822)
storage.update_booking(BOOKING, checkin_code="T6DQ")
FAV = storage.add_fav(UID, "AK-01 @ Arrakis", 3368, 11822, 46002)
storage.create_event(-100, "ABC123", "Test event", UID)
storage.add_slot(-100, "ABC123", UID, "Zilu", now + timedelta(days=1),
                 now + timedelta(days=1, hours=2))
JOB = storage.add_scheduled(UID, 3368, 11822, "LWN", "Arrakis", None,
                            now + timedelta(days=1), now + timedelta(days=1, hours=2),
                            now + timedelta(hours=5), now + timedelta(hours=6))
RULE = storage.add_rule(UID, 3368, 11822, "LWN", "Arrakis", None, [0, 2],
                        "13:30", "15:30", (now + timedelta(days=21)).date())
HOLDROW = storage.add_hold(UID, 3368, 11822, 46002, "LWN", "Arrakis",
                           "LIBLWNL-AK-01 (Capacity 1)", now + timedelta(hours=1),
                           now + timedelta(hours=3))
storage.close_hold(HOLDROW, "released")


class Msg:
    _n = 0

    def __init__(self, chat_id=UID, text=None, reply_to=None):
        Msg._n += 1
        self.message_id = Msg._n
        self.chat_id = chat_id
        self.text = text
        self.reply_to_message = reply_to

    async def reply_text(self, text, reply_markup=None, **kw):
        return Msg(self.chat_id, text)

    async def edit_text(self, text, reply_markup=None, **kw):
        return self


class Chat:
    def __init__(self, kind="private"):
        self.type = kind
        self.id = UID if kind == "private" else -100


class User:
    id = UID
    first_name = "Zilu"
    full_name = "Zilu"
    username = "zilu"


class Bot:
    async def send_message(self, *a, **k):
        return Msg()

    async def send_photo(self, *a, **k):
        return Msg()

    async def delete_message(self, *a, **k):
        pass

    async def edit_message_text(self, *a, **k):
        pass

    async def set_my_commands(self, *a, **k):
        pass


class Query:
    def __init__(self, data, chat="private"):
        self.data = data
        self.from_user = User()
        self.message = Msg(Chat(chat).id)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, *a, **k):
        pass

    async def edit_message_reply_markup(self, *a, **k):
        pass


class Update:
    def __init__(self, chat="private", query=None, text=None, reply_to=None):
        self.effective_chat = Chat(chat)
        self.effective_user = User()
        self.effective_message = Msg(self.effective_chat.id, text, reply_to)
        self.message = self.effective_message
        self.callback_query = query


class Context:
    def __init__(self, args=None):
        self.args = args or []
        self.user_data = {}
        self.chat_data = {}
        self.bot = Bot()
        self.error = None


async def try_call(name, coro_factory, timeout=45):
    try:
        await asyncio.wait_for(coro_factory(), timeout=timeout)
        return name, "ok", ""
    except Exception as e:
        return name, "FAIL", f"{type(e).__name__}: {e}"


async def main():
    results = []

    commands = {
        "start": (B.cmd_start, []), "menu": (B.cmd_menu, []),
        "help": (B.cmd_help, []), "dev": (B.cmd_dev, []),
        "dev list": (B.cmd_dev, ["list"]),
        "create": (B.cmd_create, []), "create X": (B.cmd_create, ["Trip"]),
        "events": (B.cmd_events, []), "add": (B.cmd_add, []),
        "view": (B.cmd_view, []), "edit": (B.cmd_edit, []),
        "delete": (B.cmd_delete, []), "best": (B.cmd_best, []),
        "best ABC123": (B.cmd_best, ["ABC123"]),
        "bookings": (bh.cmd_bookings, []), "checkin": (bh.cmd_checkin, []),
        "checkin CODE": (bh.cmd_checkin, ["T6DQ"]),
        "checkin CODE TIME": (bh.cmd_checkin, ["T6DQ", "14:30"]),
        "code": (bh.cmd_code, []), "code CODE": (bh.cmd_code, ["T6DQ"]),
        "email": (bh.cmd_email, []), "email addr": (bh.cmd_email, ["a@b.com"]),
        "fav": (bh.cmd_fav, []), "move": (bh.cmd_move, []),
        "cancelbooking": (bh.cmd_cancel_booking, []),
        "scheduled": (bh.cmd_scheduled, []), "holds": (bh.cmd_holds, []),
        "recurring": (bh.cmd_recurring, []),
        "holdtime": (bh.cmd_holdtime, []), "holdtime 120": (bh.cmd_holdtime, ["120"]),
        "mostused": (bh.cmd_mostused, []), "mostused 5": (bh.cmd_mostused, ["5"]),
        "rules": (bh.cmd_rules, []), "developer": (bh.cmd_developer, []),
        "setup": (bh.cmd_setup, []), "cancel_setup": (bh.cmd_cancel_setup, []),
        "botemail": (bh.cmd_botemail, []),
        "availability": (bh.cmd_availability, []),
        "groupbook (group)": (groupbook.cmd_groupbook, []),
        "groupcancel (group)": (groupbook.cmd_groupcancel, []),
    }
    for name, (fn, args) in commands.items():
        chat = "group" if "(group)" in name else "private"
        results.append(await try_call(
            f"/{name}", lambda fn=fn, args=args, chat=chat:
            fn(Update(chat), Context(args))))

    # callback branches that do not need a half-finished flow in memory
    callbacks = [
        "menu|root", "menu|library", "menu|schedule", "menu|lb_book",
        "menu|sm_view", "menu|developer", "run|bookings",
        f"bk|ci|{BOOKING}", f"bk|cx|{BOOKING}", f"bk|mv|{BOOKING}",
        f"bk|favadd|{BOOKING}", f"bk|fav|{FAV}", f"bk|favdel|{FAV}",
        "bk|fadd", "bk|mu|5", "bk|ht|ask", "bk|ht|120", "bk|hallback",
        f"bk|hagain|{HOLDROW}", "bk|hbook|999", "bk|hrel|999", "bk|hext|999",
        f"bk|scancel|{JOB}", f"bk|rpause|{RULE}", f"bk|rdel|{RULE}",
        "bk|rpause|99999", f"bk|setcode|{BOOKING}", "bk|back", "bk|home",
        "bk|abort", "view|ABC123", "best|ABC123", "bestdur|ABC123|60",
        "evt|ABC123", "del_evt|ABC123", "ignore", "ask|default",
    ]
    for data in callbacks:
        if data.startswith(("menu|",)):
            fn = B.on_menu_callback
        elif data.startswith("run|"):
            fn = B.on_run_callback
        elif data.startswith("bk|"):
            fn = bh.on_booking_callback
        elif data.startswith("ask|"):
            fn = ask.on_default_button
        else:
            fn = B.on_callback
        # anything that drives a real browser needs room: re-taking a hold,
        # and checking in (which photographs the result).
        budget = (180 if data.startswith(("bk|hagain", "bk|hallback", "bk|ci"))
                  else 45)
        results.append(await try_call(
            f"[{data}]", lambda fn=fn, data=data:
            fn(Update(query=Query(data)), Context()), timeout=budget))

    # a reply to a question the bot asked
    ask.register("event_name", B._answer_event_name)
    ctx = Context()
    upd = Update()
    await B.cmd_create(upd, ctx)
    results.append(await try_call("reply to /create question",
                                  lambda: ask.on_reply(upd, ctx)))

    bad = [r for r in results if r[1] != "ok"]
    print(f"{len(results)} handlers exercised, {len(bad)} failed\n")
    for name, status, err in results:
        if status != "ok":
            print(f"  FAIL {name}: {err}")
    if not bad:
        print("  every handler responded without raising")
    return len(bad)


sys.exit(asyncio.run(main()))
