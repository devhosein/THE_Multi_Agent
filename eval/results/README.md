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

### Follow-up live runs (same prompt, same model), 2026-11-01/02

Each run exposed the next weak point; each fix has a test that fails without it.

| run | what happened | what changed |
|---|---|---|
| v2 | The contract now had an `## Integration` section (backend serves the pages). The lead caught a wrong start command (`npm start`) by itself and added a contract fix. The integration check found the seam after the frontend and added `fix1`; the lead recorded the right decision. But the lead's review narrowed `fix1`'s files, `fix1` passed although every page still returned 404 (a leading `/` made `os.path.join` drop `public/`), and the lead deleted two new fix tasks as "redundant", trusting the agent's claim. Run ended **failed**, honestly. | A fix task passes only when its own problems are gone; the review may not remove/change a pending fix task; reviews get the latest check as *evidence*; agents get `check_project`; problem messages say what the server answered. |
| v3 | Agents used `check_project` before finishing; the site was same-origin and "fit together". QA found a bug the stdlib check cannot see (`app.js` looked up `#menu-list`, the page had `#menu-container`, guarded by `if`) and told the lead at once; the lead added a frontend fix task. Deadlock: tasks run one after another, the fix waited for QA, QA kept reporting "not fixed". Stopped. | A running agent whose report made the lead add work steps aside (paused, back in the queue behind the fix) and is told what changed when it resumes. |
| v4 | The backend agent reported contract conflicts at once; the lead recorded a decision and added a contract fix *after* the backend (correctly not pausing it: that would be a cycle). Then the backend agent built pages inside `backend/frontend/`; the check looked for them only one folder up and reported false problems, which confused it into a loop. The 35B model also had three model calls of more than 10 minutes (timeouts, retried). Stopped. | The check tries every path suffix to find pages served from a nested folder. Open: very long thinking of the 35B model; an agent doing another agent's work inside its own folder. |

### 2026-11-02 — v5 to v9: the parts fit, then the browser check

Same prompt, same model, same procedure; the palette line sent in the chat during approval. Design (Q4) scored by
the reviewer from the screenshots, as before.

| | v5 (`multi-agent-v0.3`) | v6 (`multi-agent-v0.4`) | v7 (`multi-agent-v0.5`) | v8 (`multi-agent-v0.6`) | v9 (`multi-agent-v0.7`) | single agent |
|---|---|---|---|---|---|---|
| F1–F3 backend, API, landing (30) | 30 | 30 | 30 | 30 | 25 (no story) | 30 |
| F4 menu in the browser (15) | 15 (12/12) | **0** (0/11) | 15 (8/8) | 15 (17/17) | 15 (10/10) | 15 |
| F5 booking stored + confirmed (15) | 15 | 8 (no confirmation) | 15 | 15 | 15 | 15 |
| F6 invalid booking rejected (5) | 5 | 5 | 5 | 5 | 5 | 5 |
| F7 no JS errors / failed requests (5) | 0 (`/favicon.ico` 404) | 0 (ReferenceError) | **5** | **5** | **5** | 0 |
| Q1 / Q2 / Q3 (15) | 5 / 0 / 5 | 5 / 0 / 5 | 5 / 0 / 5 | 5 / 0 / 5 | 5 / 0 / 5 | 5 / 0 / 5 |
| Q4 design (15) | 10 | not reviewed | 11 | 7 | 13 | 10 |
| **Total (100)** | **85** | **≤ 68** | **91** | **87** | **88** | **85** |
| time (approval → done) | 4.5 min | 5.5 min | 15.5 min | 13.5 min | 7 min | 3.9 min |

- **v5** — first run with each agent seeing who builds what (the backend agent said so and left the pages to the
  frontend agent), a capped output (no runaway answers) and a fixed approval panel. Everything worked; it tied the
  single agent. Lost points: the reservation form was unstyled (the CSS styled `.form-group`, the HTML used `form-row`,
  same agent: compaction had dropped its own CSS from its history) and the browser's `/favicon.ico` request failed.
  Design 10: excellent landing page, unstyled form, label/value text glued together in the confirmation.
- **v6** — the stdlib integration check passed and the QA agent "verified" by reading code, but the menu page was
  empty (the server wrapped the menu twice) and the form threw `ReferenceError` on submit. Not visible without running
  the JavaScript. (Also: the lead twice failed to record the palette instruction in aborted attempts, fixed first.)
- **v7** — with the browser check the frontend agent saw 12 problems, then 4, then none, fixing each (including a
  confirmation that was hidden by CSS). Zero JS errors, every functional point. Design 11: polished typography, a
  beautiful reservation form and confirmation; but "Loading menu…" stays visible after loading, name and price run
  together, unstyled selects, and abstract placeholder shapes instead of imagery. Slower: the frontend agent spent
  ~12 minutes on fixes, and four rewrites of a 1,200-line stylesheet were caused by the compaction-note bug (now fixed).

