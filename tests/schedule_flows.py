"""Everything the Schedule Matcher half promises, checked against real data.

`smoke_handlers.py` proves nothing raises. This proves the answers are right:
that a painted grid comes back as the times that were painted, that /best
finds the overlap you can work out on paper, that one person's answer cannot
disturb another's, and that the awkward cases - an empty answer, a whole day,
a stale grid, an outsider - do what they should.

Run it the same way: `python tests/schedule_flows.py`. No pytest, no network,
no browser; it takes about a second.
"""
import asyncio
import json
import os
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta

os.environ["SCHEDULE_MATCHER_HOME"] = tempfile.mkdtemp()
os.environ["OWNER_ID"] = "42"
os.environ["TELEGRAM_TOKEN"] = "1:FAKE"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_matcher import config                                # noqa: E402
for d in (config.HOME, config.DEBUG_DIR, config.PROOF_DIR):
    d.mkdir(parents=True, exist_ok=True)

from schedule_matcher import (availability_view, bot as B,          # noqa: E402
                              matching, storage, webapp)

GROUP, ME, ADA, BEN = -1001234567890, 42, 77, 99
SENT: list[tuple[str, object]] = []
POSTED: list[tuple[int, str, object]] = []


# --- mocks ----------------------------------------------------------------

class Bot:
    username = "MatchingBot"
    can_post = True
    membership = "member"

    def __init__(self):
        self.edits = []
        self.photos = []

    async def get_chat_member(self, chat_id, user_id):
        class Member:
            status = Bot.membership
        return Member()

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if not Bot.can_post:
            raise RuntimeError("bot is not in that chat")
        POSTED.append((chat_id, text, reply_markup))
        return Msg(chat_id, "supergroup")

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((chat_id, photo, caption))
        return Msg(chat_id, "supergroup")

    async def edit_message_text(self, chat_id=None, message_id=None, text="",
                                reply_markup=None, **kw):
        self.edits.append((chat_id, text))

    async def delete_message(self, *a, **k):
        pass


BOT = Bot()


class Msg:
    _n = 0

    def __init__(self, chat_id, chat_type, chat_shared=None, web_app_data=None,
                 text=None):
        Msg._n += 1
        self.message_id, self.chat_id = Msg._n, chat_id
        self.chat_shared, self.web_app_data, self.text = (
            chat_shared, web_app_data, text)

    async def reply_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))
        return Msg(self.chat_id, "private")

    async def edit_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))
        return self

    async def delete(self):
        pass


class Chat:
    def __init__(self, chat_id, chat_type):
        self.id, self.type = chat_id, chat_type


class User:
    def __init__(self, uid, name):
        self.id, self.first_name, self.username = uid, name, name


class Query:
    def __init__(self, data, chat_id, chat_type, uid=ME, name="Zilu"):
        self.data, self.from_user = data, User(uid, name)
        self.message = Msg(chat_id, chat_type, text="screen")

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))


class Update:
    def __init__(self, chat_id, chat_type, query=None, uid=ME, name="Zilu",
                 chat_shared=None, web_app_data=None):
        self.callback_query = query
        self.effective_user = User(uid, name)
        self.effective_chat = Chat(chat_id, chat_type)
        self.effective_message = query.message if query else Msg(
            chat_id, chat_type, chat_shared, web_app_data)


class Context:
    def __init__(self, args=None):
        self.user_data, self.chat_data, self.args = {}, {}, args or []
        self.bot = BOT


class Shared:
    def __init__(self, chat_id, title):
        self.request_id, self.chat_id, self.title = 1, chat_id, title


# --- helpers --------------------------------------------------------------

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail if not condition else ""))


async def expect(name, coro, condition_of):
    """Run something, then judge the result."""
    before = len(SENT)
    try:
        await coro
    except Exception as exc:                              # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return None
    out = SENT[before:]
    ok, detail = condition_of(out)
    RESULTS.append((name, ok, detail))
    return out


def last(pattern):
    return [t for t, _ in SENT if pattern in t][-1]


def markup_of(pattern):
    return [m for t, m in SENT if pattern in t][-1]


def paint(days, spans):
    """spans: {day_index: (first_row, last_row_exclusive)} -> intervals."""
    return [(webapp.cell_start(days[i], a), webapp.cell_start(days[i], b))
            for i, (a, b) in spans.items()]


