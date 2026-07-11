"""DevCake Dev entrypoint — shared across harness images (docs/07, docs/08).
Harness selected by the image-baked DEVCAKE_HARNESS env (claude-code | grok-build).

Exit codes per docs/07 §4: 0 ok · 10 harness crash · 11 bad result.json ·
12 auth · 13 clone/forge · 14 MCP setup · 20 entrypoint error.
"""

import base64
import json
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import redis

RUN_ID = os.environ["DEVCAKE_RUN_ID"]
REDIS_URL = os.environ["REDIS_URL"]
TRACEPARENT = os.environ.get("TRACEPARENT", "")
INGRESS = "devcake:ingress"
REPLY = f"devcake:reply:{RUN_ID}"
CHUNK_LIMIT, CHUNK_SIZE = 512 * 1024, 400 * 1024
WORKSPACE = pathlib.Path("/workspace")

r = redis.from_url(REDIS_URL, username=os.environ["REDIS_USER"],
                   password=os.environ["REDIS_PASSWORD"], decode_responses=True)


def send(kind: str, payload: dict) -> None:
    r.xadd(INGRESS, {"m": json.dumps(
        {"v": 1, "run_id": RUN_ID, "auth": os.environ["REDIS_PASSWORD"], "kind": kind,
         "ts": datetime.now(timezone.utc).isoformat(), "payload": payload})})


def send_artifacts(payload: dict) -> None:
    blob = json.dumps(payload)
    if len(blob) <= CHUNK_LIMIT:
        send("run.artifacts", payload)
        return
    parts = [blob[i:i + CHUNK_SIZE] for i in range(0, len(blob), CHUNK_SIZE)]
    for i, part in enumerate(parts, start=1):
        send("run.artifacts", {"chunk": i, "of": len(parts), "data": part})


def request_reply(kind: str, want: str, timeout: int = 90) -> dict:
    send(kind, {})
    last_id, deadline = "0", time.time() + timeout
    while time.time() < deadline:
        for _s, msgs in r.xread({REPLY: last_id}, block=5000, count=10) or []:
            for entry_id, fields in msgs:
                last_id = entry_id
                env = json.loads(fields["m"])
                if env.get("kind") == want:
                    return env["payload"]
    print(f"{kind} timed out", file=sys.stderr)
    sys.exit(20)


def heartbeat_loop(stop: threading.Event) -> None:
    send("run.heartbeat", {"phase": "starting"})   # immediate first beat: a kill in the
    while not stop.wait(30):                       # first 30s must still be detectable
        try:
            send("run.heartbeat", {"phase": "working"})
        except Exception:
            pass


