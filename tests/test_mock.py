import tempfile
import unittest
from pathlib import Path

from mission_control.eventlog import read_events
from mission_control.events import T, check_stream
from mission_control.mock import build_mock_run


class MockRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "mock.jsonl"
        cls.events = build_mock_run(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def types(self, t):
        return [e for e in self.events if e.type == t]

    def test_stream_is_fully_consistent(self):
        self.assertEqual(check_stream(self.events), [])

    def test_file_on_disk_equals_returned_events(self):
        self.assertEqual(read_events(self.path), self.events)

    def test_deterministic(self):
        again = build_mock_run(Path(self.tmp.name) / "again.jsonl")
        self.assertEqual([e.to_json() for e in again], [e.to_json() for e in self.events])

    def test_timestamps_never_go_backwards(self):
        ts = [e.ts for e in self.events]
        self.assertEqual(ts, sorted(ts))

    def test_every_ui_state_is_exercised(self):
        for t in (T.PLAN_CREATED, T.PLAN_UPDATED, T.RUN_PAUSED, T.RUN_RESUMED, T.DECISION, T.ERROR,
                  T.FILE_CHUNK, T.USER_COMMAND, T.USAGE, T.RUN_FINISHED):
            self.assertTrue(self.types(t), t)
        statuses = {e.data["status"] for e in self.types(T.TASK_STATUS)}
        self.assertEqual(statuses, {"running", "done", "verified", "failed"})
        agent_statuses = {e.data["status"] for e in self.types(T.AGENT_STATUS)}
        self.assertEqual(agent_statuses, {"running", "paused", "done", "error"})

    def test_every_task_ends_verified(self):
        last = {}
        for e in self.types(T.TASK_STATUS):
            last[e.task_id] = e.data["status"]
        self.assertEqual(set(last), {"t1", "t2", "t3", "t4", "t5", "t6"})
        self.assertEqual(set(last.values()), {"verified"})

    def test_there_is_real_parallelism(self):
        """backend and frontend must be 'running' at the same moment."""
        running, overlap = set(), False
        for e in self.events:
            if e.type == T.AGENT_STATUS:
                (running.add if e.data["status"] == "running" else running.discard)(e.agent_id)
                if {"a_backend", "a_frontend"} <= running:
                    overlap = True
        self.assertTrue(overlap)

    def test_pause_is_soft_and_run_paused_means_everyone_stopped(self):
        cmd = next(e.seq for e in self.events if e.type == T.USER_COMMAND and e.data["command"] == "pause")
        paused = next(e.seq for e in self.events if e.type == T.RUN_PAUSED)
        resumed = next(e.seq for e in self.events if e.type == T.RUN_RESUMED)
        self.assertLess(cmd, paused)
        self.assertLess(paused, resumed)

        open_before_request = set()
        running = set()
        for e in self.events:
            if e.seq < cmd:
                if e.type == T.FILE_CREATED:
                    open_before_request.add((e.agent_id, e.data["path"]))
                elif e.type == T.FILE_SAVED:
                    open_before_request.discard((e.agent_id, e.data["path"]))
            if e.type == T.AGENT_STATUS:
                (running.add if e.data["status"] == "running" else running.discard)(e.agent_id)
                if e.data["status"] == "paused":
                    running.add(("paused", e.agent_id))
            # between the request and the full stop: in-flight writes may finish, nothing new may start
            if cmd < e.seq < paused:
                self.assertNotEqual(e.type, T.FILE_CREATED, f"new file started after pause request (seq {e.seq})")
                if e.type == T.FILE_CHUNK:
                    self.assertIn((e.agent_id, e.data["path"]), open_before_request)
            # once run.paused is announced the whole system is idle
            if paused < e.seq < resumed:
                self.assertNotIn(e.type, (T.FILE_CREATED, T.FILE_CHUNK, T.FILE_SAVED, T.TASK_STATUS),
                                 f"activity while fully paused (seq {e.seq})")

        # every agent that was working must have reported `paused` before run.paused
        states = {}
        for e in self.events:
            if e.seq >= paused:
                break
            if e.type == T.AGENT_STATUS:
                states[e.agent_id] = e.data["status"]
        self.assertEqual({a for a, st in states.items() if st == "running"}, set())

    def test_every_language_tells_the_same_story(self):
        """Languages differ in words only. Compared per actor (each agent, each task, the run itself): the same events
        in the same order. The global interleaving of PARALLEL agents may differ because text length changes timing,
        and file.chunk events are excluded for the same reason."""
        from collections import defaultdict
        from mission_control.mock import TEXTS
        self.assertEqual(set(TEXTS["en"]), set(TEXTS["fa"]))
        fa = build_mock_run(Path(self.tmp.name) / "fa.jsonl", lang="fa")
        self.assertEqual(check_stream(fa), [])

        def stories(evs):
            out = defaultdict(list)
            for e in evs:
                if e.type != T.FILE_CHUNK:
                    out[("agent", e.agent_id) if e.agent_id else ("task", e.task_id) if e.task_id else ("run",)].append(
                        (e.type, e.data.get("status"), e.data.get("path"), e.data.get("command")))
            return dict(out)

        a, b = stories(self.events), stories(fa)
        self.assertEqual(set(a), set(b))
        for key in a:
            self.assertEqual(a[key], b[key], key)

    def test_each_language_is_really_in_that_language(self):
        import re
        fa = build_mock_run(Path(self.tmp.name) / "fa2.jsonl", lang="fa")
        persian = re.compile("[\u0600-\u06ff]")
        self.assertFalse(any(persian.search(e.data.get("text", "") or "") for e in self.events if e.type == T.AGENT_MESSAGE),
                         "the English demo contains Persian text")
        self.assertTrue(any(persian.search(e.data.get("text", "") or "") for e in fa if e.type == T.AGENT_MESSAGE))

    def test_usage_is_cumulative(self):
        u = [(e.data["tokens_in"], e.data["tokens_out"]) for e in self.types(T.USAGE)]
        self.assertEqual(u, sorted(u))
        self.assertGreater(u[-1][0], u[0][0])

    def test_streamed_chunks_reassemble_to_final_file_size(self):
        buf, sizes = {}, {}
        for e in self.events:
            key = (e.agent_id, e.data.get("path"))
            if e.type == T.FILE_CREATED:
                buf[key] = ""
            elif e.type == T.FILE_CHUNK:
                buf[key] += e.data["text"]
            elif e.type == T.FILE_SAVED:
                self.assertEqual(len(buf[key].encode("utf-8")), e.data["size"], key)


if __name__ == "__main__":
    unittest.main()
