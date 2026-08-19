#!/usr/bin/env python3
"""Capture one real harness run from INSIDE a baked Dev image (ADR-0018 gate 1).

Why in-image and not on the host: the host's CLI versions drift from the pins
(codex host 0.144.6 vs image 0.144.4; grok house pin is ARG GROK_VERSION).
A fixture captured at the wrong version silently stops describing what
production runs.

What it guarantees, and these are the properties that make a capture evidence
rather than decoration:

* argv comes from the entrypoint's own `harness_argv`, so a fixture cannot drift
  from the invocation production uses.
* the backend is preflighted; a routing failure aborts loudly instead of being
  recorded as "connection refused".
* `proc.wait()` is recorded verbatim, stdout and stderr stay separate, and
  everything the predicate consumes is collected (grok's `grok export`, codex's
  raw `-o` file, always stderr).
* the CURRENT predicate is run against the capture and its verdict recorded next
  to the intended one. A mismatch is the finding — it is never quietly fixed by
  editing the capture.
* nothing is fabricated. The tool runs a CLI and writes down what happened.

Usage — run it INSIDE a baked Dev image, with this repo bind-mounted at /srv so
the working tree's entrypoint (not the baked copy) supplies `harness_argv`:

    in_container.py --harness codex --name live_codex_healthy \\
        --prompt-file /capture/prompts/trivial.md --out /out \\
        --preflight http://<vllm-host>:8765/v1/models \\
        [--model ID] [--extra '<shell-quoted argv tail>'] \\
        [--intended empty_completion] [--concurrency 4] [--timeout 300]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The entrypoint reads these at import time. Set before the import below.
os.environ.setdefault("DEVCAKE_RUN_ID", "CAPTURE-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
os.environ.setdefault("REDIS_USER", "capture")
os.environ.setdefault("REDIS_PASSWORD", "capture")

# Prefer the bind-mounted WORKING TREE over the baked copy: we are grading the
# predicate we are about to change, not the one that shipped.
ENTRYPOINT_CANDIDATES = ("/srv/images/common/dev_entrypoint.py", "/dev_entrypoint.py")


def load_entrypoint():
    for path in ENTRYPOINT_CANDIDATES:
        if pathlib.Path(path).exists():
            spec = importlib.util.spec_from_file_location("ep_capture", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, path
    sys.exit(f"no dev_entrypoint.py at any of {ENTRYPOINT_CANDIDATES}")


def _ensure_dialects() -> None:
    for p in (
        pathlib.Path("/srv/images/common"),
        pathlib.Path(__file__).resolve().parents[2] / "images" / "common",
        pathlib.Path("/"),
    ):
        if (p / "devcake_dev").is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
            return


def cli_name(harness: str) -> str:
    """argv[0] from the production dialect. Unknown ids raise."""
    _ensure_dialects()
    from devcake_dev.harness.dialect import get_dialect
    return get_dialect(harness).argv(".")[0]


def capture_dump(harness: str, stdout: str, *, workspace, model: str = "") -> str:
    """Transcript dump via HarnessDialect.parse_run — no Claude else-arm."""
    _ensure_dialects()
    from devcake_dev.harness.dialect import get_dialect
    return get_dialect(harness).parse_run(
        stdout, workspace=pathlib.Path(workspace), model=model).dump


def known_harnesses() -> list[str]:
    _ensure_dialects()
    from devcake_dev.harness.dialect import dialects
    return sorted(dialects())


def cli_version(harness: str) -> str:
    """The version of the CLI actually about to run, read from this container."""
    exe = cli_name(harness)
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=60)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (
            r.stdout or r.stderr).strip() else "unknown"
    except Exception as e:                      # noqa: BLE001 — recorded, not fatal
        return f"unavailable: {type(e).__name__}"


def preflight(url: str, attempts: int = 3) -> dict:
    """Prove the backend is reachable BEFORE running the harness.

    Without this, a docker-network or LAN-routing problem is indistinguishable
    from the very fault we are trying to capture — both look like "the CLI could
    not connect", and one of them is a false finding.
    """
    last = None
    for i in range(attempts):
        try:
            started = time.monotonic()
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read(2048).decode("utf-8", "replace")
            return {"url": url, "status": resp.status,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "body_head": body[:400]}
        except Exception as e:                  # noqa: BLE001 — retried, then fatal
            last = f"{type(e).__name__}: {e}"
            time.sleep(1 + i)
    sys.exit(f"PREFLIGHT FAILED for {url} after {attempts} attempts: {last}\n"
             f"Refusing to capture — a routing failure would be recorded as a "
             f"backend fault.")


def make_workspace(root: pathlib.Path, prompt: str) -> pathlib.Path:
    """A deterministic git workspace, so a capture differs from its siblings only
    in the backend's behaviour. Mission-shaped: the harness expects a clone it
    can read and an out/ it can write."""
    wd = root / "repo"
    (wd / "src").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (wd / "README.md").write_text("# capture fixture\n\nA tiny repo.\n")
    (wd / "src" / "greet.py").write_text('def greet(n):\n    return f"hi {n}"\n')
    (wd / "PROMPT.md").write_text(prompt)
    env = {**os.environ, "GIT_AUTHOR_NAME": "capture",
           "GIT_AUTHOR_EMAIL": "capture@example.invalid",
           "GIT_COMMITTER_NAME": "capture",
           "GIT_COMMITTER_EMAIL": "capture@example.invalid"}
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "fixture"]):
        subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True,
                       timeout=60)
    return wd


def grok_session_id(out: str) -> str:
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:                       # noqa: BLE001 — skip noise
            continue
        if isinstance(ev, dict) and ev.get("sessionId"):
            return str(ev["sessionId"])
    return ""


KILL_GRACE_SECS = 15        # bounded wait after SIGKILLing the group, then give up


def kill_harness_group(proc) -> None:
    """SIGKILL the harness's whole process group.

    `proc.kill()` alone is not enough and the difference is a hang, not a
    slowdown: codex's `exec_command` leaves a persistent `/bin/bash` child that
    INHERITED the stdout pipe, so the following `communicate()` never sees EOF
    and the driver waits forever. (Measured while capturing the codex turn-cap
    runaway — 5,535 requests, no files written, killed at the container level.)
    `start_new_session=True` on the Popen puts the harness in its own process
    group, so one killpg closes every inherited pipe end at once.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:                             # group already gone, or not ours
        proc.kill()