def main() -> None:
    spec = request_reply("runspec.get", "runspec.result")
    send("runspec.ack", {})
    env = spec.get("env", {})
    os.environ.update(env)
    for f in spec.get("credential_files", []):
        p = pathlib.Path(os.path.expanduser(f["path_hint"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"])
        p.chmod(0o600)
    prompt = spec.get("prompt", "")

    send("run.started", {"container_hostname": os.uname().nodename})
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()

    # ── workspace prep (docs/07 §1) ──────────────────────────────────────────
    (WORKSPACE / "out").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "activity").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / ".devcake").mkdir(parents=True, exist_ok=True)

    act = request_reply("activity.get", "activity.result")
    (WORKSPACE / "activity" / "ACTIVITY.md").write_text(act.get("activity_md", ""))
    for a in act.get("attachments", []):
        target = WORKSPACE / "activity" / pathlib.Path(a["filename"]).name
        target.write_bytes(base64.b64decode(a["content_b64"]))

    repo_url = env["DEVCAKE_REPO_URL"]
    askpass = WORKSPACE / ".devcake" / "askpass.sh"
    askpass.write_text("#!/bin/sh\necho \"$DEVCAKE_FORGE_TOKEN\"\n")
    askpass.chmod(0o700)
    clone_url = repo_url.replace("https://", "https://x-access-token@")
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_dir = WORKSPACE / "repo"  # canonical path; dir inside named after the repo
    # git auth for clone AND the harness's own push (docs/03 §3); gh auth for PRs
    os.environ["GIT_ASKPASS"] = str(askpass)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    if env.get("DEVCAKE_FORGE_TOKEN"):
        os.environ["GH_TOKEN"] = env["DEVCAKE_FORGE_TOKEN"]
    subprocess.run(["git", "config", "--global", "user.name", "DevCake"], capture_output=True)
    subprocess.run(["git", "config", "--global", "user.email",
                    "devcake@users.noreply.github.com"], capture_output=True)
    clone = subprocess.run(
        ["git", "clone", clone_url, str(repo_dir / repo_name)],
        capture_output=True, text=True)
    if clone.returncode != 0:
        print("clone failed:", clone.stderr[-500:], file=sys.stderr)
        send_artifacts({"result": None, "transcript_md": f"clone failed:\n{clone.stderr[-2000:]}",
                        "token_report": {"extraction_method": "unavailable", "model": None}})
        sys.exit(13)
    workdir = repo_dir / repo_name

    for cmd in spec.get("mcp_setup_commands", []):                  # docs/07 §5 step 5
        res = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True)
        if res.returncode != 0:
            print("mcp setup failed:", cmd, res.stderr[-300:], file=sys.stderr)
            sys.exit(14)

    # ── telemetry (stage-2 creds — docs/07 §3) ───────────────────────────────
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "devcake-dev"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=env["OTEL_EXPORTER_OTLP_ENDPOINT"],
        headers={"Authorization": f"Basic {env['OTEL_EXPORTER_OTLP_BASIC']}"})))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("devcake-dev")
    ctx = extract({"traceparent": TRACEPARENT}) if TRACEPARENT else None

    # ── harness (docs/08 §§1,3) ──────────────────────────────────────────────
    harness = os.environ.get("DEVCAKE_HARNESS", "claude-code")
    plan_mode = env.get("DEVCAKE_MISSION_TYPE") == "PLAN"
    extra = shlex.split(env.get("DEVCAKE_EXTRA_ARGS", ""))
    if harness == "grok-build":
        mode = ["--permission-mode", "plan"] if plan_mode else ["--always-approve"]
        cmd = ["grok", "-p", prompt, "--output-format", "json", *mode, *extra]
    else:
        mode = ["--permission-mode", "plan"] if plan_mode             else ["--dangerously-skip-permissions"]
        cmd = ["claude", "-p", prompt, "--output-format", "json", *mode, *extra]
    harness_exit, out = 1, ""
    with tracer.start_as_current_span("dev.run", context=ctx) as span:
        span.set_attribute("devcake.run.id", RUN_ID)
        span.set_attribute("devcake.dev_type", env.get("DEVCAKE_DEV_TYPE", ""))
        span.set_attribute("devcake.harness", harness)
        with tracer.start_as_current_span("harness.exec"):
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
            harness_exit, out = proc.returncode, proc.stdout
        span.set_attribute("devcake.outcome", "harness_exit_%d" % harness_exit)
    provider.force_flush()

    # ── token extraction + result text (docs/08 §5) ──────────────────────────
    token_report = {"extraction_method": "unavailable", "model": harness}
    result_text, transcript_body = "", ""
    if harness == "grok-build":
        try:
            j = json.loads(out)
            result_text = j.get("text") or ""
            sid = j.get("sessionId") or ""
            sig = None
            for p in pathlib.Path.home().glob(f".grok/sessions/*/{sid}/signals.json"):
                sig = json.loads(p.read_text())
            if sig:
                token_report = {
                    "total_tokens": sig.get("contextTokensUsed") or sig.get("totalTokens"),
                    "model": (sig.get("modelsUsed") or ["grok"])[0],
                    "extraction_method": "session_json",
                    "num_turns": sig.get("turnCount"),
                }
            exp = subprocess.run(["grok", "export", sid], capture_output=True, text=True)
            if exp.returncode == 0 and exp.stdout.strip():
                transcript_body = exp.stdout
        except Exception:
            result_text = out[-4000:]
    else:
        try:
            j = json.loads(out)
            usage = j.get("usage") or {}
            mu = j.get("modelUsage") or {}
            def _weight(v):  # dominant model = the one that cost/produced the most
                return (v.get("costUSD") or 0, v.get("outputTokens") or 0)                     if isinstance(v, dict) else (0, 0)
            models = sorted(mu, key=lambda k: _weight(mu[k]), reverse=True)
            token_report = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "cost_usd": j.get("total_cost_usd"),
                "model": models[0] if models else "claude-code",
                "extraction_method": "session_json",
                "num_turns": j.get("num_turns"),
                "duration_ms": j.get("duration_ms"),
            }
            result_text = j.get("result") or ""
        except Exception:
            result_text = out[-4000:]

    if harness_exit != 0:
        err = (proc.stderr or "")[-1500:]
        auth_fail = "authentication" in err.lower() or "unauthorized" in err.lower() \
            or "log in" in err.lower()
        send_artifacts({"result": None,
                        "transcript_md": f"harness exited {harness_exit}\n\n```\n{err}\n```",
                        "token_report": token_report})
        stop.set()
        sys.exit(12 if auth_fail else 10)

    # ── result.json (docs/03 §6) ─────────────────────────────────────────────
    # Plan mode is read-only — the harness cannot write files, so the entrypoint
    # materializes PLAN.md and result.json from the returned plan text (docs/08 §3)
    if plan_mode:
        (WORKSPACE / "out" / "PLAN.md").write_text(result_text or "")
        (WORKSPACE / "out" / "result.json").write_text(json.dumps({
            "schema_version": 1, "outcome": "planned",
            "summary": (result_text or "").strip().splitlines()[0][:300]
            if result_text.strip() else "empty plan"}))
    result_path = WORKSPACE / "out" / "result.json"
    try:
        result = json.loads(result_path.read_text())
        assert result.get("outcome") in ("plan_needed", "executed_trivially",
                                         "decomposed", "planned", "executed", "reviewed")
        assert isinstance(result.get("summary"), str)
    except Exception as e:
        send_artifacts({"result": None,
                        "transcript_md": f"result.json missing/invalid: {e}\n\n---\n\n{result_text}",
                        "token_report": token_report})
        stop.set()
        sys.exit(11)

    plan_path = WORKSPACE / "out" / "PLAN.md"
    transcript = (
        f"# {env.get('DEVCAKE_SEQ')}_{env.get('DEVCAKE_MISSION_TYPE')} — run {RUN_ID}\n\n"
        f"**Dev:** {env.get('DEVCAKE_DEV_TYPE')} ({harness}) · "
        f"**turns:** {token_report.get('num_turns', '—')} · "
        f"**duration:** {token_report.get('duration_ms', '—')} ms\n\n"
        f"## Agent report\n\n{result_text}\n\n"
        + (f"## Session transcript\n\n{transcript_body}\n\n" if transcript_body else "")
        + f"## Outcome\n\n```json\n{json.dumps(result, indent=2)}\n```\n")
    payload = {"result": result, "transcript_md": transcript, "token_report": token_report}
    if plan_path.exists():
        payload["plan_md"] = plan_path.read_text()
    send_artifacts(payload)
    stop.set()
    print(f"dev {RUN_ID} done: {result.get('outcome')}")


if __name__ == "__main__":
    main()
