# Mission Control — working notes for Claude Code

Read this first, then `docs/HANDOFF.md` for the full history, decisions and next steps.

## The user and how to work with them
- The user writes in **Persian (Farsi)**: answer in Persian. Keep code, comments, docs, commit messages and the default
  UI language in **English** (the project must look international; Persian is the second UI language).
- Goal: the **flagship project of the user's résumé** — a multi-agent orchestrator that is genuinely competitive:
  a beautiful, smooth live UI and an engine that shines with strong models and still works with weak local ones.
- The user tests on **Windows (PowerShell)** with **Ollama** (`ornith-1.5:9b`, `ornith-1.5:35b`). Project folder:
  `C:\D Drive\Programing\THE_Multi_Agent\main` (git: github.com/devhosein/THE_Multi_Agent). The sibling folders
  `v1`–`v3` are old hand-made snapshots: never edit them. Paths contain spaces.
- The user's network: some downloads are blocked (pip's file host without VPN; Playwright's Chromium CDN answers 403
  "not available in your location"). Give the user the install commands; they run them. Playwright is installed in the
  user's system Python; e2e runs with the installed Edge: `python tests/e2e_ui.py --browser msedge`.
- What worked well so far: be honest about what is tested and what is not; show evidence (test output, screenshots,
  log excerpts); find root causes before fixing (see `docs/FAILURES.md`); never weaken the code for a weak model —
  add scaffolding instead (strength profiles).

## Commands
```powershell
python scripts/serve.py                         # the app: http://127.0.0.1:8765 (Live = Ollama, Demo = recorded run)
python -m unittest discover -s tests -t .       # 249 tests, ~40 s, stdlib only
node --test tests/ui_state.test.mjs             # UI reducer (Node >= 18)
python tests/e2e_ui.py                          # real browser, 120 checks (+ --browser msedge on this machine)
python scripts/run_engine.py --project C:\tmp\x --prompt "..." [--model ornith-1.5:35b] [--approve-plan] [--strength small]
python scripts/smoke_llm.py --model ornith-1.5:9b   # can this model do structured output, tools and planning?
python scripts/replay.py path\to\.mission\events.jsonl --speed 0 --no-chunks   # read a run in the terminal
python scripts/gen_mock.py [--lang=fa]          # regenerate sample/mock_run.jsonl (the Node tests read it)
```
Python ≥ 3.11, **no pip dependencies** for the app (stdlib only, on purpose). Playwright only for e2e.

## Architecture in one screen
```
browser UI (ui/*.js)  ◄── SSE /api/events (Last-Event-ID = after_seq) ──  server.py ── session ── EventLog (.mission/events.jsonl)
      └── POST /api/run|message|command|approve|resolve|retry|upload|pick-folder|reset,  GET /api/report|models|fs|status
session = EngineSession (engine.py, live) | MockSession (session.py, demo replay of mock.py)
engine: planner.py → plan.py (validated DAG) → per task: worker.py loop + tools.py (sandboxed) → checks.py
        → integration.py (runs the whole project like a browser) → orchestrator.py (lead: briefing, plan review,
        reports from agents, failure diagnosis, answering the user) → profiles.py (model strength)
```
- **The UI is a pure function of the event stream** (`ui/state.js` reducer, also tested in Node). Demo and engine emit
  the same events. Contract: `src/mission_control/events.py` (+ `docs/EVENTS.md`). `check_stream()` validates whole runs.
- A run: plan → agents → **approval gate** (per-agent model choice) → tasks in dependency order (sequential) → worker
  attempts → **lead rescue** (retry with guidance / split) → **user decision** (retry, retry with note, skip, stop)
  → plan review after each task → `run.finished`. "Retry failed tasks" = `EngineSession.continuation(old)`.
- User messages are answered at the next safe point by the addressed agent (`orchestrator.answer_user`); only
  instructions become decisions in `.mission/decisions.md` (shared with all later agents).
