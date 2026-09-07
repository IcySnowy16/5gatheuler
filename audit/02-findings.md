# Errors and logical flaws

*Updated 3 Sep: H1, H2 fixed earlier; the error-visibility work below (background tasks, group booking, `/developer`) is now done too - see the bottom section.*

*Written 2 Sep 2026. **No code was changed.** Each finding says how it was
verified, so you can re-check any of them yourself.*

Severity: **H** breaks or silently misbehaves · **M** wrong in some cases ·
**L** maintenance risk.

---

## H1. ~~The catalogue expires after 14 days and then lies~~ FIXED 3 Sep

`catalog._meta()` reads the cache with `max_age_hours=24*14`. When it expires,
every getter silently falls back to a default — and the defaults are wrong for
the categories that matter most.

Verified with an empty catalogue:

```
Arrakis advance_days = 1     (the true answer is 0 - day-of only)
Arrakis spaces       = 0     (there are 20)
window for Fri 04    = Wed 02 23:59   (should be Thu 03 23:59)
```

Consequence: a scheduled Arrakis booking would fire **a whole day early**, sit
in "window isn't open yet", exhaust its 30-minute retry budget, and give up —
silently, a day before the slot it wanted. Nothing warns that the data is
stale, and **nothing ever refreshes it**: `catalog.refresh()` exists but has no
caller (grep finds only its own definition and docstring). The data currently
in the database was seeded by hand from tonight's probes.

**Fixed:** the catalogue, space names, probed limits, learned refusals and the
bot inbox now live in a `durable` table with no expiry (only an `updated_at`
stamp), and the existing cache entries were promoted into it on first run.
**Also fixed 4 Sep:** a *fresh* install had the same problem for a different
reason - an empty database and no way to fill it, since the policy pages need
a login. It silently fell back to `advance_days = 1`, so a day-of category
would have been scheduled to fire a whole day early:

```
new install, before:  0 categories | Arrakis notice = 1 day | "Room 46002"
new install, after:  21 categories | Arrakis notice = 0 days | LIBLWNL-AK-01
```

The measured catalogue now ships with the code as `booking/catalog_seed.json`
(21 categories, 116 desk names - public library data, nothing personal) and
loads itself the first time the catalogue is read. `/refreshcatalog` re-reads
it from the site and rewrites the seed file so the correction can be
committed. There is still no *scheduled* refresh - that part remains open,
but it now matters far less: the seed is right until the library changes.

## H2. ~~Holds die with the process, and nothing tells the user~~ PARTLY FIXED 3 Sep

`holds._holds` is a plain in-memory dict of live browsers. A restart (or a
crash, or Windows rebooting overnight) loses every hold: the browsers close,
the slots quietly lapse ~5 minutes later, and the user is never told — they
believe a desk is held for them. `bot.py`'s `post_shutdown` releases holds on a
*clean* shutdown only.

