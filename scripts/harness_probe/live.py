"""Run one matrix row against the in-process stub + baked entrypoint."""

from __future__ import annotations

import json
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


def _ensure_devcake_dev() -> None:
    """Image root /devcake_dev first; checkout images/common for host tests."""
    if Path("/devcake_dev").is_dir():
        if "/" not in sys.path:
            sys.path.insert(0, "/")
        return
    planted = os.environ.get("DEVCAKE_DEV_ROOT")
    if planted and Path(planted, "devcake_dev").is_dir():
        if planted not in sys.path:
            sys.path.insert(0, planted)
        return
    for candidate in (
        Path("/srv/images/common"),
        Path(__file__).resolve().parents[2] / "images" / "common",
    ):
        if candidate.joinpath("devcake_dev").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return
    raise RuntimeError("devcake_dev not found — cannot aim the probe stub")


_ensure_devcake_dev()
from devcake_dev.harness.aim import aim, stub_lane  # noqa: E402

_STUB = None
_EP = None
_JOURNAL = None


def run_live_row(template: str, spec: RowSpec, *,
                 cli_version: str = "") -> dict:
    if not spec.required:
        return {"skipped": spec.skip_reason or "not required"}
    if spec.name == "resume":
        return _resume_row(template, cli_version=cli_version)
    if spec.kind == "flag_accept":
        return run_flag_accept(
            composed_plan_argv(template),
            timeout=60,
            env=_scenario_env(template, "healthy", _stub_base(),
                              cli_version=cli_version),
        )

    if spec.kind != "classify" or spec.name not in (
            "healthy", "http_401", "empty", "resume"):
        return {"error": f"no live runner for {spec.name}"}
    scenario = "healthy" if spec.name == "resume" else spec.name
    return _classify_row(template, scenario, cli_version=cli_version)


def _resume_row(template: str, *, cli_version: str = "") -> dict:
    ep = _entrypoint()
    first = _run_cli(template, "healthy", ep, cli_version=cli_version)
    if "error" in first:
        return first
    sid = first.get("session_id") or ""
    if not sid:
        return {"error": "first invocation exposed no session identity"}
    prompt = "Continue. Reply with exactly the word: ACKNOWLEDGED\n"
    env = _scenario_env(template, "healthy", _stub_base(),
                        cli_version=cli_version)
    extra = _probe_extra(template, "healthy", _stub_base(),
                         cli_version=cli_version)
    argv = ep.harness_resume_argv(
        template, sid, prompt, model=_probe_model(template), extra=extra)
    if argv is None:
        return {"error": f"no resume candidate for {template}"}
    return _exec_and_classify(ep, template, argv, env, prompt, timeout=180.0)


def _classify_row(template: str, scenario: str, *,
                  cli_version: str = "") -> dict:
    ep = _entrypoint()
    return _run_cli(template, scenario, ep, cli_version=cli_version)


def _run_cli(template: str, scenario: str, ep, *, cli_version: str = "") -> dict:
    prompt = "Reply with exactly the word: ACKNOWLEDGED\n"
    env = _scenario_env(template, scenario, _stub_base(),
                        cli_version=cli_version)
    extra = _probe_extra(template, scenario, _stub_base(),
                         cli_version=cli_version)
    from devcake_dev.harness.launch import composed_launch
    argv = composed_launch(
        template, prompt, plan_mode=False, model=_probe_model(template),
        extra=extra, script="")
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


def _scenario_env(template: str, scenario: str, stub: str, *,
                  cli_version: str = "") -> dict:
    env = os.environ.copy()
    env.setdefault("DEVCAKE_RUN_ID", "PROBE-1")
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    env.setdefault("REDIS_USER", "probe")
    env.setdefault("REDIS_PASSWORD", "probe")
    env["XAI_API_KEY"] = "probe-fake-key"
    env["ANTHROPIC_API_KEY"] = "probe-fake-key"
    env["ANTHROPIC_AUTH_TOKEN"] = "probe-fake-key"
    env["CODEX_API_KEY"] = "probe-fake-key"
    env["OPENAI_API_KEY"] = "probe-fake-key"
    aimed = _aimed(template, stub, scenario, cli_version)
    env.update(aimed.env)
    _write_aimed_files(aimed)
    return env


def _probe_model(template: str) -> str:
    # OpenCode --model is provider/id. aim() registers provider "devcake".
    if template == "opencode":
        return "devcake/stub-model"
    return "stub-model"


def _probe_extra(template: str, scenario: str, stub: str, *,
                 cli_version: str = "") -> list[str]:
    return list(_aimed(template, stub, scenario, cli_version).extra_argv)


def _aimed(template: str, stub: str, scenario: str, cli_version: str):
    url = stub_lane(template, stub, scenario)
    return aim(template, url, api_key="probe-fake-key",
               cli_version=cli_version, model=_probe_model(template))


def _write_aimed_files(aimed) -> None:
    home = Path(os.environ.get("HOME") or "/home/dev")
    for item in aimed.files:
        hint = item.path_hint
        if hint.startswith("~/"):
            dest = home / hint[2:]
        else:
            dest = home / hint.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(item.content)


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
    global _JOURNAL
    if _JOURNAL is None:
        dest = Path(tempfile.mkdtemp(prefix="stub-j-")) / "journal.jsonl"
        dest.write_text("")
        stub_backend.JOURNAL = str(dest)
        _JOURNAL = dest
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), stub_backend.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _STUB = f"http://127.0.0.1:{port}"
    return _STUB


def journal_hits() -> list[dict]:
    """Stub journal entries for this probe process (empty if stub unused)."""
    if _JOURNAL is None or not _JOURNAL.is_file():
        return []
    rows: list[dict] = []
    for line in _JOURNAL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows
