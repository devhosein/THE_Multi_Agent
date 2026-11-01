import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from mission_control.llm import (
    ChatResult, LLMError, OllamaClient, StructuredOutputError, extract_json, generate_structured,
)


class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    reply: dict | None = None
    stream_lines: list | None = None
    status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "ornith-1.5:9b"}, {"name": "other:1b"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _Handler.seen.append((self.path, json.loads(self.rfile.read(n))))
        self.send_response(_Handler.status)
        self.end_headers()
        if _Handler.stream_lines is not None:
            for line in _Handler.stream_lines:
                self.wfile.write((json.dumps(line) + "\n").encode())
        else:
            self.wfile.write(json.dumps(_Handler.reply).encode())


class OllamaClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.client = OllamaClient(host=f"127.0.0.1:{cls.srv.server_port}", timeout=5)  # scheme added automatically

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Handler.seen.clear()
        _Handler.stream_lines = None
        _Handler.status = 200

    def test_chat_sends_schema_options_and_parses_tokens(self):
        _Handler.reply = {"message": {"role": "assistant", "content": '{"a":1}'},
                          "prompt_eval_count": 11, "eval_count": 22, "eval_duration": 2_000_000_000}
        c = OllamaClient(host=self.client.host, timeout=5, num_ctx=8192, think=False)
        res = c.chat([{"role": "user", "content": "hi"}], schema={"type": "object"}, temperature=0.1)
        path, payload = _Handler.seen[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(payload["model"], "ornith-1.5:9b")
        self.assertEqual(payload["format"], {"type": "object"})
        self.assertEqual(payload["options"], {"temperature": 0.1, "num_ctx": 8192})
        self.assertIs(payload["think"], False)
        self.assertFalse(payload["stream"])
        self.assertEqual((res.tokens_in, res.tokens_out), (11, 22))
        self.assertAlmostEqual(res.tokens_per_second, 11.0)

    def test_tool_calls_are_returned(self):
        call = {"function": {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}}
        _Handler.reply = {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}
        res = self.client.chat([{"role": "user", "content": "go"}], tools=[{"type": "function"}])
        self.assertEqual(res.tool_calls[0]["function"]["name"], "write_file")
        self.assertIn("tools", _Handler.seen[0][1])

    def test_optional_fields_are_omitted_when_unset(self):
        _Handler.reply = {"message": {"content": "ok"}}
        self.client.chat([{"role": "user", "content": "hi"}])
        payload = _Handler.seen[0][1]
        for key in ("format", "tools", "options", "think"):
            self.assertNotIn(key, payload)

    def test_streaming(self):
        _Handler.stream_lines = [
            {"message": {"content": "سل"}, "done": False},
            {"message": {"content": "ام"}, "done": False},
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 5, "eval_duration": 1},
        ]
        chunks = list(self.client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(c["content"] for c in chunks), "سلام")
        self.assertTrue(chunks[-1]["done"])
        self.assertEqual(chunks[-1]["tokens_out"], 5)
        self.assertTrue(_Handler.seen[0][1]["stream"])

    def test_http_error_becomes_llm_error(self):
        _Handler.status = 404
        _Handler.reply = {"error": "model not found"}
        with self.assertRaises(LLMError) as cm:
            self.client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("404", str(cm.exception))

    def test_error_field_in_200_response(self):
        _Handler.reply = {"error": "boom"}
        with self.assertRaises(LLMError):
            self.client.chat([{"role": "user", "content": "hi"}])

    def test_server_down(self):
        dead = OllamaClient(host="127.0.0.1:1", timeout=2)
        with self.assertRaises(LLMError) as cm:
            dead.chat([{"role": "user", "content": "hi"}])
        self.assertIn("ollama serve", str(cm.exception))

    def test_list_models(self):
        self.assertIn("ornith-1.5:9b", self.client.list_models())


class ExtractJson(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('<think>hmm {"x":0}</think>\n{"a": 2}'), {"a": 2})
        self.assertEqual(extract_json('Sure! Here it is: {"a": 3} hope it helps'), {"a": 3})
        self.assertEqual(extract_json("[1, 2]"), [1, 2])

    def test_failure(self):
        with self.assertRaises(ValueError):
            extract_json("no json here at all")


class _Stub:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = []

    def chat(self, messages, *, schema=None, tools=None, temperature=None):
        self.calls.append(messages)
        return ChatResult(content=self.contents.pop(0), tokens_in=10, tokens_out=5)


class StructuredGeneration(unittest.TestCase):
    validate = staticmethod(lambda v: [] if isinstance(v, dict) and v.get("ok") else ["missing ok"])

    def test_first_try(self):
        r = generate_structured(_Stub('{"ok": true}'), [], schema={}, validate=self.validate)
        self.assertEqual((r.attempts, r.tokens_in, r.tokens_out), (1, 10, 5))

    def test_repairs_after_bad_json_then_invalid_then_valid(self):
        stub = _Stub("garbage", '{"nope": 1}', '{"ok": 1}')
        r = generate_structured(stub, [{"role": "user", "content": "q"}], schema={}, validate=self.validate, retries=2)
        self.assertEqual(r.attempts, 3)
        self.assertEqual(r.tokens_in, 30)
        last = stub.calls[-1]
        self.assertIn("missing ok", last[-1]["content"])  # concrete errors are fed back
        self.assertEqual(last[-2], {"role": "assistant", "content": '{"nope": 1}'})
        # the retry must still carry the ORIGINAL request, plus 2 messages per failed attempt
        self.assertEqual(last[0], {"role": "user", "content": "q"})
        self.assertEqual([len(c) for c in stub.calls], [1, 3, 5])
        # earlier attempts are preserved in order, not overwritten
        self.assertEqual(last[1], {"role": "assistant", "content": "garbage"})

    def test_gives_up_after_retries(self):
        with self.assertRaises(StructuredOutputError) as cm:
            generate_structured(_Stub("x", "y", "z"), [], schema={}, validate=self.validate, retries=2)
        self.assertEqual(cm.exception.attempts, 3)


if __name__ == "__main__":
    unittest.main()