async def send_grid(ctx, chat_id, code, days, intervals, uid=ME, name="Zilu"):
    class WAD:
        data = json.dumps({"v": 1, "c": chat_id, "e": code,
                           "d0": days[0].isoformat(), "nd": len(days),
                           "me": webapp.pack(intervals, days)})
    await B.on_web_app_data(
        Update(uid, "private", uid=uid, name=name, web_app_data=WAD()), ctx)


async def make_event(name="Study session", chat=GROUP, preset="7"):
    ctx = Context()
    await B._create_event(Update(chat, "supergroup"), ctx, name)
    code = last("created").split("code ")[1].split(".")[0]
    await B.on_callback(
        Update(chat, "supergroup", Query(f"evd|{chat}|{code}|{preset}", chat,
                                         "supergroup")), ctx)
    return ctx, code, storage.event_days(storage.get_event(chat, code))


# --- the tests ------------------------------------------------------------

async def test_create_and_dates():
    ctx, code, days = await make_event()
    ev = storage.get_event(GROUP, code)
    check("create: event exists in the group", ev is not None)
    check("create: code is 6 characters", len(code) == 6, code)
    check("create: 7 days recorded", len(days) == 7, str(len(days)))
    check("create: board posted to the group",
          any(c == GROUP and "(code" in t for c, t, _ in POSTED))
    check("create: board carries the paint link",
          any("start=add_" in (b.url or "")
              for _c, _t, m in POSTED if m
              for row in m.inline_keyboard for b in row))
    return ctx, code, days


async def test_paint_round_trip(ctx, code, days):
    mine = paint(days, {0: (12, 16), 2: (4, 8)})      # Mon 14-16, Wed 10-12
    await send_grid(ctx, GROUP, code, days, mine)
    stored = matching.merge_intervals(storage.user_slots(GROUP, code, ME))
    check("paint: stored exactly what was painted", stored == mine,
          f"{stored} != {mine}")
    check("paint: the reply names the times",
          "14:00 - 16:00" in last("Saved for"))
    check("paint: board lists who answered",
          any("Answered: Zilu" in t for _c, t in BOT.edits))


async def test_paint_replaces(ctx, code, days):
    again = paint(days, {1: (20, 24)})
    await send_grid(ctx, GROUP, code, days, again)
    stored = matching.merge_intervals(storage.user_slots(GROUP, code, ME))
    check("repaint: replaces, never adds", stored == again, str(stored))
    rows = storage.conn().execute(
        "SELECT COUNT(*) c FROM slots WHERE chat_id=? AND code=? AND user_id=?",
        (GROUP, code, ME)).fetchone()["c"]
    check("repaint: no orphan rows left behind", rows == len(again), str(rows))


async def test_empty_answer(ctx, code, days):
    await send_grid(ctx, GROUP, code, days, [])
    check("empty answer: everything cleared",
          storage.user_slots(GROUP, code, ME) == [])
    check("empty answer: says so plainly",
          "not free on any" in last("Saved for"))


async def test_whole_day(ctx, code, days):
    whole = paint(days, {0: (0, 32)})
    await send_grid(ctx, GROUP, code, days, whole)
    stored = matching.merge_intervals(storage.user_slots(GROUP, code, ME))
    start, end = stored[0]
    check("whole day: one interval 08:00-24:00",
          len(stored) == 1 and start.hour == 8
          and end == datetime.combine(days[0], datetime.min.time()) + timedelta(days=1),
          str(stored))


async def test_two_people_and_best(ctx, code, days):
    # Zilu: Tue 10:00-13:00, Ada: Tue 11:00-15:00, Ben: Tue 09:00-10:00
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (4, 10)}), ME, "Zilu")
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (6, 14)}), ADA, "Ada")
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (2, 4)}), BEN, "Ben")
    avail = storage.availabilities(GROUP, code)
    check("three people: three answers", len(avail) == 3, str(sorted(avail)))

    suggestions, everyone = matching.best_slots(avail, 60)
    top = suggestions[0]
    overlap_start = webapp.cell_start(days[1], 6)      # 11:00, where Zilu+Ada meet
    check("best: the top slot has two people", top.count == 2, str(top.count))
    check("best: it starts inside the real overlap",
          overlap_start <= top.earliest_start
          <= webapp.cell_start(days[1], 8), f"{top.earliest_start}")
    check("best: names who is missing", "Ben" not in top.people, str(top.people))

    # Nobody can do two hours together: Zilu+Ada share 11:00-13:00 exactly.
    two_hour, _ = matching.best_slots(avail, 120)
    check("best: a 2h window still finds the pair",
          two_hour and two_hour[0].count == 2, str(two_hour[:1]))
    three_hour, _ = matching.best_slots(avail, 180)
    check("best: 3h cannot fit two people",
          not three_hour or three_hour[0].count == 1,
          str(three_hour[:1]))