def partial_text(buf) -> str:
    """TimeoutExpired carries the reads it managed as BYTES, even in text mode."""
    if buf is None:
        return ""
    return buf.decode("utf-8", "replace") if isinstance(buf, bytes) else str(buf)


def capture_slot(root: pathlib.Path, index: int, concurrency: int) -> pathlib.Path:
    """The per-invocation workspace root — ONE definition, because the resume
    leg must resolve the exact directory the first leg ran in (grok keys its
    session store on the urlencoded cwd)."""
    return root if root.name == "workspace" and concurrency == 1 \
        else root / f"slot{index}"


def run_once(ep, args, index: int, root: pathlib.Path) -> dict:
    """One harness invocation in a FRESH workspace. Returns the measured record."""
    prompt = pathlib.Path(args.prompt_file).read_text()
    slot = capture_slot(root, index, args.concurrency)
    workdir = make_workspace(slot, prompt)
    out_dir = slot / "out"
    argv = ep.harness_argv(args.harness, prompt, plan_mode=args.plan_mode,
                           model=args.model, extra=shlex.split(args.extra or ""),
                           out_dir=out_dir)
    rec = execute_argv(ep, args, argv, workdir, out_dir, prompt)
    rec["slot"] = index
    return rec


def grade_capture(ep, *, exit_code, stderr: str, fault, api_status):
    """Map process outcome to (exit, class) for capture sidecars.

    Same wiring as live.py / the baked entrypoint: nonzero → classify on
    stderr; exit 0 + fault → classify on the fault (api_status → DEV_AUTH);
    clean exit 0 → 11 / "".
    """
    if exit_code != 0:
        return ep.classify_nonzero_exit((stderr or "")[-1500:], fault, api_status)
    if fault:
        return ep.classify_nonzero_exit("", fault, api_status)
    return 11, ""


