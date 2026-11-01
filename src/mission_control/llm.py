"""LLM access.

* `LLMClient` is the only thing the engine knows about models. Swapping the model (or
  mixing models per role later) means writing another class with a `chat` method.
* `OllamaClient` talks to a local Ollama server over plain HTTP (stdlib only).
* `generate_structured` is the safety net for small models: constrain output with a JSON
  schema, validate it, and on failure send the concrete errors back and retry.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

DEFAULT_MODEL = "ornith-1.5:9b"
DEFAULT_HOST = "http://localhost:11434"


class LLMError(RuntimeError):
    """Transport / server problems (Ollama down, model missing, bad HTTP status)."""


# What went wrong decides what to do about it (seen in real runs, see docs/FAILURES.md):
#   transient  - the GPU/runner hiccuped ("CUDA error ... illegal memory access"): wait, call again
#   truncated  - the answer was cut off mid tool call ("unexpected end of JSON input"): the context or the
#                output budget ran out; shrink the history and ask for smaller pieces, then call again
#   fatal      - retrying cannot help (model not installed, not enough memory to load it)
_TRUNCATED = ("invalid tool call arguments", "unexpected end of json input", "unexpected eof while parsing")
_FATAL = ("not found", "requires more system memory", "no such model", "unauthorized", "does not support tools")
_TRANSIENT = ("cuda error", "illegal memory access", "llama runner", "runner process", "connection", "timed out",
              "timeout", "temporarily", "server busy", "http 502", "http 503", "http 504", "eof", "reset by peer",
              "broken pipe", "cannot reach ollama")


def classify_llm_error(err: BaseException | str) -> str:
    text = str(err).lower()
    if any(k in text for k in _TRUNCATED):
        return "truncated"
    if any(k in text for k in _FATAL):
        return "fatal"
    if any(k in text for k in _TRANSIENT):
        return "transient"
    return "fatal" if "http 4" in text else "transient"


class StructuredOutputError(RuntimeError):
    def __init__(self, errors: list[str], attempts: int) -> None:
        super().__init__(f"no valid output after {attempts} attempt(s): " + "; ".join(errors))
        self.errors = errors
        self.attempts = attempts


@dataclass
class ChatResult:
    content: str
    thinking: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    eval_duration_ns: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    done_reason: str = ""          # "stop" normally; "length" = cut off by the context/output budget

    @property
    def tokens_per_second(self) -> float:
        return self.tokens_out / (self.eval_duration_ns / 1e9) if self.eval_duration_ns else 0.0


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ChatResult: ...


class OllamaClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str | None = None,
        *,
        timeout: float = 600.0,
        num_ctx: int | None = None,
        think: bool | None = None,
    ) -> None:
        host = host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.think = think

    # ---------------------------------------------------------------- plumbing
    def _payload(self, messages, *, schema, tools, temperature, stream) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": stream}
        if schema is not None:
            payload["format"] = schema
        if tools:
            payload["tools"] = tools
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if options:
            payload["options"] = options
        if self.think is not None:
            payload["think"] = self.think
        return payload

    def _open(self, path: str, payload: dict[str, Any] | None):
        url = self.host + path
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:                                   # Ollama wraps the real reason in {"error": "..."}: show the reason
                body = str(json.loads(body).get("error") or body)
            except (ValueError, AttributeError):
                pass
            raise LLMError(f"Ollama returned HTTP {e.code} for {path}: {body[:500]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"cannot reach Ollama at {self.host} ({e}). Is `ollama serve` running?") from e

    # --------------------------------------------------------------------- API
    def list_models(self) -> list[str]:
        return [m["name"] for m in self.list_models_detailed()]

    def list_models_detailed(self) -> list[dict[str, Any]]:
        """[{name, parameter_size, family}] - parameter_size like "9.2B" drives the automatic strength profile."""
        with self._open("/api/tags", None) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for m in data.get("models", []):
            d = m.get("details") or {}
            out.append({"name": m.get("name", ""), "parameter_size": d.get("parameter_size", ""), "family": d.get("family", "")})
        return out

    def show(self, model: str | None = None) -> dict[str, Any]:
        """Model facts that matter to the engine: maximum context length and capabilities (tools, thinking)."""
        with self._open("/api/show", {"model": model or self.model}) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ctx = 0
        for k, v in (data.get("model_info") or {}).items():
            if k.endswith(".context_length") and isinstance(v, int):
                ctx = v
        return {"context_length": ctx, "capabilities": list(data.get("capabilities") or [])}

    def chat(self, messages, *, schema=None, tools=None, temperature=None) -> ChatResult:
        payload = self._payload(messages, schema=schema, tools=tools, temperature=temperature, stream=False)
        with self._open("/api/chat", payload) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _to_result(data)

    def stream_chat(self, messages, *, schema=None, tools=None, temperature=None) -> Iterator[dict[str, Any]]:
        """Yield {'content','thinking','tool_calls','done', ...} chunks as they arrive.
        The last chunk (done=True) also carries token counts. Used for live typing in the UI."""
        payload = self._payload(messages, schema=schema, tools=tools, temperature=temperature, stream=True)
        with self._open("/api/chat", payload) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                data = json.loads(line)
                if "error" in data:
                    raise LLMError(f"Ollama stream error: {data['error']}")
                msg = data.get("message") or {}
                chunk = {
                    "content": msg.get("content", ""),
                    "thinking": msg.get("thinking", ""),
                    "tool_calls": msg.get("tool_calls") or [],
                    "done": bool(data.get("done")),
                }
                if chunk["done"]:
                    chunk.update(
                        tokens_in=int(data.get("prompt_eval_count", 0)),
                        tokens_out=int(data.get("eval_count", 0)),
                        eval_duration_ns=int(data.get("eval_duration", 0)),
                    )
                yield chunk


def _to_result(data: dict[str, Any]) -> ChatResult:
    if "error" in data:
        raise LLMError(f"Ollama error: {data['error']}")
    msg = data.get("message") or {}
    return ChatResult(
        content=msg.get("content", "") or "",
        thinking=msg.get("thinking") or None,
        tool_calls=list(msg.get("tool_calls") or []),
        tokens_in=int(data.get("prompt_eval_count", 0)),
        tokens_out=int(data.get("eval_count", 0)),
        eval_duration_ns=int(data.get("eval_duration", 0)),
        raw=data,
        done_reason=str(data.get("done_reason", "") or ""),
    )


# ------------------------------------------------------------ structured output

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Tolerant JSON extraction: strips <think> blocks and code fences, then parses the
    first JSON value found. Raises ValueError if nothing parses."""
    cleaned = _FENCE_RE.sub("", _THINK_RE.sub("", text)).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(cleaned[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value found in the model output")


@dataclass
class StructuredResult:
    value: Any
    attempts: int
    tokens_in: int
    tokens_out: int


def generate_structured(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
    validate: Callable[[Any], list[str]],
    retries: int = 2,
    temperature: float | None = 0.2,
) -> StructuredResult:
    """Ask for JSON matching `schema`; if parsing or `validate` fails, feed the exact errors
    back to the model and retry (up to `retries` extra attempts)."""
    msgs = list(messages)
    tin = tout = 0
    errors: list[str] = []
    for attempt in range(1, retries + 2):
        res = llm.chat(msgs, schema=schema, temperature=temperature)
        tin += res.tokens_in
        tout += res.tokens_out
        try:
            value = extract_json(res.content)
        except ValueError as e:
            errors = [f"output was not valid JSON ({e})"]
        else:
            errors = validate(value)
            if not errors:
                return StructuredResult(value, attempt, tin, tout)
        msgs = msgs + [
            {"role": "assistant", "content": res.content},
            {
                "role": "user",
                "content": "Your previous JSON was rejected for these reasons:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\nReturn a corrected JSON object only, with no commentary.",
            },
        ]
    raise StructuredOutputError(errors, retries + 1)
