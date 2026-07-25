#!/usr/bin/env python3
"""Model-backend stub for harness capture (ADR-0018 gate 1). Stdlib only.

Serves all three wire protocols from one process so a single stub can drive
every harness:

    POST /v1/messages          Anthropic Messages  — claude, and grok with
                                                     api_backend="messages"
    POST /v1/responses         OpenAI Responses    — codex
    POST /v1/chat/completions  OpenAI Chat         — grok default
    GET  /v1/models            model discovery     — grok requires it
    GET  /healthz              preflight

TWO jobs, and the second is why this exists at all:

1. **Deterministic failure injection.** A real backend cannot produce a 401, a
   429, a truncated stream or an empty completion on command; committed fixtures
   need exactly that reproducibility.

2. **A verbatim record of what each CLI actually sends.** `journal.jsonl` is the
   only way to answer "why does this CLI get prose from a model that
   demonstrably tool-calls" — you diff its real outbound request against one
   that works. It is also the anti-self-deception device: it proves a capture hit
   the stub rather than quietly reaching a real API.

Scenario selection, in precedence order:
    1. path prefix   /s/<scenario>/v1/...   ← load-bearing: grok and codex can
                                              only be handed a *base URL*, so
                                              this is the only way one long-lived
                                              stub serves a whole matrix
    2. header        X-Devcake-Scenario: <scenario>
    3. --scenario default (settable at runtime via POST /__mode)

Honesty rule: this answers HTTP and nothing else. Every committed fixture byte is
real CLI stdout. Never add a scenario a real backend could not produce.

    python3 stub_backend.py --port 8787 --journal /out/journal.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "stub-model"

SCENARIOS = (
    "healthy",       # a complete, ordinary turn
    "empty",         # 200 with no content at all — THE incident shape
    "whitespace",    # 200 with only whitespace text
    "refusal",       # a genuine refusal: MUST NOT be called a fault
    "tool_only",     # one tool call, no final text: MUST NOT be called a fault
    "loop",          # always a tool call — drives turn-cap exhaustion
    "http_400", "http_401", "http_429", "http_500",
    "truncated",     # headers, one event, then hang up mid-stream
    "no_route",      # 404 the protocol route (a backend lacking /v1/responses)
)

STATE = {"default": "healthy", "requests": 0, "by_scenario": {}, "tools_seen": [],
         "markers": []}
LOCK = threading.Lock()
JOURNAL = None

# [[MODE:x]] markers arrive inside mission text. Match only the FIRST one: the
# CLI resends the whole conversation and DevCake quotes feed content into later
# prompts, so a request carrying two markers is eventually inevitable.
MARKER = re.compile(r"\[\[MODE:([a-z_0-9]+)\]\]")


def journal(entry: dict) -> None:
    if not JOURNAL:
        return
    with LOCK:
        with open(JOURNAL, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def redact(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        out[k] = "<redacted>" if k.lower() in (
            "authorization", "x-api-key", "proxy-authorization") else v
    return out


def sse(*events) -> bytes:
    """Server-sent events. Both protocols use `event:`+`data:` framing."""
    buf = []
    for name, payload in events:
        buf.append(f"event: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(buf).encode()


# ── tool selection: read the request's OWN schema ────────────────────────────
# Never hardcode a tool name. The images are unpinned, so a hardcoded `Bash`
# silently stops working the day the CLI renames or reshapes its tools.

def pick_tool(body: dict, protocol: str):
    tools = body.get("tools") or []
    for t in tools:
        name = t.get("name") or (t.get("function") or {}).get("name")
        schema = (t.get("input_schema") or t.get("parameters")
                  or (t.get("function") or {}).get("parameters") or {})
        if not name:
            continue
        props = (schema.get("properties") or {})
        args = {}
        for req in (schema.get("required") or list(props)[:1]):
            spec = props.get(req) or {}
            kind = spec.get("type", "string")
            args[req] = ({"command": "echo working"}.get(req)
                         or ("stub" if kind == "string" else 1))
        return name, args
    return None, {}


def note_tools(body: dict) -> None:
    names = [t.get("name") or (t.get("function") or {}).get("name")
             for t in (body.get("tools") or [])]
    with LOCK:
        for n in names:
            if n and n not in STATE["tools_seen"]:
                STATE["tools_seen"].append(n)


def scenario_for(path_scenario, header_scenario, body: dict) -> str:
    if path_scenario in SCENARIOS:
        return path_scenario
    if header_scenario in SCENARIOS:
        return header_scenario
    m = MARKER.search(json.dumps(body)[:20000])   # first match only, bounded
    if m:
        with LOCK:
            STATE["markers"].append(m.group(1))
        if m.group(1) in SCENARIOS:
            return m.group(1)
    return STATE["default"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "devcake-stub/1"

    def log_message(self, *a):        # quiet; the journal is the record
        pass

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype="application/json", stream=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def _stream(self, code: int, chunks, truncate=False):
        """Chunked SSE. `truncate` hangs up mid-stream without a terminal event."""
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i, chunk in enumerate(chunks):
            if truncate and i >= 1:
                break
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        if not truncate:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        if truncate:
            self.close_connection = True

    def _route(self):
        """(scenario_from_path, normalized_path)"""
        path = self.path.split("?")[0]
        m = re.match(r"^/s/([a-z_0-9]+)(/.*)$", path)
        return (m.group(1), m.group(2)) if m else (None, path)

    # ── requests ─────────────────────────────────────────────────────────────
    def do_GET(self):
        _s, path = self._route()
        if path in ("/healthz", "/health", "/ping"):
            return self._json(200, {"ok": True, "stub": True})
        if path == "/__state":
            with LOCK:
                return self._json(200, dict(STATE))
        if path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "stub",
                 "max_model_len": 300000}]})
        return self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path_scenario, path = self._route()
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0) or None) or b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:                          # noqa: BLE001 — journal it anyway
            body = {"_unparseable": raw[:2000].decode("utf-8", "replace")}

        if path == "/__mode":
            with LOCK:
                STATE["default"] = body.get("default", STATE["default"])
                return self._json(200, {"default": STATE["default"]})
        if path == "/__reset":
            with LOCK:
                STATE.update(requests=0, by_scenario={}, tools_seen=[], markers=[])
            return self._json(200, {"reset": True})

        scenario = scenario_for(path_scenario,
                                self.headers.get("X-Devcake-Scenario"), body)
        note_tools(body)
        with LOCK:
            STATE["requests"] += 1
            STATE["by_scenario"][scenario] = STATE["by_scenario"].get(scenario, 0) + 1
        # THE record: the CLI's request, verbatim. This is what makes a
        # client-side question ("why prose instead of a tool call?") answerable.
        journal({"ts": time.time(), "path": path, "scenario": scenario,
                 "headers": redact(dict(self.headers)), "body": body})

        if scenario == "no_route":
            return self._json(404, {"error": {"message": f"{path} not found",
                                              "type": "invalid_request_error"}})
        if scenario.startswith("http_"):
            code = int(scenario.split("_")[1])
            return self._json(code, {"type": "error", "error": {
                "type": "invalid_request_error" if code < 500 else "api_error",
                "message": f"stub injected HTTP {code}"}})

        if path.endswith("/responses"):
            return self._responses(scenario, body)
        if path.endswith("/messages"):
            return self._messages(scenario, body)
        if path.endswith("/chat/completions"):
            return self._chat(scenario, body)
        return self._json(404, {"error": "unhandled route", "path": path})

    # ── OpenAI Responses (codex) ─────────────────────────────────────────────
    def _responses(self, scenario: str, body: dict):
        rid = f"resp_{uuid.uuid4().hex[:12]}"
        name, args = pick_tool(body, "responses")
        items = []
        if scenario == "healthy":
            items = [{"type": "message", "id": "msg_1", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text",
                                   "text": "Done. " + "Detail. " * 60}]}]
        elif scenario == "whitespace":
            items = [{"type": "message", "id": "msg_1", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text", "text": "  \n "}]}]
        elif scenario == "refusal":
            items = [{"type": "message", "id": "msg_1", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text",
                                   "text": "I can't help with that."}]}]
        elif scenario in ("tool_only", "loop") and name:
            items = [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                      "name": name, "arguments": json.dumps(args),
                      "status": "completed"}]
        usage = {"input_tokens": 120,
                 "output_tokens": 0 if scenario in ("empty",) else 24}
        events = [("response.created", {"type": "response.created",
                                        "response": {"id": rid, "status": "in_progress"}})]
        for it in items:
            events.append(("response.output_item.added",
                           {"type": "response.output_item.added", "item": it}))
            events.append(("response.output_item.done",
                           {"type": "response.output_item.done", "item": it}))
        events.append(("response.completed", {"type": "response.completed",
                                              "response": {"id": rid, "status": "completed",
                                                           "output": items, "usage": usage}}))
        if not body.get("stream", True):
            return self._json(200, {"id": rid, "status": "completed",
                                    "output": items, "usage": usage})
        chunks = [sse(e) for e in events]
        return self._stream(200, chunks, truncate=(scenario == "truncated"))

    # ── Anthropic Messages (claude, grok api_backend=messages) ───────────────
    def _messages(self, scenario: str, body: dict):
        name, args = pick_tool(body, "messages")
        blocks, stop = [], "end_turn"
        if scenario == "healthy":
            blocks = [{"type": "text", "text": "Done. " + "Detail. " * 60}]
        elif scenario == "whitespace":
            blocks = [{"type": "text", "text": "  \n "}]
        elif scenario == "refusal":
            blocks = [{"type": "text", "text": "I can't help with that."}]
        elif scenario in ("tool_only", "loop") and name:
            blocks = [{"type": "tool_use", "id": "tu_1", "name": name, "input": args}]
            stop = "tool_use"
        usage = {"input_tokens": 120,
                 "output_tokens": 0 if scenario == "empty" else 24}
        msg = {"id": "msg_stub", "type": "message", "role": "assistant",
               "model": body.get("model") or MODEL, "content": blocks,
               "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            return self._json(200, msg)
        events = [("message_start", {"type": "message_start",
                                     "message": {**msg, "content": []}})]
        for i, b in enumerate(blocks):
            events.append(("content_block_start",
                           {"type": "content_block_start", "index": i,
                            "content_block": ({**b, "input": {}} if b["type"] == "tool_use"
                                              else {**b, "text": ""})}))
            if b["type"] == "text":
                events.append(("content_block_delta",
                               {"type": "content_block_delta", "index": i,
                                "delta": {"type": "text_delta", "text": b["text"]}}))
            else:
                events.append(("content_block_delta",
                               {"type": "content_block_delta", "index": i,
                                "delta": {"type": "input_json_delta",
                                          "partial_json": json.dumps(b["input"])}}))
            events.append(("content_block_stop",
                           {"type": "content_block_stop", "index": i}))
        events.append(("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": stop},
                                         "usage": usage}))
        events.append(("message_stop", {"type": "message_stop"}))
        return self._stream(200, [sse(e) for e in events],
                            truncate=(scenario == "truncated"))

    # ── OpenAI Chat Completions (grok default) ───────────────────────────────
    def _chat(self, scenario: str, body: dict):
        name, args = pick_tool(body, "chat")
        msg = {"role": "assistant", "content": None}
        finish = "stop"
        if scenario == "healthy":
            msg["content"] = "Done. " + "Detail. " * 60
        elif scenario == "whitespace":
            msg["content"] = "  \n "
        elif scenario == "refusal":
            msg["content"] = "I can't help with that."
        elif scenario in ("tool_only", "loop") and name:
            msg["tool_calls"] = [{"id": "call_1", "type": "function",
                                  "function": {"name": name,
                                               "arguments": json.dumps(args)}}]
            finish = "tool_calls"
        elif scenario == "empty":
            msg["content"] = ""
        payload = {"id": "chatcmpl_stub", "object": "chat.completion",
                   "created": int(time.time()), "model": body.get("model") or MODEL,
                   "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 120, "completion_tokens":
                             0 if scenario == "empty" else 24, "total_tokens": 144}}
        if not body.get("stream"):
            return self._json(200, payload)
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = msg["tool_calls"]
        chunks = [
            sse(("chunk", {"id": "chatcmpl_stub", "object": "chat.completion.chunk",
                           "choices": [{"index": 0, "delta": delta,
                                        "finish_reason": None}]})),
            sse(("chunk", {"id": "chatcmpl_stub", "object": "chat.completion.chunk",
                           "choices": [{"index": 0, "delta": {},
                                        "finish_reason": finish}],
                           "usage": payload["usage"]})),
            b"data: [DONE]\n\n",
        ]
        return self._stream(200, chunks, truncate=(scenario == "truncated"))


def main() -> None:
    global JOURNAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--scenario", default="healthy", choices=SCENARIOS)
    ap.add_argument("--journal", default="")
    args = ap.parse_args()
    JOURNAL = args.journal or None
    STATE["default"] = args.scenario
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"stub on {args.bind}:{args.port} default={args.scenario} "
          f"journal={JOURNAL or '-'}", flush=True)
    print(f"  scenarios: {', '.join(SCENARIOS)}", flush=True)
    print(f"  per-request override: /s/<scenario>/v1/... or X-Devcake-Scenario",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
