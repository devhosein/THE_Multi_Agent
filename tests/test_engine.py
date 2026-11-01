import json
import threading
import time
import unittest
from pathlib import Path

from mission_control.events import T as EV
from mission_control.llm import ChatResult
from mission_control.session import SessionError

from .helpers import (APP_PY, CONTRACT, FINISH, INDEX, PLAN3, T1, T2, T3, ScriptedLLM, T, assert_valid, call,
                      happy_workers, make_engine, run_engine)


def of(events, type_, **match):
    return [e for e in events if e.type == type_ and all(getattr(e, k, None) == v or e.data.get(k) == v for k, v in match.items())]


def statuses(events, task_id):
    return [e.data["status"] for e in events if e.type == EV.TASK_STATUS and e.task_id == task_id]


class HappyPath(unittest.TestCase):
    def test_full_run(self):
        llm = ScriptedLLM()
        s, ev = run_engine(llm)
        assert_valid(self, ev)
        self.assertEqual(ev[0].type, EV.RUN_STARTED)
        self.assertFalse(ev[0].data["mock"])
        self.assertEqual(ev[0].data["model"], "test-model")
        self.assertEqual(ev[-1].type, EV.RUN_FINISHED)
        self.assertEqual(ev[-1].data["status"], "completed")
        for tid in ("t1", "t2", "t3"):
            self.assertEqual(statuses(ev, tid), ["running", "done", "verified"], tid)
        self.assertEqual([e.agent_id for e in of(ev, EV.AGENT_CREATED)], ["a_t1", "a_t2", "a_t3"])
        self.assertEqual([e.data["title"] for e in of(ev, EV.AGENT_CREATED)], ["Architect", "Backend", "Frontend"])
        root = s.ws.root
        self.assertIn("GET /api/menu", (root / "contracts/api.md").read_text())
        self.assertTrue((root / "backend/app.py").is_file() and (root / "frontend/index.html").is_file())
        self.assertEqual(json.loads(s.ws.plan_path.read_text())["version"], 1)

    def test_tasks_run_in_dependency_order(self):
        _, ev = run_engine(ScriptedLLM())
        order = [(e.task_id, e.data["status"]) for e in of(ev, EV.TASK_STATUS)]
        self.assertLess(order.index(("t1", "verified")), order.index(("t2", "running")))
        self.assertLess(order.index(("t2", "verified")), order.index(("t3", "running")))   # sequential in phase 2

    def test_usage_is_cumulative_and_exact(self):
        _, ev = run_engine(ScriptedLLM())
        u = [(e.data["tokens_in"], e.data["tokens_out"]) for e in of(ev, EV.USAGE)]
        self.assertEqual(u, sorted(u))
        self.assertEqual(u[-1], (300 + 6 * 100, 120 + 6 * 40))     # 1 planner call + 6 worker calls

    def test_plan_reviews_are_counted_in_usage_too(self):
        _, ev = run_engine(ScriptedLLM(), review=True)
        u = [(e.data["tokens_in"], e.data["tokens_out"]) for e in of(ev, EV.USAGE)]
        self.assertEqual(u[-1], (300 + 6 * 100 + 2 * 200, 120 + 6 * 40 + 2 * 60))   # + two reviews

    def test_pulse_ordering_for_the_ui(self):
        """dispatch/report effects in the UI derive from task.status running/done; agent.status must agree."""
        _, ev = run_engine(ScriptedLLM())
        a2 = [e.data["status"] for e in of(ev, EV.AGENT_STATUS, agent_id="a_t2")]
        self.assertEqual(a2, ["running", "done"])

    def test_files_stream_as_events_and_match_disk(self):
        s, ev = run_engine(ScriptedLLM())
        buf = {}
        for e in ev:
            if e.type == EV.FILE_CREATED:
                buf[e.data["path"]] = ""
            elif e.type == EV.FILE_CHUNK:
                buf[e.data["path"]] += e.data["text"]
        self.assertEqual(set(buf), {"contracts/api.md", "backend/app.py", "frontend/index.html"})
        for path, text in buf.items():
            self.assertEqual((s.ws.root / path).read_text(), text)

    def test_persian_status_lines(self):
        _, ev = run_engine(ScriptedLLM(), lang="fa")
        lines = " ".join(e.data["text"] for e in of(ev, EV.ORCH_STATUS))
        self.assertIn("تحلیل", lines)
        self.assertIn("شروع", lines)


