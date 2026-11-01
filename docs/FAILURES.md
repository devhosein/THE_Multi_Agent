# Failures seen in real runs, and what the engine does about them

Three live runs on the user's Windows machine (Ollama, `ornith-1.5:9b` and `ornith-1.5:35b`) drove most of the
robustness work in `worker.py`, `llm.py`, `tools.py` and `orchestrator.py`. Every row below is a real failure.

| # | Run | What happened (from `.mission/events.jsonl`) | Root cause | Fix in the engine |
|---|---|---|---|---|
| 1 | test 1 (9B), task t4 | `HTTP 500: invalid tool call arguments for "write_file": unexpected end of JSON input` | The model's answer was **cut off mid tool call**: the context window was full (our own `num_ctx=16384`) while it was writing a large file | No `num_ctx` is sent by default (Ollama decides); history **compaction** (written content and old reads become short notes); on a cut-off, shrink harder and tell the model to write in parts (`write_file` + new `append_file`); `classify_llm_error` → `truncated` |
| 2 | test 1 (9B), t4 attempt 2 | ~9000 output tokens, no content, no tool call → "produced no work" | Same budget problem: thinking ate the room before any tool call | Same as above; `done_reason == "length"` is detected and treated like a cut-off |
| 3 | test 1 (9B), task t5 | `write_file backend/test.sh` 14 times, the agent itself writing "I'm in a loop" | Agents cannot run programs; the model kept trying to make a test runnable | Prompt rule "you cannot run programs or tests"; **loop detection** (same call ×3 → warning, ×5 → stop with a diagnosis); a write with identical content returns "no change, do not write it again" |
| 4 | test 2 (35B), task t1 | `HTTP 500: CUDA error: an illegal memory access was encountered` | A GPU/runner hiccup in Ollama | `transient` errors are retried with back-off (2 s, 5 s, 10 s) **without losing the attempt** |
| 5 | test 2 (35B), task t3 | Rule 1 again, on **both** attempts | The backend agent first read ~20 KB of frontend files: prompt ≈ 8k tokens + thinking + a large file > 16k | Same as 1; `read_file` is paged by model strength; a single enormous line is cut |
| 6 | test 3 (35B, no context limit) | Completed. Largest prompt: 14,666 tokens. One attempt hit the old 14-step cap | Confirms 1; the step cap was a second limit that got in the way | The step budget is now only a safety net (80–100 model calls); loops are caught separately |
| 7 | test 1, user message | "tell me what are u doing right now?" got a canned acknowledgement and was recorded as a **decision** | Messages were only queued | The addressed agent answers right away (next safe point) in its own voice; only instructions go into the decision ledger; questions just get an answer |

When a task still fails after its attempts, the lead **diagnoses** it (`orchestrator.diagnose_failure`): retry with
concrete guidance, or split it into smaller tasks. If that does not work either, the user gets a decision panel with the
reasons and suggestions (retry, retry with a note, skip, stop). Infrastructure failures (Ollama down/crashing) are
diagnosed by rule, without asking the failing model.

How to investigate a new failure: open **Logs** in the UI (or the `log` events in `.mission/events.jsonl`), then
**Download report** — it contains the settings, every error with its evidence and the agent's last words.
