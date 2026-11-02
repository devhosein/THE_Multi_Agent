# Eval task: task board

Written **before** any system was run on it (2026-11-02), as a second task the engine was not developed against: the
cafe task shaped the engine's checks for a whole day, so a result on the cafe alone could be over-fitted to it.

## Prompt (identical for every system)

> Build a small task board web app. One page lists the tasks loaded from a small JSON API; a form adds a task with a
> title and a priority (low, medium or high); each task can be marked as done and can be deleted. Backend: Python
> standard library only (http.server), storing the tasks in a JSON file. Frontend: plain HTML, CSS and JavaScript,
> responsive, clean and modern design.

Plus one instruction, given to every system: *"Use a calm palette of slate blue and white with a teal accent. Keep all
page text in English."* (Mission Control receives it as a chat message during plan approval; the single agent receives
it appended to the prompt.)

Model for every system: `ornith-1.5:35b`, same machine, same tools.

## How the app is opened (fixed procedure, `eval/score_taskboard.py`)

Like the cafe: the backend under `backend/` (or the project root) that runs `http.server` is started with `PORT=8000`;
if it answers `GET /` with HTML the app is opened from it, otherwise a plain static server serves the project root.
A real browser (Playwright, Edge) opens the page and uses it like a person: adds a task with a unique title, marks it
done, deletes it. Every change the scorer makes is undone afterwards.

## Rubric (100 points)

**Works (70)** — measured automatically:

| id | check | pts |
|---|---|---|
| F1 | the backend starts and listens; only standard-library imports | 10 |
| F2 | the task list endpoint the page calls (default `GET /api/tasks`) answers 200 with JSON (a list, or an object holding one) | 10 |
| F3 | the page loads in the browser and has a form with a text field and a way to submit it | 10 |
| F4 | adding a task through the form: it appears on the page (8) and is stored in a JSON file (7) | 15 |
| F5 | through the page, marking that task done changes its stored state (7); deleting it removes it from the page (4) and from the JSON file (4) | 15 |
| F6 | the API rejects an empty task (`POST` with `{}`) with a 4xx status | 5 |
| F7 | no JavaScript errors and no failed requests while doing all of the above | 5 |

**Quality (30)**: Q1 no sideways scroll at 375 px (5) · Q2 the project's own tests pass, if any (5) · Q3 a run command
is documented (5) · Q4 design, reviewer, 0–15 (palette as instructed, modern, consistent, readable, responsive).
