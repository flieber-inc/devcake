"""DevCake Dev entrypoint — shared across harness images (docs/07, docs/08).

Composition root / façade for the Zone-B package `devcake_dev` (fault, render,
workspace, bus). ENTRYPOINT path stays `/dev_entrypoint.py`.

Exit codes per docs/07 §4: 0 ok · 10 harness crash · 11 bad result.json ·
12 auth · 13 clone/forge · 14 MCP setup · 15 harness fault · 16 turn budget ·
20 entrypoint error.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time

# Package next to this façade (checkout: images/common/; image: /devcake_dev).
_here = pathlib.Path(__file__).resolve().parent
for _p in (_here, pathlib.Path("/")):
    if (_p / "devcake_dev").is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
        break

# Env must be present before bus import (Redis client constructed at import).
from devcake_dev.adapters.bus import (  # noqa: E402
    CHUNK_LIMIT,
    CHUNK_SIZE,
    MAX_ARTIFACT_BYTES,
    RUN_ID,
    SHRINKABLE_FIELDS,
    TRUNCATE_FLOOR,
    _fit_payload,
    heartbeat_loop,
    request_reply,
    send,
    send_artifacts,
)
from devcake_dev.domain.fault import (  # noqa: E402
    CLAUDE_FAULT_TERMINAL_REASONS,
    DISTINCTIVE_AUTH_MARKERS,
    FAULT_DETAIL_MAX,
    FAULT_EMPTY_COMPLETION,
    FAULT_NO_TERMINAL_EVENT,
    FAULT_TERMINAL_ERROR,
    FAULT_TURN_BUDGET,
    GROK_FAULT_STOP_REASONS,
    HARNESS_AUTH_MARKERS,
    HARNESS_STATUS_PATTERNS,
    auth_evidence_is_distinctive,
    classify_harness_failure,
    classify_nonzero_exit,
    claude_result_event,
    claude_run_fault,
    codex_run_fault,
    grok_export_activity,
    grok_run_fault,
    harness_api_error_status,
    harness_error_messages,
    harness_fault,
    _dict,
    _one_line,
)
from devcake_dev.harness.argv import (  # noqa: E402
    RESUME_SPECS,
    ResumeSpec,
    harness_argv,
    harness_resume_argv,
)
from devcake_dev.harness.continuation import (  # noqa: E402
    ContinuationConfig,
    ContinuationDecision,
    ContinuationState,
    continuation_config,
    export_sids,
    fresh_nudge_prompt,
    last_sid,
    merge_token_reports,
    merged_transcript_dump,
    next_continuation,
    record_session,
    resume_nudge_prompt,
    session_identity,
    terminal_evidence,
)
from devcake_dev.harness.render import (  # noqa: E402
    BATCH_LINES,
    FLUSH_SECS,
    GrokCoalescer,
    LINE_LIMIT,
    LogRelay,
    MAX_RELAY_LINES,
    SILENCE_NOTICE_SECS,
    _progress_loop,
    _pump,
    render_claude,
    render_codex,
    render_stderr,
)
from devcake_dev.harness.tokens import (  # noqa: E402
    claude_text_dump,
    codex_text_dump,
    grok_end_event,
    grok_end_report,
    grok_signals_report,
    grok_stream_parse,
)
from devcake_dev.workspace.activity import (  # noqa: E402
    clone_activity_repo,
    materialize_activity,
    write_activity_payload,
    _safe_activity_relpath,
)
from devcake_dev.workspace.clone import (  # noqa: E402
    clone_error_class,
    clone_extra_repos,
    mirror_clone_error_class,
    set_origin_cmd,
)
from devcake_dev.workspace.provision import (  # noqa: E402
    MARKER_REL,
    marker_error,
    mirror_clone_argv,
    mirror_clone_env,
    phase_of,
    sentinel_error,
)
from devcake_dev.workspace.forensics import (  # noqa: E402
    FORENSIC_LISTING_BUDGET,
    FORENSIC_MAX_ENTRIES,
    FORENSIC_STDERR_TAIL,
    bad_output_reason,
    find_result_json,
    workspace_fingerprint,
    workspace_forensics,
    _git_tracked,
)
from devcake_dev.workspace.setup import (  # noqa: E402
    MCP_SETUP_TIMEOUT_SECS,
    install_skills,
    run_mcp_setup,
)
from devcake_dev.workspace.transcript import (  # noqa: E402
    assemble_transcript,
    with_session,
)

TRACEPARENT = os.environ.get("TRACEPARENT", "")
WORKSPACE = pathlib.Path("/workspace")


def forge_dialect(env: dict) -> tuple:
    """(clone_user, git_name, git_email, cli_token_envs) for the clone
    bootstrap. Values come from the app's ForgeDescriptor via spec_env
    (docs/06, docs/07). App and images deploy in lockstep (docs/13 §8), so
    every var is always present — a KeyError here means a mismatched build
    and should crash the run loudly."""
    cli_envs = [e for e in env.get("DEVCAKE_FORGE_CLI_ENVS", "").split(",") if e]
    return (env["DEVCAKE_CLONE_USER"], env["DEVCAKE_GIT_NAME"],
            env["DEVCAKE_GIT_EMAIL"], cli_envs)

def _fetch_spec(phase: str | None) -> dict:
    """runspec over the bus. The app serves the provision phase a REDUCED
    spec — no credential files, no harness/Dev-Type secret env (ADR-0025
    R1). None = no phase field: the monolithic rollback path asks exactly
    like the pre-split entrypoint did."""
    spec = request_reply("runspec.get", "runspec.result",
                         payload={"phase": phase} if phase else None)
    send("runspec.ack", {})
    return spec


def _write_credential_files(spec: dict) -> None:
    for f in spec.get("credential_files", []):
        p = pathlib.Path(os.path.expanduser(f["path_hint"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"])
        p.chmod(0o600)


def _start_heartbeats() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()
    return stop


def _fail_20(stop: threading.Event | None, headline: str, detail: str) -> None:
    """Entrypoint-fault exit WITH artifacts (ADR-0025): sentinel/marker
    failures must reach finalize promptly (DEV_CRASH) instead of waiting
    out the watchdog's heartbeat grace."""
    print(f"{headline}: {detail[-500:]}", file=sys.stderr)
    send_artifacts({"result": None, "exit_code": 20, "error_class": "DEV_CRASH",
                    "error_detail": detail[-500:],
                    "transcript_md": f"{headline}:\n\n```\n{detail}\n```",
                    "token_report": {"extraction_method": "unavailable",
                                     "model": None}})
    if stop is not None:
        stop.set()
    sys.exit(20)