**Fixed:** holds are written to a `holds` table as they are taken, renewed and
released. On startup `holds.restore()` tries to re-take each one for up to 8
minutes (the old session's hold lapses after ~5.5) and DMs the outcome either
way. It is *re-acquisition*, not resumption - the slot can still be lost to
someone else in the gap, which is unavoidable given the single-use checkout.

## H3. Two sources of truth for booking length

`libcal.category_limits()` (probed once from a cart) and
`catalog.max_each_minutes()` (the published policy) both claim to answer "how
long can this booking be". `_r_dur` merges them with a rule that differs per
mode (`policy_max if sched else max(hi, policy_max)`). The cart-probed figure
reflects only the neighbouring bookings on the day it was probed — Cinema Room
was recorded as "30-30 min" when its real policy is 4 hours.

*Fix direction:* policy is the ceiling; the live grid (`valid_ends`) is the
per-slot truth. Delete the probed cache or demote it to a diagnostic.

## H4. `config.MAX_BOOKING_MINUTES` silently caps every category

`libcal.valid_ends()` stops walking at `config.MAX_BOOKING_MINUTES` (240). Any
category permitting longer would be truncated with no message. Today nothing
exceeds 4h so it is invisible — it will bite the day a policy changes.

## M5. Timezone is decorative

`config.TIMEZONE = "Asia/Singapore"` is **never used** (grep: one hit, its own
definition). Everything else is naive `datetime.now()`. The bot is therefore
correct only while the host machine is on SGT. On a laptop that travels, every
window calculation, check-in gate and fire time shifts silently.

## M6. One flow state per user, shared by five commands

`user_data["bk"]` is written by `/book`, `/fav`, `/move`, `/schedulebook` and
`/extendedbooking`. `flows.py` now closes the *previous screen*, but the
underlying dict is still single-slot: starting `/move` while a `/book` is
half-finished overwrites `bk` (including `move_from`), and a stale callback
from the old screen would act on the new state. The screen-superseding makes
this unlikely, not impossible.

## M7. Concurrent Playwright sessions race on one session file

`browser.book`, `browser.harvest_names` and `holds.create` all read and write
`pw_state_<user>.json` (6 call sites). Two operations for the same user at once
— a scheduled job firing while the user books by hand, or `/extendedbooking`
creating several holds — can interleave writes and corrupt or stale-ify the
saved cookies, forcing an unnecessary re-login.

## M8. `_checkin_scan` gates on attempt count, not on time

Attempts are indexed by `checkin_attempts`, so the three gates (T-2, T, T+5)
are consumed in order regardless of *when* they run. If the bot is offline
until T+6, it will fire attempt 1 (the "T-2" gate) immediately, then attempt 2,
then attempt 3 — three rapid-fire attempts inside one tick window instead of a
spread. Harmless today because the site tolerates repeats, but it is not the
schedule the code claims.

## M9. Cancelling and checking out are recorded as the same thing

`_cancel_any` sets `status='cancelled'` after a successful `/r/checkout`.
Checking out of a session you are *in* and cancelling a booking that has not
started are different acts with different consequences (no-show rules). The
history cannot tell them apart afterwards.

## M10. Claimed group legs never time out

`groupbook._claim` marks a leg `claimed` and DMs the member. If they never
tap Confirm, the leg stays claimed forever: nobody else can take it, and the
organiser's chope on it was already released at claim time. Nothing reaps it.

## M11. Code extraction is loose

`emailcode.CODE_RE` matches `code[:\s-]*([A-Z0-9]{4,8})`. Pasting a whole
email that happens to contain "…zip code 639798…" or a tracking reference can
capture the wrong token. The booking chooser limits the damage (you pick which
booking), but the value itself is taken on trust.

## M12. Availability grid ignores anything before 08:00 or after midnight

`availability_view.ROWS` spans 08:00-24:00. A slot someone adds at 07:00 (the
scheduling half allows 08:00+ only, but migrated pickle data may differ) is
silently absent from the picture while still counting in `/best`, so the grid
and the recommendation can disagree.

## L13. `handlers.py` is a 1,964-line god module

UI rendering, business rules, storage calls, browser orchestration and command
registration all live together. `_r_*` renderers, `cmd_*` handlers, favourites,
holds glue and the callback dispatcher are one file. It is the main reason each
change in this project has needed careful patching rather than confident edits.

*Fix direction:* split into `flow.py` (renderers + state machine),
`commands.py` (entry points), `favourites.py`, keeping `handlers.py` as wiring.

## L14. The catch-all callback handler must stay registered last

`bot.py` registers `CallbackQueryHandler(on_callback)` with no pattern. It
works only because every prefixed handler (`bk|`, `gb|`, `av|`, `menu|`,
`run|`) is registered before it. Anyone adding a prefixed handler after that
line will find it silently dead.

## L15. Feature discovery is inconsistent for hidden features

`/dev` returns silence for non-developers, but `/chope` and `/holds` reply with
an explanatory message — so the features are discoverable anyway. Pick one.

## L16. `bot.py` still holds the whole Schedule Matcher

The plan called for `scheduling.py`; the code still has events, slots, the grid
viewer and the shared `on_callback` inside `bot.py`, which is supposed to be
wiring only. The two products are separated in the *menus* but not in the
*code*.

## L17. No automated tests

Everything has been verified by ad-hoc scripts in a scratch folder that are not
part of the repo. There is no `tests/`, so none of the invariants
(shortlisting, mode switching, hours, window maths, superseding) are protected
against a future edit.

---

## What is genuinely solid

Worth stating, since the list above is one-sided:

- **The booking mechanics are measured, not assumed** — the AJAX contract, the
  23:59 opening (watched across midnight), the 5.5-minute hold lifetime, the
  45-minute buffer semantics, the Continue→tick→Submit checkout order.
- **Secrets handling** — DPAPI at rest, everything outside OneDrive, owner id
  in `.env`, password message deleted from the chat after reading.
- **Freshness discipline** — the grid is re-checked at confirm and again before
  submit, so the bot rarely promises a slot it cannot get.
- **Honest failure reporting** — the site's own refusal text is surfaced rather
  than a generic error, and refusals are collected in `rules.py` for later.


---

## Fixed 3 Sep: failures are now visible

The `_pick_booking` bug (`/cancelbooking` and `/move` doing nothing at all)
was a missing `context` argument. Beyond the one-line fix:

- **Global error handler** - any handler that raises now answers in the chat
  ("Something went wrong on my side…"); developers also see the exception.
- **`tasks.spawn`** wraps the four background jobs that plain
  `asyncio.create_task` would have silenced (`_poll_botmail` ×2,
  `holds._recover_one`, `scheduler._run_job`): each logs, records, and DMs the
  person waiting, naming the feature.
- **`storage.record_error` / `recent_errors`** keep the last 50 failures in the
  durable table, and **`/developer` lists them** - so errors are visible in
  Telegram, not only in `bot.log`.
- **Group booking** reports a failed tap instead of going quiet.
- **`tests/smoke_handlers.py`** drives all 74 commands and callback branches
  with mocks; it is what would have caught the original bug. Currently: 74
  exercised, 0 failing.

Still open from the list above: H3 (two sources of truth for booking length),
H4, M5 (timezone), M6-M12, L13-L17.

---

## Fixed 4 Sep: four faults found by measuring, not reading

**H18. A check-in took 74 seconds, 59 of them doing nothing.**
Profiling each phase showed the work finished in ~11 s and `browser.close()`
then blocked for 9-50 s (highly variable, and Chromium's own shutdown - no
ordering of page/context/browser close avoids it; leaving it to the
`with sync_playwright()` block is the same or worse). Two changes: the page
now waits for the check-in POST to answer rather than for `networkidle`,
which that page never reaches; and the answer is handed back through a queue
the moment the screenshot exists, leaving the browser to shut down in its own
thread. Measured before and after, same codes: **74 s -> 4.1 s**.

**H19. A wrong code was treated as "too early" and saved anyway.**
`/checkin ABC123` saved the code against whatever single active booking
existed, called the site, and on any failure said "Too early? I'll keep
trying". The site had actually said *"Unable to find booking matching code"*.
So a typo silently overwrote a good booking's code and promised retries that
could never work. `/checkin` now asks the site what the code is **before**
touching anything, and distinguishes what the site distinguishes:

```
3WQ  -> wrong code: say so, save nothing, never retry
4K7  -> real, but already checked out: say the session is over
3WT  -> real: LIBLWNL-AK-05 13:45-15:00, filed against that booking
```

The same answer, "Unable to find booking matching code", also comes back for
a booking that is not live on the site yet, so the reply says so rather than
asserting a typo.

**H20. The bot token was written to the log 9,692 times.**
python-telegram-bot polls `api.telegram.org/bot<TOKEN>/getUpdates` and httpx
logs the URL at INFO; `bot.log` and its two rotations carried the token in
full, and `/developer` prints log tails. A filter now redacts it from every
handler, and httpx's polling chatter is silenced (which also stops the log
churning through 1 MB rotations). Existing log files still contain it -
delete them, or treat the token as compromised and reissue it.

