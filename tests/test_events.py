import tempfile
import threading
import unittest
from pathlib import Path

from mission_control.eventlog import EventLog, read_events, replay
from mission_control.events import Event, EventError, T, check_stream


class EventContract(unittest.TestCase):
    def mk(self, type_, data=None, agent=None, task=None, seq=1):
        return Event(seq=seq, ts="2026-01-01T00:00:00+00:00", run_id="r", type=type_,
                     data=data or {}, agent_id=agent, task_id=task)

    def test_unknown_type(self):
        self.assertTrue(self.mk("nope").validate())

    def test_missing_fields(self):
        self.assertTrue(self.mk(T.FILE_CHUNK, {"path": "a"}, agent="x").validate())  # no text
        self.assertTrue(self.mk(T.FILE_CHUNK, {"path": "a", "text": "t"}).validate())  # no agent
        self.assertTrue(self.mk(T.TASK_STATUS, {"status": "running"}).validate())  # no task

    def test_enums(self):
        self.assertTrue(self.mk(T.TASK_STATUS, {"status": "weird"}, task="t1").validate())
        self.assertTrue(self.mk(T.USER_COMMAND, {"command": "explode", "target": "all"}).validate())
        self.assertEqual(self.mk(T.TASK_STATUS, {"status": "verified"}, task="t1").validate(), [])

    def test_json_roundtrip_keeps_persian(self):
        e = self.mk(T.ORCH_STATUS, {"text": "در حال ساخت پلن"})
        self.assertIn("در حال ساخت پلن", e.to_json())
        self.assertEqual(Event.from_json(e.to_json()), e)

    def test_malformed_json(self):
        with self.assertRaises(EventError):
            Event.from_json("{not json")
        with self.assertRaises(EventError):
            Event.from_json('{"seq": 1}')