def _git_identity_and_lfs(env: dict) -> None:
    """Git identity + LFS smudge posture, in $HOME. Runs in BOTH phases
    (ADR-0025 C1): smudge fires at clone/checkout time, so the provision
    container needs the posture for its clones, and the harness container
    (fresh HOME) needs it again for mid-run pulls and checkouts."""
    subprocess.run(["git", "config", "--global", "user.name",
                    env["DEVCAKE_GIT_NAME"]], capture_output=True)
    subprocess.run(["git", "config", "--global", "user.email",
                    env["DEVCAKE_GIT_EMAIL"]], capture_output=True)
    # ADR-0024: with DEVCAKE_LFS off, --skip-smudge keeps pointer files as
    # pointers (mirrors carry no LFS content, and this is byte-identical to
    # the pre-git-lfs images); with it on, the full filter pulls real
    # content from the mirror's LFS store via git-lfs's standalone file://
    # transfer (probe-verified 2026-08-03).
    subprocess.run(["git", "lfs", "install"]
                   + ([] if env.get("DEVCAKE_LFS") else ["--skip-smudge"]),
                   capture_output=True)


def _oauth_login(env: dict, stop: threading.Event) -> None:
    """OAuth helper mode (docs/08 §4): device-code login, not a mission run.
    Runs in the HARNESS step (the captured credential lives in that
    container's HOME); the provision step exits 0 early for OAuth runs."""
    import re as _re
    cmd = env["DEVCAKE_OAUTH_LOGIN_CMD"].split()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    ansi = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    url = code = None
    sent = False
    for raw in proc.stdout:
        print(raw, end="")
        line = ansi.sub("", raw)                 # harness CLIs colorize output
        if url is None:
            m = _re.search(r"https://[^\s\x1b]+", line)
            if m and ("user_code=" in m.group(0) or "device" in m.group(0)):
                url = m.group(0).rstrip(".,)")
                cm = _re.search(r"user_code=([A-Z0-9-]+)", url)
                code = cm.group(1) if cm else None
        if code is None:                         # codex prints the code on its own line
            cm = _re.search(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b", line)
            if cm and "http" not in line:
                code = cm.group(1)
        if url and (code or "user_code=" in url) and not sent:
            sent = True
            send("run.log", {"oauth_url": url, "code": code})
    proc.wait()
    if proc.returncode != 0:
        send("run.log", {"oauth_error": f"login exited {proc.returncode}"})
        stop.set()
        sys.exit(12)
    auth = pathlib.Path(os.path.expanduser(env["DEVCAKE_OAUTH_AUTH_PATH"]))
    send("oauth.result", {"content": auth.read_text()})
    stop.set()
    print("oauth login captured")


def _provision_workspace(spec: dict, env: dict) -> pathlib.Path:
    """Workspace materialization (docs/07 §1): dirs, askpass, activity,
    identity/LFS posture, work-repo clone (mirror or direct), extras.
    Shared by the provision phase and the monolithic rollback path; every
    failure exit is identical to the pre-split flow."""
    (WORKSPACE / "out").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "activity").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / ".devcake").mkdir(parents=True, exist_ok=True)

    repo_url = env["DEVCAKE_REPO_URL"]
    askpass = WORKSPACE / ".devcake" / "askpass.sh"
    askpass.write_text("#!/bin/sh\necho \"$DEVCAKE_FORGE_TOKEN\"\n")
    askpass.chmod(0o700)
    # git auth set up BEFORE the activity clone (ADR-0014 D4) — its per-clone
    # token env override rides the same askpass
    os.environ["GIT_ASKPASS"] = str(askpass)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    for note in materialize_activity(spec, WORKSPACE / "activity",
                                     request_reply):
        print(note)

    clone_user, _gn, _ge, cli_envs = forge_dialect(env)
    clone_url = repo_url.replace("https://", f"https://{clone_user}@")
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_dir = WORKSPACE / "repo"  # canonical path; dir inside named after the repo
    # git auth for clone AND the harness's own push (docs/03 §3, askpass set
    # above); CLI auth for PRs
    if env.get("DEVCAKE_FORGE_TOKEN"):
        for var in cli_envs:
            os.environ[var] = env["DEVCAKE_FORGE_TOKEN"]
    _git_identity_and_lfs(env)

    def clone_failed(detail: str, error_class: str, headline: str) -> None:
        print(f"{headline}:", detail[-500:], file=sys.stderr)
        send_artifacts({"result": None, "exit_code": 13,
                        "error_class": error_class, "error_detail": detail,
                        "transcript_md": f"{headline}:\n{detail}",
                        "token_report": {"extraction_method": "unavailable", "model": None}})
        sys.exit(13)

    mirror_path = env.get("DEVCAKE_MIRROR_PATH", "")
    if mirror_path:
        # ADR-0025 R3: credential-stripped (a file:// clone needs none) with
        # the LFS endpoint pinned to the run's OWN mirror — repo-committed
        # .lfsconfig cannot steer git-lfs anywhere else
        clone = subprocess.run(
            mirror_clone_argv(mirror_path, str(repo_dir / repo_name)),
            capture_output=True, text=True, env=mirror_clone_env(os.environ))
    else:
        clone = subprocess.run(
            ["git", "clone", clone_url, str(repo_dir / repo_name)],
            capture_output=True, text=True)
    if clone.returncode != 0:
        detail = clone.stderr[-2000:]
        if mirror_path:
            # never DEV_FORGE_AUTH: a mirror clone failure is infrastructure
            # (volume unmounted, corrupt pack), and the detail names it
            clone_failed(f"mirror clone failed ({mirror_path}): {detail}",
                         mirror_clone_error_class(detail),
                         "mirror clone failed")
        clone_failed(detail, clone_error_class(detail), "clone failed")
    workdir = repo_dir / repo_name
    if mirror_path:
        # origin must be the REAL forge — push/PR/mid-run fetch identical to
        # a direct clone; the Dev never learns where the bytes came from
        rewrite = subprocess.run(set_origin_cmd(str(workdir), clone_url),
                                 capture_output=True, text=True)
        if rewrite.returncode != 0:
            clone_failed(f"origin rewrite failed: {rewrite.stderr[-1500:]}",
                         "DEV_FORGE", "origin rewrite failed")

    # multi-repo ONBOARD triage (item 2): sibling read-only clones — the
    # playbook's repo_options section names them; failures are non-fatal
    for note in clone_extra_repos(spec.get("extra_repos") or [], repo_dir):
        print(note)
    return workdir