- **The lead acts like a project manager during the work**: an agent's `note_to_lead` is handled at its next step
  (`orchestrator.handle_note`: reply to the agent, shared decision, or an immediate plan review). After every task
  `integration.py` runs the project; problems in the agent's own files are retried by it, problems between agents
  become a `fixN` task right after the current one (later pending tasks then depend on it); a final check runs
  after the last task, and the run is `failed` if problems remain. Measure changes with `eval/` (see
  `eval/results/README.md`: baseline 53/100 vs a single agent's 85).

## Rules (keep them; tests enforce most)
1. **New event type** → `T` + `SPEC` in `events.py`, `docs/EVENTS.md`, reducer in `ui/state.js`, `textview.py`,
   report if relevant, tests. Emit through the session's `_emit` (drops events after the run ended).
2. **Safety lives in code, not prompts**: path jail (`workspace.resolve_inside`), write ownership (`plan.path_allowed`),
   loop detection, error classification. Tools return `ERROR: …` to the model instead of raising.
3. **Never send `num_ctx` by default** (a too-small context was the root cause of the cut-off tool calls). It is an
   optional user setting only. Profiles never set it.
4. Model/user text goes into the DOM with **`textContent` only**, never `innerHTML`.
5. **`app.js` contains no human-language text**: everything via `I18N.t()` from `ui/i18n.js`; `en` and `fa` must have
   identical keys and placeholders; every server error `code` has an `err.<code>` string (tests check all of it).
6. Text direction of user/model text: `textDir()` (any RTL letter ⇒ rtl). **Not `dir="auto"`** (breaks Persian
   sentences that start with "API …").
7. Rendering: rebuild DOM only when a **structure signature** changes (`patch()`), update fields in place (`setText`),
   animate only on a real state change. Re-attaching nodes restarts CSS animations and swallows clicks.
8. Room containers use `overflow: clip` (in RTL, hidden overflow was still scrolled by focus/scrollIntoView).
9. Every behaviour change gets a test; UI changes also get an e2e check **and a screenshot you actually look at**
   (layout bugs here were invisible to DOM assertions). For critical logic, inject a bug on purpose and confirm a
   test fails (this found 4 weak tests so far).
10. Windows: write files as bytes (no CRLF translation), `EventLog` opens with `newline="\n"`, paths may contain spaces
    and quotes from "Copy as path" (`paths.clean_path_input`). Subprocess output: always `encoding="utf-8"`. Scripts
    call `utf8_console()` (redirected output is cp1252 on Windows). A POST answered before its body is read must drain
    the body (`Handler._drain`), or Windows resets the connection and the client never sees the answer.
11. CI (`.github/workflows/ci.yml`) runs the unit and reducer tests on Ubuntu + Windows and e2e on Ubuntu: keep it green.

## Where things are
`src/mission_control/`: `engine.py` run orchestration · `orchestrator.py` lead prompts/review/diagnosis/replies ·
`worker.py` agent loop, retries, compaction, loop detection · `tools.py` list/read/write/append/note/finish ·
`checks.py` mechanical verification · `planner.py` · `plan.py` DAG, ownership, replan rules · `profiles.py` strength ·
`llm.py` Ollama client + error classes · `events.py` · `eventlog.py` · `session.py` demo player · `mock.py` demo script
(en/fa) · `server.py` HTTP · `report.py` diagnostic report · `paths.py` · `folder_picker.py` native dialog (tkinter,
separate process) · `textview.py` · `ui/` (`index.html`, `app.js`, `state.js`, `i18n.js`, `app.css`).
`tests/helpers.py` has `ScriptedLLM` / `OllamaStandIn`: a scripted model to test the engine without Ollama.

## Current status
Phases 0–2 done plus the post-live-test hardening (approval, rescue, decisions, logs/report, strength, replies,
continuation, i18n, native picker). **Unit and reducer tests pass on the user's Windows machine** (2026-11-01, after
fixing 4 Windows-only problems). **e2e passes there with Edge** (120 checks).
**Still not verified on Windows**: a Live run with each model, and the native folder dialog. Next: Live run + report, then
`docs/HANDOFF.md` §9 (real verification first).
