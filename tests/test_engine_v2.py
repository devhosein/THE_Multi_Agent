"""Engine features added after the first live runs: approval with per-agent models, immediate replies,
loop detection, history compaction, the lead rescuing failed tasks, user decisions, continuation runs, strength."""
import json
import threading
import time
import unittest

from mission_control.engine import EngineSession
from mission_control.events import T as EV, check_stream
from mission_control.llm import ChatResult
from mission_control.orchestrator import answer_user
from mission_control.session import SessionError
from mission_control.worker import compact

from .helpers import (APP_PY, CONTRACT, FINISH, INDEX, PLAN3, T, T1, T2, T3, OllamaStandIn, ScriptedLLM, call,
                      happy_workers, make_engine)


def of(events, type_, **match):
    return [e for e in events if e.type == type_ and all(getattr(e, k, None) == v or e.data.get(k) == v for k, v in match.items())]


def statuses(events, tid):
    return [e.data["status"] for e in events if e.type == EV.TASK_STATUS and e.task_id == tid]


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


BROKEN = call("write_file", path="backend/app.py", content="def broken(:\n")


class Approval(unittest.TestCase):
    def test_work_waits_for_the_go_ahead_and_models_can_be_changed(self):
        lead = OllamaStandIn(installed=("test-model:latest", "big:35b"))
        other = OllamaStandIn(installed=("test-model:latest", "big:35b"), workers=happy_workers())
        made = []
        s = make_engine(lead, require_approval=True, factory=lambda m: made.append(m) or other)
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.APPROVAL_REQUESTED)))
        time.sleep(0.3)
        self.assertEqual(of(s.log.read(), EV.TASK_STATUS), [], "work started before approval")
        req = of(s.log.read(), EV.APPROVAL_REQUESTED)[0].data
        self.assertEqual(req["agents"], {"a_t1": "test-model", "a_t2": "test-model", "a_t3": "test-model"})
        self.assertIn("big:35b", req["models"])
        with self.assertRaises(SessionError) as cm:
            s.post_approval({"a_t2": "not-installed:7b"})
        self.assertEqual(cm.exception.code, "model_missing")
        with self.assertRaises(SessionError):
            s.post_approval({"ghost": "big:35b"})
        s.post_approval({"a_t2": "big:35b"})
        s.join(10)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        self.assertEqual([(e.agent_id, e.data["model"]) for e in of(ev, EV.AGENT_UPDATED)], [("a_t2", "big:35b")])
        self.assertEqual(of(ev, EV.APPROVAL_GRANTED)[0].data["models"]["a_t2"], "big:35b")
        self.assertEqual(made, ["big:35b"])
        self.assertEqual([c[1] for c in other.calls if c[0] == "worker"], ["t2", "t2"])   # t2 really used the other model
        self.assertEqual(ev[-1].data["status"], "completed")
        with self.assertRaises(SessionError) as cm:
            s.post_approval({})
        self.assertEqual(cm.exception.code, "run_finished")

    def test_approval_is_not_awaited_twice(self):
        s = make_engine(ScriptedLLM())
        s.start()
        s.join(10)
        s2 = make_engine(ScriptedLLM(on_worker=lambda t, m: time.sleep(0.05)))
        s2.start()
        with self.assertRaises(SessionError) as cm:
            s2.post_approval({})
        self.assertEqual(cm.exception.code, "not_awaiting")
        s2.stop()

    def test_the_lead_answers_while_you_review_the_plan_and_adjusts_it_before_starting(self):
        t4 = T("t4", "Docs", "docs", ["t2"], ["docs/**"])
        w = happy_workers()
        w["t4"] = [call("write_file", path="docs/a.md", content="# a\n"), FINISH]
        llm = ScriptedLLM(workers=w, reviews=[{"action": "update", "reason": "user asked for docs", "tasks": [T1, T2, T3, t4]},
                                              {"action": "keep", "reason": "ok", "tasks": []}])
        s = make_engine(llm, require_approval=True, review=True)
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.APPROVAL_REQUESTED)))
        s.post_message("orchestrator", "what will the backend do?")
        s.post_message("orchestrator", "add a docs task")
        self.assertTrue(wait_for(lambda: len([m for m in of(s.log.read(), EV.AGENT_MESSAGE, agent_id="orchestrator")
                                              if m.data["text"].startswith("Got it")]) == 2))
        s.post_approval({})
        s.join(10)
        ev = s.log.read()
        self.assertEqual([d.data["text"] for d in of(ev, EV.DECISION)], ["add a docs task"])     # the question is not a decision
        first_review = [c for c in llm.calls if c[0] == "review"][0][2][-1]["content"]
        self.assertIn("add a docs task", first_review)
        granted = of(ev, EV.APPROVAL_GRANTED)[0].seq
        self.assertLess(granted, of(ev, EV.PLAN_UPDATED)[0].seq)
        self.assertLess(of(ev, EV.PLAN_UPDATED)[0].seq, of(ev, EV.TASK_STATUS)[0].seq)          # adjusted BEFORE any work
        self.assertEqual(check_stream(ev), [])

    def test_stop_while_waiting_for_approval(self):
        s = make_engine(ScriptedLLM(), require_approval=True)
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.APPROVAL_REQUESTED)))
        s.post_command("stop")
        s.join(5)
        self.assertEqual(s.log.read()[-1].data["status"], "stopped")