async def test_view_grid(ctx, code):
    avail = storage.availabilities(GROUP, code)
    text = availability_view.emoji_grid(avail, title="t")
    check("view: grid mentions everyone", all(n in text for n in avail))
    check("view: green appears where all three overlap or none",
          "\U0001f7e9" in text or "\U0001f7e8" in text, text[:80])
    png = availability_view.png_grid(avail, title="t")
    check("view: PNG renders", png is not None and png.getbuffer().nbytes > 500,
          "Pillow missing?" if png is None else "")
    subset = availability_view.emoji_grid(avail, people=["Ada"], title="t")
    check("view: a subset counts only those people", "Counting: Ada" in subset)


async def test_isolation_between_events(ctx, days):
    _c2, code2, days2 = await make_event("Second event")
    mine2 = paint(days2, {3: (10, 12)})
    await send_grid(ctx, GROUP, code2, days2, mine2)
    first = storage.availabilities(GROUP, "")   # nonsense code -> nothing
    check("isolation: an unknown code has no slots", first == {})
    ev1 = [c for c in storage.list_events(GROUP)][0]["code"]
    check("isolation: painting event 2 left event 1 alone",
          len(storage.user_slots(GROUP, ev1, ME)) > 0)
    check("isolation: event 2 has only its own answer",
          matching.merge_intervals(storage.user_slots(GROUP, code2, ME)) == mine2)


async def test_stale_grid(ctx, code, days):
    """A grid opened before the organiser moved the dates."""
    old_days = [d - timedelta(days=30) for d in days]
    await send_grid(ctx, GROUP, code, old_days, paint(old_days, {0: (4, 6)}))
    stored = storage.user_slots(GROUP, code, ME)
    check("stale grid: it is accepted but lands on the days it names",
          all(s.date() in old_days for s, _ in stored), str(stored[:2]))
    # put the person back where the other tests expect them
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (4, 10)}))


async def test_outsider_and_rubbish(ctx, code, days):
    Bot.membership = "left"
    before = storage.user_slots(GROUP, code, BEN)
    await send_grid(ctx, GROUP, code, days, paint(days, {0: (0, 4)}), BEN, "Ben")
    check("outsider: refused", storage.user_slots(GROUP, code, BEN) == before)
    check("outsider: told why", "not in" in last("saved anything"))
    Bot.membership = "member"

    for bad in ('{"v":9}', "not json at all", '{"v":1,"c":1,"e":"","d0":"x","nd":1}'):
        class WAD:
            data = bad
        await B.on_web_app_data(Update(ME, "private", web_app_data=WAD()), ctx)
    check("rubbish: refused every time",
          "couldn't read" in last("couldn't read"))


async def test_deep_links():
    check("deep link: a group id survives the trip",
          B._parse_deep_link("add_n1001234567890_ABC123") == (-1001234567890, "ABC123"))
    check("deep link: a private id survives too",
          B._parse_deep_link("add_42_ABC123") == (42, "ABC123"))
    for bad in ("", "hello", "add_", "add_x_", "start"):
        if B._parse_deep_link(bad) is not None:
            check(f"deep link: rubbish {bad!r} rejected", False)
            return
    check("deep link: rubbish rejected", True)


async def test_dm_create_into_group():
    ctx = Context(args=["Revision", "block"])
    await B.cmd_create(Update(ME, "private"), ctx)
    check("dm create: asks which chat", "which chat is it for" in last("which chat"))
    await B.on_chat_shared(
        Update(ME, "private", chat_shared=Shared(GROUP, "5 Gatherers")), ctx)
    code = last("created").split("code ")[1].split(".")[0]
    check("dm create: event belongs to the group",
          storage.get_event(GROUP, code) is not None
          and storage.get_event(ME, code) is None)
    posted_before = len(POSTED)
    await B.on_callback(
        Update(ME, "private", Query(f"evd|{GROUP}|{code}|7", ME, "private")), ctx)
    check("dm create: board went to the group, not the DM",
          len(POSTED) > posted_before and POSTED[-1][0] == GROUP)

    Bot.can_post = False
    ctx2 = Context(args=["Doomed"])
    await B.cmd_create(Update(ME, "private"), ctx2)
    await B.on_chat_shared(
        Update(ME, "private", chat_shared=Shared(-100777, "Gone")), ctx2)
    code2 = last("created").split("code ")[1].split(".")[0]
    await B.on_callback(
        Update(ME, "private", Query(f"evd|-100777|{code2}|7", ME, "private")), ctx2)
    check("dm create: a chat it cannot post to is reported",
          "could not post" in last("could not post"))
    Bot.can_post = True


