# Schedule Matcher

A Telegram bot that is really two tools sharing one process:

**📅 Schedule Matcher** — find a time everyone is free (when2meet, in a chat).
**📚 Library Booking** — reserve an NTU library space on
[libcalendar.ntu.edu.sg](https://libcalendar.ntu.edu.sg/), check in, and get
out again.

Everything the bot knows about the library was measured from the site rather
than assumed — opening hours, how much notice each category needs, how long a
"chope" lasts, and the fact that day-of windows open at **23:59:00** exactly.
See [`audit/03-site-data.md`](audit/03-site-data.md).

---

## Install on a new laptop

You need **Python 3.11+** and **git**. Everything else is installed below.

```powershell
git clone https://github.com/IcySnowy16/5gatheuler.git
cd 5gatheuler

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium   # ~400 MB, needed to book
```

On Linux add the browser's system libraries:

```bash
python -m playwright install --with-deps chromium
```

### Configure

The bot keeps secrets **outside the repo**, in a per-machine folder:

| | Windows | Linux / macOS |
|---|---|---|
| Folder | `%LOCALAPPDATA%\ScheduleMatcher\` | `~/.schedule-matcher/` |

Copy `.env.example` there as `.env` and fill in two values:

```ini
TELEGRAM_TOKEN=123456:ABC...     # from @BotFather
OWNER_ID=                        # your Telegram user id, from @userinfobot
```

`OWNER_ID` is the only account that can use `/dev` and grant developer mode.
Leaving it blank simply means nobody has developer features.

### Run

```powershell
python "Schedule Matcher.py"
```

When it prints `Bot is running. Data dir: …` it is live. Message it `/start`
in Telegram, then `/setup` (private chat) to save your NTU login — it is
encrypted at rest and never leaves the machine.

---

## Using it

Send `/menu` — everything is buttons; nothing has to be typed.

**Library booking**
- `/book` — library → category → day → duration → slot → space → confirm.
  Only genuinely free days, times and spaces are ever offered, and the grid is
  re-checked immediately before submitting.
- Modes on the same screen: **Book** now · **Schedule** for a window that has
  not opened · **Extended** for a session longer than one booking allows.
- **Extended hops desks.** If no single table is free for the whole window,
  the bot works out the fewest tables that cover it and says where you move
  and when — `11:30-13:15 AK-14`, `13:15-14:45 AK-12`, "One move, at 13:15".
  Spans needing a hop are marked `⇄2` while you choose the time. The first
  leg is booked; the rest are choped so nobody takes them.
- `/checkin` — check in now, or `/checkin ABC123 14:30` for a booking you made
  on the website yourself. Auto check-in runs at T−2 min, T and T+5 min.
- **Paste the confirmation email** into the chat: the bot reads the space,
  times, code and cancellation link and files the booking for you.
- `/cancelbooking` — cancel *is* check-out on this site; works until the
  booking ends. `/move` books the new slot first, then releases the old one.
- `/fav`, `/bookings`, `/scheduled`, `/availability`, `/rules`, `/mostused`.

**Schedule Matcher**
- `/create` → `/add` → `/view` (emoji grid, or a PNG; pick a subset of people)
  → `/best`. `/edit` and `/delete` change your own times.

**Developer only** (`OWNER_ID`, or someone added with `/dev add <id>`)
- `/chope` holds a space without booking it, `/holds` manages them,
  `/holdtime` sets how long, `/developer` shows diagnostics and recent errors.

---

## Deploying to a server

The bot only needs outbound HTTPS — no domain, no open ports.

- **Set the timezone to `Asia/Singapore`.** The code uses local time
  throughout, so a UTC server shifts every booking window by 8 hours.
- Budget **~300 MB of RAM per concurrent hold** (each is a headless Chromium)
  plus ~100 MB for the bot. Tune `MAX_HOLDS` to fit; add swap on a 1 GB box.
- `HOLD_MAX_MINUTES=0` lets chopes run indefinitely.
- Point `SCHEDULE_MATCHER_HOME` at a folder you back up — it holds the
  database and your credentials.
- Run under `systemd` with `Restart=always`; the bot re-takes its holds when
  it starts.

> **Linux note:** credentials are encrypted with Windows DPAPI. On Linux that
> falls back to plain storage, so treat the data folder as sensitive until
> platform encryption lands.

---

## Working on the code

```powershell
python -m pyflakes schedule_matcher/*.py schedule_matcher/booking/*.py
python tests/smoke_handlers.py     # drives all 74 commands + callbacks
```

`tests/smoke_handlers.py` calls every command and callback branch with mock
Telegram objects and reports any that raise. It exists because a handler once
failed silently for want of an argument; run it before every push.

**Branching.** `main` stays working. Anything that fixes a bug or adds a
feature gets its own branch and a pull request:

```powershell
git switch -c fix/checkin-window-off-by-one
# …work, then:
python tests/smoke_handlers.py
git commit -am "Fix the check-in window boundary"
git push -u origin fix/checkin-window-off-by-one
```

Name branches `fix/…`, `feat/…` or `chore/…` after what they do.

## Layout

```
Schedule Matcher.py         launcher
schedule_matcher/
  bot.py                    menus, Schedule Matcher half, error handler
  storage.py                SQLite: bookings, events, holds, settings
  config.py                 .env and where private files live
  flows.py                  one live screen per tool, in DMs
  ask.py                    asks for a value instead of printing "Usage:"
  availability_view.py      when2meet grid (emoji + PNG)
  matching.py               overlap search behind /best
  tasks.py                  background work that cannot fail silently
  booking/
    handlers.py             every library screen
    libcal.py               public grid, check-in, code lookup
    browser.py              the logged-in booking flow (Playwright)
    holds.py                choping
    scheduler.py            scheduled bookings + auto check-in
    catalog.py              hours, notice periods, caps, spaces
    groupbook.py            splitting a session across people
    rules.py                the library's rules, and ones we learn
audit/                      structure map, findings, measured site data
tests/smoke_handlers.py     the regression net
```