**M21. Cancel and move trusted the local database completely.**
There is no page on this LibCal that lists a person's bookings: twenty
endpoints were probed, and the availability grid's slots carry no owner
(`keys: checksum, className, end, itemId, start`). What the grid does prove
is whether a desk is taken at a time - so `confirm_booking()` checks each
stored booking against it before `/cancelbooking`, `/move` or `/bookings`
offer it. A booking the site shows as free again is marked cancelled and
reported, instead of being offered as something to cancel. Verified: a free
cell returns `gone`, a taken one `held`, and a past day returns `unknown`
rather than guessing.

For someone whose bookings were all made on the website, the honest answer is
that the bot cannot enumerate them - so those commands now say so and point
at the two things that do work: the check-in code, or pasting the
confirmation email.


---

## Fixed 7 Sep: four categories were never offered at all

`libcal.fetch_locations()` built the category list by reading `lid`/`gid` out
of the homepage's links. Four categories are linked without ids and were
silently discarded by the `lid == 0` guard:

| Category | Library | Homepage link | Real ids |
|---|---|---|---|
| Griffin Booth | Lee Wee Nam | `/reserve/collab` | `3368 / 8423` |
| Computer Room | Humanities | `/space/52771` | `4906 / 13378` |
| Study Pod | Humanities | `/space/52888` | `4906 / 13402` |
| Window Seat | Humanities | `/space/52772` | `4906 / 13380` |

