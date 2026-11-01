import unittest

from mission_control.plan import (
    Plan, PlanError, Task, diff_plans, patterns_overlap, path_allowed, plan_json_schema, plan_stages,
    replan_json_schema, validate_plan, validate_replan,
)


def task(id, deps=(), owns=(), acc=("ok",), **kw):
    return Task(id=id, title=kw.get("title", id), role=kw.get("role", "dev"), goal=kw.get("goal", "g"),
                depends_on=tuple(deps), owns=tuple(owns), acceptance=tuple(acc))


def plan(*tasks):
    return Plan(1, tuple(tasks))


class PatternOverlap(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(patterns_overlap("a/b.py", "a/b.py"))

    def test_disjoint_dirs(self):
        self.assertFalse(patterns_overlap("backend/**", "frontend/**"))

    def test_nested_globs(self):
        self.assertTrue(patterns_overlap("frontend/**", "frontend/css/**"))

    def test_literal_inside_glob(self):
        self.assertTrue(patterns_overlap("frontend/index.html", "frontend/**"))
        self.assertFalse(patterns_overlap("backend/app.py", "frontend/**"))

    def test_literal_dir_vs_file(self):
        self.assertTrue(patterns_overlap("backend", "backend/app.py"))
        self.assertFalse(patterns_overlap("backend", "backend2/app.py"))

    def test_partial_name_glob(self):
        self.assertFalse(patterns_overlap("docs/**", "doc/**"))


class Validation(unittest.TestCase):
    def test_valid_plan(self):
        p = plan(task("t1", owns=["contracts/**"]),
                 task("t2", ["t1"], ["backend/**"]),
                 task("t3", ["t1"], ["frontend/**"]))
        self.assertEqual(validate_plan(p), [])

    def test_empty(self):
        self.assertEqual(validate_plan(plan()), ["plan has no tasks"])

    def test_duplicate_and_bad_ids(self):
        errs = validate_plan(plan(task("t1"), task("t1"), task("Bad Id")))
        self.assertTrue(any("duplicate" in e for e in errs))
        self.assertTrue(any("invalid" in e for e in errs))

    def test_unknown_dependency_and_self_dependency(self):
        errs = validate_plan(plan(task("t1", ["t1"]), task("t2", ["nope"])))
        self.assertTrue(any("depends on itself" in e for e in errs))
        self.assertTrue(any("unknown task 'nope'" in e for e in errs))

    def test_cycle(self):
        errs = validate_plan(plan(task("a", ["b"]), task("b", ["c"]), task("c", ["a"])))
        self.assertTrue(any("cycle" in e for e in errs))

    def test_missing_acceptance(self):
        errs = validate_plan(plan(task("t1", acc=())))
        self.assertTrue(any("acceptance" in e for e in errs))

    def test_unsafe_owns(self):
        errs = validate_plan(plan(task("a", owns=["/etc/passwd"]), task("b", ["a"], ["../x"]),
                                  task("c", ["b"], [".mission/plan.json"])))
        self.assertEqual(len([e for e in errs if "owns" in e]), 3)

    def test_parallel_overlap_is_rejected(self):
        p = plan(task("t1"), task("t2", ["t1"], ["app/**"]), task("t3", ["t1"], ["app/ui/**"]))
        errs = validate_plan(p)
        self.assertEqual(len(errs), 1)
        self.assertIn("may run in parallel", errs[0])

    def test_sequential_overlap_is_allowed(self):
        p = plan(task("t1", owns=["site/**"]), task("t2", ["t1"], ["site/index.html"]))
        self.assertEqual(validate_plan(p), [])

    def test_transitive_dependency_counts_as_ordered(self):
        p = plan(task("a", owns=["x/**"]), task("b", ["a"]), task("c", ["b"], ["x/y.txt"]))
        self.assertEqual(validate_plan(p), [])

    def test_too_many_tasks(self):
        tasks = [task(f"t{i}") for i in range(13)]
        self.assertTrue(any("maximum" in e for e in validate_plan(plan(*tasks))))


class Structure(unittest.TestCase):
    def test_stages(self):
        p = plan(task("t1"), task("t2", ["t1"]), task("t3", ["t1"]), task("t4", ["t2", "t3"]))
        self.assertEqual(plan_stages(p), [["t1"], ["t2", "t3"], ["t4"]])

    def test_stages_cycle_raises(self):
        with self.assertRaises(PlanError):
            plan_stages(plan(task("a", ["b"]), task("b", ["a"])))

    def test_roundtrip(self):
        p = plan(task("t1", owns=["a/**"]), task("t2", ["t1"]))
        self.assertEqual(Plan.from_dict(p.to_dict()), p)

    def test_malformed_input(self):
        for bad in ({}, {"tasks": "x"}, {"tasks": [1]}, {"tasks": [{"id": "a"}]},
                    {"tasks": [{"id": "a", "title": "t", "role": "r", "goal": "g", "owns": "oops"}]}):
            with self.assertRaises(PlanError):
                Plan.from_dict(bad)

    def test_diff(self):
        a = plan(task("t1"), task("t2"))
        b = plan(task("t1", title="changed"), task("t3"))
        self.assertEqual(diff_plans(a, b), {"added": ["t3"], "removed": ["t2"], "changed": ["t1"]})

    def test_schema_requires_all_task_fields(self):
        req = plan_json_schema()["properties"]["tasks"]["items"]["required"]
        self.assertEqual(set(req), {"id", "title", "role", "goal", "depends_on", "owns", "acceptance"})



class WriteTimeOwnership(unittest.TestCase):
    def test_glob_semantics(self):
        self.assertTrue(path_allowed("backend/app.py", ["backend/**"]))
        self.assertTrue(path_allowed("backend/a/b/c.py", ["backend/**"]))
        self.assertFalse(path_allowed("frontend/app.py", ["backend/**"]))
        self.assertFalse(path_allowed("backend2/app.py", ["backend/**"]))
        self.assertFalse(path_allowed("backend", ["backend/**"]))           # the folder itself is not a file to write

    def test_single_star_stays_in_one_folder(self):
        self.assertTrue(path_allowed("src/a.py", ["src/*.py"]))
        self.assertFalse(path_allowed("src/sub/a.py", ["src/*.py"]))
        self.assertTrue(path_allowed("src/sub/a.py", ["src/**/*.py"]))
        self.assertTrue(path_allowed("src/a.py", ["src/**/*.py"]))          # '**/' may match zero folders

    def test_literals_cover_file_or_folder(self):
        self.assertTrue(path_allowed("README.md", ["README.md"]))
        self.assertTrue(path_allowed("docs/a/b.md", ["docs"]))
        self.assertFalse(path_allowed("docsx/a.md", ["docs"]))
        self.assertFalse(path_allowed("README.md.bak", ["README.md"]))

    def test_nothing_owned_means_nothing_allowed(self):
        self.assertFalse(path_allowed("a.txt", []))
        self.assertFalse(path_allowed("a.txt", [""]))

    def test_windows_separators_and_slashes_are_normalised(self):
        self.assertTrue(path_allowed("backend\\app.py", ["backend/**"]))
        self.assertTrue(path_allowed("/backend/app.py", ["backend/**"]))

    def test_regex_metacharacters_in_names_are_literal(self):
        self.assertTrue(path_allowed("a+b/c.py", ["a+b/**"]))
        self.assertFalse(path_allowed("aab/c.py", ["a+b/**"]))


class Replanning(unittest.TestCase):
    def base(self):
        return plan(task("t1", owns=["c/**"]), task("t2", ["t1"], ["backend/**"]), task("t3", ["t1"], ["frontend/**"]))

    ST = {"t1": "verified", "t2": "running", "t3": "pending"}

    def test_adding_and_editing_pending_work_is_fine(self):
        old = self.base()
        new = plan(*old.tasks[:2], Task("t3", "new title", "dev", "g2", ("t1",), ("frontend/**",), ("ok",)),
                   task("t4", ["t3"], ["docs/**"]))
        self.assertEqual(validate_replan(old, new, self.ST), [])

    def test_removing_pending_work_is_fine(self):
        old = self.base()
        self.assertEqual(validate_replan(old, plan(*old.tasks[:2]), self.ST), [])

    def test_history_is_frozen(self):
        old = self.base()
        changed = Task("t1", "other", "dev", "g", (), ("c/**",), ("ok",))
        errs = validate_replan(old, plan(changed, *old.tasks[1:]), self.ST)
        self.assertTrue(any("'t1' is already verified and cannot be modified" in e for e in errs))
        errs = validate_replan(old, plan(old.tasks[0], old.tasks[2]), self.ST)       # t2 is running
        self.assertTrue(any("'t2' is already running and cannot be removed" in e for e in errs))

    def test_failed_work_is_frozen_too(self):
        old = self.base()
        errs = validate_replan(old, plan(*old.tasks[:2]), {**self.ST, "t3": "failed"})
        self.assertTrue(any("'t3' is already failed" in e for e in errs))

    def test_the_new_plan_must_itself_be_valid(self):
        old = self.base()
        bad = plan(*old.tasks, task("t4", ["nope"]))
        self.assertTrue(any("unknown task 'nope'" in e for e in validate_replan(old, bad, self.ST)))
        clash = plan(*old.tasks[:2], task("t3", ["t1"], ["backend/x/**"]))             # parallel with running t2
        self.assertTrue(any("may run in parallel" in e for e in validate_replan(old, clash, self.ST)))

    def test_schema_requires_action_reason_tasks(self):
        sch = replan_json_schema()
        self.assertEqual(set(sch["required"]), {"action", "reason", "tasks"})
        self.assertEqual(sch["properties"]["action"]["enum"], ["keep", "update"])


if __name__ == "__main__":
    unittest.main()
