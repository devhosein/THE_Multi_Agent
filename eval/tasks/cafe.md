# Eval task: cafe website

Written **before** the single-agent baseline was run (2026-11-01), so the result could not shape the rubric.

## Prompt (identical for every system)

> Build a small cafe website. A landing page with the cafe's story and opening hours, a menu page that loads the menu
> from a small JSON API, and a table-reservation form that sends reservations to the API. Backend: Python standard
> library only (http.server), storing reservations in a JSON file. Frontend: plain HTML, CSS and JavaScript,
> responsive, warm and modern design.

Plus one instruction, given to every system: *"Use a warm color palette: coffee brown, cream and a soft orange accent.
Keep all page text in English."* (Mission Control received it as a chat message during plan approval; the single
agent receives it appended to the prompt. Same information, different channel.)

Model for every system: `ornith-1.5:35b` on the same machine. Agents cannot run programs (same tools for everyone).

## How the site is opened (fixed procedure, `eval/score_site.py`)

1. Start the backend the obvious way: the `.py` file under `backend/` (or the project root) that has a
   `__main__` entry and imports `http.server`, run from the project root with `PORT=8000`.
2. If the backend answers `GET /` with HTML, the site is opened from the backend. Otherwise the pages are served by a
   plain static file server on the project root (what a developer would do with "Live Server"), port 8001.
3. A real browser (Chromium/Edge through Playwright) opens the landing page, follows its links to the menu and the
   reservation page, and fills in the reservation form like a user.

## Rubric (100 points)

**Works (70)** — measured automatically, no judgement involved:

| id | check | pts |
|---|---|---|
| F1 | the backend starts and listens; only standard-library imports | 10 |
| F2 | `GET /api/menu` (or the menu endpoint the frontend calls) returns JSON with at least 3 items that have a name and a price | 10 |
| F3 | the landing page loads and mentions the story and the opening hours | 10 |
| F4 | in the browser, the menu page shows at least 80 % of the item names the API returns | 15 |
| F5 | filling in and submitting the reservation form in the browser stores a reservation in a JSON file (8) and shows a visible confirmation (7) | 15 |
| F6 | the API rejects an invalid reservation (empty body) with a 4xx status | 5 |
| F7 | no JavaScript errors and no failed requests on the three pages | 5 |

**Quality (30)**:

| id | check | pts |
|---|---|---|
| Q1 | at 375 px width no page scrolls sideways (automatic) | 5 |
| Q2 | the project's own tests, if it wrote any, pass when run as documented (automatic; no tests = 0) | 5 |
| Q3 | it says how to start it (README or a usage docstring/comment in the server) (automatic: a run command is present) | 5 |
| Q4 | design: warm palette as instructed, modern, consistent, readable, responsive (reviewer, 0-15, from desktop + mobile screenshots) | 15 |

Not scored, reported next to the score: wall-clock time, tokens in/out, number of model calls, files and lines.
