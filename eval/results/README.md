# Results

## 2026-11-01 — baseline, v0.2: Mission Control vs a single agent (cafe task)

Task and rubric: `eval/tasks/cafe.md` (written before the single-agent run). Model `ornith-1.5:35b` for both, same
machine, same tools. Scored with `python eval/score_site.py <project> --label <label> --browser msedge`.
One run each — a first data point, not a statistic.

| | Mission Control (multi-agent) | Single agent |
|---|---|---|
| F1 backend starts, stdlib only (10) | 10 | 10 |
| F2 menu API returns items (10) | 10 (10 items) | 10 (22 items) |
| F3 landing: story + opening hours (10) | 10 | 10 |
| F4 menu page shows the items in a browser (15) | **0** — blocked by CORS | 15 (22/22) |
| F5 booking through the form is stored + confirmed (15) | **0** — blocked by CORS | 15 |
| F6 invalid booking rejected (5) | 5 (400) | 5 (422) |
| F7 no JS errors / failed requests (5) | 0 (CORS, 2 null-element errors) | 0 (one missing image) |
| Q1 no sideways scroll at 375 px (5) | 5 | 5 |
| Q2 its own tests pass (5) | 0 (its test fails) | 0 (wrote none) |
| Q3 documents how to run it (5) | 0 | 5 (README) |
| Q4 design, reviewer (15) | 13 | 10 |
| **Total (100)** | **53** | **85** |
| time | 13.1 min | 3.9 min |
| tokens in / out | 457,680 / 97,502 | 315,418 / 32,332 |
| files / lines | 12 / 2,356 | 8 / 2,016 |

Design (Q4, reviewer): the multi-agent site is the more polished one (consistent cards, hours as cards, feature
section, clean mobile layout; 13). The single-agent site is good (striped hero, a strong menu page with category
filters) but has an empty image placeholder, unstyled bullet list, a reservation form that looks plain and a footer
whose columns collapse into one centred stack (10).

### What it says
- On this small, tightly coupled task the single agent wins clearly: **85 vs 53**, 3.4x faster, 3x fewer output tokens.
  This matches the published research the roadmap is built on: splitting coupled work across agents costs
  coordination and creates integration bugs.
- Mission Control lost almost all its points on **integration**, not on the quality of each part: the frontend (port
  8001) calls the API on port 8000, the backend sends no CORS header and does not serve the pages. Every task was
  "verified" because verification is mechanical (files exist and parse). The QA agent even noticed that the backend
  does not serve the pages, but it was the last task, so no review followed and nothing was fixed.
- One agent writing both sides simply made the backend serve the pages (same origin, no CORS problem).

### What to build from it
1. Real verification (phase 6): run the project the way this scorer does (start the server, load the pages, read the
   console) and send failures back to the responsible agent. The scorer is a ready prototype of that verifier.
2. A final integration review after the last task (currently skipped when nothing is pending).
3. Let the lead choose the team size: a small coupled request should get one agent, or a contract that also fixes
   integration details (same origin / CORS, who serves the pages).
4. Repeat each system 3 times per task before claiming a difference, and add more tasks (phase 7).
