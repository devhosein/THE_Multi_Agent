# Mission Control — complete handoff

Everything that happened from the first idea to the current state, why each decision was made, what is verified and
what is not, and what to build next. Written to continue the work in Claude Code without losing context.
(Short operational version: `CLAUDE.md`. Real-run failure analysis: `docs/FAILURES.md`. Event contract: `docs/EVENTS.md`.)

---

## 0. Where we stopped (2026-11-02, 00:10) — read this first when the user says "continue"

**Done and committed locally (not pushed; last pushed commit is `32dec13`):** single-agent baseline +
automatic site scorer (`eval/`), integration check (`integration.py`), live reports to the lead, fix tasks, final
check, evidence-over-claims, `check_project` tool, agents waiting for fixes they caused, `## Integration` in contracts.
278 unit tests green (+16 Node). Mission Control server stopped; no run is going.

**What the live runs showed** (details: `eval/results/README.md`, `docs/FAILURES.md` row 8):
- v1 (`live-runs/cafe-35b`): all tasks "verified", site broken (CORS between separately built parts). Scored
  **53/100** vs a single agent's **85/100** (`live-runs/cafe-solo-35b`, 3.9 min vs 13.1 min).
- v2: integration check + fix task + right lead decision, but the lead believed a fix agent's claim → run failed
  honestly (pages 404: leading `/` in `os.path.join`). Fixed: fix tasks judged by the running project, protected
  from the review, reviews get evidence.
- v3: same-origin site, agents used `check_project`; QA found a guarded selector mismatch the stdlib check cannot see;
  deadlock (fix waited for QA). Fixed: the reporting agent steps aside until the fix is done. Stopped by us.
- v4: lead handled contract conflicts well; the check missed pages served from a nested folder (fixed: every path
  suffix is tried) and confused the backend agent into a loop; three 35B model calls ran > 10 min (timeouts). Stopped
  by us before the end; not scored.

**Open:** (1) the 35B model sometimes "thinks" > 10 min per call (worker timeout 600 s, then retried) — try limiting
thinking (`OllamaClient(think=…)` / `num_predict` for workers) and measure quality; (2) an agent may build another
agent's part inside its own folder (owns `backend/**` allowed `backend/frontend/…`); (3) the stdlib check cannot run
JavaScript (guarded selector bugs need a browser; `eval/score_site.py` has one); (4) small UI items: approval
overlay covers the chat box in the narrow layout, no "message received" indicator, worker notes not shown in the UI;
(5) the lead sometimes classifies a clear instruction as a comment (no decision recorded).

**Exact next step:** decide with the user how to handle the long thinking (1), then a fresh live run
`live-runs/cafe-35b-v5` with the same prompt (`eval/tasks/cafe.prompt.txt` minus the palette line; send the palette
line as a chat message during approval), score it with
`python eval/score_site.py "<project>" --label multi-agent-v0.3 --browser msedge`, compare with 53 and 85 in
`eval/results/README.md`. Push to GitHub only when the user asks.

## 1. TL;DR

- **What**: a local multi-agent software builder. A *lead agent* turns a request into a validated plan, creates one
  specialist agent per task, gets the user's go-ahead (and per-agent model choices), runs the tasks in dependency
  order with sandboxed file tools, checks every result, rescues failures (retry with guidance / split the task / ask the
  user), revises the plan mid-run, and answers the user's messages at any moment. All of it is visible live in a
  graphical "operations room".
- **Stack**: Python ≥ 3.11 **stdlib only** (server, engine) + vanilla JS/CSS UI (no build step) + Ollama.
  Tests: unittest (249), Node reducer tests (16), Playwright e2e (120 checks); CI on Ubuntu + Windows.
- **State**: roadmap phases 0, 1, 2 done; parts of 3, 4, 6, 7 done; phase 5 (parallel execution) and real verification
  (tests run by the engine, reviewer agent) are the big open items. The latest batch (approval, rescue, decisions,
  logs/report, strength profiles, immediate replies, continuation) passes the unit tests on the user's Windows machine,
  but **has not had a Live run there yet** — do that first.

## 2. Vision and what makes it competitive

