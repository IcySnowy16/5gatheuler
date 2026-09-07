"""Drive the availability grid in a real browser.

The page and the bot each implement the same bitmask, in different languages,
and nothing stops them drifting apart except a test that runs both. This one
opens `docs/index.html` in headless Chromium with parameters the bot itself
produced, drags a finger down the grid, and checks that what the page would
send decodes - in Python - to exactly the times that were painted.

Needs Playwright's Chromium (`python -m playwright install chromium`), so it
lives apart from `schedule_flows.py`, which needs nothing and runs in a
second. Takes about half a minute.
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import urlencode

os.environ.setdefault("SCHEDULE_MATCHER_HOME", tempfile.mkdtemp())
os.environ.setdefault("TELEGRAM_TOKEN", "1:FAKE")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta                               # noqa: E402

from schedule_matcher import config                                # noqa: E402
for d in (config.HOME, config.DEBUG_DIR, config.PROOF_DIR):
    d.mkdir(parents=True, exist_ok=True)

from schedule_matcher import webapp                                # noqa: E402
from schedule_matcher.booking import browser as B                  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), "" if ok else detail))


def main() -> int:
    from playwright.sync_api import sync_playwright

    days = [date(2026, 9, 14) + timedelta(days=n) for n in range(7)]
    mine = [(webapp.cell_start(days[1], 12), webapp.cell_start(days[1], 18))]
    others = {
        "Ada": [(webapp.cell_start(days[1], 12), webapp.cell_start(days[1], 16))],
        "Ben": [(webapp.cell_start(days[1], 14), webapp.cell_start(days[1], 20))],
    }
    params = {"v": 1, "c": -100, "e": "ABC123", "n": "Study session",
              "d0": days[0].isoformat(), "nd": len(days),
              "me": webapp.pack(mine, days),
              "heat": webapp.heat(others, days), "tot": len(others)}
    url = pathlib.Path(__file__).resolve().parent.parent / "docs" / "index.html"
    url = url.as_uri() + "?" + urlencode(params)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=B.LAUNCH_ARGS)
        page = browser.new_page(viewport={"width": 390, "height": 780})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(400)

        check("page: loads without a JavaScript error", not errors, str(errors))
        check("page: knows it is not inside Telegram",
              not page.is_hidden("#standalone"))
        check("page: draws one whole day", len(page.query_selector_all(".cell")) == 32)
        check("page: names the event", "Study session" in page.inner_text("#event"))
        check("page: starts on the first day",
              "14" in page.inner_text("#dayLabel"), page.inner_text("#dayLabel"))

        # what the bot packed must come back unchanged
        got = webapp.read_reply(page.evaluate("payload()"))
        check("round trip: an untouched grid returns what it was given",
              got and got["intervals"] == mine, str(got and got["intervals"]))

        # the heatmap has to reach the right cells: on Tuesday, Ada and Ben
        # overlap at 14:00-15:00 only
        page.evaluate("state.day = 1; paint();")
        shades = page.eval_on_selector_all(
            ".cell", "els => { const out = []; els.forEach(e => "
                     "out[+e.dataset.row] = e.dataset.h); return out; }")
        check("heatmap: both people show darkest where they overlap",
              shades[14] == "4" and shades[15] == "4", str(shades[12:20]))
        check("heatmap: one person shows lighter",
              shades[12] == "2" and shades[18] == "2", str(shades[12:20]))
        check("heatmap: nobody shows blank", shades[0] == "0", shades[0])

        # drag to paint Wednesday 09:00-11:00
        page.evaluate("state.day = 2; paint();")
        boxes = page.eval_on_selector_all(
            ".cell", "els => els.map(e => { const r = e.getBoundingClientRect();"
                     " return {row: +e.dataset.row, x: r.x + r.width / 2,"
                     " y: r.y + r.height / 2}; })")
        at = {b["row"]: b for b in boxes}
        page.mouse.move(at[2]["x"], at[2]["y"])
        page.mouse.down()
        for row in (3, 4, 5):
            page.mouse.move(at[row]["x"], at[row]["y"])
        page.mouse.up()
        page.wait_for_timeout(120)
        painted = webapp.read_reply(page.evaluate("payload()"))["intervals"]
        wed = [iv for iv in painted if iv[0].date() == days[2]]
        check("drag: paints exactly the cells dragged over",
              wed == [(webapp.cell_start(days[2], 2), webapp.cell_start(days[2], 6))],
              str(wed))
        check("drag: leaves the other day alone",
              [iv for iv in painted if iv[0].date() == days[1]] == mine)

        # drag over them again to rub out
        page.mouse.move(at[2]["x"], at[2]["y"])
        page.mouse.down()
        for row in (3, 4, 5):
            page.mouse.move(at[row]["x"], at[row]["y"])
        page.mouse.up()
        page.wait_for_timeout(120)
        after = webapp.read_reply(page.evaluate("payload()"))["intervals"]
        check("drag again: rubs the same cells out",
              not [iv for iv in after if iv[0].date() == days[2]], str(after))

        # a single tap is a single half hour
        page.mouse.click(at[10]["x"], at[10]["y"])
        page.wait_for_timeout(120)
        one = [iv for iv in webapp.read_reply(page.evaluate("payload()"))["intervals"]
               if iv[0].date() == days[2]]
        check("tap: one cell is 30 minutes",
              one == [(webapp.cell_start(days[2], 10),
                       webapp.cell_start(days[2], 11))], str(one))

        # moving between days, and copying one
        page.click("#next")
        check("days: Next moves forward one day",
              "17" in page.inner_text("#dayLabel"), page.inner_text("#dayLabel"))
        page.click("#copy")
        page.wait_for_timeout(120)
        copied = webapp.read_reply(page.evaluate("payload()"))["intervals"]
        check("days: Copy previous day duplicates it",
              any(iv[0].date() == days[3] for iv in copied), str(copied))
        page.click("#prev")
        check("days: Previous goes back", "16" in page.inner_text("#dayLabel"))
        page.evaluate("state.day = 0; paint();")
        check("days: the last day disables Next",
              page.is_disabled("#prev"), "Previous should be disabled on day one")

        browser.close()

    width = max(len(n) for n, _, _ in RESULTS)
    failed = 0
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}"
              + (f"   {detail}" if detail else ""))
        failed += not ok
    print(f"\n{len(RESULTS)} checks, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
