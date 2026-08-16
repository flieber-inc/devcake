"""Run one matrix row against the in-process stub + baked entrypoint."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from .entrypoint import load_baked_entrypoint
from .matrix import RowSpec
from .plan_mode import composed_plan_argv, run_flag_accept

_STUB = None
_EP = None


def run_live_row(template: str, spec: RowSpec) -> dict:
    if not spec.required:
        return {"skipped": spec.skip_reason or "not required"}
    if spec.name == "resume":
        return _resume_row(template)
    if spec.kind == "flag_accept":
        return run_flag_accept(
            composed_plan_argv(template),
            timeout=60,
            env=_scenario_env(template, "healthy", _stub_base()),
        )

    if spec.kind != "classify" or spec.name not in (
            "healthy", "http_401", "empty", "resume"):
        return {"error": f"no live runner for {spec.name}"}
    scenario = "healthy" if spec.name == "resume" else spec.name
    return _classify_row(template, scenario)


def _resume_row(template: str) -> dict:
    ep = _entrypoint()
    first = _run_cli(template, "healthy", ep)
    if "error" in first:
        return first
    sid = first.get("session_id") or ""
    if not sid:
        return {"error": "first invocation exposed no session identity"}
    prompt = "Continue. Reply with exactly the word: ACKNOWLEDGED\n"
    env = _scenario_env(template, "healthy", _stub_base())
    extra = _codex_extra(template, "healthy", _stub_base())
    argv = ep.harness_resume_argv(
        template, sid, prompt, model="stub-model", extra=extra)
    if argv is None:
        return {"error": f"no resume candidate for {template}"}
    return _exec_and_classify(ep, template, argv, env, prompt, timeout=180.0)


def _classify_row(template: str, scenario: str) -> dict:
    ep = _entrypoint()
    return _run_cli(template, scenario, ep)


def _run_cli(template: str, scenario: str, ep) -> dict:
    prompt = "Reply with exactly the word: ACKNOWLEDGED\n"
    env = _scenario_env(template, scenario, _stub_base())
    extra = _codex_extra(template, scenario, _stub_base())
    argv = ep.harness_argv(
        template, prompt, plan_mode=False, model="stub-model", extra=extra)
    # grok_empty.meta.json recorded 344s — the CLI waits out an empty 200.
    timeout = 400.0 if scenario == "empty" else 180.0
    return _exec_and_classify(ep, template, argv, env, prompt, timeout=timeout)


def _exec_and_classify(ep, template: str, argv: list, env: dict, prompt: str,
                       timeout: float = 180.0) -> dict:
    with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
        work = Path(tmp) / "repo"
        work.mkdir()
        (work / "README.md").write_text("probe\n")
        try:
            proc = subprocess.run(
                argv, cwd=work, capture_output=True, text=True,
                timeout=timeout, env=env)
        except FileNotFoundError as exc:
            return {"error": f"binary missing: {exc}"}
        except subprocess.TimeoutExpired:
            return {"error": "classify row timed out"}
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        exit_code = proc.returncode
        sid_fn = getattr(ep, "session_identity", None)
        session_id = sid_fn(template, stdout) if sid_fn else ""
        fault = ep.harness_fault(
            template, stdout, exit_code, dump="",
            last_message="", prompt=prompt)
        api_status = ep.harness_api_error_status(template, stdout)
        # Same wiring as the baked entrypoint: nonzero → classify_nonzero_exit
        # on stderr; exit 0 + fault (no result.json) → classify_nonzero_exit
        # on the fault; clean exit 0 → 11 / "".
        if exit_code != 0:
            observed_exit, observed_class = ep.classify_nonzero_exit(
                stderr[-1500:], fault, api_status)
        elif fault:
            observed_exit, observed_class = ep.classify_nonzero_exit(
                "", fault, api_status)
        else:
            observed_exit, observed_class = 11, ""
        return {
            "observed": {
                "exit": observed_exit,
                "class": observed_class or "",
                "reason": (fault or {}).get("reason"),
            },
            "session_id": session_id or "",
        }


def _scenario_env(template: str, scenario: str, stub: str) -> dict:
    env = os.environ.copy()
    env.setdefault("DEVCAKE_RUN_ID", "PROBE-1")
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    env.setdefault("REDIS_USER", "probe")
    env.setdefault("REDIS_PASSWORD", "probe")
    env["XAI_API_KEY"] = "probe-fake-key"
    env["ANTHROPIC_API_KEY"] = "probe-fake-key"
    env["ANTHROPIC_AUTH_TOKEN"] = "probe-fake-key"
    env["CODEX_API_KEY"] = "probe-fake-key"
    lane = f"{stub}/s/{scenario}"
    if template == "grok-build":
        env["GROK_MODELS_BASE_URL"] = f"{lane}/v1"
    elif template == "claude-code":
        env["ANTHROPIC_BASE_URL"] = lane
    return env


def _codex_extra(template: str, scenario: str, stub: str) -> list[str]:
    if template != "codex":
        return []
    base = f"{stub}/s/{scenario}/v1"
    return [
        "-c", "model_provider=stub",
        "-c", "model_providers.stub.name=Stub",
        "-c", "model_providers.stub.env_key=CODEX_API_KEY",
        "-c", "model_providers.stub.wire_api=responses",
        "-c", f"model_providers.stub.base_url={base}",
        "-c", "model_providers.stub.request_max_retries=0",
        "-c", "model_providers.stub.stream_max_retries=0",
    ]


def _entrypoint():
    global _EP
    if _EP is None:
        os.environ.setdefault("DEVCAKE_RUN_ID", "PROBE-1")
        os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
        os.environ.setdefault("REDIS_USER", "probe")
        os.environ.setdefault("REDIS_PASSWORD", "probe")
        _EP = load_baked_entrypoint()
    return _EP


def _stub_base() -> str:
    return _ensure_stub()


def _ensure_stub() -> str:
    global _STUB
    if _STUB is not None:
        return _STUB
    capture = Path(__file__).resolve().parent.parent / "harness_capture"
    if str(capture) not in sys.path:
        sys.path.insert(0, str(capture))
    import stub_backend  # noqa: E402
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), stub_backend.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _STUB = f"http://127.0.0.1:{port}"
    return _STUB