The user's brief (paraphrased): a "giant" multi-agent project for their résumé; a lead agent that creates the agents a
project needs (e.g. backend, frontend, SEO), decides what runs first and what can run in parallel, supervises quality
so the final result is flawless, shows every step live in a gorgeous UI, and lets the user chat with each agent and
with the lead.

Research done at the start (summary of published material reviewed in the chat):
- Orchestrator–worker systems can beat a single agent on parallelizable work (Anthropic reported +90.2% on its internal
  research eval) but cost far more tokens (~15× chat), and the authors warn that tightly coupled work (e.g. coding with
  shared context) is a poor fit.
- A large Google study (180 configurations) found multi-agent setups helped strongly on parallelizable tasks but
  **hurt on sequential ones** (−39% to −70%), with independent agents amplifying errors (reported 17.2×); centralized
  coordination contained errors best.
- Berkeley's MAST taxonomy: failures cluster in bad specifications, inter-agent misalignment and weak verification.
- Practitioners converged on planner → workers → judge (Cursor), clean-context reviewers and single-threaded writes
  (Cognition), agent teams with a shared task list and direct chat with teammates (Claude Code), artifact-based trust
  UIs (Google Antigravity), and RL-trained swarms (Kimi). Simple dashboards over CLI agents are a crowded, fragile market.

Conclusion that shaped the roadmap — the edge is **not** "having a lead agent" but:
1. **Contract first** for coupled work (the plan creates a `contracts/` task others depend on).
2. **Executable acceptance criteria** written before work starts (planned, see §9).
3. **Independent verification** with a clean context (planned).
4. **Shared decision ledger** (`.mission/decisions.md`) read by every agent (done).
5. **Honest parallelism**: only run in parallel what is truly independent (plan validation already forbids parallel
   tasks owning overlapping files; parallel execution itself is phase 5).
6. **Model routing / strength-aware scaffolding** (per-agent models + strength profiles: done).
7. **A transparent UI** (done) and **proof with numbers** — an eval harness vs a single agent (planned).

## 3. Roadmap and status

| Phase | Content | Status |
|---|---|---|
| 0 Foundations | event contract, plan model + validation, event log, Ollama client, planner, mock run | ✅ |
| 1 UI on mock data | start screen, operations room, cards on a "bus" loop, live plan, live file tree/viewer, chat drawer | ✅ (+ English/Persian, native folder dialog) |
| 2 Minimal real engine | lead plans, real worker writes files, sequential, everything shown live | ✅ |
| 3 Multi-agent sequential | roles, plan validation, **live plan edits by the lead**, git commit per task | ✅ except **git** |
| 4 User control | soft pause/stop at safe points, chat with any agent, decisions recorded | ✅ (pause of one agent = whole run, since execution is sequential) |
| 5 Parallel execution | concurrent tasks, isolation, concurrency cap, merge | ❌ next big item |
| 6 Quality layer | contract-first ✅, executable acceptance ❌, independent verifier ❌ (mechanical checks only), replan on failure ✅ (lead rescue) | partial |
| 7 Intelligence & proof | per-agent models ✅ (manual), strength profiles ✅, cost control ❌, **eval harness / benchmark** ❌ | partial |
| extra (from live tests) | approval gate, lead rescue, user decision panel, logs + downloadable report, retry failed tasks, immediate replies | ✅ |

## 4. History, phase by phase (what was built, what went wrong, what we learned)

**Phase 0 (core, stdlib only).** Event contract with validation and whole-stream checks (legal status transitions);
plan as a DAG with rules (unique ids, deps exist, no cycles, acceptance criteria required, `owns` paths safe, **two
tasks that can run concurrently may not own overlapping paths**); append-only JSONL log with torn-tail repair;
Ollama client (stdlib `urllib`); `generate_structured` = JSON schema + validation + feed exact errors back + retry.
Bugs found by tests: soft-pause semantics (`run.paused` must come after every agent stopped); a retry that dropped the
original prompt (caught by mutation testing). Smoke test on the user's machine: `ornith-1.5:9b` passed tool calling,
structured output, and produced valid plans 3/3 on the first try.