def execute_argv(ep, args, argv: list, workdir: pathlib.Path,
                 out_dir: pathlib.Path, prompt: str) -> dict:
    """Execute one argv in an EXISTING workspace and measure it — the shared
    core of run_once and the resume second leg (ADR-0022 Phase 0), so both
    legs are recorded by identical machinery."""
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=workdir, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ}, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_harness_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=KILL_GRACE_SECS)
        except subprocess.TimeoutExpired as partial:
            # something escaped the group and still holds a pipe end: keep the
            # partial reads and stop waiting. A capture driver that can hang is
            # worse than one that records an incomplete run and says so.
            stdout, stderr = partial_text(partial.stdout), partial_text(partial.stderr)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            proc.kill()
            try:
                proc.wait(timeout=KILL_GRACE_SECS)
            except subprocess.TimeoutExpired:   # unreapable; exit_code stays None
                pass
    exit_code = proc.returncode
    duration_ms = int((time.monotonic() - started) * 1000)

    # everything the predicate consumes, per harness
    last_message = ""
    lm = out_dir / "last_message.txt"
    if args.harness == "codex" and lm.exists():
        last_message = lm.read_text()
    # session_identity is the production reader (ADR-0022; all three
    # harnesses); the grok-only scan stays as the fallback for a baked
    # entrypoint that predates it (/dev_entrypoint.py without /srv mounted)
    sid_fn = getattr(ep, "session_identity", None)
    session_id = (sid_fn(args.harness, stdout) if sid_fn
                  else grok_session_id(stdout)
                  if args.harness == "grok-build" else "")
    dump = capture_dump(args.harness, stdout, workspace=workdir)

    # grade the CURRENT predicate — the whole point of the exercise
    fault = ep.harness_fault(args.harness, stdout, exit_code, dump=dump,
                             last_message=last_message, prompt=prompt)
    api_status = ep.harness_api_error_status(args.harness, stdout)
    observed_exit, observed_class = grade_capture(
        ep, exit_code=exit_code, stderr=stderr, fault=fault,
        api_status=api_status)

    return {
        "argv": argv, "exit_code": exit_code,
        "timed_out": timed_out, "duration_ms": duration_ms,
        "stdout": stdout, "stderr": stderr,
        "stdout_bytes": len(stdout), "stdout_lines": len(stdout.splitlines()),
        "stderr_bytes": len(stderr),
        "last_message": last_message, "dump": dump, "session_id": session_id,
        "observed_reason": (fault or {}).get("reason"),
        "observed_detail": (fault or {}).get("detail"),
        "devcake_exit_now": observed_exit,
        "devcake_class_now": observed_class,
        # What pre-ADR-0018 DevCake would have reported: the ONLY signal it had
        # was the process exit status, classified on stderr. Recording it is how
        # we tell "this could have been the incident" (exit 11) from "this is
        # merely classification evidence" (exit 10/12).
        "devcake_exit_before": (
            ep.classify_harness_failure(stderr[-1500:]) if exit_code != 0
            else 11),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True,
                    choices=known_harnesses())
    ap.add_argument("--name", required=True, help="capture basename")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out", default="/out")
    ap.add_argument("--preflight", default="", help="URL that must answer first")
    ap.add_argument("--model", default="")
    ap.add_argument("--extra", default="", help="shell-quoted extra argv")
    ap.add_argument("--resume-prompt-file", default="",
                    help="ADR-0022 Phase 0: after the first invocation, run a "
                         "SECOND one in the SAME workspace via the production "
                         "harness_resume_argv, resuming the first's session — "
                         "recorded as <name>_resume.*. Requires --concurrency 1 "
                         "(session stores are keyed on the cwd).")
    ap.add_argument("--plan-mode", action="store_true")
    ap.add_argument("--intended", default="",
                    help="the reason we EXPECT; recorded for comparison only")
    ap.add_argument("--condition", default="", help="human label for the condition")
    ap.add_argument("--workspace-root", default="/tmp/capture",
                    help="use /workspace for production fidelity — the real "
                         "playbook prompt names absolute /workspace paths, and a "
                         "prompt whose paths do not resolve is a different test")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    ep, ep_path = load_entrypoint()
    out_root = pathlib.Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(args.workspace_root)
    work.mkdir(parents=True, exist_ok=True)

    pre = preflight(args.preflight) if args.preflight else None
    version = cli_version(args.harness)

    # Concurrency runs N harness processes in ONE container rather than N
    # containers. From the backend's point of view that is the same N concurrent
    # request streams, which is what a saturation fault depends on; it does not
    # reproduce per-container memory pressure, and the sidecar says so.
    if args.concurrency > 1:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            records = list(pool.map(
                lambda i: run_once(ep, args, i, work), range(args.concurrency)))
    else:
        records = [run_once(ep, args, 0, work)]

    # ADR-0022 Phase 0: the resume second leg — same workspace, same session,
    # argv through the PRODUCTION harness_resume_argv (the rig's whole reason
    # to exist: a fixture must not drift from what production would run).
    if args.resume_prompt_file:
        if args.concurrency != 1:
            sys.exit("--resume-prompt-file requires --concurrency 1")
        first = records[0]
        sid = first["session_id"]
        if not sid:
            sys.exit("first invocation exposed no session identity — nothing "
                     "to resume (that itself is a finding: record the first "
                     "capture and stop)")
        resume_prompt = pathlib.Path(args.resume_prompt_file).read_text()
        slot = capture_slot(work, 0, args.concurrency)
        argv = ep.harness_resume_argv(args.harness, sid, resume_prompt,
                                      model=args.model,
                                      extra=shlex.split(args.extra or ""),
                                      out_dir=slot / "out")
        if argv is None:
            sys.exit(f"harness_resume_argv has no candidate for {args.harness}")
        rec = execute_argv(ep, args, argv, slot / "repo", slot / "out",
                           resume_prompt)
        rec["slot"] = 0
        rec["name_override"] = f"{args.name}_resume"
        rec["resume_of"] = args.name
        rec["first_session_id"] = sid
        records.append(rec)

    summary = []
    for rec in records:
        suffix = "" if args.concurrency == 1 else f".{rec['slot']}"
        base = rec.get("name_override") or f"{args.name}{suffix}"
        (out_root / f"{base}.jsonl").write_text(rec["stdout"])
        if rec["stderr"]:
            (out_root / f"{base}.stderr.txt").write_text(rec["stderr"])
        if rec["dump"]:
            (out_root / f"{base}.dump.txt").write_text(rec["dump"])
        if rec["last_message"]:
            (out_root / f"{base}.last_message.txt").write_text(rec["last_message"])
        meta = {
            "name": base, "harness": args.harness, "cli_version": version,
            "entrypoint_source": ep_path,
            "condition": args.condition or args.name,
            "model": args.model or None, "plan_mode": args.plan_mode,
            "argv": rec["argv"], "extra": args.extra or None,
            "preflight": pre, "concurrency": args.concurrency,
            "concurrency_note": (
                "N processes in one container — equivalent backend load, does "
                "NOT reproduce per-container memory pressure"
                if args.concurrency > 1 else None),
            "exit_code": rec["exit_code"], "timed_out": rec["timed_out"],
            "duration_ms": rec["duration_ms"],
            "stdout_bytes": rec["stdout_bytes"], "stdout_lines": rec["stdout_lines"],
            "stderr_bytes": rec["stderr_bytes"],
            "dump_bytes": len(rec["dump"]),
            "last_message_bytes": len(rec["last_message"]),
            "session_id": rec["session_id"] or None,
            # resume leg only (ADR-0022 Phase 0): which capture it resumed and
            # the FIRST leg's session id — equality with session_id above is
            # the fork-semantics answer (same id = resumed in place)
            "resume_of": rec.get("resume_of"),
            "first_session_id": rec.get("first_session_id"),
            # measured verdicts. The EXPECTED reason deliberately lives in the
            # pytest parametrize table, never here — measured facts and
            # expectations must not share a file, or an expectation can be
            # "corrected" without review.
            "observed_reason": rec["observed_reason"],
            "observed_detail": rec["observed_detail"],
            "devcake_exit_now": rec["devcake_exit_now"],
            "devcake_class_now": rec["devcake_class_now"],
            "devcake_exit_before_adr0018": rec["devcake_exit_before"],
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (out_root / f"{base}.meta.json").write_text(json.dumps(meta, indent=2))
        summary.append((base, rec["exit_code"], rec["observed_reason"],
                        rec["devcake_exit_now"], rec["devcake_exit_before"],
                        rec["stdout_bytes"], len(rec["dump"])))

    print(f"\n=== {args.name} · {args.harness} · {version} ===")
    print(f"{'capture':38s} {'exit':>5s} {'reason':20s} {'now':>4s} {'before':>7s} "
          f"{'stdout':>8s} {'dump':>7s}")
    for base, ec, reason, now, before, sb, db in summary:
        print(f"{base:38s} {ec!s:>5s} {reason or '-':20s} {now!s:>4s} "
              f"{before!s:>7s} {sb:>8d} {db:>7d}")
    if args.intended:
        got = summary[0][2]
        verdict = "MATCH" if got == args.intended else "MISMATCH"
        print(f"\nintended={args.intended!r} observed={got!r} -> {verdict}")
        if verdict == "MISMATCH":
            print("  ^ this is a FINDING. Do not edit the capture; fix the "
                  "predicate (Phase E) or correct the intent.")


if __name__ == "__main__":
    main()
