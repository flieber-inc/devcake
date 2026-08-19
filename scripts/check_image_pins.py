#!/usr/bin/env python3
"""Digest-pin gate (docs/16 M8, ISSUES #29): every external base image —
Dockerfile `# syntax=` frontends / ARG defaults / FROM lines / COPY --from
refs and compose `image:` values — must be pinned `tag@sha256:<64 hex>`.
Local bake images (devcake/*) and stage references are exempt; every
devcake/* compose service must declare `pull_policy: never` (otherwise a
typo'd tag would pull from the public — squattable — Docker Hub `devcake`
namespace). Exit non-zero listing offenders.

Files are DISCOVERED (rglob Dockerfile*, glob docker-compose*.yml, glob
scripts/*.sh + scripts/*.py), not hardcoded — a new Dockerfile or a
docker-compose.override.yml (which compose auto-loads) is scanned the moment
it exists (audit A14). `docker run` invocations in ops scripts are covered
too (2026-08-12 audit OPS-M1: the backup/restore containers — root, RW on
secrets-bearing volumes — ran floating `alpine`, structurally invisible to
this gate): the image token must be pinned, a devcake/* local image, or a
variable whose in-file default resolves to one.

Bump procedure: docker buildx imagetools inspect <image:tag> → paste the
manifest-list digest.

Harness CLI versions are a different pin (images/Dockerfile ARG defaults;
Grok via `ARG GROK_VERSION` + installer argv). This checker covers image
refs only.
"""

import ast
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_PARTS = {".git", "node_modules", ".buildx-cache", "dist"}

PINNED = re.compile(r"@sha256:[0-9a-f]{64}\b")
LOCAL_PREFIX = "devcake/"


def _dockerfiles() -> list[Path]:
    return sorted(p for p in ROOT.rglob("Dockerfile*")
                  if not (_SKIP_PARTS & set(p.parts)) and p.is_file())


def _compose_files() -> list[Path]:
    return sorted(ROOT.glob("docker-compose*.yml")) + sorted(
        ROOT.glob("docker-compose*.yaml"))


def _script_files() -> tuple[list[Path], list[Path]]:
    scripts = ROOT / "scripts"
    return (sorted(scripts.glob("*.sh")), sorted(scripts.glob("*.py")))


def _strip_platform(tokens: list[str]) -> list[str]:
    """FROM/COPY may carry --platform=<x> (and COPY --chown/--chmod) before
    the ref — flags are not the base image (audit A14 false positive)."""
    return [t for t in tokens if not t.startswith("--")]


def check_dockerfile(path: Path, offenders: list[str]) -> None:
    """Every `# syntax=` frontend, FROM, and external COPY --from must
    resolve to a pinned ref: stage refs and bare stage indexes are local; a
    ${VAR} base is resolved against ANY collected ARG default (not just
    *_IMAGE-named ones — a rename must not slip past the gate)."""
    args: dict[str, str] = {}
    stages: set[str] = set()
    rel = path.relative_to(ROOT)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        # BuildKit syntax frontend is an EXTERNAL image pull on every cold
        # bake — floating tags are the same class of drift as unpinned FROM.
        m = re.match(r"#\s*syntax\s*=\s*(\S+)", line, re.IGNORECASE)
        if m:
            if not PINNED.search(m.group(1)):
                offenders.append(f"{rel}:{lineno}: {line}")
            continue
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)", line)
        if m:
            args[m.group(1)] = m.group(2)
            continue
        m = re.match(r"COPY\s+(.*)", line, re.IGNORECASE)
        if m:
            src = re.search(r"--from=(\S+)", m.group(1))
            if src:
                ref = src.group(1)
                # a stage alias, a bare stage index, or a local bake image is
                # fine; anything else pulls an EXTERNAL image at build time
                # exactly like FROM and must be pinned (audit A14)
                if (ref not in stages and not ref.isdigit()
                        and not ref.startswith(LOCAL_PREFIX)
                        and not PINNED.search(ref)):
                    offenders.append(f"{rel}:{lineno}: {line}")
            continue
        m = re.match(r"FROM\s+(.+)", line, re.IGNORECASE)
        if not m:
            continue
        tokens = _strip_platform(m.group(1).split())
        if not tokens:
            continue
        base = tokens[0]
        alias = None
        if len(tokens) >= 3 and tokens[1].upper() == "AS":
            alias = tokens[2]
        if alias:
            stages.add(alias)
        if base in stages:
            continue                       # local stage reference
        var = re.fullmatch(r"\$\{(\w+)(?::-[^}]*)?\}", base)
        if var:
            base = args.get(var.group(1), "")
            if not base:
                offenders.append(
                    f"{rel}:{lineno}: FROM ${{{var.group(1)}}} "
                    f"has no in-file ARG default to verify")
                continue
        if not PINNED.search(base):
            offenders.append(f"{rel}:{lineno}: {line}")