**Phase 1 (UI).** Visual language: a *technical drawing* (cool paper, ink lines, sharp title-block cards, status colours
only where they mean something); one bold gesture — a light pulse travelling along the bus loop from the lead to an
agent when work is handed out, green when it reports back. Server: stdlib HTTP + **SSE** (chosen over WebSocket:
`Last-Event-ID` = `after_seq`, gap-free reconnects, page reload rebuilds the room from the log). Bugs found by looking
at screenshots: the plan was nearly invisible (DOM rebuilt on every event restarted animations — fixed with
structure-signature patching); the whole room slid 418 px sideways (RTL overflow scrolled by focus — `overflow: clip`);
a scripted CSS edit silently duplicated a block (static asset tests added); spinner glyphs off-centre (grid placement).

**Phase 2 (real engine) + i18n + native folder dialog.** English default UI with Persian as a mirrored second language
(identical keys enforced); content-based text direction; quoted Windows paths accepted; native folder dialog via a
separate tkinter process with fallback to an in-app browser. Real engine: planner → agents → workers with tools →
mechanical checks → plan review after each task (the lead may add/remove/edit *pending* tasks; history is frozen).
Tested end to end against a scripted model, including over the real Ollama HTTP protocol.

**Live tests on Windows (see `docs/FAILURES.md`).** 9B: two failures (a tool call cut off mid-JSON; an agent looping
on `test.sh` because it cannot run programs). 35B: a CUDA crash and two cut-off tool calls. Root cause of the cut-offs:
**our own `num_ctx=16384`**. With the limit removed by the user, test 3 (35B) completed cleanly (largest prompt
14,666 tokens) — one attempt still hit the old 14-step cap.