class Replies(unittest.TestCase):
    def test_a_running_agent_answers_after_its_current_step_then_continues(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "x" not in holder:
                holder["x"] = 1
                holder["s"].post_message("a_t2", "which framework are you using?")

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        s.join(10)
        ev = s.log.read()
        chat = [(e.data["role"], e.data["text"]) for e in of(ev, EV.AGENT_MESSAGE, agent_id="a_t2")]
        self.assertEqual(chat[0], ("user", "which framework are you using?"))
        self.assertEqual(chat[1], ("agent", "Got it: which framework are you using?"))       # a real reply, in its voice
        self.assertEqual(of(ev, EV.DECISION), [])                                            # a question is not a decision
        reply_ctx = [c for c in llm.calls if c[0] == "reply"][0][2]
        self.assertIn("TASK_ID: t2", reply_ctx[0]["content"])                                # answered with ITS context
        # the reply came before the agent's next model call, and the work went on
        seqs = [e.seq for e in ev if (e.type == EV.AGENT_MESSAGE and e.agent_id == "a_t2" and e.data["role"] == "agent"
                                      and e.data["text"].startswith("Got it")) or (e.type == EV.TASK_STATUS and e.task_id == "t2"
                                                                                    and e.data["status"] == "done")]
        self.assertEqual(len(seqs), 2)
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_a_finished_agent_still_answers_and_forwards_instructions_to_the_lead(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t3" and "x" not in holder:
                holder["x"] = 1
                holder["s"].post_message("a_t1", "add an auth section to the contract")

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm, review=True)
        holder["s"] = s
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertTrue(any(m.data["text"].startswith("Got it") for m in of(ev, EV.AGENT_MESSAGE, agent_id="a_t1")))
        self.assertEqual([d.data["text"] for d in of(ev, EV.DECISION)], ["add an auth section to the contract"])

    def test_a_failed_reply_never_loses_the_message(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "x" not in holder:
                holder["x"] = 1
                holder["s"].post_message("a_t2", "use port 8080")

        llm = ScriptedLLM(on_worker=hook, replies=["not json at all"])
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertIn("I got your message", of(ev, EV.AGENT_MESSAGE, agent_id="a_t2", role="agent")[0].data["text"])
        self.assertEqual([d.data["text"] for d in of(ev, EV.DECISION)], ["use port 8080"])   # kept as an instruction
        self.assertTrue(any("Could not generate a reply" in e.data["message"] for e in of(ev, EV.LOG)))


class TellingInstructionsApart(unittest.TestCase):
    """Live run cafe-35b-v5: a clear instruction during approval was answered ("I'll fold this into the frontend")
    but not recorded as a decision, so no agent would have seen it."""

    class Stub:
        def __init__(self, obj):
            self.obj = obj

        def chat(self, messages, *, schema=None, tools=None, temperature=None):
            return ChatResult(content=json.dumps(self.obj))

    def ask(self, obj, text="Use a warm palette."):
        return answer_user(self.Stub({"reply": "Got it.", **obj}), [{"role": "system", "content": "lead"}], text).instruction

    def test_either_signal_makes_it_an_instruction(self):
        self.assertEqual(self.ask({"is_instruction": True, "instruction": ""}), "Use a warm palette.")
        self.assertEqual(self.ask({"is_instruction": False, "instruction": "Use coffee brown."}), "Use coffee brown.")
        self.assertEqual(self.ask({"is_instruction": True, "instruction": "Use coffee brown."}), "Use coffee brown.")

    def test_the_lead_is_told_that_a_wish_that_fits_the_plan_is_still_an_instruction(self):
        seen = []

        class Spy(self.Stub):
            def chat(s, messages, **kw):
                seen.append(messages[-1]["content"])
                return super().chat(messages, **kw)

        answer_user(Spy({"reply": "ok", "is_instruction": True, "instruction": "x"}), [], "Use warm colors.")
        self.assertIn("EVEN IF it already fits the current plan", seen[0])
        self.assertIn("when in doubt, it is an instruction", seen[0])

    def test_a_question_stays_a_question(self):
        self.assertIsNone(self.ask({"is_instruction": False, "instruction": ""}, "Which port do you use?"))


class Loops(unittest.TestCase):
    def test_the_test_sh_loop_from_the_real_run_is_stopped_with_a_diagnosis(self):
        same = call("write_file", path="backend/test.sh", content="node test.js\n")
        w = happy_workers()
        w["t2"] = [same] * 6 + [APP_PY, FINISH]
        llm = ScriptedLLM(workers=w)
        s, ev = None, None
        s = make_engine(llm, max_attempts=2)
        s.start()
        s.join(10)
        ev = s.log.read()
        err = of(ev, EV.ERROR)[0].data["message"]
        self.assertIn("stuck in a loop", err)
        self.assertIn("write_file backend/test.sh", err)
        warned = [m for c in llm.calls if c[0] == "worker" and c[1] == "t2" for m in c[2]
                  if m["role"] == "user" and "exactly the same call" in m["content"]]
        self.assertTrue(warned, "the agent must be warned before it is stopped")
        self.assertEqual(statuses(ev, "t2")[:3], ["running", "failed", "running"])            # the retry then succeeded
        self.assertEqual(statuses(ev, "t2")[-1], "verified")

    def test_an_answer_that_never_ends_is_told_to_act_and_the_task_goes_on(self):
        """Live run cafe-35b-v4: three answers thought until the 600 s timeout; the same calls then took seconds.
        With the output cap such an answer comes back cut off ("length") and without a tool call."""
        runaway = ChatResult(content="", thinking="Let me reconsider the document root. " * 3000,
                             tokens_in=9000, tokens_out=32768, done_reason="length")
        w = happy_workers()
        w["t2"] = [runaway, APP_PY, FINISH]
        llm = ScriptedLLM(workers=w)
        s = make_engine(llm)
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(statuses(ev, "t2"), ["running", "done", "verified"])              # same attempt, no failure
        second = [c for c in llm.calls if c[0] == "worker" and c[1] == "t2"][1][2]
        self.assertIn("ran out of room while you were still thinking", second[-1]["content"])
        self.assertTrue(any("thought until it ran out of room" in e.data["message"] for e in of(ev, EV.LOG)))

    def test_different_calls_are_not_a_loop(self):
        w = happy_workers()
        w["t2"] = [call("read_file", path=f"x{i}") for i in range(8)] + [APP_PY, FINISH]
        _, ev = None, None
        s = make_engine(ScriptedLLM(workers=w))
        s.start()
        s.join(10)
        self.assertEqual(statuses(s.log.read(), "t2"), ["running", "done", "verified"])


class Compaction(unittest.TestCase):
    def test_written_content_leaves_the_history(self):
        big = "x = 1\n" * 2000
        w = happy_workers()
        w["t2"] = [call("write_file", path="backend/app.py", content=big), call("list_files"), FINISH]
        llm = ScriptedLLM(workers=w)
        s = make_engine(llm)
        s.start()
        s.join(10)
        third = [c for c in llm.calls if c[0] == "worker" and c[1] == "t2"][2][2]
        dumped = json.dumps(third)
        self.assertNotIn("x = 1\\nx = 1", dumped)
        call_args = [c["function"]["arguments"] for m in third if m["role"] == "assistant" for c in m.get("tool_calls") or []]
        self.assertEqual(call_args[0], {"path": "backend/app.py"})          # no content, and no note to imitate
        self.assertIn(f"{len(big)} characters of content were removed from this conversation", dumped)

    def test_dropped_content_keeps_the_names_other_files_must_match(self):
        """Live run cafe-35b-v5: the frontend agent wrote styles.css (.form-group), its content left the history,
        and the page it wrote next used form-row: the reservation form came out unstyled. The names stay, in the tool
        result (live runs v4/v7/v8: a note inside the call itself was imitated as file content)."""
        css = ".form-group { margin: 8px; }\n.btn-primary:hover { color: #fff; } /* .old */\n" + "x { y: z; }\n" * 400
        page = '<form id="booking"><div class="form-group wide"><input></div></form>'
        js = 'const f = document.getElementById("booking"); f.classList.add("is-sent"); document.querySelector(".wide");'
        msgs = [{"role": "system", "content": "s"}]
        for path, content in (("css/style.css", css), ("index.html", page), ("app.js", js), ("x.txt", "y")):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [call("write_file", path=path, content=content)]})
            msgs.append({"role": "tool", "tool_name": "write_file", "content": f"OK: wrote {path}"})
        msgs.append({"role": "assistant", "content": "done"})
        compact(msgs)
        self.assertEqual([m["tool_calls"][0]["function"]["arguments"] for m in msgs[1:9:2]],
                         [{"path": p} for p in ("css/style.css", "index.html", "app.js", "x.txt")])
        notes = [m["content"] for m in msgs[2:10:2]]
        self.assertIn("It styles the classes: btn-primary, form-group.]", notes[0])
        self.assertNotIn("old", notes[0])
        self.assertIn("It has the ids: booking; uses the classes: form-group, wide.]", notes[1])
        self.assertIn("It looks up the ids: booking; uses the classes: is-sent, wide.]", notes[2])
        self.assertTrue(notes[3].startswith("OK: wrote x.txt\n[1 characters of content were removed"), notes[3])
        compact(msgs)                                                        # nothing is added twice
        self.assertEqual(msgs[2]["content"].count("were removed"), 1)

    def test_compact_keeps_the_newest_reads_whole(self):
        msgs = [{"role": "system", "content": "s"}]
        for i in range(4):
            msgs.append({"role": "tool", "tool_name": "read_file", "content": f"R{i}" + "y" * 3000})
        saved = compact(msgs)
        self.assertGreater(saved, 0)
        self.assertIn("older read shortened", msgs[1]["content"])
        self.assertNotIn("older read shortened", msgs[4]["content"])
        self.assertNotIn("older read shortened", msgs[3]["content"])
        compact(msgs, aggressive=True)
        self.assertIn("older read shortened", msgs[3]["content"])
        self.assertEqual(len(msgs[4]["content"]), 3002)                                       # the newest stays whole