def check_compose(path: Path, offenders: list[str]) -> None:
    """External images must be pinned; devcake/* images are exempt from
    pinning but MUST carry `pull_policy: never` in the same service block —
    the exemption otherwise trusts an unclaimed public registry namespace."""
    rel = path.relative_to(ROOT)
    lines = path.read_text().splitlines()
    for lineno, raw in enumerate(lines, 1):
        m = re.match(r"(\s*)image:\s*(\S+)", raw)
        if not m:
            continue
        indent, ref = m.group(1), m.group(2)
        # exact-boundary match: only local bake images (devcake/<name>) are
        # exempt — a registry image merely NAMED devcake-something is not
        if ref.startswith(LOCAL_PREFIX):
            if not _service_block_has(lines, lineno - 1, len(indent),
                                      "pull_policy: never"):
                offenders.append(
                    f"{rel}:{lineno}: {ref} is a local image without "
                    f"pull_policy: never — a missing local tag would pull "
                    f"from the public devcake/ Docker Hub namespace")
            continue
        if not PINNED.search(ref):
            offenders.append(f"{rel}:{lineno}: {raw.strip()}")


# --- Dagu pin ↔ dev-run.yaml decode coupling (audit 2026-08-13) -------------

# dev-run.yaml's nested `host: {resources: {…}}` block is correct ONLY while
# the pinned Dagu lacks the mapstructure Squash fix (dagucloud/dagu#2557, our
# upstream PR): 2.13.0 silently DROPS the embedded HostConfig.Resources
# fields in the flat form, and post-fix the nested form dies while flattened
# fields start working. A bump past the fix would silently zero
# Memory/NanoCPUs/PidsLimit AND drop the /dev/fuse + /dev/net/tun devices —
# and nothing else in CI would go red. This tripwire does.
DAGU_COUPLED_TAG = "2.13.0"


def check_dagu_coupling(path: Path, offenders: list[str]) -> None:
    rel = path.relative_to(ROOT)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"\s*image:\s*\S*dagucloud/dagu:([^@\s]+)", raw)
        if m and m.group(1) != DAGU_COUPLED_TAG:
            offenders.append(
                f"{rel}:{lineno}: Dagu bumped {DAGU_COUPLED_TAG} → "
                f"{m.group(1)}, but dev-run.yaml's nested host.resources "
                f"block (Memory/NanoCPUs/PidsLimit/Devices) is decode-coupled "
                f"to {DAGU_COUPLED_TAG} (dagucloud/dagu#2557): if the Squash "
                f"fix landed, FLATTEN those fields into direct host: keys, "
                f"re-verify live per docs/13 §4, then update "
                f"DAGU_COUPLED_TAG in this gate")


def _service_block_has(lines: list[str], image_idx: int, indent: int,
                       needle: str) -> bool:
    """Scan the service block around the image: line — sibling keys share the
    image line's indentation; the block ends at a shallower indent."""
    def _siblings(rng):
        for i in rng:
            raw = lines[i]
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            cur = len(raw) - len(raw.lstrip())
            if cur < indent:
                break                      # left the service block
            if cur == indent:
                yield raw.strip()
    for stripped in _siblings(range(image_idx + 1, len(lines))):
        if stripped.startswith(needle):
            return True
    for stripped in _siblings(range(image_idx - 1, -1, -1)):
        if stripped.startswith(needle):
            return True
    return False


# --- `docker run` in ops scripts (2026-08-12 audit OPS-M1) -----------------

# docker-run flags that consume the NEXT token as their value — the walk must
# skip both, or a flag value would be mistaken for the image ref.
_RUN_FLAGS_WITH_VALUE = {
    "-e", "--env", "--env-file", "-v", "--volume", "--mount", "-w",
    "--workdir", "-u", "--user", "--name", "--network", "--network-alias",
    "--entrypoint", "--platform", "-p", "--publish", "--expose", "-l",
    "--label", "--hostname", "--tmpfs", "--add-host", "--cap-add",
    "--cap-drop", "--device", "--gpus", "--restart", "--memory", "-m",
    "--cpus", "--pids-limit", "--security-opt", "--log-driver", "--log-opt",
    "--stop-timeout", "--pull", "--health-cmd", "--health-interval",
}

_ARRAY_SPLICE = re.compile(r"\$\{\w+\[@\]\}")
_VAR_TOKEN = re.compile(r"\$\{?(\w+)\}?$")

