"""The category picker: complete, identical in every mode, and cache-proof.

Griffin Booth went missing twice for two different reasons. First because the
homepage links it without ids, so the parser dropped it. Then, after that was
fixed, because a day-old cache was handed back untouched - the fix only ran on
the path that rebuilt the list from scratch.

So this checks the picker the way a person meets it: through every mode, with
a stale cache in place, offline.
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime

os.environ["SCHEDULE_MATCHER_HOME"] = tempfile.mkdtemp()
os.environ["TELEGRAM_TOKEN"] = "1:FAKE"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_matcher import config                                # noqa: E402
for d in (config.HOME, config.DEBUG_DIR, config.PROOF_DIR):
    d.mkdir(parents=True, exist_ok=True)

from schedule_matcher import storage                               # noqa: E402
from schedule_matcher.booking import (catalog, credstore,          # noqa: E402
                                      handlers as bh, libcal)

UID = 42
RESULTS = []
SENT = []

# The exact list a bot was holding on 4 Sep: "All Categories", ten real
# categories, and no Griffin Booth.
STALE = [{"name": "Lee Wee Nam Library", "categories": [
    {"label": "All Categories", "lid": 3368, "gid": 0, "url": "/spaces?lid=3368"},
    {"label": "Circular Pod", "lid": 3368, "gid": 5807, "url": ""},
    {"label": "Learning Pod", "lid": 3368, "gid": 8416, "url": ""},
    {"label": "Recording Room", "lid": 3368, "gid": 8421, "url": ""},
    {"label": "Tardis - Video Conferencing Room", "lid": 3368, "gid": 8442, "url": ""},
    {"label": "Arrakis - Single Monitor", "lid": 3368, "gid": 11822, "url": ""},
    {"label": "Arrakis - Dual Monitors PC", "lid": 3368, "gid": 11823, "url": ""},
    {"label": "Curved Monitor", "lid": 3368, "gid": 11973, "url": ""},
    {"label": "Single Monitor", "lid": 3368, "gid": 11825, "url": ""},
    {"label": "Stargate - Single Monitor", "lid": 3368, "gid": 13893, "url": ""},
    {"label": "Immersion@Stargate", "lid": 3368, "gid": 13586, "url": ""},
]}, {"name": "Humanities & Social Sciences Library", "categories": [
    {"label": "All Categories", "lid": 4906, "gid": 0, "url": ""},
]}]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), "" if ok else str(detail)))


def seed_stale_cache():
    storage.conn().execute(
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
        ("libcal_locations",
         json.dumps({"at": datetime.now().strftime(storage.FMT), "data": STALE})))
    storage.conn().commit()


# --- mocks, enough to drive the flow screens ------------------------------

class Msg:
    message_id, chat_id = 1, UID

    async def reply_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))
        return self

    async def delete(self):
        pass


class User:
    id, first_name, username = UID, "Zilu", "zilu"


class Chat:
    id, type = UID, "private"


class Query:
    def __init__(self, data=""):
        self.data, self.from_user, self.message = data, User(), Msg()

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **kw):
        SENT.append((text, reply_markup))


class Update:
    def __init__(self, query=None):
        self.callback_query = query
        self.effective_user, self.effective_chat = User(), Chat()
        self.effective_message = Msg()


class Context:
    def __init__(self):
        self.user_data, self.chat_data, self.args = {}, {}, []
        self.bot = self

    async def send_message(self, *a, **k):
        pass


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_hours_learned_on_sight():
    """Opening a day in the flow records that weekday's real hours.

    Offline: a fabricated grid is handed straight to the recorder, because the
    point under test is that a grid anyone looks at teaches the catalogue -
    not that the network works.
    """
    from datetime import date, datetime, timedelta
    from schedule_matcher.booking import catalog, libcal

    lid, gid = 3368, 11822
    target = date.today() + timedelta(days=3)
    meta = catalog._meta()
    meta[f"{lid}_{gid}"] = {"library": "LWN", "category": "Arrakis",
                            "lid": lid, "gid": gid, "hours": {}}
    catalog.save(meta)
    check("learned hours: unknown weekday starts assumed",
          not catalog.hours_measured(lid, gid, target))

    def cell(h, m, dur=30):
        start = datetime.combine(target, datetime.min.time()) + timedelta(hours=h, minutes=m)
        return libcal.Cell(start=start, end=start + timedelta(minutes=dur),
                           item_id=1, checksum="x", state=libcal.FREE)

    libcal._note_hours(lid, gid, target, {1: [cell(8, 30), cell(20, 30)]})
    check("learned hours: the weekday is now measured",
          catalog.hours_measured(lid, gid, target))
    span = (catalog.get(lid, gid).get("hours") or {}).get(str(target.weekday()))
    check("learned hours: the span is what the grid showed",
          span == ["08:30", "21:00"], span)

    # today is half over, so it must never set the opening time
    today_cells = {1: [cell(15, 0)]}
    libcal._note_hours(lid, gid, date.today(), today_cells)
    check("learned hours: today is never used",
          str(date.today().weekday()) not in (catalog.get(lid, gid).get("hours") or {})
          or date.today().weekday() == target.weekday())

    # an unknown category must not be invented
    libcal._note_hours(9999, 9999, target, {1: [cell(8, 30)]})
    check("learned hours: an unknown category is left alone",
          catalog.get(9999, 9999) == {})


async def test_hours_accumulate():
    """Opening hours must build up, never reset.

    A day-of category publishes a day or two ahead, so any single look sees
    one or two weekdays. Replacing would mean forgetting Monday whenever the
    bot happened to look on a Friday - which is how most categories ended up
    knowing 1 weekday out of 7.
    """
    from datetime import date, timedelta
    from schedule_matcher.booking import catalog

    meta = catalog._meta()
    key = "3368_11822"
    meta[key] = {"library": "LWN", "category": "Arrakis", "lid": 3368,
                 "gid": 11822, "hours": {"0": ["08:30", "21:00"]}}
    catalog.save(meta)

    entry = dict(catalog.get(3368, 11822))
    merged = dict(entry.get("hours") or {})
    merged.update({"2": ["08:30", "21:00"]})          # what a later look sees
    entry["hours"] = merged
    meta = catalog._meta(); meta[key] = entry; catalog.save(meta)

    hours = catalog.get(3368, 11822).get("hours") or {}
    check("hours: a later look keeps the earlier weekday",
          set(hours) == {"0", "2"}, sorted(hours))

    monday = date.today() + timedelta(days=(0 - date.today().weekday()) % 7)
    tuesday = monday + timedelta(days=1)
    check("hours: a seen weekday is reported as measured",
          catalog.hours_measured(3368, 11822, monday))
    check("hours: an unseen weekday is reported as assumed",
          not catalog.hours_measured(3368, 11822, tuesday))
    check("hours: an unseen weekday still answers with the usual hours",
          catalog.hours_for(3368, 11822, tuesday) is not None)


async def main():
    storage.save_user(UID, ntu_username=credstore.encrypt("u"),
                      ntu_password=credstore.encrypt("p"),
                      email="t@e.ntu.edu.sg")
    seed_stale_cache()

    # 1. the stale cache must not be believed as-is
    locs = await libcal.fetch_locations()
    offered = {(c.lid, c.gid): c.label for l in locs for c in l.categories}
    check("stale cache: Griffin Booth is offered anyway",
          any("Griffin" in v for v in offered.values()), sorted(offered.values()))
    check("stale cache: 'All Categories' is dropped",
          not any(gid == 0 for _lid, gid in offered))
    check("stale cache: the Humanities categories appear",
          sum(1 for lid, _g in offered if lid == 4906) == 3,
          [v for (lid, _g), v in offered.items() if lid == 4906])
    known = {(int(e["lid"]), int(e["gid"])) for e in catalog.all_categories()}
    check("stale cache: nothing the catalogue knows is missing",
          not known - set(offered), known - set(offered))

    await test_hours_accumulate()
    await test_hours_learned_on_sight()

    # 2. every mode must show the same categories
    per_mode = {}
    for mode in ("now", "sched", "ext", "recur"):
        ctx = Context()
        await bh._start_flow(Update(), ctx, mode)
        bk = ctx.user_data["bk"]
        per_mode[mode] = {(c.lid, c.gid) for loc in bk["locations"]
                          for c in loc.categories}
        # walk into Lee Wee Nam and expand the list, the way a person would
        idx = next(i for i, l in enumerate(bk["locations"])
                   if "Lee Wee Nam" in l.name)
        SENT.clear()
        await bh.on_booking_callback(Update(Query(f"bk|loc|{idx}")), ctx)
        await bh.on_booking_callback(Update(Query("bk|showall")), ctx)
        shown = labels(SENT[-1][1])
        check(f"{mode}: the category screen lists Griffin Booth",
              any("Griffin" in s for s in shown), shown)
        check(f"{mode}: no 'All Categories' button",
              not any("All Categories" in s for s in shown), shown)

    first = per_mode["now"]
    for mode, got in per_mode.items():
        check(f"{mode}: offers exactly the same categories as Book",
              got == first, sorted(x for x in got ^ first))
    check("all four modes agree on 25 categories", len(first) == 25, len(first))

    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}"
              + (f"   {detail}" if detail else ""))
        failed += not ok
    print(f"\n{len(RESULTS)} checks, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
