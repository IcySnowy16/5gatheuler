# What to clean up in `handlers.py`

*Written 12 Sep 2026. **No code was changed** — this is a reading list for when
you have time. Every number below was measured from the file, not estimated.*

`schedule_matcher/booking/handlers.py` is **3,306 lines**, a quarter of the
whole codebase, holding **123 top-level functions**. It is the only file in the
project where a change needs care rather than confidence, so it is worth
knowing exactly which parts are heavy and why.

## Where the weight is

| Group | Lines | What it is |
|---|---:|---|
| `cmd_*` commands | 629 | the 20 commands the library half answers |
| helpers | 619 | labels, keyboards, grid fetching, small formatters |
| bookings & cancel | 544 | `_bk_go`, `_cancel_any`, `_do_cancel`, `/move`, the Fix screen |
| screens `_r_*` | 506 | the 14 step renderers |
| codes & check-in | 347 | `_attach_code`, `_file_code`, `checkin_booking`, proofs |
| holds & extended | 147 | `_run_hold_plan`, hop plans, chope glue |
| favourites | 65 | the `/fav` sub-flow |
| dispatchers | 54 | `on_booking_callback`, `on_private_text` |
| recurring | 49 | weekday picker, rule lines, `rule_upcoming` |

The longest single functions:

```
on_booking_callback   352 lines   line 2922
cmd_selfcheck         110 lines   line 2518
cmd_checkin           101 lines   line 1891
_bk_go                 94 lines   line 1033
_r_confirm             81 lines   line  810
_run_hold_plan         73 lines   line 1150
```

---

## 1. The callback dispatcher is the real problem

`on_booking_callback` is **352 lines and 51 branches**, handling every one of
these actions in one `if/elif` chain:

```
rnew rpause rdel codeto codenew fix fclr fdel scancel ci cx favadd favdel
fav fadd fal fac fas mv mu setcode hext hallback ht hagain hbook hrel
loc cat again day dur rg st en sp hop any fine fire wd wdone until
favu favt go showall mode favhere back home abort
```

Two different kinds of thing are mixed in it. The first group acts on a
**stored row** — a booking, a rule, a favourite — and works whether or not a
flow is in progress. The second group (`loc` through `abort`) only makes sense
**inside a live flow** and sits after the `if bk is None` guard.

*Worth doing:* a dispatch table — `{"scancel": _on_scancel, ...}` — split into
two dicts, one for row actions and one for flow actions. Each branch becomes a
named function, the guard applies to one dict rather than being a line in the
middle of a 352-line function, and the file gains 40 short functions instead of
one long one. This is the single change that would most improve the file.

## 2. Three clusters could be their own modules

Each of these is cohesive, has few callers, and would move almost intact:

- **`booking/checkin.py`** (~347 lines) — `_attach_code`, `_file_code`,
  `_learn_from_checkin`, `checkin_booking`, `_proof_only`,
  `_send_checkin_proof`, `purge_proofs`, `cmd_checkin`, `cmd_code`.
  Depends on `libcal`, `browser`, `storage`, `tasks`. Nothing in the step
  machine calls into it except `_report_booking`.
- **`booking/favourites.py`** (~65 lines) — the whole `/fav` sub-flow, already
  self-contained with its own `user_data["fadd"]` state.
- **`booking/diagnostics.py`** (~200 lines) — `cmd_selfcheck`, `cmd_developer`,
  `cmd_refreshcatalog`, `_write_seed`. These are about the machine, not about
  booking anything.

That is roughly 600 lines out, with no change to behaviour, and it is the
lowest-risk order to do them in (favourites first — it is the smallest and has
the fewest callers).

## 3. Smaller things noticed while counting

- **`_require_private` is repeated 18 times** as the first two lines of a
  command. A decorator (`@private_only`) would remove 36 lines and make it
  impossible to forget on a new command.
- **103 calls to `query.edit_message_text`**, many with the same
  "message + `_kb(items, bk=bk)`" shape. A small `screen()` helper would make
  the renderers read as a list of screens rather than a list of Telegram calls.
- **`cmd_selfcheck` at 110 lines** is one long function building a report. It
  wants to be a list of small check functions returning `(name, ok, detail)`,
  which would also let a test assert on individual checks.
- **`_bk_go` (94 lines)** branches on mode at the top — `recur`, `sched`, then
  `chope`/`ext`, then the live booking path. Those four are separate enough to
  be four functions behind one small dispatcher.
- **`on_private_text`'s handler chain** is fine as it is, but the five
  `_*_input` functions it calls all share the shape "am I waiting? no → return
  False". That shape could be one decorator too.

## What is *not* worth changing

- The **step machine itself** (`_render` and the 14 `_r_*` functions). It is
  long but flat, each renderer does one screen, and the dispatch dict makes the
  whole flow readable in one glance. Splitting it would hide the shape.
- The **callback-data strings** (`bk|dur|120`). They are terse because Telegram
  caps callback data at 64 bytes; renaming them for readability would cost
  compatibility with any button already sitting in somebody's chat history.
- **`storage.py` at 1,001 lines.** It is a flat list of small functions grouped
  by table, which is exactly what a data layer should look like.

---

## A note on the step names

While reading, the three step names that are not obvious:

| Step | Screen the user sees | Why it is called that |
|---|---|---|
| `dur` | "how long do you need?" — 30 min to 4 h | **dur**ation of the booking |
| `range` | "which slot?" — `13:30-15:30`, `14:00-16:00`… | the time **range**: start and end chosen together, instead of two screens |
| `space` | "which space?" — `LIBLWNL-AK-07`, "Any space" | the **space**, which is the library's own word for a desk, pod or room |

`range` exists because picking a start and then an end used to be two screens
and four taps; offering whole ranges that actually fit made it one tap. The old
two-screen path is still there as `start` → `end`, reached by the
"Custom start & end" button.