The Humanities library was therefore offered with nothing but an "All
Categories" entry, which is a `gid=0` view with no grid of its own - so none
of its four categories could be booked.

The list now merges the homepage with the catalogue, which ships with the
code, so a machine that has never logged in still sees all 25. `refresh()`
reads each library's own category dropdown (`select#gid`), which is the
authoritative list, and its Policy blurb, which is the only source for the
notice period and the length caps - Griffin Booth turns out to be 1 day's
notice, 2 hours each.

**The Humanities categories book seats, not rooms.** Asking for their grid the
ordinary way answers with a single item - the room itself, the `/space/NNNNN`
the homepage links - and the real seats appear only when the request says
`seat=1`:

```
Study Pod   default -> 1 space  [52888]
Study Pod   seat=1  -> 4 spaces [7554, 7555, 7556, 7557]  (SP1-SP4)
Window Seat seat=1  -> 10 spaces (WS01-WS10)
```

`refresh()` detects this by itself - a category whose real spaces are none of
the ids the public grid returned is a seat category - and records `seats:
true`, which `fetch_grid` then honours. Griffin Booth is an ordinary category
(12 spaces) and needs none of it.


---

## Fixed 7 Sep: a code overwrote the booking it was attached to

Reported from the chat, with the evidence in one screenshot. Before
`/code M3P5`:

```
#2 LIBLWNL-AK-01 (Capacity 1) (Lee Wee Nam Library) 17:30 to 19:00 - no code
```

after:

```
#2 your booking ((booked by you)) 17:30 to 19:30 - code M3P5
```

The desk name, the library and the end time were all replaced with
placeholders. `_attach_code` computed `space = found["space"] or "your
booking"`, `where = found["location"] or "(booked by you)"` and `end =
found["end"] or start + 2h`, then wrote all four fields back over the matched
row - so when the site knew only the start time, which is the normal answer
before a check-in window opens, three real values were traded for invented
ones and a 90-minute booking silently became two hours.

Now only what the site actually said is written, and the times are rewritten
only when it gives a complete, sane pair (a successful check-in reports both).
The reply is rendered from the stored row afterwards, so it shows the real
desk rather than the probe's blanks.

**And the bot no longer guesses which booking a code belongs to.** When the
site names the space, that settles it. When it gives only a time - as here -
the bot asks, offering that day's bookings closest-first plus "a separate
booking I made myself":

```
Code M3P5 is for a booking starting 17:30 on Mon 07 Sep, but the library
did not say which space.

Is it one of these, or a booking of its own?
  [ LIBLWNL-AK-01 17:30-19:00 ]
  [ your booking 13:30-15:30 - already has a code ]
  [ A separate booking I made myself ]
```

Writing the fix turned up a latent trap in the five generic `update_*(id,
**fields)` helpers: with no fields they built `UPDATE bookings SET  WHERE
id=?` and raised `sqlite3.OperationalError`. An empty update is now a no-op.


---

## Fixed 8 Sep: Griffin Booth was still missing from a running bot

Reported with a screenshot of the live picker: "All Categories" and ten Lee
Wee Nam categories, no Griffin Booth - exactly the list the 7 Sep fix was
supposed to correct.

The fix was real but incomplete. `fetch_locations()` merges the catalogue into
the homepage parse, which is what makes Griffin Booth and the three Humanities
categories visible - but only on the path that rebuilds the list. The cached
path returns 24 hours early:

```python
cached = storage.cache_get("libcal_locations", max_age_hours=24)
if cached:
    return [Location(...) for loc in cached]   # merge never runs
```

The database confirmed it: one `kv` row written 4 Sep 10:43 holding eleven Lee
Wee Nam entries, the first of them `All Categories | gid 0` - the row cut off
at the top of the screenshot.

Now a single `_normalise()` runs on every path, cached or fresh: it drops
`gid=0` views and merges the catalogue. Against the live database the picker
went from that stale eleven to the correct 25 across six libraries, with no
"All Categories" rows.

Checked at the same time, since the report asked: every one of the 25
categories the catalogue knows is offered, none is offered that the catalogue
does not know, and every one returns a real availability grid - AV Room 1
space through Arrakis 20 and PC / Single Monitor 18. All four modes - Book,
Schedule, Extended, Repeat - draw from the same list, which
`tests/booking_categories.py` now proves by seeding that exact stale cache and
walking each mode into the category screen.