def provision_main() -> None:
    """ADR-0025 step 1: trusted entrypoint code, no agent. Mounts /mirrors
    RO plus this run's workspace, clones everything, writes the marker,
    exits 0. The ONLY dev_entrypoint sender of run.started (the harness
    step's liveness signal is its heartbeat), and never a credential-file
    writer — the app serves this phase a reduced spec (R1)."""
    spec = _fetch_spec("provision")
    env = spec.get("env", {})
    os.environ.update(env)
    err = sentinel_error(WORKSPACE, RUN_ID)
    if err:
        # wrong directory (daemon autocreate / WS_HOST drift) — never
        # provision into it (R4)
        _fail_20(None, "workspace sentinel check failed", err)
    send("run.started", {"container_hostname": os.uname().nodename})
    stop = _start_heartbeats()
    if env.get("DEVCAKE_OAUTH_MODE"):
        # nothing to provision — the login runs in the harness step
        print("provision: oauth run, workspace not needed")
        stop.set()
        return
    workdir = _provision_workspace(spec, env)
    marker = WORKSPACE / MARKER_REL
    try:
        marker.write_text(RUN_ID)
    except OSError as e:
        _fail_20(stop, "provision marker write failed",
                 f"{marker}: {type(e).__name__}: {e}")
    stop.set()
    print(f"provision {RUN_ID} done: {workdir} ready")


def main() -> None:
    # Phase dispatch (ADR-0025): the two-step DAG sets DEVCAKE_PHASE per
    # step; unset means an OLD single-step DAG → monolithic rollback path.
    phase = phase_of(os.environ)
    if phase == "provision":
        provision_main()
        return
    harness_main(monolithic=phase == "monolithic")


