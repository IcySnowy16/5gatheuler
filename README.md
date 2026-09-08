# Schedule Matcher

A Telegram bot that is really two tools sharing one process:

**📅 Schedule Matcher** — find a time everyone is free (when2meet, in a chat).
**📚 Library Booking** — reserve an NTU library space on
[libcalendar.ntu.edu.sg](https://libcalendar.ntu.edu.sg/), check in, and get
out again.

All 25 categories in six libraries are offered, including the four the
homepage links without ids - Griffin Booth, and the Humanities library's
Computer Room, Study Pod and Window Seat, which book individual seats rather
than whole rooms.

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
python -m playwright install chromium   # ~700 MB on disk, needed to book
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

### Moving to a second laptop

The code is all you need to copy — clone it, or sync the folder. Nothing from
the data folder should travel with it, and none of it has to:

| Left behind | What happens |
|---|---|
| `.env` | Write a new one (same token, or a second bot from @BotFather). |
| `schedule_matcher.db` | Rebuilt on first run. Past bookings and favourites stay on the old machine. |
| NTU login | Re-enter with `/setup`. Windows encrypts it per machine, so a copied database could not decrypt it anyway. |
| `pw_state_*.json` | The bot signs in again by itself. |
| `bot.log`, `debug/`, `proof/` | Recreated as needed. |

The library catalogue — hours, notice periods, per-booking caps and all 116
desk names — **ships with the code** in `booking/catalog_seed.json`, so a new
install knows that Arrakis is day-of-only before anyone signs in. `/refreshcatalog`
re-reads it from the site if the library ever changes, and rewrites that file
so the correction can be committed.

> **One bot, one machine.** Two copies polling the same `TELEGRAM_TOKEN` fight
> over updates and both misbehave. Stop the old one first, or give the second
> laptop its own bot token.

### Running it on a small or older laptop

Measured, not estimated (Core Ultra 7; a 7th-gen dual-core is roughly 2–3×
slower on the CPU-bound parts):

| | Cost |
|---|---|
| Bot idle, no browser | **47 MB** RAM, ~0% CPU |
| Each live hold | **~270 MB** RAM (a headless Chromium, 4 processes) |
| Booking at a window opening | ~3 s from cold to ready-to-book |
| Data folder | 5 MB, plus a capped `debug/` |
| Chromium, one-off | ~700 MB on disk |

Idle it is nothing; the browsers are the whole cost. On **4 GB** set
`MAX_HOLDS=2`, on **8 GB** `MAX_HOLDS=4`. The bot now refuses a hold when the
machine has less than `MIN_FREE_RAM_MB` (500) free, rather than launching one
and risking Windows killing the process — which would drop every other hold
with it.

**Sleep is the real problem, not speed.** A booking window opens at 23:59:00,
and a sleeping laptop misses it entirely — the process is suspended, so
nothing fires and nothing is logged. On a tablet or laptop that runs it:

```powershell
powercfg /change standby-timeout-ac 0     # never sleep on mains
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10    # screen off is fine
```

Keep it plugged in for a 23:59 booking, and if it has a detachable keyboard,
set "closing the lid" to do nothing. Battery power will still sleep it.

**Restart it after a reboot.** Windows Update will restart the machine
eventually; nothing brings the bot back by itself. Register it once with Task
Scheduler:

```powershell
$py  = "C:\path\to\5gatheuler\.venv\Scripts\pythonw.exe"
$app = "C:\path\to\5gatheuler\Schedule Matcher.py"
schtasks /create /tn "ScheduleMatcher" /tr "`"$py`" `"$app`"" /sc onlogon /rl highest
```

It re-takes its holds on startup, so a restart costs a gap, not the slots.

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
- **Repeat weekly.** Pick the weekdays and an end date once and the bot books
  that slot every week - one race per occurrence, set up as each week's window
  comes near, never a term's worth of requests at once. `/recurring` lists
  them with pause and delete.
- `/bookings` shows what is on now or coming up; `/bookings all` opens the
  history. Finished bookings are kept only to work out your usual spots for
  `/mostused`.
- `/fav`, `/scheduled`, `/availability`, `/rules`, `/mostused`.

**Schedule Matcher**
- `/create` names the event and asks which days it covers, then posts one
  message in the group with a **Paint my availability** link.
- **`/add` answers privately.** Run it in the group and the bot messages *you*
  — the group hears nothing. You get both ways in: drag the grid, or tap
  through the calendar, whichever suits. (Someone who has never started the
  bot gets a link in the group, since otherwise they would get nothing.)
- **`/create` works from a private chat too.** Telegram's own group picker
  offers the groups the bot is already in; the event is created there and the
  message appears in the group, not in your DM. *Just for me* keeps it
  private instead.
- That link opens a **grid you drag across**, one day per screen, with
  everyone else's answers shaded underneath yours. It runs inside Telegram as
  a Mini App; sending replaces your previous answer.
- The group is never spammed: the original message is *edited* to list who has
  answered, however many people reply.
- `/view` (emoji grid, or a PNG; pick a subset of people) → `/best`.
  `/edit` and `/delete` still tap through a calendar, for old clients.

**Developer only** (`OWNER_ID`, or someone added with `/dev add <id>`)
- `/chope` holds a space without booking it, `/holds` manages them,
  `/holdtime` sets how long, `/developer` shows diagnostics and recent errors.

---

## The availability grid (Mini App)

Telegram cannot draw a paintable grid in a chat: an inline keyboard is
discrete buttons, capped at 8 per row and 100 in total, and a week of
half-hours is 224 cells. So the grid is a small web page opened inside
Telegram, served straight from this repo.

**Turn it on once:** repo Settings → Pages → Source *Deploy from a branch* →
`main` / `/docs`. That publishes `docs/index.html` at
`https://<user>.github.io/5gatheuler`, which is what `WEBAPP_URL` points at.

Syncing the folder to another machine does **not** do this. The page has to be
reachable by Telegram on somebody's phone, which needs a public HTTPS address;
OneDrive or a shared drive only moves files between your own computers.

If it is not published the bot notices at startup, says so in the log, and
falls back to the tap-through calendar - so nothing breaks and no button ever
opens a 404. Set `WEBAPP_URL=` (empty) to switch the grid off deliberately.

Nothing is hosted by us and no server is added. Everything the page needs
arrives in the URL - the days, your current answer, and everyone else's as a
4-bit-per-cell heatmap - and its reply comes back through Telegram's own
`sendData`, which is why painting happens in a private chat: Telegram only
allows a Mini App to answer from one. The group link carries the group's id,
and the bot checks with `getChatMember` that the sender really is a member
before writing anything.

`docs/index.html` opens in an ordinary browser too, with query parameters
faked, which is how it is developed:

```
docs/index.html?c=-100&e=ABC123&n=Test&d0=2026-09-14&nd=7&tot=0
```

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
python tests/smoke_handlers.py     # every command and callback, does it raise?
python tests/schedule_flows.py     # is the scheduling half's answer correct?
python tests/webapp_page.py        # drives the grid in a real browser
python tests/booking_categories.py # every library category, in every mode
python tests/booking_codes.py      # what the bot makes of a check-in code
```

`tests/smoke_handlers.py` calls every command and callback branch with mock
Telegram objects and reports any that raise. It exists because a handler once
failed silently for want of an argument; run it before every push.

`tests/schedule_flows.py` goes further for the scheduling half: it checks the
*answers*, not just the absence of a crash — that a painted grid comes back as
the times painted, that `/best` finds the overlap you can work out on paper,
that one person's answer cannot disturb another's, and that an empty answer, a
whole day, a stale grid and an outsider all do the right thing. No network, no
browser, about a second.

`tests/webapp_page.py` opens `docs/index.html` in headless Chromium and drags
a finger down the grid, then decodes what the page would send — in Python — to
prove the two implementations of the bitmask have not drifted apart.

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
