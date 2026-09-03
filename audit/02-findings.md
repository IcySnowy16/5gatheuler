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
`catalog.refresh()` still has no scheduled caller - that part remains open.

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
