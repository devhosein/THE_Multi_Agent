"""The engine runs the project after each task and at the end (integration.py), like a lead who tries the product.

Scenario from a real run (cafe-35b): the backend served only the API, the frontend called it from another origin
without CORS; every task was "verified" and the menu page stayed empty."""
import unittest

from mission_control.events import T as EV

from .helpers import FINISH, T, ScriptedLLM, assert_valid, call, run_engine
from .test_integration import APP_JS, PAGE, SERVER

PLAN = {"tasks": [T("t1", "Backend", "backend", owns=["backend/**"]),
                  T("t2", "Frontend", "frontend", ["t1"], ["index.html", "app.js", "style.css"])]}


def backend(pages=False, cors=False):
    return call("write_file", path="backend/server.py", content=SERVER.format(pages=pages, cors=cors, start="pass"))


def frontend(base="", js=None):
    return [call("write_file", path="index.html", content=PAGE.format(css="style.css", extra="")),
            call("write_file", path="style.css", content="body { color: #4a2c1a; }"),
            call("write_file", path="app.js", content=js if js is not None else APP_JS.format(base=base))]


class EngineIntegration(unittest.TestCase):
    def test_the_cross_origin_failure_from_the_real_run_is_found_and_fixed_by_a_new_task(self):
        plan = {"tasks": PLAN["tasks"] + [T("t3", "QA", "qa", ["t2"], ["tests/**"])]}
        llm = ScriptedLLM(plan=plan, workers={
            "t1": [backend(pages=False), FINISH],
            "t2": [*frontend(base="http://localhost:8000"), FINISH],
            "t3": [call("write_file", path="tests/notes.md", content="checked"), FINISH],
            # the fix task owns exactly the files involved: it makes the backend serve the pages, the frontend relative
            "fix1": [backend(pages=True), *frontend(base=""), FINISH]},
            notes={"reply": "", "decision": "The backend serves the pages; the frontend uses relative URLs like /api/menu.",
                   "replan": False, "reason": "the parts are on different origins"})
        s, evs = run_engine(llm, review=False, integration=True)
        fin = [e for e in evs if e.type == EV.RUN_FINISHED][0]
        self.assertEqual(fin.data["status"], "completed", fin.data)
        upd = [e for e in evs if e.type == EV.PLAN_UPDATED]
        self.assertEqual(upd[0].data["added"], ["fix1"])
        fix = next(t for t in upd[0].data["tasks"] if t["id"] == "fix1")
        self.assertEqual(sorted(fix["owns"]), ["app.js", "backend/server.py"])
        self.assertEqual(fix["depends_on"], ["t1", "t2"])
        self.assertIn("cross_origin", upd[0].data["reason"])
        self.assertIn("CORS", fix["goal"])
        self.assertIn("The backend serves the pages", fix["goal"] + " ".join(e.data["text"] for e in evs if e.type == EV.DECISION))
        self.assertEqual(s._status, {"t1": "verified", "t2": "verified", "fix1": "verified", "t3": "verified"})
        # the fix comes right after the frontend, before QA, and QA now waits for it (it tests the fixed project)
        self.assertEqual([t["id"] for t in upd[0].data["tasks"]], ["t1", "t2", "fix1", "t3"])
        self.assertIn("fix1", next(t for t in upd[0].data["tasks"] if t["id"] == "t3")["depends_on"])
        ran = [tid for k, tid, _ in llm.calls if k == "worker"]
        self.assertLess(ran.index("fix1"), ran.index("t3"))
        logs = [e.data for e in evs if e.type == EV.LOG and e.data["source"] == "integration"]
        self.assertIn("everything fits together", logs[-1]["message"])
        assert_valid(self, evs)

    def test_a_problem_inside_one_agents_own_files_goes_back_to_that_agent(self):
        broken = 'document.getElementById("menu-container").innerHTML = "x";'
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=True), FINISH],
            "t2": [*frontend(js=broken), FINISH,
                   call("write_file", path="app.js", content=APP_JS.format(base="")), FINISH]})
        s, evs = run_engine(llm, review=False, integration=True)
        self.assertEqual(s._status, {"t1": "verified", "t2": "verified"})
        retry = llm.kinds("worker", "t2")[-2][2][1]["content"]       # the second attempt's briefing
        self.assertIn("integration check", retry)
        self.assertIn("#menu-container", retry)
        self.assertFalse([e for e in evs if e.type == EV.PLAN_UPDATED], "no fix task for a problem the agent can fix")
        assert_valid(self, evs)

    def test_a_cosmetic_problem_is_sent_back_once_but_never_fails_the_task_or_the_run(self):
        """Live run cafe-35b-v5: the HTML used form-row, the CSS styled .form-group. The agent hears about it; if it
        cannot fix it, the site still works, so the task and the run pass with a warning."""
        unstyled = PAGE.format(css="style.css", extra='<div class="form-row"><p class="form-note">x</p></div>')
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=True), FINISH],
            "t2": [*frontend(), call("write_file", path="index.html", content=unstyled), FINISH, FINISH]})
        s, evs = run_engine(llm, review=False, integration=True)
        retry = llm.kinds("worker", "t2")[-1][2][1]["content"]
        self.assertIn("form-note, form-row", retry)                                    # told once, with the names
        self.assertEqual(s._status, {"t1": "verified", "t2": "verified"})
        fin = [e for e in evs if e.type == EV.RUN_FINISHED][0]
        self.assertEqual(fin.data["status"], "completed", fin.data)
        self.assertTrue(any("accepted with 1 minor problem(s) left" in e.data["message"] for e in evs if e.type == EV.LOG))
        self.assertFalse([e for e in evs if e.type == EV.PLAN_UPDATED], "no fix task for a cosmetic problem")
        assert_valid(self, evs)

    def test_problems_that_remain_make_the_run_fail_honestly(self):
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=False), FINISH],
            "t2": [*frontend(base="http://localhost:8000"), FINISH]})
        s, evs = run_engine(llm, review=False, integration=True, integration_fixes=0)   # no fix tasks allowed
        fin = [e for e in evs if e.type == EV.RUN_FINISHED][0]
        self.assertEqual(s._status, {"t1": "verified", "t2": "verified"})
        self.assertEqual(fin.data["status"], "failed", "every task verified is not enough: the parts must work together")
        self.assertIn("2 integration problem(s) remain", fin.data["summary"])
        assert_valid(self, evs)

    def test_a_fix_that_is_only_claimed_is_not_accepted(self):
        """Real run cafe-35b-v2: the fix agent said "fixed", the lead believed it, the server still 404'd every page."""
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=False), FINISH],
            "t2": [*frontend(base=""), FINISH],
            "fix1": [backend(pages=False), call("finish", summary="The backend now serves the pages."),
                     backend(pages=False), call("finish", summary="Fixed for real this time."),
                     backend(pages=False), call("finish", summary="Definitely fixed.")]})
        s, evs = run_engine(llm, review=False, integration=True, integration_fixes=1, max_attempts=1, recoveries=0)
        self.assertNotEqual(s._status["fix1"], "verified", "a claim is not a fix")
        failed = [e.data["message"] for e in evs if e.type == EV.LOG and e.data["source"] == "task" and "fix1" in e.data["message"]]
        self.assertTrue(failed and "relative" in failed[0], failed)
        self.assertEqual([e.data["status"] for e in evs if e.type == EV.RUN_FINISHED], ["failed"])

    def test_the_lead_may_not_delete_a_fix_task_while_its_problem_remains_and_sees_the_evidence(self):
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=False), FINISH],
            "t2": [*frontend(base=""), FINISH],
            "fix1": [backend(pages=True), FINISH]},
            reviews=[{"action": "keep", "reason": "fine", "tasks": []},
                     {"action": "update", "reason": "fix1 is redundant", "tasks": PLAN["tasks"]},   # tries to drop fix1
                     {"action": "keep", "reason": "ok", "tasks": []}])
        s, evs = run_engine(llm, review=True, integration=True)
        self.assertEqual(s._status.get("fix1"), "verified")
        rejected = [c for c in llm.kinds("review") if "may not be removed or changed" in c[2][-1]["content"]]
        self.assertTrue(rejected, "the attempt to drop the fix task must be refused and the reason fed back")
        with_evidence = [c[2][1]["content"] for c in llm.kinds("review")
                         if "Integration check (the engine ran the project" in c[2][1]["content"] and "[relative_api]" in c[2][1]["content"]]
        self.assertTrue(with_evidence, "the review after the problem appeared must carry it as evidence")
        self.assertIn("the server answered: GET / -> 404", with_evidence[0])

    def test_agents_can_run_the_project_check_themselves(self):
        llm = ScriptedLLM(plan=PLAN, workers={
            "t1": [backend(pages=True), call("check_project"), FINISH],
            "t2": [*frontend(base=""), call("check_project"), FINISH]})
        s, evs = run_engine(llm, review=False, integration=True)
        results = [m["content"] for _, tid, msgs in llm.kinds("worker", "t2") for m in msgs
                   if m.get("role") == "tool" and m.get("tool_name") == "check_project"]
        self.assertTrue(results)
        self.assertIn("Result of running the project", results[0])
        self.assertIn("everything fits together", results[0])

    def test_it_can_be_switched_off(self):
        llm = ScriptedLLM(plan=PLAN, workers={"t1": [backend(pages=False), FINISH],
                                              "t2": [*frontend(base="http://localhost:8000"), FINISH]})
        s, evs = run_engine(llm, review=False, integration=False)
        self.assertEqual([e.data["status"] for e in evs if e.type == EV.RUN_FINISHED], ["completed"])
        self.assertFalse([e for e in evs if e.type == EV.LOG and e.data["source"] == "integration"])


class Replies(unittest.TestCase):
    def test_an_answered_question_is_not_put_back_in_front_of_the_agent(self):
        """Real run: shown the question again, the agent answered it a second time."""
        from mission_control.worker import WorkerLimits, run_worker
        from mission_control.tools import ToolBox
        from mission_control.eventlog import EventLog
        from .helpers import make_engine
        s = make_engine(ScriptedLLM())
        seen = []

        class Model:
            def chat(self, messages, **kw):
                seen.append([dict(m) for m in messages])
                from mission_control.llm import ChatResult
                return ChatResult(content="", tool_calls=[call("finish", summary="ok")])

        box = ToolBox(s.ws, ["**"], EventLog(s.ws.events_path, "r"), "a1", chunk_delay_cap=0)
        inbox = [["Which port will the server use?"]]
        run_worker(Model(), box, "sys", "user", gate=lambda: None, deliver=lambda: inbox.pop() if inbox else [],
                   reply=lambda msgs, text: None, on_message=lambda t: None, on_usage=lambda a, b: None,
                   limits=WorkerLimits())
        after = seen[0][-1]["content"]
        self.assertNotIn("Which port", after)
        self.assertIn("Do not answer it again", after)


if __name__ == "__main__":
    unittest.main()
