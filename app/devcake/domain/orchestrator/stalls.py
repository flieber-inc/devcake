"""Stalled dispatches (ADR-0042 §5 addendum): a mission that cannot start
is a state a person must be able to see.

The scheduler tells this module what happened to every candidate it tried
(`observe` for a refused/deferred dispatch, `dispatched` for a launch) and
closes each cycle with `end_cycle`. From that, one ledger per instance —
persisted under /data/state, restart-safe — keeps for every mission the
current *block* (its identity: a kind and a subject), when the block was
first seen, and the *episode* it belongs to. Three outputs:

- `/health.stalled_dispatches` (the admin Overview alert and the board
  badge): every block older than the threshold, with its age.
- ONE ✋ notice on the ticket per block per episode, so a person is
  notified once and never again for the same occurrence.
- The mission's status comment, created early when none exists and edited
  in place, so its Now line says "waiting to start since …" and why.

Rules, each pinned by a test:
- a *dependency* wait (blocked by an open ticket, a parent not finalized)
  is never a stall — it is the gate working;
- a block younger than the threshold is silent (a re-pin's bake);
- the same identity within one episode writes nothing more, however often
  it flickers; the age in the status line is a date, so it never needs an
  edit;
- an episode ends when the mission dispatches (or a person parks/closes
  it); a block after that is a new episode and notifies again;
- a block that cleared without a dispatch and returns after a day counts
  as new (a person tried something and it did not hold);
- at most one write per mission per hour, whatever happens;
- both the notice and the status comment are dropped from the Dev's
  folder by marker: they are about DevCake's inability to proceed, not
  about the mission's work.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ...ports.pmo import PMOBudgetExceeded, PMOTransient
from ..model import LABEL_FAILED, LABEL_NEEDS_HUMAN, LABEL_SKIP, Mission, MissionRef
from ..run import aware, utcnow
from . import feed, status_comment
from .markers import STALL_MARKER

log = logging.getLogger("devcake.missions")

# Kinds a person can act on (loud). Dependency waits are not kinds at all.
KIND_HARNESS = "harness"        # no receipt / not staffed
KIND_UPSTREAM = "upstream"      # ancestor / project activity unavailable
KIND_REPO = "repo"              # repository could not be prepared
KIND_PMO = "pmo"                # the board could not be read at dispatch
KIND_CONFIG = "config"          # secret / assignment / dev type trouble
KIND_OTHER = "other"

_QUIET = re.compile(r"^(blocked by |decomposition of )")
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"no receipt for|not staffed|receipt"), KIND_HARNESS),
    (re.compile(r"upstream activity unavailable"), KIND_UPSTREAM),
    (re.compile(r"repo resolve failed|default branch unresolved|"
                r"workspace base unusable|no repository resolved|"
                r"^repository "), KIND_REPO),
    (re.compile(r"PMO (activity )?read failed"), KIND_PMO),
    (re.compile(r"secret env|unassigned|assigned to Dev Type|Unstaffed|"
                r"dev type "), KIND_CONFIG),
]
_RECEIPT_RE = re.compile(r"no receipt for (\S+)(?: (\S+))?")
_UPSTREAM_KEYS_RE = re.compile(r"dispatch deferred: (.*)$")

REPEAT_AFTER = timedelta(days=1)       # a cleared block returning counts as new
WRITE_CAP = timedelta(hours=1)          # never more than one write per mission
CRITICAL_AFTER = timedelta(days=1)      # the panel paints it critical


def classify(text: str) -> tuple[str, str] | None:
    """(kind, subject) for a deferral reason; None for a dependency wait."""
    t = (text or "").strip()
    if not t or _QUIET.match(t):
        return None
    for rx, kind in _RULES:
        if rx.search(t):
            if kind == KIND_HARNESS:
                m = _RECEIPT_RE.search(t)
                subject = " ".join(x for x in (m.groups() if m else ()) if x)
                return kind, subject or t[:80]
            if kind == KIND_UPSTREAM:
                m = _UPSTREAM_KEYS_RE.search(t)
                keys = sorted({p.split(":", 1)[0].strip()
                               for p in (m.group(1).split(";") if m else [])
                               if p.strip()})
                return kind, ", ".join(keys) or t[:80]
            return kind, t[:80]
    return KIND_OTHER, t[:80]


@dataclass
class Stall:
    kind: str
    subject: str
    text: str
    first_seen: str                       # ISO — this block, this episode
    episode: str                          # ISO — when the episode began
    last_seen: str
    cleared_at: str | None = None
    notified: str | None = None           # "kind|subject" notified this episode
    status_text: str | None = None        # the waiting text last written
    last_write: str | None = None
    seen_cycle: int = field(default=0)

    @property
    def identity(self) -> str:
        return f"{self.kind}|{self.subject}"


def _path() -> Path:
    return (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state"
            / "stalls.json")


class StallLedger:
    """instance → {pmo_id: Stall}; one JSON file, atomic replace."""

    def __init__(self, instance: str, path: Path | None = None):
        self.instance = instance
        self.path = path or _path()
        self.cycle = 0
        self._warned = False
        self.stalls: dict[str, Stall] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 — a corrupt ledger restarts the clocks, never wedges the poll
            log.exception("stall ledger unreadable — starting empty")
            return
        for pmo_id, row in (raw.get(self.instance) or {}).items():
            try:
                self.stalls[pmo_id] = Stall(**{
                    k: v for k, v in row.items() if k in Stall.__dataclass_fields__})
            except Exception:  # noqa: BLE001 — one bad row is dropped
                continue

    def save(self) -> None:
        try:
            try:
                raw = json.loads(self.path.read_text())
            except Exception:  # noqa: BLE001 — absent or corrupt: rewrite
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw[self.instance] = {k: asdict(v) for k, v in self.stalls.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".stalls-")
            with os.fdopen(fd, "w") as fh:
                json.dump(raw, fh, indent=1)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 — advisory telemetry: a read-only data dir must not stop the poll
            if not self._warned:
                self._warned = True
                log.warning("stall ledger not persisted at %s", self.path,
                            exc_info=True)


def ledger(mgr) -> StallLedger:
    led = getattr(mgr, "stall_ledger", None)
    if led is None:
        led = StallLedger(getattr(mgr, "instance_name", "default"),
                          getattr(mgr, "_stall_path", None))
        try:
            mgr.stall_ledger = led
        except Exception:  # noqa: BLE001 — a frozen double
            pass
    return led


def _threshold(mgr) -> timedelta:
    secs = getattr(getattr(mgr, "config", None), "stall_after_seconds", 1800)
    return timedelta(seconds=int(secs or 1800))


def begin_cycle(mgr) -> None:
    ledger(mgr).cycle += 1


def dispatched(mgr, pmo_id: str) -> None:
    """A launch ends the episode: the next block is news again."""
    led = ledger(mgr)
    if led.stalls.pop(pmo_id, None) is not None:
        led.save()


def observe(mgr, mission: Mission, text: str) -> None:
    """The scheduler saw this mission refused or deferred with `text`."""
    cls = classify(text)
    led = ledger(mgr)
    if cls is None:
        if led.stalls.pop(mission.pmo_id, None) is not None:
            led.save()
        return
    kind, subject = cls
    now = utcnow().isoformat()
    cur = led.stalls.get(mission.pmo_id)
    if cur is None:
        led.stalls[mission.pmo_id] = Stall(kind, subject, text, now, now, now,
                                           seen_cycle=led.cycle)
        led.save()
        return
    cur.seen_cycle = led.cycle
    cur.last_seen = now
    if cur.kind == kind and cur.subject == subject:
        if cur.cleared_at and (utcnow() - aware(datetime.fromisoformat(
                cur.cleared_at))) >= REPEAT_AFTER:
            # returned after a day without a dispatch: a new block
            cur.first_seen, cur.notified, cur.status_text = now, None, None
        cur.cleared_at = None
        cur.text = text
    else:
        # a different block in the same episode: new identity, new clock
        cur.kind, cur.subject, cur.text = kind, subject, text
        cur.first_seen, cur.cleared_at = now, None
    led.save()


def _age(s: Stall) -> timedelta:
    return utcnow() - aware(datetime.fromisoformat(s.first_seen))


def stalled(mgr) -> list[dict]:
    """Blocks past the threshold, for /health (advisory)."""
    led = ledger(mgr)
    out = []
    for pmo_id, s in led.stalls.items():
        if s.cleared_at or _age(s) < _threshold(mgr):
            continue
        age = _age(s)
        out.append({"pmo_id": pmo_id, "kind": s.kind, "subject": s.subject,
                    "text": s.text, "since": s.first_seen,
                    "seconds": int(age.total_seconds()),
                    "severity": "critical" if age >= CRITICAL_AFTER else "warning"})
    return out


def _words(s: Stall) -> tuple[str, str]:
    """(what, todo) in plain words for the ticket."""
    if s.kind == KIND_UPSTREAM:
        return (f"the activity of {s.subject}, which this mission descends "
                f"from, is not on the board any more (archived or deleted?)",
                "Unarchive it, or turn off strict context sourcing for this "
                "board, and DevCake starts the mission on its next poll.")
    if s.kind == KIND_HARNESS:
        return (f"the Dev image for {s.subject} has no receipt yet",
                "A running bake clears this by itself; otherwise check the "
                "Fleet page.")
    if s.kind == KIND_REPO:
        return (f"its repository could not be prepared: {s.text}",
                "Check the repository's card on the Repos page.")
    if s.kind == KIND_PMO:
        return (f"the board could not be read at dispatch: {s.text}",
                "Check the PMO connection on the Connections page.")
    if s.kind == KIND_CONFIG:
        return (f"its Dev Type is not ready: {s.text}",
                "Fix it on the Config page.")
    return (s.text, "Check the admin panel's Overview alert.")


def waiting_line(s: Stall) -> str:
    since = aware(datetime.fromisoformat(s.first_seen))
    what, _todo = _words(s)
    return (f"⏳ Waiting to start since {since:%Y-%m-%d %H:%M} UTC — {what}. "
            f"See the ✋ notice above.")


def _may_write(s: Stall) -> bool:
    if not s.last_write:
        return True
    return utcnow() - aware(datetime.fromisoformat(s.last_write)) >= WRITE_CAP


async def end_cycle(mgr, missions: list[Mission]) -> None:
    """Close the cycle: blocks not seen this cycle are cleared (the episode
    goes on); blocks past the threshold are surfaced on the ticket —
    once per identity per episode. Never raises."""
    led = ledger(mgr)
    by_id = {m.pmo_id: m for m in missions}
    changed = False
    for pmo_id, s in list(led.stalls.items()):
        m = by_id.get(pmo_id)
        if m is None or m.status in ("done", "canceled") or \
                m.labels & {LABEL_SKIP, LABEL_FAILED, LABEL_NEEDS_HUMAN}:
            # gone, closed, or a person took it: the episode is over
            led.stalls.pop(pmo_id, None)
            changed = True
            continue
        if s.seen_cycle != led.cycle and not s.cleared_at:
            s.cleared_at = utcnow().isoformat()
            changed = True
            if s.status_text and _may_write(s):
                try:
                    await _status(mgr, m, None)
                    s.status_text, s.last_write = None, utcnow().isoformat()
                except Exception:  # noqa: BLE001 — retried next cycle
                    log.debug("status clear deferred for %s", m.key)
            continue
        if s.cleared_at or _age(s) < _threshold(mgr):
            continue
        if s.notified == s.identity and s.status_text == waiting_line(s):
            continue
        if not _may_write(s):
            continue
        try:
            await _surface(mgr, m, s)
            changed = True
        except (PMOBudgetExceeded, PMOTransient) as e:
            log.info("stall surfacing deferred for %s: %s", m.key, e)
        except Exception:  # noqa: BLE001 — a view/notice never breaks the poll
            log.exception("stall surfacing failed for %s", m.key)
    if changed:
        led.save()


async def _surface(mgr, m: Mission, s: Stall) -> None:
    if m.pmo_kind != "issue":
        return
    if s.notified != s.identity:
        what, todo = _words(s)
        body = feed.notice(
            mgr, feed.NEEDS_YOU,
            f"This mission cannot start: {what}.",
            f"{STALL_MARKER} kind={s.kind}\n\n{s.text}",
            todo=todo)
        await mgr._feed(m.pmo_id, "issue", body, externalize=False)
        mgr._audit(m.pmo_id, "stall_notice", f"{s.kind}: {s.subject}")
        s.notified = s.identity
        s.last_write = utcnow().isoformat()
    line = waiting_line(s)
    if s.status_text != line:
        await _status(mgr, m, line)
        mgr._audit(m.pmo_id, "stall_status", s.kind)
        s.status_text = line
        s.last_write = utcnow().isoformat()


async def _status(mgr, m: Mission, waiting: str | None) -> None:
    """Create the status comment when none exists (one full read to find
    an existing one), else edit it in place with the waiting line."""
    runs = status_comment.runs_of(mgr, m.pmo_id)
    act = await mgr.pmo.get_activity(MissionRef(m.pmo_id, "issue"), full=True)
    entry = (status_comment._known_entry(mgr, m.pmo_id, None)
             or feed.find_status_entry(act.entries) or "")
    body = status_comment.render(mgr, m, runs, pr_url=None,
                                 collapsible=feed.collapsible_of(mgr),
                                 waiting=waiting, entries=act.entries)
    if entry:
        await mgr._edit(m.pmo_id, "issue", entry, body)
    else:
        entry = await mgr._feed(m.pmo_id, "issue", body, externalize=False) or ""
        if entry:
            mgr._audit(m.pmo_id, "status_comment_created", entry)
    if entry:
        status_comment._cache(mgr)[m.pmo_id] = entry
