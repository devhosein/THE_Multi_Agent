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