class Rescue(unittest.TestCase):
    def test_the_lead_rescues_a_failing_task_with_guidance(self):
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH, BROKEN, FINISH, APP_PY, FINISH]
        llm = ScriptedLLM(workers=w, diagnoses=[{"diagnosis": "the agent writes invalid Python", "user_summary": "Syntax errors.",
                                                 "suggestions": ["Retry"], "action": "retry",
                                                 "guidance": "Write backend/app.py with one simple function and check the brackets.",
                                                 "tasks": []}])
        s = make_engine(llm, max_attempts=2)
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "running", "failed", "running", "done", "verified"])
        third = [c for c in llm.calls if c[0] == "worker" and c[1] == "t2"][4][2]
        self.assertIn("Guidance from the lead agent", third[1]["content"])
        self.assertIn("check the brackets", third[1]["content"])
        diag_prompt = [c for c in llm.calls if c[0] == "diagnose"][0][2][1]["content"]
        self.assertIn("syntax error", diag_prompt)                 # the lead saw the real evidence
        self.assertTrue(any("Rescue: retrying t2" in e.data["message"] for e in of(ev, EV.LOG)))
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_the_lead_splits_a_task_that_is_too_big(self):
        a = T("t2a", "Backend part A", "backend", ["t1"], ["backend/a/**"])
        b = T("t2b", "Backend part B", "backend", ["t2a"], ["backend/b/**"])
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH, BROKEN, FINISH]
        w["t2a"] = [call("write_file", path="backend/a/x.py", content="a = 1\n"), FINISH]
        w["t2b"] = [call("write_file", path="backend/b/y.py", content="b = 1\n"), FINISH]
        llm = ScriptedLLM(workers=w, diagnoses=[{"diagnosis": "too big", "user_summary": "Split into two parts.",
                                                 "suggestions": ["Retry"], "action": "split", "guidance": "",
                                                 "tasks": [T1, a, b, T3]}])
        s = make_engine(llm, max_attempts=2)
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        upd = of(ev, EV.PLAN_UPDATED)[0].data
        self.assertEqual((upd["added"], upd["removed"]), (["t2a", "t2b"], ["t2"]))
        self.assertEqual([e.agent_id for e in of(ev, EV.AGENT_REMOVED)], ["a_t2"])
        self.assertEqual(statuses(ev, "t2b")[-1], "verified")
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_an_invalid_split_is_rejected_and_the_model_told_why(self):
        bad = {"diagnosis": "x", "user_summary": "x", "suggestions": [], "action": "split", "guidance": "",
               "tasks": [T1, T2, T3]}                               # keeps the failed task: not a split
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH, BROKEN, FINISH]
        llm = ScriptedLLM(workers=w, diagnoses=[bad, bad, bad])
        s = make_engine(llm, max_attempts=2)
        s.start()
        s.join(10)
        feedback = [c for c in llm.calls if c[0] == "diagnose"][1][2][-1]["content"]
        self.assertIn("must be replaced by new tasks", feedback)
        self.assertEqual(statuses(s.log.read(), "t2")[-1], "skipped")

    def test_infrastructure_failures_are_diagnosed_without_asking_the_failing_model(self):
        from mission_control.llm import LLMError

        class Down(ScriptedLLM):
            def chat(self, messages, **kw):
                if kw.get("tools"):
                    raise LLMError("cannot reach Ollama at http://x (connection refused)")
                return super().chat(messages, **kw)

        llm = Down()
        s = make_engine(llm, on_failure="ask")
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.ATTENTION_REQUIRED), 15))
        att = of(s.log.read(), EV.ATTENTION_REQUIRED)[0].data
        self.assertIn("model server kept failing", att["summary"])
        self.assertTrue(any("Ollama" in x for x in att["suggestions"]))
        self.assertEqual([c for c in llm.calls if c[0] == "diagnose"], [])
        self.assertEqual(statuses(s.log.read(), "t1"), ["running", "failed"])            # one attempt, not three
        s.post_resolution("t1", "stop")
        s.join(5)


