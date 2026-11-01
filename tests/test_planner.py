import json
import unittest

from mission_control.llm import ChatResult, StructuredOutputError
from mission_control.plan import validate_plan
from mission_control.planner import build_planner_messages, make_plan

GOOD = {"tasks": [
    {"id": "t1", "title": "قرارداد", "role": "architect", "goal": "g", "depends_on": [],
     "owns": ["contracts/**"], "acceptance": ["contracts/api.md exists"]},
    {"id": "t2", "title": "بک‌اند", "role": "backend", "goal": "g", "depends_on": ["t1"],
     "owns": ["backend/**"], "acceptance": ["app starts"]},
    {"id": "t3", "title": "فرانت", "role": "frontend", "goal": "g", "depends_on": ["t1"],
     "owns": ["frontend/**"], "acceptance": ["page renders"]},
]}
OVERLAP = json.loads(json.dumps(GOOD))
OVERLAP["tasks"][2]["owns"] = ["backend/static/**"]  # parallel with t2 -> conflict


class Stub:
    def __init__(self, *contents):
        self.contents, self.calls = list(contents), []

    def chat(self, messages, *, schema=None, tools=None, temperature=None):
        self.calls.append((messages, schema))
        return ChatResult(content=self.contents.pop(0))


class PlannerTests(unittest.TestCase):
    def test_valid_plan_first_try(self):
        plan, res = make_plan(Stub(json.dumps(GOOD)), "build a site")
        self.assertEqual(res.attempts, 1)
        self.assertEqual([t.id for t in plan.tasks], ["t1", "t2", "t3"])
        self.assertEqual(validate_plan(plan), [])

    def test_conflict_is_fed_back_and_repaired(self):
        stub = Stub(json.dumps(OVERLAP), "```json\n" + json.dumps(GOOD) + "\n```")
        plan, res = make_plan(stub, "build a site")
        self.assertEqual(res.attempts, 2)
        feedback = stub.calls[1][0][-1]["content"]
        self.assertIn("may run in parallel", feedback)
        self.assertEqual(validate_plan(plan), [])

    def test_schema_is_sent_to_the_model(self):
        stub = Stub(json.dumps(GOOD))
        make_plan(stub, "x")
        self.assertIn("tasks", stub.calls[0][1]["properties"])

    def test_wrong_top_level_shape(self):
        with self.assertRaises(StructuredOutputError):
            make_plan(Stub("[1]", "[2]", "[3]"), "x")

    def test_messages_mention_input_files(self):
        msgs = build_planner_messages("سایت بساز", ["inputs/menu.pdf"])
        self.assertIn("inputs/menu.pdf", msgs[1]["content"])
        self.assertIn("سایت بساز", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