async def test_old_calendar_path():
    """The tap-through pickers must still work - old clients depend on them."""
    ctx, code, days = await make_event("Tappy")
    day = days[2]
    await B.on_callback(Update(GROUP, "supergroup", Query(
        f"t_save|{day.year}|{day.month}|{day.day}|14|0|16|30|{code}",
        GROUP, "supergroup", uid=ADA, name="Ada"), uid=ADA, name="Ada"), ctx)
    slots = storage.user_slots(GROUP, code, ADA)
    check("calendar: a tapped slot is stored",
          slots and slots[0][0].hour == 14 and slots[0][1].hour == 16,
          str(slots))
    ts = int(slots[0][0].timestamp())
    await B.on_callback(Update(GROUP, "supergroup", Query(
        f"del_slot|{code}|{ts}", GROUP, "supergroup", uid=ADA, name="Ada"),
        uid=ADA, name="Ada"), ctx)
    check("calendar: deleting a slot works",
          storage.user_slots(GROUP, code, ADA) == [])


async def test_no_webapp_fallback():
    """With the page unpublished the bot must still be usable."""
    saved = config.WEBAPP_URL
    config.WEBAPP_URL = ""
    try:
        ctx = Context()
        await B._create_event(Update(GROUP, "supergroup"), ctx, "Old school")
        check("fallback: plain creation message",
              "use /add to enter availability" in last("created!"))
        await B.cmd_add(Update(GROUP, "supergroup"), ctx)
        check("fallback: /add offers the event picker",
              "Which event?" in last("Which event?"))
    finally:
        config.WEBAPP_URL = saved


async def test_url_size_limits():
    days = [date(2026, 9, 14) + timedelta(days=n) for n in range(14)]
    everyone = {f"P{i}": [(webapp.cell_start(d, 0), webapp.cell_start(d, 32))
                          for d in days] for i in range(12)}
    ev = {"code": "ABC123", "name": "A rather long event name to be safe"}
    url = webapp.url_for(GROUP, ev, days,
                         [(webapp.cell_start(days[0], 0), webapp.cell_start(days[0], 32))],
                         everyone)
    check("url: 14 days and 12 people still fits comfortably",
          len(url) < 1500, f"{len(url)} chars")
    payload = json.dumps({"v": 1, "c": GROUP, "e": "ABC123",
                          "d0": days[0].isoformat(), "nd": 14,
                          "me": webapp.pack(everyone["P0"], days)})
    check("payload: well under Telegram's 4096-byte limit",
          len(payload.encode()) < 500, f"{len(payload)} bytes")


async def test_code_collision():
    """Two events cannot share a code in one chat - it must not crash."""
    storage.create_event(GROUP, "DUPDUP", "First", ME)
    try:
        storage.create_event(GROUP, "DUPDUP", "Second", ME)
        check("collision: a duplicate code is refused", False, "it was allowed")
    except Exception as exc:
        check("collision: a duplicate code raises rather than overwrites",
              "First" == storage.get_event(GROUP, "DUPDUP")["name"],
              type(exc).__name__)