class Log(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "m" / "events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_assigns_sequence_and_reads_back(self):
        log = EventLog(self.path, "r1")
        a = log.append(T.ORCH_STATUS, {"text": "a"})
        b = log.append(T.ORCH_STATUS, {"text": "b"})
        self.assertEqual((a.seq, b.seq), (1, 2))
        self.assertEqual([e.data["text"] for e in log.read(after_seq=1)], ["b"])

    def test_invalid_event_is_rejected_and_not_written(self):
        log = EventLog(self.path, "r1")
        with self.assertRaises(EventError):
            log.append(T.TASK_STATUS, {"status": "running"})
        self.assertEqual(log.last_seq, 0)
        self.assertEqual(read_events(self.path), [])

    def test_resume_continues_sequence(self):
        EventLog(self.path, "r1").append(T.ORCH_STATUS, {"text": "a"})
        again = EventLog(self.path, "r1")
        self.assertEqual(again.append(T.ORCH_STATUS, {"text": "b"}).seq, 2)

    def test_torn_tail_is_repaired(self):
        log = EventLog(self.path, "r1")
        log.append(T.ORCH_STATUS, {"text": "a"})
        with self.path.open("a", encoding="utf-8") as f:
            f.write('{"v":1,"seq":2,"ts":"x","run_id":"r1","ty')  # crash mid-write
        self.assertEqual(len(read_events(self.path)), 1)  # reader tolerates it
        again = EventLog(self.path, "r1")  # writer repairs it
        self.assertEqual(again.append(T.ORCH_STATUS, {"text": "b"}).seq, 2)
        self.assertEqual(len(read_events(self.path)), 2)

    def test_corruption_in_the_middle_raises(self):
        self.path.parent.mkdir(parents=True)
        good = Event(1, "2026-01-01T00:00:00+00:00", "r", T.ORCH_STATUS, {"text": "a"}).to_json()
        self.path.write_text(f"{good}\nGARBAGE\n{good}\n", encoding="utf-8")
        with self.assertRaises(EventError):
            read_events(self.path)

    def test_listeners_receive_events_in_order_and_cannot_break_the_log(self):
        log = EventLog(self.path, "r1")
        got = []
        log.add_listener(lambda e: got.append(e.seq))
        log.add_listener(lambda e: 1 / 0)  # broken subscriber
        unsub = log.add_listener(lambda e: got.append(-e.seq))
        log.append(T.ORCH_STATUS, {"text": "a"})
        unsub()
        log.append(T.ORCH_STATUS, {"text": "b"})
        self.assertEqual(got, [1, -1, 2])

    def test_concurrent_appends_keep_unique_sequence(self):
        log = EventLog(self.path, "r1")
        threads = [threading.Thread(target=lambda: [log.append(T.ORCH_STATUS, {"text": "x"}) for _ in range(25)])
                   for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([e.seq for e in read_events(self.path)], list(range(1, 201)))

    def test_replay_respects_timing_and_caps_gaps(self):
        evs = [Event(1, "2026-01-01T00:00:00+00:00", "r", T.ORCH_STATUS, {"text": "a"}),
               Event(2, "2026-01-01T00:00:01+00:00", "r", T.ORCH_STATUS, {"text": "b"}),
               Event(3, "2026-01-01T00:10:00+00:00", "r", T.ORCH_STATUS, {"text": "c"})]
        waits = []
        list(replay(evs, speed=2.0, max_gap=1.5, sleep=waits.append))
        self.assertEqual(waits, [0.5, 0.75])  # 1s/2, then the 10min gap capped to 1.5s/2
        waits.clear()
        list(replay(evs, speed=0, sleep=waits.append))
        self.assertEqual(waits, [])


class StreamCheck(unittest.TestCase):
    def build(self, *specs):
        out = []
        for i, (type_, data, agent, task) in enumerate(specs, 1):
            out.append(Event(i, "2026-01-01T00:00:00+00:00", "r", type_, data, agent, task))
        return out

    PLAN = (T.PLAN_CREATED, {"version": 1, "tasks": [{"id": "t1"}]}, None, None)
    AGENT = (T.AGENT_CREATED, {"role": "x", "model": "m", "title": "t", "task_ids": ["t1"]}, "a1", None)

    def test_clean_stream(self):
        evs = self.build(self.PLAN, self.AGENT,
                         (T.TASK_STATUS, {"status": "running"}, None, "t1"),
                         (T.AGENT_STATUS, {"status": "running"}, "a1", None),
                         (T.TASK_STATUS, {"status": "done"}, None, "t1"),
                         (T.TASK_STATUS, {"status": "verified"}, None, "t1"))
        self.assertEqual(check_stream(evs), [])

    def test_illegal_task_transition(self):
        evs = self.build(self.PLAN, (T.TASK_STATUS, {"status": "verified"}, None, "t1"))
        self.assertTrue(any("illegal transition pending->verified" in e for e in check_stream(evs)))

    def test_unknown_agent_and_task(self):
        evs = self.build(self.PLAN, (T.FILE_CREATED, {"path": "a"}, "ghost", None),
                         (T.TASK_STATUS, {"status": "running"}, None, "t9"))
        errs = check_stream(evs)
        self.assertTrue(any("unknown agent" in e for e in errs))
        self.assertTrue(any("unknown task" in e for e in errs))

    def test_agent_removal(self):
        evs = self.build(self.PLAN, self.AGENT, (T.AGENT_REMOVED, {}, "a1", None))
        self.assertEqual(check_stream(evs), [])
        # after removal the agent no longer exists
        evs = self.build(self.PLAN, self.AGENT, (T.AGENT_REMOVED, {}, "a1", None),
                         (T.AGENT_STATUS, {"status": "running"}, "a1", None))
        self.assertTrue(any("unknown agent" in e for e in check_stream(evs)))
        # unknown agents and the lead agent cannot be removed
        evs = self.build(self.PLAN, (T.AGENT_REMOVED, {}, "ghost", None), (T.AGENT_REMOVED, {}, "orchestrator", None))
        self.assertEqual(sum("cannot remove" in e for e in check_stream(evs)), 2)

    def test_seq_gap(self):
        evs = self.build(self.PLAN, self.AGENT)
        evs[1] = Event(5, evs[1].ts, "r", evs[1].type, evs[1].data, "a1")
        self.assertTrue(any("seq should be 2" in e for e in check_stream(evs)))


if __name__ == "__main__":
    unittest.main()