class UserDecisions(unittest.TestCase):
    def failing(self):
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH, BROKEN, FINISH, APP_PY, FINISH]
        return ScriptedLLM(workers=w)

    def test_the_user_is_asked_with_reasons_and_suggestions_then_retry_with_a_note(self):
        s = make_engine(self.failing(), on_failure="ask")
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.ATTENTION_REQUIRED)))
        att = of(s.log.read(), EV.ATTENTION_REQUIRED)[0]
        self.assertEqual(att.task_id, "t2")
        self.assertTrue(any("syntax error" in r for r in att.data["reasons"]))
        self.assertTrue(att.data["suggestions"])
        self.assertEqual(att.data["options"], ["retry", "skip", "stop"])
        time.sleep(0.3)
        n = len(s.log.read())
        self.assertEqual(len(s.log.read()), n, "nothing may happen while waiting for the user")
        with self.assertRaises(SessionError):
            s.post_resolution("t9", "retry")
        with self.assertRaises(SessionError):
            s.post_resolution("t2", "explode")
        s.post_resolution("t2", "retry", "keep it to one function")
        s.join(10)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        self.assertEqual(of(ev, EV.ATTENTION_RESOLVED)[0].data, {"decision": "retry", "note": "keep it to one function"})
        self.assertEqual(statuses(ev, "t2")[-1], "verified")
        self.assertIn("keep it to one function", [d.data["text"] for d in of(ev, EV.DECISION)][0])
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_skip_and_stop(self):
        s = make_engine(self.failing(), on_failure="ask")
        s.start()
        self.assertTrue(wait_for(lambda: of(s.log.read(), EV.ATTENTION_REQUIRED)))
        s.post_resolution("t2", "skip")
        s.join(10)
        ev = s.log.read()
        self.assertEqual(statuses(ev, "t2")[-1], "skipped")
        self.assertEqual(statuses(ev, "t3")[-1], "verified")               # independent work continues
        self.assertEqual(ev[-1].data["status"], "failed")
        s2 = make_engine(self.failing(), on_failure="ask")
        s2.start()
        self.assertTrue(wait_for(lambda: of(s2.log.read(), EV.ATTENTION_REQUIRED)))
        s2.post_resolution("t2", "stop")
        s2.join(10)
        self.assertEqual(s2.log.read()[-1].data["status"], "stopped")
        self.assertEqual(check_stream(s2.log.read()), [])

    def test_policy_stop(self):
        s = make_engine(self.failing(), on_failure="stop")
        s.start()
        s.join(10)
        self.assertEqual(s.log.read()[-1].data["status"], "stopped")