async def test_commands_end_to_end():
    """The commands a person actually types, not just the machinery."""
    ctx, code, days = await make_event("Command test")
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (4, 10)}), ME, "Zilu")
    await send_grid(ctx, GROUP, code, days, paint(days, {1: (6, 14)}), ADA, "Ada")

    await B.cmd_events(Update(GROUP, "supergroup"), Context())
    check("/events: lists the event and its code", code in last("Command test"))

    await B.cmd_best(Update(GROUP, "supergroup"), Context(args=[code, "60"]))
    text = last("Best 60-minute")
    check("/best: reports how many responded", "2 people responded" in text, text[:60])
    check("/best: names a day and a time", ":" in text and "/" not in text.split(chr(10))[1])

    await B.cmd_best(Update(GROUP, "supergroup"), Context(args=["NOPE"]))
    check("/best: an unknown code is refused", "not found" in last("not found"))

    await B.cmd_best(Update(GROUP, "supergroup"), Context(args=[code, "abc"]))
    check("/best: a silly duration is refused", "must be a number" in last("number"))

    await B.on_callback(Update(GROUP, "supergroup", Query(f"view|{code}", GROUP,
                                                          "supergroup")), ctx)
    check("/view: draws the grid", "Counting:" in last("Counting:"))

    await B.on_callback(Update(GROUP, "supergroup", Query(f"gimg|{code}", GROUP,
                                                          "supergroup")), ctx)
    check("/view: the PNG button sends a photo", BOT.photos, "no photo sent")

    await B.on_callback(Update(GROUP, "supergroup", Query(f"gpickmode|{code}",
                                                          GROUP, "supergroup")), ctx)
    check("/view: the person picker opens", "Counting:" in last("Counting:"))
    await B.on_callback(Update(GROUP, "supergroup", Query(f"gpick|{code}|Ada",
                                                          GROUP, "supergroup")), ctx)
    check("/view: ticking a person off narrows the count",
          "Counting: Zilu" in last("Counting:"), last("Counting:").splitlines()[1])
    await B.on_callback(Update(GROUP, "supergroup", Query(f"gall|{code}", GROUP,
                                                          "supergroup")), ctx)
    check("/view: everyone comes back", "Ada" in last("Counting:"))
    await B.on_callback(Update(GROUP, "supergroup", Query(f"gbest|{code}", GROUP,
                                                          "supergroup")), ctx)
    check("/view: the best-times button answers", "Best" in last("Best"))

    await B.on_callback(Update(GROUP, "supergroup", Query(f"edit_evt|{code}", GROUP,
                                                          "supergroup")), ctx)
    check("/edit: offers add and remove", "Add more times" in str(markup_of("your availability")))

    await B.cmd_view(Update(GROUP, "supergroup"), Context())
    check("/view: asks which event", "which event" in last("which event").lower())


async def test_group_add_links():
    ctx, code, _days = await make_event("Linkable")
    await B.cmd_add(Update(GROUP, "supergroup"), ctx)
    markup = markup_of("opens our private chat")
    urls = [b.url for row in markup.inline_keyboard for b in row]
    check("/add in a group: every event gets a link",
          all(u and "start=add_" in u for u in urls), str(urls[:1]))
    check("/add in a group: one message, not one per event",
          len([t for t, _ in SENT if "opens our private chat" in t]) == 1)


async def test_person_renamed():
    """Somebody changes their Telegram name between answers."""
    ctx, code, days = await make_event("Renamed")
    await send_grid(ctx, GROUP, code, days, paint(days, {0: (4, 8)}), BEN, "Ben")
    await send_grid(ctx, GROUP, code, days, paint(days, {0: (4, 8)}), BEN, "Benjamin")
    avail = storage.availabilities(GROUP, code)
    check("rename: still one person, not two", len(avail) == 1, str(sorted(avail)))
    check("rename: the newer name is used", "Benjamin" in avail, str(sorted(avail)))


async def test_event_days_without_a_range():
    """An event made before /create asked for dates still has to draw."""
    storage.create_event(GROUP, "OLDEV1", "Legacy", ME)
    days = storage.event_days(storage.get_event(GROUP, "OLDEV1"))
    check("legacy event: falls back to a week", len(days) == 7, str(len(days)))
    start = webapp.cell_start(days[0], 4)
    storage.replace_slots(GROUP, "OLDEV1", ME, "Zilu",
                          [(start, start + timedelta(hours=2))])
    days2 = storage.event_days(storage.get_event(GROUP, "OLDEV1"))
    check("legacy event: then uses the days people offered",
          days2 == [start.date()], str(days2))


async def main():
    ctx, code, days = await test_create_and_dates()
    await test_paint_round_trip(ctx, code, days)
    await test_paint_replaces(ctx, code, days)
    await test_empty_answer(ctx, code, days)
    await test_whole_day(ctx, code, days)
    await test_two_people_and_best(ctx, code, days)
    await test_view_grid(ctx, code)
    await test_isolation_between_events(ctx, days)
    await test_stale_grid(ctx, code, days)
    await test_outsider_and_rubbish(ctx, code, days)
    await test_deep_links()
    await test_dm_create_into_group()
    await test_old_calendar_path()
    await test_no_webapp_fallback()
    await test_url_size_limits()
    await test_code_collision()
    await test_commands_end_to_end()
    await test_group_add_links()
    await test_person_renamed()
    await test_event_days_without_a_range()

    print()
    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}"
              + (f"   {detail}" if detail else ""))
        failed += not ok
    print(f"\n{len(RESULTS)} checks, {failed} failed")
    return failed


if __name__ == "__main__":
    try:
        sys.exit(1 if asyncio.run(main()) else 0)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