def harness_main(monolithic: bool) -> None:
    if monolithic:
        # rollback-only (ADR-0025 R12): an old single-step DAG runs the
        # pre-split flow in one container, order preserved exactly
        spec = _fetch_spec(None)
        env = spec.get("env", {})
        os.environ.update(env)
        _write_credential_files(spec)
        prompt = spec.get("prompt", "")
        send("run.started", {"container_hostname": os.uname().nodename})
        stop = _start_heartbeats()
    else:
        # heartbeat FIRST (ADR-0025 R5): the first beat is immediate, so a
        # phase-2 boot fault (runspec timeout, Redis trouble) keeps the
        # liveness clock ticking on a run already marked running
        stop = _start_heartbeats()
        spec = _fetch_spec("harness")
        env = spec.get("env", {})
        os.environ.update(env)
        _write_credential_files(spec)
        prompt = spec.get("prompt", "")
        # NO run.started here — provision sent it; the app's dispatched-only
        # guard would drop a second send anyway

    # ── OAuth helper mode (docs/08 §4): device-code login, not a mission run ──
    if env.get("DEVCAKE_OAUTH_MODE"):
        _oauth_login(env, stop)
        return

    if monolithic:
        workdir = _provision_workspace(spec, env)
    else:
        err = (sentinel_error(WORKSPACE, RUN_ID)
               or marker_error(WORKSPACE, RUN_ID))
        if err:
            # distinguishes operator-rm/daemon-recreate (root-owned, empty)
            # from a half-finished provision — forensics ride the artifact
            _fail_20(stop, "workspace not provisioned", err)
        repo_url = env["DEVCAKE_REPO_URL"]
        repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        workdir = WORKSPACE / "repo" / repo_name
        # re-adopt the provision step's askpass; recompute what a fresh
        # container forgot (the spec env is authoritative — docs/07 §3)
        askpass = WORKSPACE / ".devcake" / "askpass.sh"
        os.environ["GIT_ASKPASS"] = str(askpass)
        os.environ["GIT_TERMINAL_PROMPT"] = "0"
        _cu, _gn, _ge, cli_envs = forge_dialect(env)
        if env.get("DEVCAKE_FORGE_TOKEN"):
            for var in cli_envs:
                os.environ[var] = env["DEVCAKE_FORGE_TOKEN"]
        _git_identity_and_lfs(env)   # C1: fresh HOME — posture again

    # skill store: materialize selected skills into the harness's registry-
    # declared skills dir — NOT into the repo clone (the Dev would commit
    # them, and codex scans repo .agents/skills)
    for note in install_skills(spec.get("skills") or [],
                               skills_dir=spec.get("skills_dir")
                               or ".claude/skills"):
        print(note)

    failed = run_mcp_setup(spec.get("mcp_setup_commands", []), workdir)  # docs/07 §5 step 5
    if failed:
        cmd, detail = failed
        # `cmd` is the raw config string ($VAR unexpanded — no secret can
        # appear); artifacts mirror the exit-13 clone block so the app maps
        # the failure to a visible DEV_MCP_SETUP run error
        print("mcp setup failed:", cmd, detail[-300:], file=sys.stderr)
        send_artifacts({"result": None, "exit_code": 14,
                        "error_class": "DEV_MCP_SETUP",
                        "error_detail": f"{cmd}: {detail}",
                        "transcript_md": (f"MCP setup command failed:\n`{cmd}`"
                                          f"\n\n```\n{detail}\n```"),
                        "token_report": {"extraction_method": "unavailable", "model": None}})
        sys.exit(14)

    # ── telemetry (stage-2 creds — docs/07 §3) ───────────────────────────────
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "devcake-dev"}))
    # unauthenticated: the endpoint is the stack's otel-collector, which alone
    # holds the OpenObserve credentials — Devs carry none (ISSUES #13)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=env["OTEL_EXPORTER_OTLP_ENDPOINT"])))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("devcake-dev")
    ctx = extract({"traceparent": TRACEPARENT}) if TRACEPARENT else None

    # ── harness (docs/08 §§1,3) ──────────────────────────────────────────────
    harness = os.environ.get("DEVCAKE_HARNESS", "claude-code")
    plan_mode = env.get("DEVCAKE_MISSION_TYPE") == "PLAN"
    extra = shlex.split(env.get("DEVCAKE_EXTRA_ARGS", ""))
    model = env.get("DEVCAKE_MODEL", "").strip()  # per-DevType pin; "" = harness default
    cmd = harness_argv(harness, prompt, plan_mode=plan_mode, model=model,
                       extra=extra)
    inv_prompt = prompt   # THIS invocation's prompt — the nudge on relaunches;
    #                       it is what anchors grok_export_activity honestly

    # ── continuation loop state (ADR-0022) ───────────────────────────────────
    cont_cfg = continuation_config(env)
    cont = ContinuationState()
    mode = "initial"                   # "initial" | "resume" | "fresh"
    chains: list[list[str]] = []       # session-id chains (fork tolerance)
    inv_reports: list[dict] = []       # per-invocation token reports
    inv_modes: list[str] = []          # index-aligned with inv_reports
    dump_segments: list[str] = []      # grok: one per chain (resume REPLACES —
    dump_labels: list[str] = []        #   the export is cumulative);
    #                                      claude/codex: one per invocation
    resume_spec = RESUME_SPECS.get(harness)
    recovered_path, recovery_note = None, ""

    relay = LogRelay()
    # ONE flusher for the whole loop: relay.stop is a one-shot Event, so it is
    # set only at the very end (fail() or the success epilogue) — setting it
    # between invocations would silence every later one.
    flusher = threading.Thread(target=relay.loop, daemon=True)
    flusher.start()

    # The freshness gate for misplaced-result recovery: anything older than
    # this was in the clone before the harness FIRST ran. Pinned to the first
    # launch across relaunches so a stray written by continuation N-1 stays
    # adoptable at landing N (ADR-0018, ADR-0022).
    harness_started_at = time.time()

    # per-type legality (docs/03 §6) — loop-invariant; the nudge prompts name
    # this legal outcome set. First-line defense only — the app enforces the
    # same table authoritatively at finalization (missions.LEGAL_OUTCOMES).
    result_path = WORKSPACE / "out" / "result.json"
    legal_outcomes = {
        "ONBOARD": {"plan_needed", "decomposed", "human_needed"},
        "PLAN": {"planned"},
        "EXECUTE": {"executed", "human_needed"},
        "REVIEW": {"reviewed", "human_needed"},
        "MAPPER": {"relations_mapped"},
    }
    mission_type = env.get("DEVCAKE_MISSION_TYPE", "")
    legal = legal_outcomes.get(mission_type,
                               set().union(*legal_outcomes.values()))

    def load_result(path):
        loaded = json.loads(pathlib.Path(path).read_text())
        assert loaded.get("outcome") in legal, \
            f"outcome {loaded.get('outcome')!r} illegal for {env.get('DEVCAKE_MISSION_TYPE')}"
        assert isinstance(loaded.get("summary"), str)
        return loaded

    # dev.run is opened WITHOUT `with`: fail() exits mid-loop via sys.exit, and
    # the span must be ended + flushed on that path too — both exits close it
    # explicitly. harness.exec children parent onto it via dev_ctx.
    span = tracer.start_span("dev.run", context=ctx)
    span.set_attribute("devcake.run.id", RUN_ID)
    span.set_attribute("devcake.dev_type", env.get("DEVCAKE_DEV_TYPE", ""))
    span.set_attribute("devcake.harness", harness)
    dev_ctx = trace.set_span_in_context(span)

    def run_once(argv_now, attempt):
        """One harness invocation: fresh renderer (GrokCoalescer is stateful),
        fresh line lists (they die with this frame), fresh pump and progress
        threads (_progress_loop binds one proc and self-exits with it). The
        relay and its flusher deliberately live outside the loop."""
        out_lines: list[str] = []
        err_lines: list[str] = []
        render = {"codex": render_codex,
                  "grok-build": GrokCoalescer()}.get(harness, render_claude)
        with tracer.start_as_current_span("harness.exec", context=dev_ctx) as hspan:
            hspan.set_attribute("devcake.continuation", attempt)
            proc = subprocess.Popen(argv_now, cwd=workdir, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
            relay.add(f"[devcake] {harness} started; waiting for model output",
                      visible_output=False)
            pumps = [threading.Thread(target=_pump, daemon=True, args=(
                         proc.stdout, out_lines, render, relay, sys.stdout)),
                     threading.Thread(target=_pump, daemon=True, args=(
                         proc.stderr, err_lines, render_stderr, relay, sys.stderr))]
            progress = threading.Thread(target=_progress_loop, daemon=True,
                                        args=(proc, relay, harness))
            for t in (*pumps, progress):
                t.start()
            exit_code = proc.wait()
            for t in pumps:
                t.join(timeout=10)
            progress.join(timeout=10)
        joined_out = "".join(out_lines)
        joined_err = "".join(err_lines)
        n_bytes, n_lines = len(joined_out), len(out_lines)
        # The list holds the same bytes as the joined copy PLUS per-line object
        # overhead and a pointer array — the larger of the two live copies, and
        # this is the moment the artifact path starts allocating.
        out_lines.clear()
        err_lines.clear()
        return exit_code, joined_out, joined_err, n_bytes, n_lines

    while True:
        harness_exit, out, err_text, out_bytes, out_lines_n = run_once(
            cmd, cont.used)
        span.set_attribute("devcake.outcome", "harness_exit_%d" % harness_exit)

        # ── token extraction + result text (docs/08 §5) ──────────────────────
        token_report = {"extraction_method": "unavailable", "model": harness}
        result_text, transcript_body = "", ""
        codex_last = ""  # RAW `-o` content: result_text is overwritten with a
        #                  stdout tail on any parse failure, so the fault
        #                  predicate must not read it (fixtures README)
        if harness == "codex":
            try:
                last = WORKSPACE / "out" / "last_message.txt"
                codex_last = last.read_text() if last.exists() else ""
                result_text = codex_last
                for line in out.splitlines():       # JSONL events (verified 0.144.1)
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") == "turn.completed":
                        u = ev.get("usage") or {}
                        token_report = {
                            "input_tokens": u.get("input_tokens"),
                            "output_tokens": u.get("output_tokens"),
                            "cache_read_tokens": u.get("cached_input_tokens"),
                            "model": "codex",
                            "extraction_method": "session_json",
                            "notes": f"reasoning_output_tokens={u.get('reasoning_output_tokens')}",
                        }
                if not result_text:
                    result_text = out[-4000:]
            except Exception:
                result_text = out[-4000:]
        elif harness == "grok-build":
            sid, terminal = "", None  # `terminal`: the event carrying usage/turns
            try:
                parsed = grok_stream_parse(out)
                if parsed is not None:
                    result_text, sid = parsed
                    terminal = grok_end_event(out)
                else:  # EXTRA_ARGS overrode the format back to a plain json blob
                    j = json.loads(out)
                    result_text = j.get("text") or ""
                    sid = j.get("sessionId") or ""
                    terminal = j   # same {usage, num_turns, modelUsage} keys
            except Exception:
                result_text = out[-4000:]
            # Token report — its own guard, because a failure here must cost only
            # the report (INV-5 then posts "unavailable"), never the result text or
            # the transcript. The `end` event is PREFERRED: at 0.2.112 it carries
            # the full split inline, needing no session id and no filesystem read
            # (docs/08 §5). `signals.json` stays as the fallback — its survival at
            # this version is an uncommitted campaign note, so dropping it would be
            # as much of a guess as relying on it.
            try:
                token_report = (grok_end_report(terminal)
                                or grok_signals_report(sid) or token_report)
            except Exception:  # noqa: BLE001 — the artifact path outranks its own token report
                print("token extraction failed; reporting unavailable", file=sys.stderr)
            try:
                # no sessionId ⇒ nothing to export: an `error` event never carries
                # one, and the export is the only grok dump source (docs/08 §6)
                exp = (subprocess.run(["grok", "export", sid], capture_output=True,
                                      text=True) if sid else None)
                if exp is not None and exp.returncode == 0 and exp.stdout.strip():
                    transcript_body = exp.stdout
            except Exception:  # noqa: BLE001 — no export ⇒ no dump; the fault predicate handles an empty one
                print("grok export failed; transcript falls back to the agent report",
                      file=sys.stderr)
        else:
            try:
                # stream-json: the final result event carries the exact fields of
                # the old --output-format json blob (verified live); blob fallback
                # covers an EXTRA_ARGS format override
                j = claude_result_event(out) or json.loads(out)
                usage = j.get("usage") or {}
                mu = j.get("modelUsage") or {}
                def _weight(v):  # dominant model = the one that cost/produced the most
                    return ((v.get("costUSD") or 0, v.get("outputTokens") or 0)
                            if isinstance(v, dict) else (0, 0))
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

        # ADR-0014 D1: the full dump of assistant-visible text, per harness
        # (grok: the `grok export` session already includes every message).
        # Guarded like every other parse of `out` — a dump failure must never
        # abort the artifact path (the no-dump fallback handles "").
        try:
            if harness == "codex":
                inv_dump = codex_text_dump(out)
            elif harness == "grok-build":
                inv_dump = transcript_body
            else:
                inv_dump = claude_text_dump(out)
        except Exception:
            inv_dump = ""

        # ── continuation aggregation (ADR-0022) ──────────────────────────────
        # Everything kept across invocations is SMALL (reports, sids, dump
        # segments — bounded by the operator's budget); each invocation's
        # `out` is released below exactly as before. With zero continuations
        # every merged value is byte-identical to its single-invocation input.
        inv_reports.append(token_report)
        inv_modes.append(mode)
        record_session(chains, session_identity(harness, out), mode)
        if (harness == "grok-build" and mode == "resume" and dump_segments
                and (inv_dump or "").strip()):
            # the resumed export is cumulative over the chain — supersede
            dump_segments[-1] = inv_dump
            dump_labels[-1] = f"continuation {cont.used} (resume)"
        else:
            dump_segments.append(inv_dump)
            dump_labels.append("initial" if mode == "initial"
                               else f"continuation {cont.used} ({mode})")
        token_report = merge_token_reports(
            inv_reports, inv_modes,
            resume_cumulative=resume_spec.usage_cumulative if resume_spec
            else False)
        dump = merged_transcript_dump(dump_segments, dump_labels)

        # ── harness verdict (ADR-0018) ───────────────────────────────────────
        # Did the harness actually work? The process exit status alone cannot
        # say: a saturated backend answering 200-with-nothing exits 0, and
        # stderr — the channel classify_harness_failure reads — is empty on
        # every failure we measured. Judged per INVOCATION: this invocation's
        # own dump and prompt (the nudge, on relaunches — what separates the
        # export's echo from new work). Compute BEFORE `out` is released.
        fault = harness_fault(harness, out, harness_exit, dump=inv_dump,
                              last_message=codex_last, prompt=inv_prompt)
        # Every harness, not just claude: codex and grok expose no structured
        # status field, so without this a 401 on either lands on 15 (excusable,
        # correlation-eligible) instead of 12 (latch the breaker, tell the operator).
        api_status = harness_api_error_status(harness, out)
        forensics = workspace_forensics(WORKSPACE / "out", harness_exit, out_bytes,
                                        out_lines_n, err_text[-FORENSIC_STDERR_TAIL:])
        # ADR-0022 PR-1: names the terminal event on the exit-11 paths, where the
        # exit status cannot distinguish "ended cleanly but early" (stopReason
        # EndTurn — the narrate-and-stop shape) from a stream that just stopped.
        # Computed here because `out` is released on the next statement.
        terminal_ev = terminal_evidence(harness, out)
        # `out` is not read again below; releasing the joined copy here keeps the
        # peak off the artifact path, where the payload is serialized repeatedly.
        out = ""

        def fail(code: int, error_class: str, detail: str, transcript: str,
                 **extra) -> None:
            # Shutdown order is load-bearing: end + flush the spans, drain the
            # relay (its stop is one-shot — this is the only failure-path place
            # it is set), THEN artifacts, exactly the drain-before-artifacts
            # ordering the single-invocation flow had.
            payload = {"result": None, "exit_code": code, "error_class": error_class,
                       "error_detail": _one_line(detail, 500), "evidence": forensics,
                       "transcript_md": with_session(
                           f"{transcript}\n\n```json\n"
                           f"{json.dumps(forensics, indent=2)}\n```", dump),
                       "token_report": token_report,
                       "continuations_used": cont.used}
            payload.update(extra)
            span.set_attribute("devcake.continuations", cont.used)
            span.end()
            provider.force_flush()
            relay.stop.set()
            flusher.join(timeout=10)
            send_artifacts(payload)
            stop.set()
            sys.exit(code)

        if harness_exit != 0:
            err = err_text[-1500:]
            # the whole rule lives in the pure helper (docs/15 §4 asymmetry: a
            # false 12 pauses an entire Dev Type) — this only renders it
            code, error_class = classify_nonzero_exit(err, fault, api_status)
            if code == 16:
                fail(16, error_class, fault["detail"],
                     f"harness exited {harness_exit} — turn budget exhausted\n\n"
                     f"{fault['detail']}\n\n```\n{err}\n```")
            if code == 12:
                # a revoked credential leaves stderr EMPTY, so the in-band status
                # is the only detail there is to name
                fail(12, error_class, err or f"api_error_status={api_status}",
                     f"harness exited {harness_exit}\n\n```\n{err}\n```")
            if code == 15:
                fail(15, error_class, fault["detail"],
                     f"harness exited {harness_exit} — {fault['reason']}\n\n"
                     f"{fault['detail']}\n\n```\n{err}\n```")
            fail(10, error_class, err or f"harness exited {harness_exit}",
                 f"harness exited {harness_exit}\n\n```\n{err}\n```")

        # ── result.json (docs/03 §6) ─────────────────────────────────────────
        # Plan mode is read-only — the harness cannot write files, so the
        # entrypoint materializes PLAN.md and result.json from the returned plan
        # text (docs/08 §3). Plan mode NEVER continues (ADR-0022): it either
        # fails here or materializes and succeeds through row 5 below.
        if plan_mode:
            # The fault check runs BEFORE materialization: plan mode's result.json
            # is synthesized from `result_text`, so a backend that returned junk
            # would otherwise be laundered into a "planned" outcome.
            if fault:
                code = 16 if fault["reason"] == FAULT_TURN_BUDGET else 15
                cls = "DEV_TURN_BUDGET" if code == 16 else "DEV_HARNESS_FAULT"
                fail(code, cls, fault["detail"],
                     f"plan mode: {fault['reason']}\n\n{fault['detail']}")
            if len((result_text or "").strip()) < 200:  # a real plan is never this short
                fail(11, "DEV_BAD_OUTPUT",
                     f"plan mode returned {len(result_text or '')} chars",
                     f"plan mode returned no usable plan "
                     f"({len(result_text or '')} chars):\n\n{result_text}",
                     bad_output_reason="empty_plan")
            (WORKSPACE / "out" / "PLAN.md").write_text(result_text)
            (WORKSPACE / "out" / "result.json").write_text(json.dumps({
                "schema_version": 1, "outcome": "planned",
                "summary": result_text.strip().splitlines()[0][:300]}))

        try:
            result = load_result(result_path)      # row 5 — canonical wins
            break
        except Exception as e:
            reason = bad_output_reason(e)
            # Diagnosis is UNCONDITIONAL: whether or not recovery is enabled, a
            # misplaced result.json is named in the artifact, the transcript and
            # the live run terminal, so the prompt can be fixed.
            stray, note = find_result_json(WORKSPACE, workdir, harness_started_at)
            if note:
                print(note, file=sys.stderr)
                try:
                    send("run.log", {"lines": [note]})
                except Exception:  # noqa: BLE001 — advisory relay line; never fail a run over it
                    pass
                reason = "misplaced"
            # rows 6/7 — the harness's own verdict beats any recovered file, so a
            # backend fault plus a stray can never manufacture a PMO transition.
            # A fault on a CONTINUATION invocation lands here too, classified
            # exactly like a first invocation's (ADR-0022: fault machinery
            # outranks the loop, even with budget left).
            if fault:
                code = 16 if fault["reason"] == FAULT_TURN_BUDGET else 15
                cls = "DEV_TURN_BUDGET" if code == 16 else "DEV_HARNESS_FAULT"
                fail(code, cls, f"{fault['detail']} | {note}" if note else fault["detail"],
                     f"{fault['reason']}: no usable result.json ({e})\n\n"
                     f"{note}\n\n{fault['detail']}" if note
                     else f"{fault['reason']}: no usable result.json ({e})\n\n"
                          f"{fault['detail']}")
            # row 8 — recovery, opt-out via config (default on). `stray` can be
            # the canonical path itself when that file exists but is unreadable/
            # invalid; re-reading it would only reproduce the same error.
            if (stray is not None and stray != result_path
                    and env.get("DEVCAKE_RECOVER_MISPLACED_RESULT")):
                try:
                    result = load_result(stray)
                    recovered_path, recovery_note = str(stray), note
                    print(f"[devcake] recovered result.json from {stray}",
                          file=sys.stderr)
                    break
                except Exception as e2:            # the stray is no better
                    forensics["terminal"] = terminal_ev
                    fail(11, "DEV_BAD_OUTPUT", f"{note} | recovered file invalid: {e2}",
                         f"result.json missing/invalid: {e}\n\n{note}\n\n"
                         f"recovered file also invalid: {e2}\n\n---\n\n{result_text}",
                         bad_output_reason=bad_output_reason(e2))
            # ── row 9 — the continuation decision point (ADR-0022) ───────────
            # Exit 0, no fault, nothing to recover: the run looks healthy but
            # is demonstrably unfinished. Fingerprints are taken ONLY here, so
            # healthy runs pay nothing; the comparison is landing N vs N-1.
            decision = next_continuation(
                cont_cfg, cont, plan_mode=plan_mode,
                session_id=last_sid(chains),
                resume_supported=resume_spec is not None,
                fingerprint=workspace_fingerprint(workdir))
            if decision.action == "stop":
                forensics["terminal"] = terminal_ev
                fail(11, "DEV_BAD_OUTPUT", f"{e}{' | ' + note if note else ''}",
                     f"result.json missing/invalid: {e}"
                     + (f"\n\n{note}" if note else "")
                     + (f"\n\ncontinuation: {decision.reason}"
                        if cont_cfg.max_continuations > 0 else "")
                     + f"\n\n---\n\n{result_text}",
                     bad_output_reason=reason)
            if decision.action == "resume":
                mode = "resume"
                inv_prompt = resume_nudge_prompt(
                    mission_type, legal, attempt=cont.used,
                    budget=cont_cfg.max_continuations, stray_note=note)
                cmd = harness_resume_argv(harness, last_sid(chains), inv_prompt,
                                          model=model, extra=extra)
            if decision.action == "fresh" or cmd is None:
                # `cmd is None` is defensive only: a harness in RESUME_SPECS
                # always has a builder arm — degrade to fresh, never crash
                mode = "fresh"
                inv_prompt = fresh_nudge_prompt(
                    prompt, mission_type, legal, attempt=cont.used,
                    budget=cont_cfg.max_continuations, stray_note=note)
                cmd = harness_argv(harness, inv_prompt, plan_mode=False,
                                   model=model, extra=extra)
            relay.add(f"[devcake] continuation {cont.used}/"
                      f"{cont_cfg.max_continuations} ({mode}): "
                      f"{decision.reason} — relaunching {harness}",
                      visible_output=False)

    # ── success epilogue ─────────────────────────────────────────────────────
    span.set_attribute("devcake.continuations", cont.used)
    span.end()
    provider.force_flush()
    relay.stop.set()
    flusher.join(timeout=10)

    plan_path = WORKSPACE / "out" / "PLAN.md"
    transcript = assemble_transcript(
        seq=env.get("DEVCAKE_SEQ"), mtype=env.get("DEVCAKE_MISSION_TYPE"),
        run_id=RUN_ID, dev_type=env.get("DEVCAKE_DEV_TYPE"), harness=harness,
        token_report=token_report, dump=dump, result_text=result_text,
        result=result)
    # ADR-0014 D1: the last message rides separately for the inline feed
    # comment; the app treats a missing/empty key as "post the pointer only".
    # continuations_used (ADR-0022) rides success AND failure payloads — 0 on
    # every run that never needed the loop.
    payload = {"result": result, "transcript_md": transcript,
               "last_message_md": result_text, "token_report": token_report,
               "continuations_used": cont.used}
    if recovered_path:
        # The run succeeds, so there is no failure artifact to carry the
        # evidence — the transcript is the only durable surface, and it is
        # always posted (INV-5). A live relay line alone would vanish.
        payload["recovered_result_path"] = recovered_path
        payload["transcript_md"] = (
            f"> {recovery_note}\n\n" + payload["transcript_md"])
    if plan_path.exists():
        payload["plan_md"] = plan_path.read_text()
    send_artifacts(payload)
    stop.set()
    print(f"dev {RUN_ID} done: {result.get('outcome')}")


if __name__ == "__main__":
    main()