- **v8** — every functional point again (17/17 items, booking confirmed, no errors). Two new failures, both found
  and fixed after the run: (1) the frontend agent kept writing the compaction note as file content; the new guard
  refused every attempt, but the agent looped on it (two failed attempts, then the lead's rescue worked); fix: the
  history no longer shows a note inside the call at all (only the path; the note is in the tool result). (2) Design 7:
  white headings and footer text on a cream page on every page, because the dark backgrounds used `var(--coffee-800)`,
  which was never defined; fix: the stdlib check reports undefined CSS variables and the browser check reports text
  whose colour (almost) equals the colour behind it. Otherwise good: a clean menu list, a well-made form.

- **v9** — the imitation loop is gone (one call without content, refused with the explanation, then the agent went
  on); 7 minutes. Every page works with no errors; the cleanest design so far (13: consistent cards, a three-column
  menu with filters, a confirmation with the booking details; the feature cards touch the window edges). But the
  landing page has no story, only a tagline and feature cards: a requirement nobody checked (the checks test that
  things work, not that every requested piece of content is there).

**Since the browser check (v7–v9): 91, 87, 88** — every run above the single agent's 85, with every functional point
and no JavaScript errors. The remaining gap is quality and time, not integration: no tests (Q2), requested content
not verified, and 7–15 minutes against 4.

**Caveats, honestly:** one run per version, and runs vary a lot (v5 85, v6 ≤ 68, v7 91): a claim needs several runs
(phase 7). The engine's browser check now works like this scorer (load the pages, use the form), so this task no
longer measures independently what the engine checks: new tasks the engine was not developed against are needed to
show the gain is general. The single agent got no browser check; giving it one would be the fair next comparison.

## 2026-11-02 — benchmark: three set-ups, two tasks (stopped early)

A second task the engine was not developed against (`eval/tasks/taskboard.md`, rubric and scorer written and
committed before its first run) and a fair single agent: `scripts/solo_agent.py --check` gives it Mission Control's
project check. Runner: `eval/run_matrix.py`; charts: `eval/make_report.py` (it lists exactly which runs count).

The plan was 3 runs × 3 set-ups × 2 tasks. It was stopped after about four hours with fewer runs, because the
benchmark kept finding bugs **in the check itself**, and each fix restarted the checked set-ups (honest record):

| folder | code | what happened |
|---|---|---|
| `bench-pilot` | `72ac24d` | Mission Control's navigation linked to `/menu`, the pages were `/menu.html`: the check never clicked links → link check added (`584d70e`) |
| `bench-pilot2` | `584d70e` | a single agent hard-coded the temporary port the check reported (15/85) → the port is no longer shown (`8b60c01`); a second runner started by another session corrupted one run |
| `bench-pilot3` | `8b60c01` | ids in URLs (`/api/tasks/${id}`) reported as missing routes, an empty API answer while the data file was full, an icon link in `<body>` → fixed (`ff38b84`) |
| `bench` | `ff38b84` | plain single agent 4 runs (its code never changed, so those runs were kept), single agent + checks 2, Mission Control 1 |

All runs with the browser check in place (scores out of 85, automatic):

| set-up | runs | scores | mean | every feature works | no JS errors | minutes (mean) |
|---|---|---|---|---|---|---|
| single agent | 6 | 70 75 53 47 75 40 | **60** | 3/6 | 1/6 | 3 |
| single agent + our checks | 6 | 75 75 70 63 68 75 | **71** | 4/6 | 4/6 | 9 |
| Mission Control | 7 | 75 78 80 50 80 80 75 | **74** | 5/7 | 6/7 | 12 |

What it says: the verification layer is what makes the result dependable (the worst run goes from 40 to 63, JS-error-
free runs from 1/6 to 4/6); with the same checks, Mission Control's lead and team add little on apps this small
(+3 on average, one bad run of 50 caused by a data bug the check did not yet catch) and cost time. Small samples,
mixed code versions (listed above): read it as a direction, not a statistic. Next measurements worth making: a larger
task, a strong API model as the lead with local workers (cost in dollars), and a reviewer agent.

### What to build from it
1. Real verification (phase 6): run the project the way this scorer does (start the server, load the pages, read the
   console) and send failures back to the responsible agent. The scorer is a ready prototype of that verifier.
2. A final integration review after the last task (currently skipped when nothing is pending).
3. Let the lead choose the team size: a small coupled request should get one agent, or a contract that also fixes
   integration details (same origin / CORS, who serves the pages).
4. Repeat each system 3 times per task before claiming a difference, and add more tasks (phase 7).