_UNRESOLVED = ""  # sentinel: an image token exists but is not a literal


def _walk_run_args(tokens: list) -> str | None:
    """First non-flag token after `run` is the image ref. Entries are str
    for literal tokens, None for opaque ones (python f-strings). Returns the
    token, _UNRESOLVED for an opaque image position, None if none found."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok is None:
            return _UNRESOLVED
        tok = tok.strip("\"'")
        if _ARRAY_SPLICE.fullmatch(tok):
            i += 1                       # bash array splice: args, not image
            continue
        if tok in _RUN_FLAGS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1                       # standalone flag or --flag=value
            continue
        return tok
    return None


def _shell_logical_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = lineno
        line = raw.rstrip()
        if line.endswith("\\"):
            buf.append(line[:-1])
            continue
        buf.append(line)
        out.append((start, " ".join(buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _resolve_shell_default(name: str, text: str) -> str | None:
    """Resolve NAME=VALUE to a literal, unwrapping ${VAR:-default} layers —
    `IMG="${OVERRIDE:-alpine@sha256:…}"` pins via its in-file default."""
    m = re.search(rf"^\s*{re.escape(name)}=(\S+)", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip().strip("\"'")
    while True:
        inner = re.fullmatch(r"\$\{\w+:-(.*)\}", val)
        if not inner:
            return val
        val = inner.group(1)


def _check_script_image(rel, lineno: int, img, text: str,
                        offenders: list[str]) -> None:
    if img is None:
        offenders.append(
            f"{rel}:{lineno}: `docker run` with no locatable image token — "
            f"restructure so the gate can verify the pin")
        return
    if img is not _UNRESOLVED:
        var = _VAR_TOKEN.fullmatch(img)
        if var:
            resolved = _resolve_shell_default(var.group(1), text)
            if resolved is None:
                offenders.append(
                    f"{rel}:{lineno}: image ${var.group(1)} has no in-file "
                    f"literal default to verify — give it a pinned default")
                return
            img = resolved
    if img is _UNRESOLVED:
        offenders.append(
            f"{rel}:{lineno}: image ref is not a resolvable literal — "
            f"pin it via a module/script constant")
        return
    if img.startswith(LOCAL_PREFIX) or PINNED.search(img):
        return
    offenders.append(f"{rel}:{lineno}: docker run {img}")


def check_shell_script(path: Path, offenders: list[str]) -> None:
    rel = path.relative_to(ROOT)
    text = path.read_text()
    for lineno, line in _shell_logical_lines(text):
        if "docker run" not in line or line.lstrip().startswith("#"):
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            # unterminated quote — a `docker run … sh -c '…multi-line…'`
            # whose payload opens on this logical line; naive split still
            # exposes the flag/image tokens, quotes stripped in the walk
            tokens = line.split()
        idx = next((i for i in range(len(tokens) - 1)
                    if tokens[i].strip("\"'").split("(")[-1] == "docker"
                    and tokens[i + 1] == "run"), None)
        if idx is None:
            continue
        img = _walk_run_args(tokens[idx + 2:])
        _check_script_image(rel, lineno, img, text, offenders)


def check_python_script(path: Path, offenders: list[str]) -> None:
    """Scan list literals shaped ["docker", "run", …] — the subprocess
    idiom. Module-level str constants resolve (export_receipts.ALPINE_IMAGE);
    anything non-literal in image position is an offender, fail-closed."""
    rel = path.relative_to(ROOT)
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return
    consts: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            try:
                val = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(val, str):
                consts[node.targets[0].id] = val
    for node in ast.walk(tree):
        if not (isinstance(node, ast.List) and len(node.elts) >= 3):
            continue
        head = [e.value for e in node.elts[:2]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if head != ["docker", "run"]:
            continue
        tokens: list = []
        for e in node.elts[2:]:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                tokens.append(e.value)
            elif isinstance(e, ast.Name) and e.id in consts:
                tokens.append(consts[e.id])
            else:
                tokens.append(None)
        img = _walk_run_args(tokens)
        _check_script_image(rel, node.lineno, img, "", offenders)


def main() -> int:
    offenders: list[str] = []
    for df in _dockerfiles():
        check_dockerfile(df, offenders)
    for cf in _compose_files():
        check_compose(cf, offenders)
        check_dagu_coupling(cf, offenders)
    sh_files, py_files = _script_files()
    for f in sh_files:
        check_shell_script(f, offenders)
    for f in py_files:
        check_python_script(f, offenders)
    if offenders:
        print("UNPINNED image references (ISSUES #29 — pin tag@sha256:…):")
        for o in offenders:
            print(f"  {o}")
        return 1
    print("image pins: all external references digest-pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