class SafetyAndRecovery(unittest.TestCase):
    def test_write_outside_ownership_is_refused_and_the_model_recovers(self):
        w = happy_workers()
        w["t2"] = [call("write_file", path="frontend/x.html", content="<p>"), APP_PY, FINISH]
        llm = ScriptedLLM(workers=w)
        s, ev = run_engine(llm)
        assert_valid(self, ev)
        self.assertFalse((s.ws.root / "frontend/x.html").exists())
        second = llm.kinds("worker", "t2")[1][2]
        tool_msgs = [m["content"] for m in second if m["role"] == "tool"]
        self.assertTrue(tool_msgs[0].startswith("ERROR: you may not write"))
        self.assertEqual(statuses(ev, "t2"), ["running", "done", "verified"])

    def test_escape_attempts_never_touch_the_disk(self):
        w = happy_workers()
        w["t2"] = [call("write_file", path="../evil.txt", content="x"),
                   call("write_file", path=".mission/plan.json", content="{}"),
                   call("write_file", path="/tmp/evil2.txt", content="x"),
                   APP_PY, FINISH]
        s, ev = run_engine(ScriptedLLM(workers=w))
        self.assertFalse((s.ws.root.parent / "evil.txt").exists())
        self.assertFalse(Path("/tmp/evil2.txt").exists())
        self.assertEqual(json.loads(s.ws.plan_path.read_text())["version"], 1)
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_json_string_arguments_are_accepted(self):
        w = happy_workers()
        w["t2"] = [{"function": {"name": "write_file", "arguments": json.dumps({"path": "backend/app.py", "content": "x = 1\n"})}}, FINISH]
        s, ev = run_engine(ScriptedLLM(workers=w))
        self.assertEqual(ev[-1].data["status"], "completed")
        self.assertTrue((s.ws.root / "backend/app.py").is_file())

    def test_unknown_tool_is_reported_to_the_model(self):
        w = happy_workers()
        w["t2"] = [call("delete_everything"), APP_PY, FINISH]
        llm = ScriptedLLM(workers=w)
        run_engine(llm)
        msgs = [m["content"] for m in llm.kinds("worker", "t2")[1][2] if m["role"] == "tool"]
        self.assertIn("unknown tool", msgs[0])

    def test_think_tags_never_reach_the_chat(self):
        w = happy_workers()
        w["t2"] = [("<think>secret reasoning</think>Writing the backend now", [APP_PY]), FINISH]
        _, ev = run_engine(ScriptedLLM(workers=w))
        texts = [e.data["text"] for e in of(ev, EV.AGENT_MESSAGE, agent_id="a_t2")]
        self.assertEqual(texts, ["Writing the backend now"])

    def test_failed_checks_trigger_a_retry_with_feedback(self):
        w = happy_workers()
        w["t2"] = [call("write_file", path="backend/app.py", content="def broken(:\n"), FINISH, APP_PY, FINISH]
        llm = ScriptedLLM(workers=w)
        s, ev = run_engine(llm)
        assert_valid(self, ev)
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "running", "done", "verified"])
        self.assertEqual([e.data["status"] for e in of(ev, EV.AGENT_STATUS, agent_id="a_t2")], ["running", "error", "running", "done"])
        err = of(ev, EV.ERROR)
        self.assertEqual(len(err), 1)
        self.assertIn("syntax error", err[0].data["message"])
        retry_prompt = llm.kinds("worker", "t2")[2][2][1]["content"]
        self.assertIn("previous attempt was rejected", retry_prompt)
        self.assertIn("syntax error", retry_prompt)
        self.assertIn("Files you already wrote: backend/app.py.", retry_prompt)   # memory of the failed attempt survives
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_a_good_file_from_a_failed_attempt_is_remembered_for_the_review(self):
        w = happy_workers()
        w["t2"] = [call("write_file", path="backend/ok.py", content="x = 1\n"),
                   call("write_file", path="backend/bad.py", content="def broken(:\n"), FINISH,
                   call("write_file", path="backend/bad.py", content="y = 2\n"), FINISH]
        llm = ScriptedLLM(workers=w)
        run_engine(llm, review=True)
        prompt = llm.kinds("review")[1][2][-1]["content"]          # the review after t2
        self.assertIn("backend/ok.py", prompt)
        self.assertIn("backend/bad.py", prompt)

    def test_owning_files_but_writing_none_fails_the_check(self):
        w = happy_workers()
        w["t2"] = [FINISH, APP_PY, FINISH]
        _, ev = run_engine(ScriptedLLM(workers=w))
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "running", "done", "verified"])
        self.assertIn("No file was written", of(ev, EV.ERROR)[0].data["message"])

    def test_two_failures_fail_the_task_skip_dependents_and_keep_going(self):
        plan = {"tasks": [T1, T2, T3, T("t4", "Docs", "docs", ["t2"], ["docs/**"])]}
        bad = call("write_file", path="backend/app.py", content="def broken(:\n")
        w = happy_workers()
        w["t2"] = [bad, FINISH, bad, FINISH]
        s, ev = run_engine(ScriptedLLM(plan=plan, workers=w))
        assert_valid(self, ev)
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "running", "failed", "skipped"])  # policy: skip
        self.assertEqual(statuses(ev, "t4"), ["skipped"])             # depends on the failed task
        self.assertEqual(statuses(ev, "t3"), ["running", "done", "verified"])   # independent work still happens
        self.assertEqual(ev[-1].data["status"], "failed")
        self.assertIn("2 of 4", ev[-1].data["summary"])

    def test_step_limit(self):
        w = happy_workers()
        w["t2"] = [call("list_files", path=f"p{i}") for i in range(6)]     # all different: not a loop, just endless
        _, ev = run_engine(ScriptedLLM(workers=w), max_steps=3, max_attempts=1)
        self.assertIn("did not finish within 3 model calls", of(ev, EV.ERROR)[0].data["message"])
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "skipped"])

    def test_model_that_never_uses_tools(self):
        w = happy_workers()
        plain = ChatResult(content="I will build the backend.", tokens_in=10, tokens_out=5)
        w["t2"] = [plain] * 4
        _, ev = run_engine(ScriptedLLM(workers=w))
        self.assertEqual(statuses(ev, "t2"), ["running", "failed", "running", "failed", "skipped"])
        self.assertIn("produced no work", of(ev, EV.ERROR)[0].data["message"])

    def test_forgetting_to_call_finish_is_tolerated_if_work_was_done(self):
        w = happy_workers()
        w["t2"] = [APP_PY, ChatResult(content="All done.", tokens_in=1, tokens_out=1)]
        _, ev = run_engine(ScriptedLLM(workers=w))
        self.assertEqual(statuses(ev, "t2"), ["running", "done", "verified"])

    def test_planner_failure_ends_the_run_cleanly(self):
        _, ev = run_engine(ScriptedLLM(plan=["nonsense"]))
        assert_valid(self, ev)
        self.assertEqual(ev[-1].type, EV.RUN_FINISHED)
        self.assertEqual(ev[-1].data["status"], "failed")
        self.assertIn("valid plan", of(ev, EV.ERROR)[0].data["message"])
        self.assertEqual(of(ev, EV.AGENT_CREATED), [])

    def test_a_planner_that_conflicts_is_repaired_by_feedback(self):
        bad = {"tasks": [T1, T("t2", "Backend", "backend", ["t1"], ["app/**"]), T("t3", "Frontend", "frontend", ["t1"], ["app/ui/**"])]}
        llm = ScriptedLLM(plan=[bad, PLAN3])
        _, ev = run_engine(llm)
        self.assertEqual(len(llm.kinds("plan")), 2)
        self.assertIn("may run in parallel", llm.kinds("plan")[1][2][-1]["content"])
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_engine_bug_surfaces_as_an_error_not_a_silent_thread_death(self):
        class Boom(ScriptedLLM):
            def chat(self, messages, **kw):
                if kw.get("tools"):
                    raise ZeroDivisionError("kaboom")
                return super().chat(messages, **kw)
        _, ev = run_engine(Boom())
        self.assertEqual(ev[-1].data["status"], "failed")
        self.assertIn("kaboom", of(ev, EV.ERROR)[0].data["message"])


