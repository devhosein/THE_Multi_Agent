"""The engine driven through the REAL OllamaClient over a REAL socket.

A tiny fake Ollama server speaks Ollama's HTTP protocol (/api/tags, /api/chat) and is backed by the scripted
model. Everything between "engine" and "model" is therefore the production code path: request payloads
(tools, format schemas, tool messages), response parsing (tool_calls, token counts), error handling."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mission_control.engine import EngineConfig, EngineSession
from mission_control.events import T, check_stream
from mission_control.llm import OllamaClient
from mission_control.workspace import init_workspace

from .helpers import ScriptedLLM, call, happy_workers, T2


class FakeOllama:
    def __init__(self, llm):
        outer = self
        self.llm, self.requests, self.fail_next = llm, [], 0
        self.fail_message = "model runner crashed"

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json({"models": [{"name": "test-model:latest"}]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(payload)
                if outer.fail_next:
                    outer.fail_next -= 1
                    return self._json({"error": outer.fail_message}, 500)
                res = outer.llm.chat(payload["messages"], schema=payload.get("format"), tools=payload.get("tools"))
                msg = {"role": "assistant", "content": res.content}
                if res.tool_calls:
                    msg["tool_calls"] = [{"function": {**c["function"], "index": i}} for i, c in enumerate(res.tool_calls)]
                self._json({"message": msg, "prompt_eval_count": res.tokens_in, "eval_count": res.tokens_out, "eval_duration": 10**9})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OverHttp(unittest.TestCase):
    def run_session(self, llm, **cfg):
        fake = FakeOllama(llm)
        self.addCleanup(fake.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = init_workspace(Path(tmp.name) / "p")
        client = OllamaClient("test-model", fake.host, timeout=10, num_ctx=4096)
        s = EngineSession(ws, "Build a cafe site", client, model="test-model",
                          config=EngineConfig(chunk_delay_cap=0, **cfg))
        s.start()
        s.join(30)
        self.assertFalse(s._thread.is_alive())
        return s, s.log.read(), fake

    def test_full_run_over_the_wire(self):
        s, ev, fake = self.run_session(ScriptedLLM(), review=True)
        self.assertEqual(check_stream(ev), [])
        self.assertEqual(ev[-1].data["status"], "completed")
        self.assertTrue((s.ws.root / "backend" / "app.py").is_file())

    def test_what_actually_goes_over_the_wire(self):
        _, _, fake = self.run_session(ScriptedLLM(), review=True)
        planner, workers, reviews = [], [], []
        for p in fake.requests:
            if p.get("tools"):
                workers.append(p)
            elif "action" in p["format"]["properties"]:
                reviews.append(p)
            else:
                planner.append(p)
        self.assertEqual((len(planner), len(workers), len(reviews)), (1, 6, 2))
        self.assertEqual(planner[0]["format"]["required"], ["tasks"])                      # constrained to the plan schema
        self.assertEqual(planner[0]["options"]["num_ctx"], 4096)
        self.assertFalse(planner[0]["stream"])
        names = {t["function"]["name"] for t in workers[0]["tools"]}
        self.assertEqual(names, {"list_files", "read_file", "write_file", "append_file", "note_to_lead", "finish"})
        # the second worker request carries the first tool result back to the model
        second = [p for p in workers if len(p["messages"]) > 2][0]["messages"]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "write_file")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_name"], "write_file")
        self.assertTrue(second[-1]["content"].startswith("OK: wrote"))

    def test_a_server_error_mid_run_is_retried_without_losing_the_attempt(self):
        """The real CUDA crash from test 2: one HTTP 500 in the middle of a task. It must cost a few seconds, not an attempt."""
        fake_llm = ScriptedLLM()
        fake = FakeOllama(fake_llm)
        self.addCleanup(fake.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = init_workspace(Path(tmp.name) / "p")
        waits = []
        s = EngineSession(ws, "x", OllamaClient("test-model", fake.host, timeout=10), model="test-model",
                          config=EngineConfig(review=False, chunk_delay_cap=0), sleep=waits.append)
        orig = fake_llm.chat

        def chat(messages, **kw):
            if kw.get("tools") and not getattr(chat, "tripped", False):
                chat.tripped = True
                fake.fail_next = 1
                fake.fail_message = "an error was encountered while running the model: CUDA error: an illegal memory access was encountered"
            return orig(messages, **kw)

        fake_llm.chat = chat
        s.start()
        s.join(30)
        ev = s.log.read()
        self.assertEqual(check_stream(ev), [])
        self.assertEqual([e for e in ev if e.type == T.ERROR], [])                           # no attempt was burned
        self.assertEqual(ev[-1].data["status"], "completed")
        warns = [e.data["message"] for e in ev if e.type == T.LOG and e.data["level"] == "warn"]
        self.assertTrue(any("CUDA error" in w and "retrying in 2s" in w for w in warns), warns)
        self.assertEqual(waits, [2.0])

    def test_a_cut_off_tool_call_is_recovered_with_smaller_pieces(self):
        """The real failure from tests 1 and 2: 'invalid tool call arguments ... unexpected end of JSON input'."""
        fake_llm = ScriptedLLM()
        fake = FakeOllama(fake_llm)
        self.addCleanup(fake.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = init_workspace(Path(tmp.name) / "p")
        s = EngineSession(ws, "x", OllamaClient("test-model", fake.host, timeout=10), model="test-model",
                          config=EngineConfig(review=False, chunk_delay_cap=0), sleep=lambda x: None)
        orig = fake_llm.chat

        def chat(messages, **kw):
            if kw.get("tools") and not getattr(chat, "tripped", False):
                chat.tripped = True
                fake.fail_next = 1
                fake.fail_message = 'llama-server returned invalid tool call arguments for "write_file": unexpected end of JSON input'
            return orig(messages, **kw)

        fake_llm.chat = chat
        s.start()
        s.join(30)
        ev = s.log.read()
        self.assertEqual([e for e in ev if e.type == T.ERROR], [])
        self.assertEqual(ev[-1].data["status"], "completed")
        self.assertTrue(any("cut off" in e.data["message"] for e in ev if e.type == T.LOG))
        hinted = [p for p in fake.requests if p.get("tools") and any("append_file for each next part" in m.get("content", "")
                                                                      for m in p["messages"])]
        self.assertTrue(hinted, "the retried call must tell the model to write in smaller parts")

    def test_the_server_down_from_the_start_fails_the_run_with_a_clear_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = init_workspace(Path(tmp.name) / "p")
        s = EngineSession(ws, "x", OllamaClient("test-model", "127.0.0.1:1", timeout=3), model="test-model",
                          config=EngineConfig(chunk_delay_cap=0))
        s.start()
        s.join(30)
        ev = s.log.read()
        self.assertEqual(ev[-1].data["status"], "failed")
        self.assertIn("valid plan", [e for e in ev if e.type == T.ERROR][0].data["message"])
        self.assertIn("Is `ollama serve` running", [e for e in ev if e.type == T.ERROR][0].data["message"])


if __name__ == "__main__":
    unittest.main()