**Post-test hardening (latest batch).**
- No context limit by default; optional setting in *Advanced*.
- Worker loop: transient errors retried with back-off (attempt not lost); cut-off calls recovered (compaction + "write
  in parts" + `append_file`); history compaction every step; loop detection (warn ×3, stop ×5); step cap only a safety net.
- Strength profiles (auto from Ollama's parameter size: <14B small, <40B medium, else large) changing attempts,
  rescues, plan size, read size, temperature and how detailed the worker briefing is.
- Approval gate after planning with a model per agent; the lead answers chat while you review and can revise the plan
  before work starts.
- Lead rescue of failed tasks and a user decision panel (reasons, lead's suggestions, optional note).
- Immediate replies to user messages in the addressed agent's voice; only instructions become decisions.
- Logs panel (filters, evidence per error) and a downloadable/copyable Markdown diagnostic report.
- "Retry failed tasks" continuation run that keeps verified work.
- Ollama error bodies unwrapped so logs show the real reason (e.g. "HTTP 500: CUDA error: …").

## 5. Architecture

### 5.1 Processes and data
- One local process: `scripts/serve.py` → `server.main()`, `ThreadingHTTPServer` on 127.0.0.1:8765.
- Per project folder: `inputs/` (copies of uploaded files), `contracts/` (written by agents),
  `.mission/plan.json`, `.mission/events.jsonl` (one per run; older runs rotated to `events.<stamp>.jsonl`),
  `.mission/decisions.md` (the ledger every agent reads).
- Security: localhost only, `Host` and `Origin` checked on every request (no DNS rebinding, no cross-site driving of the
  agents or of the folder dialog), path jail for every file operation, model text never rendered as HTML.

### 5.2 Event contract (summary; full table in `docs/EVENTS.md`)
Envelope `{v, seq, ts, run_id, type, agent_id?, task_id?, data}`. Types: run.started/finished/paused/resumed,
orchestrator.status, plan.created/updated, agent.created/removed/updated/status/message, task.status, file.created/
chunk/saved/deleted, decision.recorded, user.command, usage.updated, error, log, approval.requested/granted,
attention.required/resolved. Two-level ticks: `done` (agent says so) → `verified` (independent check).

### 5.3 Engine (`engine.py`)
`EngineSession(ws, prompt, llm, model=, lang=, config=EngineConfig(...), llm_factory=, carry=)`; public:
`start, stop, join, is_running, post_message, post_command, post_approval, post_resolution, can_continue,
continuation(old)`. Config: `strength, max_steps, max_attempts, recoveries, review, require_approval,
on_failure (ask|skip|stop), plan_retries, review_retries, chunk_delay_cap, backoff, context`.
Flow in `_run` → `_schedule` → `_execute(task)` → `_attempts` → `_diagnose` / `_ask_user` → `_review` / `_apply_update`.
Safe points: `_checkpoint` (stop, pause, answer the inbox) and `_wait` (approval, decisions, resume).
All diagnostics through `_log(level, source, message, detail, task_id, agent_id)`.

### 5.4 Worker (`worker.py`)
`run_worker(llm, toolbox, system, user, gate, deliver, reply, on_message, on_usage, log, limits, sleep)` →
`WorkerResult(ok, summary, error, kind)`; kinds: loop, step_cap, model_fatal, model_unavailable, truncated, no_work.
`call_model` (transient retries), `compact` (written content → notes, old reads shortened), loop signatures.

### 5.5 Lead (`orchestrator.py`)
`build_worker_system(task, plan, ws, decisions, profile)` (rules learned from real runs; worked example for weaker
models), `build_worker_user`, `review_plan` (keep/update, validated by `validate_replan`), `diagnose_failure`
(retry/split/ask_user; rule-based for infrastructure failures; `SUGGESTIONS` per failure kind), `answer_user`
(`{reply, is_instruction, instruction}`), `lead_context`.

### 5.6 Tools (`tools.py`)
`list_files, read_file(path, start_line) [paged], write_file, append_file, note_to_lead, finish`; ownership enforced
with `plan.path_allowed` (glob where `*` stays in one folder, `**` crosses folders); identical rewrite → "no change".

### 5.7 Profiles (`profiles.py`)
small: 3 attempts, 2 rescues, 3–6 tasks, reads 12k chars, temperature 0.15, detailed briefing.
medium: 2/1, 3–8, 20k, 0.2, detailed. large: 2/1, 3–10, 30k, 0.3, terse. `max_steps` 80–100 (safety net only).

### 5.8 Server endpoints
GET `/`, static files, `/api/status`, `/api/events` (SSE), `/api/fs`, `/api/models` (with sizes + strengths),
`/api/report` (Markdown download). POST `/api/run` (mode live|demo, model, ollama_host, lang, strength, num_ctx,
require_approval, on_failure, review, speed, batch), `/api/upload`, `/api/message`, `/api/command`, `/api/approve`,
`/api/resolve`, `/api/retry`, `/api/pick-folder`, `/api/reset`. Errors are `{error, code, params}`; the UI translates codes.

### 5.9 UI (`ui/`)
`state.js` pure reducer (+ `planGroups`, `fileTree`, `agentDisplayStatus`, `strengthOf`), `app.js` rendering (no
framework), `i18n.js` (en/fa), `app.css`. Layout: plan sidebar, stage with the lead block on top and agent cards placed
along a rectangular loop around the file explorer (collision-checked for 1–8 agents; flow layout under 900×500), dock
for chatting with the lead, a drawer for an agent (task, files, chat) or for Logs, and a *gate* overlay for approval and
decisions. Rendering rules in `CLAUDE.md` §Rules 5–8.

## 6. Testing

| Suite | Command | Count | Notes |
|---|---|---|---|
| Python | `python -m unittest discover -s tests -t .` | 249 | engine via `ScriptedLLM`, engine over the real Ollama HTTP protocol (`FakeOllama`), server on a real socket, SSE, security, paths, report, i18n/asset checks, repo hygiene |
| Node | `node --test tests/ui_state.test.mjs` | 16 | reducer on the recorded demo run |
| Browser | `python tests/e2e_ui.py` | 120 | layout 1–8 agents × 4 window sizes × 2 languages, live run, approval, replan, failure decision, logs, report, retry, language switch, RTL |

Discipline that paid off: mutation testing (inject realistic bugs, confirm a test fails — more than 30 so far; every
survivor became a new test), looking at screenshots for every UI change, and checking whether a test that passed on
the first run actually measures anything.

## 7. Known limits and unverified parts
- **On Windows** (2026-11-01): unit + reducer tests pass after fixing 4 Windows-only problems (CRLF in a test, a
  cp1252 subprocess decode, symlinks needing admin rights — now also tested with junctions — and a connection reset
  when the server refused a POST without reading its body). e2e passes there with the installed Edge
  (`--browser msedge`; Playwright's Chromium download is geo-blocked); it found that Persian text in the file viewer
  fell back to a monospace font (Courier New) that splits the letters — fixed with a Persian-only `@font-face`. Not
  yet run there: a Live run, the native folder dialog (tkinter is present).
- Execution is sequential; "verified" means mechanical checks only (files exist, non-empty, .py/.json/.html/.css parse).
- Agents cannot run code. Integration problems across tasks (e.g. a stylesheet not linked from a file another task
  owns) depend on the agent calling `note_to_lead` and the lead's review acting on it.
- Each user message costs one extra model call (the reply). The plan review after each task costs one call (can be
  turned off).
- Ollama's own default context may be small depending on its version/settings; if cut-offs reappear, raise the
  context in Ollama or set *Context* in Advanced. The report shows the setting used.
- Single session per server, no authentication (localhost only by design). Web fonts load from Google Fonts when online.

## 8. First steps in Claude Code
1. ~~Run the three test suites on Windows; fix anything platform-specific.~~ Done (e2e with `--browser msedge`).
2. `python scripts/serve.py` → Live run with `ornith-1.5:9b` and with `ornith-1.5:35b`, approval on, then try: change a
   model during approval, message an agent while it works, force a failure (ask for something impossible), use the
   decision panel, open Logs, **Download report**.
3. Check the native folder dialog (Browse…).
4. Read the report of each run; compare with `docs/FAILURES.md`.

## 9. Next steps (suggested order)
1. **Real verification (phase 6)** — the biggest quality lever.
   - The engine (not the model) runs safe checks after a task in the project folder: `node --check`, `python -m
     py_compile`, and, when present, the project's own tests (`npm test`, `pytest`) with a timeout, an allow-list of
     commands, and output captured into the log. Failures go back to the worker as feedback.
   - The planner writes **executable acceptance criteria** (commands or small test files) per task; "verified" = they pass.
   - An **independent reviewer agent** with a clean context reads the diff and the acceptance criteria and returns
     pass/fail with reasons (Cognition's lesson: a clean-context reviewer catches real bugs).
2. **Parallel execution (phase 5)**: run ready tasks whose `owns` are disjoint concurrently (already guaranteed by plan
   validation); a worker thread per task, a concurrency cap, Ollama's `OLLAMA_NUM_PARALLEL`; soft pause across agents
   is already specified (`run.paused` after every agent paused) and demonstrated by the demo; the UI already handles
   several running agents and parallel waves.
3. **Git integration**: init a repo, commit per verified task, revert a task, show diffs in the viewer.
4. **Eval harness (phase 7)**: 10–15 standard tasks (landing page, todo API, CLI tool…), single-agent baseline vs
   Mission Control, metrics (checks passed, time, tokens), a generated report — the résumé "proof with numbers".
5. Automatic model routing by role and strength; time/token budgets per run.
6. UX: run history and replay of past runs (the event log already supports it), diff view, dark theme.
7. Packaging: `pyproject` entry point (`mission-control` command), optional single executable; FastAPI migration only
   if the stdlib server becomes limiting.

## 10. Glossary
- **lead / orchestrator** — the agent that plans, briefs, reviews, rescues and answers; `agent_id = "orchestrator"`.
- **owns** — paths a task alone may write; enforced at write time.
- **contract** — shared interface written first under `contracts/`.
- **decision ledger** — `.mission/decisions.md`, instructions every agent must follow.
- **safe point** — where pause/stop/messages take effect (before a model call, between tool calls).
- **rescue** — the lead's own attempt to fix a failed task before asking the user.
- **continuation** — a new run that keeps verified tasks and redoes the rest ("Retry failed tasks").
- **strength profile** — small/medium/large scaffolding level chosen from the model size (or by the user).
