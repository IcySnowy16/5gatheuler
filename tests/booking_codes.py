"""What the bot makes of a check-in code, without touching the network.

Two reported bugs came from this one function reading the library's reply.
First it called a rejected code "known" and promised retries; then it read the
clock out of "booking starts at 12:30pm Wednesday, September 9, 2026" and
filed it under today. Both are cheap to get wrong again, so every sentence the
site is known to answer with is pinned here.
"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime

os.environ["SCHEDULE_MATCHER_HOME"] = tempfile.mkdtemp()
os.environ["TELEGRAM_TOKEN"] = "1:FAKE"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_matcher import config                                # noqa: E402
for d in (config.HOME, config.DEBUG_DIR, config.PROOF_DIR):
    d.mkdir(parents=True, exist_ok=True)

from schedule_matcher.booking import libcal                        # noqa: E402

RESULTS = []
TODAY = date.today()

# Every reply the live site has been observed to give, verbatim.
REPLIES = {
    "future booking": (400,
        "Unable to Check In for this booking until 12:25pm Wednesday, "
        "September 9, 2026 (booking starts at 12:30pm Wednesday, "
        "September 9, 2026)."),
    "today, not open yet": (400,
        "Unable to Check In for this booking until 10:25am (booking starts "
        "at 10:30am)."),
    "checked in": (200,
        "Check In Already Checked In at: 2:07pm Name / Email: #WANG ZILU# "
        "ZWANG094 / zwang094@e.ntu.edu.sg Location: Lee Wee Nam Library "
        "Space: LIBLWNL-AK-05 Start Time: 1:45pm Check Out time: 3:00pm"),
    "already over": (400, "This booking has already been Checked Out"),
    "wrong code": (400, "Unable to find booking matching code"),
    "empty code": (400, "Invalid value."),
}


class FakeResponse:
    def __init__(self, status, text):
        self.status_code, self.text = status, f"<html><body>{text}</body></html>"

    def json(self):
        raise ValueError("not json")


class FakeClient:
    reply = REPLIES["wrong code"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return FakeResponse(*FakeClient.reply)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), "" if ok else str(detail)))


async def probe(kind):
    FakeClient.reply = REPLIES[kind]
    return await libcal.probe_code("someone@e.ntu.edu.sg", "E8H")


async def test_records_can_be_corrected():
    """A code on the wrong booking, and a record that should never have been.

    Exactly the mess the date bug left behind: a phantom row holding the code,
    and the real booking holding none.
    """
    from datetime import timedelta
    from schedule_matcher import storage

    uid = 4242
    storage.save_user(uid, email="t@e.ntu.edu.sg")
    now = datetime.now().replace(second=0, microsecond=0)
    phantom = storage.add_booking(uid, "(booked by you)", "your own booking",
                                  "your booking", None,
                                  now - timedelta(hours=6), now - timedelta(hours=4))
    storage.update_booking(phantom, checkin_code="E8H")
    real = storage.add_booking(uid, "Lee Wee Nam Library", "Learning Pod",
                               "LWNL Pod 1", 46002, now + timedelta(days=1),
                               now + timedelta(days=1, hours=3))

    # a code belongs to one booking: filing it anew takes it off the old one
    moved = storage.clear_code_elsewhere(uid, "E8H", real)
    storage.update_booking(real, checkin_code="E8H")
    check("code moves: taken off the booking that had it", moved == [phantom], moved)
    check("code moves: the old record keeps everything else",
          storage.get_booking(phantom) is not None
          and storage.get_booking(phantom)["checkin_code"] is None)
    check("code moves: the right booking has it now",
          storage.get_booking(real)["checkin_code"] == "E8H")

    # and a record that was never a booking can be forgotten
    storage.delete_booking(phantom)
    check("forget: the record is gone", storage.get_booking(phantom) is None)
    check("forget: it took nothing else with it",
          storage.get_booking(real) is not None)
    check("forget: /bookings has only the real one",
          [r["id"] for r in storage.list_bookings(uid, active_only=False)] == [real])


async def main():
    libcal._client = lambda: FakeClient()                 # no network at all

    got = await probe("future booking")
    check("future booking: the day the site named, not today",
          got["start"] == datetime(2026, 9, 9, 12, 30), got["start"])
    check("future booking: recognised as a real code", got["known"])
    check("future booking: not treated as checked in", not got["checked_in"])

    got = await probe("today, not open yet")
    check("no date given: falls back to today",
          got["start"] == datetime.combine(TODAY, datetime.min.time()).replace(
              hour=10, minute=30), got["start"])

    got = await probe("checked in")
    check("checked in: reports the space", got["space"] == "LIBLWNL-AK-05",
          got["space"])
    check("checked in: reports the library",
          got["location"] == "Lee Wee Nam Library", got["location"])
    check("checked in: start is 13:45 today",
          got["start"] and got["start"].hour == 13 and got["start"].minute == 45,
          got["start"])
    check("checked in: end is 15:00",
          got["end"] and got["end"].hour == 15, got["end"])
    check("checked in: says so", got["checked_in"])

    got = await probe("already over")
    check("already over: known but finished",
          got["known"] and got["finished"], got)

    got = await probe("wrong code")
    check("wrong code: not known", not got["known"])
    check("wrong code: not finished either", not got["finished"])

    got = await probe("empty code")
    check("empty code: not known", not got["known"])

    # the date reader on its own
    check("date reader: 'September 9, 2026'",
          libcal._calendar_date("September 9, 2026") == date(2026, 9, 9))
    check("date reader: short month",
          libcal._calendar_date("Sep 9 2026") == date(2026, 9, 9))
    check("date reader: nonsense is None",
          libcal._calendar_date("next Tuesday") is None)
    check("date reader: nothing is None", libcal._calendar_date(None) is None)

    await test_records_can_be_corrected()

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
