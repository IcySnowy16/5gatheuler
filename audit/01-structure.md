# Structure, top to bottom

*Written 2 Sep 2026. **No code was changed to produce this document.***

## 1. What the thing is

Two independent products sharing one Telegram bot process:

| | Schedule Matcher | Library Booking |
|---|---|---|
| Question it answers | "when are we all free?" | "get me a desk at NTU library" |
| Lives in | groups and DMs | DMs only (credentials) |
| External system | none - all local data | libcalendar.ntu.edu.sg (Springshare LibCal) |
| State | events + slots in SQLite | bookings, holds, scheduled jobs, NTU session |

## 2. Layers

```
                    Telegram (python-telegram-bot, long polling)
                                    │
   bot.py ── menus, /start /menu /dev, the whole Schedule Matcher half
      │                             │
      │                    booking/handlers.py ── every library screen
      │                             │
      ├── matching.py        booking/libcal.py    public grid + check-in (HTTP)
      ├── availability_view  booking/browser.py   logged-in booking (Playwright, sync)
      ├── keyboards.py       booking/holds.py     live "chope" sessions (Playwright, async)
      ├── flows.py           booking/scheduler.py background loop: fire jobs, check in
      │                      booking/catalog.py   hours / notice / caps / spaces
      │                      booking/groupbook.py split a session across people
      │                      booking/rules.py     known site rules + learned refusals
      │                      booking/botmail.py   the bot's own mail.tm inbox
      │                      booking/emailcode.py parse code + cancel link
      │                      booking/credstore.py DPAPI encrypt/decrypt
      └──────────────── storage.py  (SQLite, %LOCALAPPDATA%) ── config.py (.env)
```

Line counts: `handlers.py` 1964, `bot.py` 678, `storage.py` 579, `browser.py` 535,
`groupbook.py` 447, `holds.py` 436, `libcal.py` 351, `scheduler.py` 207,
`catalog.py` 195. Total ~6,250.

## 3. Feature → file → function

### Schedule Matcher
| Feature | Where | Key functions |
|---|---|---|
| Create/list events | `bot.py` | `cmd_create`, `cmd_events`, `storage.create_event` |
| Add availability | `bot.py` + `keyboards.py` | `cmd_add` → `on_callback` (`evt/nav/date/t_start/t_save`) → `storage.add_slot` |
| Availability grid | `availability_view.py` | `counts`, `emoji_grid`, `png_grid`; picker in `bot._send_grid` + `gpick*` callbacks |
| Best common time | `matching.py` | `merge_intervals`, `best_slots`, `format_suggestions` |
| Edit / delete slots | `bot.py` | `cmd_edit`, `cmd_delete`, `del_evt`/`del_slot` |

### Library Booking
| Feature | Where | Key functions |
|---|---|---|
| Browse availability | `libcal.py` | `fetch_locations`, `fetch_grid`, `bookable_starts`, `valid_ends`, `spaces_free_for` |
| The booking flow | `handlers.py` | `_start_flow` → `_render` → `_r_home/_r_cat/_r_day/_r_dur/_r_range/_r_space/_r_confirm` |
| Modes | `handlers.py` | `MODE_ORDER`, `_mode_row`, `_switch_mode` |
| Actually booking | `browser.py` | `book()`: cart add → end update → `/ajax/space/times` → login → `accept_terms` → `submit_booking_form` |
| Choping (dev only) | `holds.py` | `create`, `renew`, `release`, `book`, `watcher` |
| Scheduled bookings | `scheduler.py` | `run`, `_run_job`, `_retry_or_fail`, `_tick_seconds` |
| Auto check-in | `scheduler.py` | `_checkin_scan` (T-2min / T / T+5min) |
| Rules and limits | `catalog.py`, `rules.py` | `hours_for`, `window_opens_at`, `max_each_minutes`; `precheck`, `record_refusal` |
| Group sessions | `groupbook.py` | `_split_legs`, `_chope_legs`, `_claim`, `_book_leg` |
| Codes and email | `emailcode.py`, `botmail.py` | `parse_text`; `ensure_inbox`, `fetch_messages` |

## 4. The concepts that matter

1. **Public vs private halves of LibCal.** Availability (`/spaces/availability/grid`)
   and check-in/check-out (`/r/checkin`, `/r/checkout`) are plain HTTP with no
   login. Everything that *reserves* goes through NTU SSO, so it needs a real
   browser. This split is why `libcal.py` (fast, httpx) and `browser.py`
   (slow, Playwright) are separate modules.
2. **The booking flow is a state machine in `user_data["bk"]`.** One dict holds
   mode, library, category, day, duration, start/end, space and the fetched
   grids; `_render(step)` draws whichever screen the dict currently implies,
   and `_prev_step` walks it backwards.
3. **A chope is the checkout page.** Reaching checkout marks the slot taken for
   everyone; leaving that page (or ~5.5 min) frees it. Hence a hold must own a
   *live* browser, and holds cannot be resumed after a restart.
4. **Two clocks.** The site opens each day's window at 23:59:00 exactly
   (measured), and the check-in window is start-5min → start+15min. Almost
   every scheduling decision in the bot derives from those two facts.
5. **Freshness over memory.** Grids are re-fetched at the confirm screen and
   again immediately before submitting, because availability changes second by
   second.
6. **Secrets stay on the machine.** `.env` and the SQLite file live in
   `%LOCALAPPDATA%`, never OneDrive; the NTU password is DPAPI-encrypted;
   `OWNER_ID` is read from `.env` so no Telegram id is committed.

## 5. Request lifecycles

**Booking now**
```
/book → _start_flow (mode=now) → library → category → day → duration
      → range (only genuinely free slots) → space → confirm
      → freshness re-check → browser.book (thread)
      → storage.add_booking → poll bot inbox for the code
      → scheduler._checkin_scan checks in at start time
```

**Scheduled booking**
```
/book (mode=sched) → day (closed days hidden, window time shown)
      → duration (policy cap) → slot (inside opening hours) → space (catalogue)
      → fire time (defaults to the real window opening)
      → storage.add_scheduled
      → scheduler.run tick (5 s while racing) → _run_job → browser.book
      → retries every 15 s for 5 min, then every 2 min for 30 min
```

**Group session**
```
/groupbook (group) → category/day/period → legs
      → optional: chope every leg under the organiser
      → member taps Claim → DM confirm → release that leg's hold
      → book under the member's own credentials → update the group message
```