class Replanning(unittest.TestCase):
    def update(self, tasks, reason="Frontend is not needed", decisions=()):
        return {"action": "update", "reason": reason, "decisions": list(decisions), "tasks": tasks}

    def test_lead_adds_and_removes_pending_tasks_mid_run(self):
        t4 = T("t4", "Docs", "docs", ["t2"], ["docs/**"])
        w = happy_workers()
        w["t4"] = [call("write_file", path="docs/USAGE.md", content="# Usage\n"), FINISH]
        llm = ScriptedLLM(workers=w, reviews=[self.update([T1, T2, t4], decisions=["Use Flask"]),
                                              {"action": "keep", "reason": "ok", "tasks": []}])
        s, ev = run_engine(llm, review=True)
        assert_valid(self, ev)
        upd = of(ev, EV.PLAN_UPDATED)
        self.assertEqual(len(upd), 1)
        self.assertEqual((upd[0].data["version"], upd[0].data["added"], upd[0].data["removed"]), (2, ["t4"], ["t3"]))
        self.assertEqual(upd[0].data["reason"], "Frontend is not needed")
        self.assertEqual([e.agent_id for e in of(ev, EV.AGENT_REMOVED)], ["a_t3"])
        created = [e.agent_id for e in of(ev, EV.AGENT_CREATED)]
        self.assertEqual(created, ["a_t1", "a_t2", "a_t3", "a_t4"])
        seqs = {t: [e.seq for e in ev if e.type == t][-1] for t in (EV.PLAN_UPDATED, EV.AGENT_REMOVED)}
        self.assertLess(seqs[EV.PLAN_UPDATED], seqs[EV.AGENT_REMOVED])        # snapshot first, then the agent changes
        self.assertEqual(statuses(ev, "t4"), ["running", "done", "verified"])
        self.assertNotIn("t3", {e.task_id for e in of(ev, EV.TASK_STATUS)})   # the removed task never ran
        self.assertFalse((s.ws.root / "frontend").exists())
        self.assertEqual(json.loads(s.ws.plan_path.read_text())["version"], 2)
        self.assertEqual(ev[-1].data["status"], "completed")
        d = of(ev, EV.DECISION)
        self.assertEqual((d[0].data["id"], d[0].data["source"], d[0].data["text"]), ("d1", "orchestrator", "Use Flask"))
        self.assertIn("Use Flask", s.ws.decisions_path.read_text())

    def test_decisions_reach_later_workers(self):
        llm = ScriptedLLM(reviews=[{"action": "keep", "reason": "ok", "decisions": ["All endpoints live under /api"], "tasks": []}])
        run_engine(llm, review=True)
        self.assertIn("All endpoints live under /api", llm.kinds("worker", "t2")[0][2][0]["content"])

    def test_history_cannot_be_rewritten_and_the_model_is_told_why(self):
        rewritten = dict(T1, title="Totally different")
        t4 = T("t4", "Docs", "docs", ["t2"], ["docs/**"])
        w = happy_workers()
        w["t4"] = [call("write_file", path="docs/a.md", content="# a\n"), FINISH]
        llm = ScriptedLLM(workers=w, reviews=[self.update([rewritten, T2, T3, t4]), self.update([T1, T2, T3, t4], "Docs needed"),
                                              {"action": "keep", "reason": "ok", "tasks": []}])
        _, ev = run_engine(llm, review=True)
        feedback = llm.kinds("review")[1][2][-1]["content"]
        self.assertIn("already verified and cannot be modified", feedback)
        self.assertEqual(of(ev, EV.PLAN_UPDATED)[0].data["added"], ["t4"])
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_a_review_that_never_works_keeps_the_plan_and_the_run_continues(self):
        llm = ScriptedLLM(reviews=["definitely not json"])
        _, ev = run_engine(llm, review=True)
        assert_valid(self, ev)
        self.assertEqual(of(ev, EV.PLAN_UPDATED), [])
        self.assertTrue(any("keeping the current plan" in e.data["text"] for e in of(ev, EV.ORCH_STATUS)))
        self.assertEqual(ev[-1].data["status"], "completed")

    def test_update_that_changes_nothing_counts_as_keep(self):
        llm = ScriptedLLM(reviews=[self.update([T1, T2, T3], "no real change")])
        _, ev = run_engine(llm, review=True)
        self.assertEqual(of(ev, EV.PLAN_UPDATED), [])

    def test_worker_notes_reach_the_review(self):
        w = happy_workers()
        w["t1"] = [call("note_to_lead", text="the contract has no auth section"), CONTRACT, FINISH]
        llm = ScriptedLLM(workers=w)
        run_engine(llm, review=True)
        first_review = llm.kinds("review")[0][2][-1]["content"]
        self.assertIn("the contract has no auth section", first_review)
        self.assertIn("[t1]", first_review)
        self.assertNotIn("the contract has no auth section", llm.kinds("review")[1][2][-1]["content"])   # consumed once

    def test_the_review_sees_what_really_happened(self):
        llm = ScriptedLLM()
        run_engine(llm, review=True)
        prompt = llm.kinds("review")[0][2][-1]["content"]
        self.assertIn("contracts/api.md", prompt)
        self.assertIn('"status": "verified"', prompt)
        self.assertIn('"result_summary": "done"', prompt)

    def test_no_review_after_the_last_task(self):
        llm = ScriptedLLM()
        run_engine(llm, review=True)
        self.assertEqual(len(llm.kinds("review")), 2)        # after t1 and after t2, not after t3


