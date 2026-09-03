# Audit folder

Everything in here is **documentation only - no code was changed to produce
it**. It exists so you can see the shape of the project and its weak points in
one place, and decide what to fix.

| File | What it is |
|---|---|
| `01-structure.md` | Top to bottom: the two products, the layers, feature → file → function, the concepts, and the lifecycle of each kind of request |
| `02-findings.md` | 17 errors and logical flaws, each with how it was verified, plus a short note on what is genuinely solid |
| `03-site-data.md` | The measured facts about libcalendar.ntu.edu.sg: hours, notice periods, caps and spaces for all 21 categories |

Findings are graded **H** (breaks or silently misbehaves), **M** (wrong in some
cases), **L** (maintenance risk). The two worth reading first are **H1** (the
catalogue expires after 14 days and then reports wrong booking windows, with no
refresh path) and **H2** (holds are lost silently when the bot restarts).