class Continuation(unittest.TestCase):
    def test_retry_failed_tasks_keeps_the_good_work_and_redoes_the_rest(self):
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH, BROKEN, FINISH]
        llm = ScriptedLLM(workers=w)
        s = make_engine(llm)
        s.start()
        s.join(10)
        self.assertEqual(s.log.read()[-1].data["status"], "failed")
        self.assertTrue(s.can_continue)
        llm.workers["t2"] = [APP_PY, FINISH]
        s2 = EngineSession.continuation(s)
        s2.start()
        s2.join(10)
        ev = s2.log.read()
        self.assertEqual(check_stream(ev), [])
        self.assertTrue(ev[0].data["resumed"])
        self.assertTrue(all(e.data.get("carried") for e in of(ev, EV.TASK_STATUS) if e.task_id in ("t1", "t3")))
        self.assertEqual(statuses(ev, "t2"), ["running", "done", "verified"])
        self.assertNotIn("t1", {c[1] for c in llm.calls[-4:] if c[0] == "worker"})          # nothing redone that was fine
        self.assertEqual(ev[-1].data["status"], "completed")
        self.assertFalse(s2.can_continue)


class Strength(unittest.TestCase):
    def test_a_small_model_gets_more_attempts_and_a_smaller_plan(self):
        w = happy_workers()
        w["t2"] = [BROKEN, FINISH] * 3
        llm = OllamaStandIn(workers=w, installed=("test-model:latest",), sizes={"test-model:latest": "9.2B"})
        s = make_engine(llm, max_attempts=None)
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(statuses(ev, "t2").count("failed"), 3)                 # small: 3 attempts, not 2
        self.assertEqual(ev[0].data["settings"]["profile"], "small")
        self.assertIn("between 3 and 6 tasks", [c for c in llm.calls if c[0] == "plan"][0][2][0]["content"])
        self.assertIn("Example of a good sequence", [c for c in llm.calls if c[0] == "worker"][0][2][0]["content"])

    def test_the_strength_can_be_overridden(self):
        llm = OllamaStandIn(installed=("test-model:latest",), sizes={"test-model:latest": "9.2B"})
        s = make_engine(llm, strength="large")
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(ev[0].data["settings"]["profile"], "large")
        self.assertNotIn("Example of a good sequence", [c for c in llm.calls if c[0] == "worker"][0][2][0]["content"])


class Logs(unittest.TestCase):
    def test_failures_are_logged_with_evidence(self):
        w = happy_workers()
        w["t2"] = [("I will write it now", [BROKEN]), FINISH, BROKEN, FINISH]
        s = make_engine(ScriptedLLM(workers=w))
        s.start()
        s.join(10)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        errors = [e for e in of(ev, EV.LOG) if e.data["level"] == "error"]
        self.assertTrue(errors)
        self.assertIn("syntax error", errors[0].data["detail"])
        self.assertIn("I will write it now", errors[0].data["detail"])            # the agent's last words
        self.assertEqual(errors[0].task_id, "t2")
        info = of(ev, EV.LOG, level="info")[0].data["message"]
        self.assertIn("strength auto", info)


if __name__ == "__main__":
    unittest.main()