class Control(unittest.TestCase):
    def test_stop_ends_the_run_immediately_and_late_work_is_dropped(self):
        entered, release = threading.Event(), threading.Event()

        def hook(tid, msgs):
            if tid == "t2":
                entered.set()
                release.wait(5)

        s = make_engine(ScriptedLLM(on_worker=hook))
        s.start()
        self.assertTrue(entered.wait(5))
        r = s.post_command("stop")
        self.assertTrue(r["applied"])
        ev = s.log.read()
        self.assertEqual(ev[-1].type, EV.RUN_FINISHED)
        self.assertEqual(ev[-1].data["status"], "stopped")
        n = len(ev)
        release.set()                 # the in-flight model call now returns...
        s.join(5)
        self.assertEqual(len(s.log.read()), n)                       # ...and nothing more is recorded
        self.assertFalse((s.ws.root / "backend/app.py").exists())     # ...and nothing more is written
        assert_valid(self, s.log.read())
        with self.assertRaises(SessionError):
            s.post_command("pause")

    def test_pause_and_resume_at_safe_points(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "done" not in holder:
                holder["done"] = True
                holder["note"] = holder["s"].post_command("pause")["note"]

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        deadline = time.time() + 5
        while time.time() < deadline and not of(s.log.read(), EV.RUN_PAUSED):
            time.sleep(0.02)
        ev = s.log.read()
        self.assertTrue(of(ev, EV.RUN_PAUSED), "run never paused")
        self.assertIn("safe point", holder["note"])
        # the agent reports `paused` BEFORE the run does
        paused_agent = [e.seq for e in of(ev, EV.AGENT_STATUS, agent_id="a_t2") if e.data["status"] == "paused"]
        self.assertEqual(len(paused_agent), 1)
        self.assertLess(paused_agent[0], of(ev, EV.RUN_PAUSED)[0].seq)
        n = len(ev)
        time.sleep(0.5)
        self.assertEqual(len(s.log.read()), n, "events kept flowing while paused")
        self.assertFalse((s.ws.root / "backend/app.py").exists())
        self.assertEqual(s.post_command("pause")["applied"], False)    # already pausing
        self.assertTrue(s.post_command("resume")["applied"])
        s.join(10)
        ev = s.log.read()
        assert_valid(self, ev)
        self.assertLess(of(ev, EV.RUN_PAUSED)[0].seq, of(ev, EV.RUN_RESUMED)[0].seq)
        self.assertEqual([e.data["status"] for e in of(ev, EV.AGENT_STATUS, agent_id="a_t2")], ["running", "paused", "running", "done"])
        self.assertEqual(ev[-1].data["status"], "completed")
        self.assertTrue((s.ws.root / "backend/app.py").is_file())

    def test_pause_is_honoured_before_the_next_model_call_not_after_it(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "x" not in holder:
                holder["x"] = 1
                holder["s"].post_command("pause")

        # the first reply has no tool call, so the next safe point is the one right before the second model call
        w = happy_workers()
        w["t2"] = [ChatResult(content="thinking out loud", tokens_in=1, tokens_out=1), APP_PY, FINISH]
        llm = ScriptedLLM(workers=w, on_worker=hook)
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        while not of(s.log.read(), EV.RUN_PAUSED):
            time.sleep(0.02)
        time.sleep(0.3)
        self.assertEqual(len(llm.kinds("worker", "t2")), 1, "a whole extra model call ran after the pause request")
        s.post_command("resume")
        s.join(10)
        self.assertEqual(s.log.read()[-1].data["status"], "completed")

    def test_resume_when_not_paused_is_a_noop(self):
        entered, release = threading.Event(), threading.Event()
        s = make_engine(ScriptedLLM(on_worker=lambda tid, m: (entered.set(), release.wait(5)) if tid == "t1" else None))
        s.start()
        entered.wait(5)
        r = s.post_command("resume")
        self.assertFalse(r["applied"])
        release.set()
        s.join(10)

    def test_stop_while_paused(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "x" not in holder:
                holder["x"] = 1
                holder["s"].post_command("pause")

        s = make_engine(ScriptedLLM(on_worker=hook))
        holder["s"] = s
        s.start()
        while not of(s.log.read(), EV.RUN_PAUSED):
            time.sleep(0.02)
        s.post_command("stop")
        s.join(5)
        ev = s.log.read()
        self.assertEqual(ev[-1].data["status"], "stopped")
        assert_valid(self, ev)


class UserMessages(unittest.TestCase):
    def test_message_to_a_running_agent_arrives_at_its_next_step_and_becomes_a_decision(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t2" and "sent" not in holder:
                holder["sent"] = True
                holder["s"].post_message("a_t2", "please use port 8080")

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        s.join(10)
        ev = s.log.read()
        assert_valid(self, ev)
        second = llm.kinds("worker", "t2")[1][2]
        self.assertTrue(any(m["role"] == "user" and "Instruction from the user (you have already replied to them): please use port 8080"
                            in m["content"] for m in second))
        d = of(ev, EV.DECISION)
        self.assertEqual((d[0].data["source"], d[0].data["text"]), ("user", "please use port 8080"))
        self.assertIn("please use port 8080", s.ws.decisions_path.read_text())
        chat = of(ev, EV.AGENT_MESSAGE, agent_id="a_t2")
        self.assertEqual([m.data["role"] for m in chat][:2], ["user", "agent"])          # the message and the ack

    def test_message_to_a_queued_agent_is_in_its_first_prompt(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t1" and "sent" not in holder:
                holder["sent"] = True
                holder["s"].post_message("a_t3", "make the page dark")

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm)
        holder["s"] = s
        s.start()
        s.join(10)
        first = llm.kinds("worker", "t3")[0][2]
        self.assertIn("make the page dark", first[1]["content"])

    def test_message_to_the_lead_reaches_the_review_as_an_instruction(self):
        holder = {}

        def hook(tid, msgs):
            if tid == "t1" and "sent" not in holder:
                holder["sent"] = True
                holder["s"].post_message("orchestrator", "skip the frontend")

        llm = ScriptedLLM(on_worker=hook)
        s = make_engine(llm, review=True)
        holder["s"] = s
        s.start()
        s.join(10)
        review = llm.kinds("review")[0][2][-1]["content"]
        self.assertIn("Messages from the user", review)
        self.assertIn("skip the frontend", review)

    def test_validation(self):
        s = make_engine(ScriptedLLM())
        s.start()
        s.join(10)                                    # run is over
        with self.assertRaises(SessionError) as cm:
            s.post_message("a_t1", "hello")
        self.assertEqual(cm.exception.status, 409)
        s2 = make_engine(ScriptedLLM(on_worker=lambda t, m: time.sleep(0.05)))
        s2.start()
        time.sleep(0.2)
        for target, text, status in (("ghost", "x", 404), ("orchestrator", "   ", 400), ("orchestrator", "x" * 5000, 400)):
            with self.assertRaises(SessionError) as cm:
                s2.post_message(target, text)
            self.assertEqual(cm.exception.status, status, (target, text[:5]))
        with self.assertRaises(SessionError):
            s2.post_command("explode")
        s2.stop()


if __name__ == "__main__":
    unittest.main()
