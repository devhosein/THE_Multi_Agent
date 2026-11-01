# Event contract (v1)

The engine and the UI are connected by exactly one thing: an **event stream**. Everything on screen (card colours,
ticks, files typing themselves, the plan) is a function of these events; the UI has no hidden logic.
The demo player and the real engine emit the same stream, so the UI cannot tell them apart.
Source of truth and validation: `src/mission_control/events.py`.

## Envelope

```json
{"v":1, "seq":42, "ts":"2026-10-27T09:00:12.920+00:00", "run_id":"run_001",
 "type":"task.status", "agent_id":"a_backend", "task_id":"t2", "data":{"status":"running"}}
```

| field | meaning |
|---|---|
| `seq` | gap-free sequence from 1. A reconnecting UI asks for "everything after seq N" (SSE `Last-Event-ID`) |
| `ts` | ISO-8601 UTC; replay uses the gaps between timestamps |
| `agent_id` | for agent events. The reserved id `orchestrator` is the lead agent |
| `task_id` | required for `task.status` |

## Events

| type | `data` (required) | `agent_id` | meaning |
|---|---|---|---|
| `run.started` | `project_path`, `prompt` (+`inputs`, `mock`, `model`, `settings`, `resumed`) | | a run begins (`resumed`: "Retry failed tasks") |
| `run.finished` | `status`: completed / failed / stopped (+`summary`) | | the end |
| `run.paused` / `run.resumed` | — | | **every** agent has stopped / work continues |
| `orchestrator.status` | `text` | | the lead agent's one-line status |
| `plan.created` | `version`, `tasks` (+`stages`) | | first plan |
| `plan.updated` | `version`, `reason`, `tasks` (+`stages`, `added`, `removed`, `changed`) | | the lead changed the plan mid-run (always a full snapshot) |
| `agent.created` | `role`, `model`, `title`, `task_ids` | ✔ | a card appears (initial status `queued`) |
| `agent.removed` | — | ✔ | the lead dropped this agent's task; the card leaves |
| `agent.status` | `status`: queued / running / paused / done / error | ✔ | card colour |
| `agent.message` | `role`: user / agent, `text` | ✔ | chat with that agent (lead: `agent_id=orchestrator`) |
| `task.status` | `status`: pending / running / done / verified / failed / skipped (+`carried`) | task_id | plan ticks (`carried`: replayed from the previous run, no animation) |
| `file.created` | `path` (+`rewrite:true`) | ✔ | the file appears in the tree; `rewrite` = content restarts |
| `file.chunk` | `path`, `text` | ✔ | appended content (live typing) |
| `file.saved` | `path`, `size` | ✔ | writing finished (`size` in bytes) |
| `file.deleted` | `path` | ✔ | removed |
| `decision.recorded` | `id`, `text`, `source`: user / orchestrator / agent | | entry in the shared decision ledger |
| `user.command` | `command`: pause / resume / stop, `target`: all / orchestrator / agent_id | | records a user **request** |
| `usage.updated` | `tokens_in`, `tokens_out` (cumulative) (+`cost_usd`) | | token usage |
| `error` | `message` | optional | something went wrong (task failures carry `task_id`) |
| `log` | `level`: debug / info / warn / error, `source`, `message` (+`detail`) | optional | diagnostics for the Logs panel and the report |
| `approval.requested` | `agents` {agent_id: model} (+`models`, `sizes`) | | plan and team ready; waiting for the user's go-ahead |
| `approval.granted` | `models` {agent_id: model} | | work may start |
| `agent.updated` | `model` | ✔ | the agent's model changed (chosen during approval) |
| `attention.required` | `title`, `summary`, `reasons`, `suggestions` (+`diagnosis`, `options`) | task_id | the lead could not rescue a task: the user decides |
| `attention.resolved` | `decision`: retry / skip / stop (+`note`) | task_id | the user's answer |

## Semantics the UI relies on

**1. Two-level ticks.** `done` = the agent says it finished. `verified` = an independent check agreed
(in this version: mechanical checks - files exist, are non-empty and parse; tests/reviewers come later).

**2. Soft pause has three steps:** `user.command(pause)` → each running agent finishes its current step and
reports `agent.status=paused` → only when **all** have stopped, `run.paused`. Until then the UI shows "Pausing…".
A file being written finishes; no new file starts. Chatting is allowed while paused.

**3. Chat is `agent.message`.** A user message has `role=user` and the target's `agent_id`; replies have `role=agent`.
Every agent (and the lead) has its own thread.

**4. Plans are always full snapshots.** `added/removed/changed` only drive animations; `stages` groups tasks into
waves for display (tasks in one wave could run in parallel). A removed task is shown struck through until the next
plan event; its agent gets `agent.removed` right after the `plan.updated`.

**5. Running, done, verified and failed tasks are history.** A mid-run plan change may only add, remove or edit
pending tasks (`plan.validate_replan`).

**6. Legal transitions** (anything else is an engine bug; `check_stream()` verifies whole streams):

```
task : pending→running|skipped   running→done|failed|pending   done→verified|running|failed
       failed→running|skipped    verified→running              skipped→pending
agent: queued→running   running→paused|done|error   paused→running   error→running   done→running
```

**7. Waiting states.** Between `approval.requested` and `approval.granted`, and between `attention.required` and
`attention.resolved`, no work happens; the user can still chat (and gets answers). `check_stream` rejects a grant
without a request and a resolution without an open request.

## Adding an event type
1) constant in `T` and a row in `SPEC`  2) a row in this table  3) a test  4) bump `SCHEMA_VERSION` only for an incompatible change.
