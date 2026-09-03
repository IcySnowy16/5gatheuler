# Measured facts about libcalendar.ntu.edu.sg

*Probed 1-2 Sep 2026 from the public availability grid, each category's
own Policy text, and a midnight watch across the window opening.*

## Opening hours (identical for every category)

| Day | Hours |
|---|---|
| Mon-Fri | 08:30 - 21:00 |
| Saturday | 08:30 - 16:30 |
| Sunday | **closed** - no category returns a single slot |

## The booking window opens at 23:59:00

Watched live across midnight on 1 Sep, polling every 20 s:

```
23:58:56  Arrakis: Wed 02 still closed
23:58:57  Learning Pod: Thu 03 still closed
23:59:18  Arrakis: Wed 02 OPENED (20 spaces, 1000 slots)
23:59:19  Learning Pod: Thu 03 OPENED (6 spaces, 300 slots)
```

So a category taking bookings `A` days ahead opens day `D` at 23:59 on
`D - A - 1`. Contested desks go within seconds, which is why the
scheduler now retries every 15 s for the first 5 minutes.

## Per category

| Library | Category | Notice | Max/booking | Max/day | Spaces |
|---|---|---|---|---|---|
| Art, Design & Media Library | AV Room | 30 days | 4h | 4h | 1 |
| Art, Design & Media Library | Audio-Visual Pods | 1 day | 4h | 4h | 3 |
| Lee Wee Nam Library | Circular Pod | 1 day | 2h | 2h | 5 |
| Lee Wee Nam Library | Learning Pod | 1 day | 4h | 4h | 6 |
| Lee Wee Nam Library | Recording Room | 30 days | 2h | 2h | 1 |
| Lee Wee Nam Library | Tardis - Video Conferencing Room | 1 day | 2h | 2h | 1 |
| Lee Wee Nam Library | Arrakis - Single Monitor | day-of (23:59 night before) | 2h | 8h | 20 |
| Lee Wee Nam Library | Arrakis - Dual Monitors PC | day-of (23:59 night before) | 2h | 8h | 5 |
| Lee Wee Nam Library | Curved Monitor | day-of (23:59 night before) | 2h | 8h | 4 |
| Lee Wee Nam Library | Single Monitor | day-of (23:59 night before) | 2h | 8h | 8 |
| Lee Wee Nam Library | Stargate - Single Monitor | day-of (23:59 night before) | 2h | 8h | 6 |
| Lee Wee Nam Library | Immersion@Stargate | 1 day | 2h | 8h | 2 |
| Business Library | Cinema Room | 30 days | 4h | 4h | 1 |
| Business Library | Discussion Pod | 1 day | 4h | 4h | 7 |
| Business Library | Language Learning Room | 1 day | 4h | 4h | 5 |
| Business Library | Study Room | 1 day | 4h | 4h | 14 |
| Business Library | PC - Bloomberg | 1 day | 2h | 8h | 4 |
| Business Library | PC / Single Monitor | day-of (23:59 night before) | 2h | 8h | 14 |
| Communication & Information Library | Single Monitor | day-of (23:59 night before) | 1h | 8h | 5 |
| Communication & Information Library | Discussion Pod | 1 day | 4h | 4h | 3 |
| Chinese Library | PC - Wind Financial Terminal | 1 day | 2h | 8h | 1 |

## Other rules the bot encodes

- Check-in window: 5 min before start until 15 min after.
- Cancelling *is* checking out: POST email + code to `/r/checkout`.
- The 45 minutes before someone else's booking show red, but you may
  END inside that buffer - it blocks starts, not bookings.
- No two back-to-back bookings of the same facility by one person.
- Reaching the checkout page holds a slot for ~5.5 min (measured);
  the page's own 'held until' text is unreliable in both directions.
- There is no 'my bookings' page: `/r` offers only Reserve, Check In
  and Check Out.
