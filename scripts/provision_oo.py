#!/usr/bin/env python3
"""Provision the DevCake dashboard (+ alerts, when a destination is configured)
in OpenObserve — docs/12 §5, docs/15 §6.

Dual path (docs/12 §5): app boot already creates/resyncs the OO ingest service
account for telemetry connectivity. This host script still ensures ingest
(repair without restart), then creates the **DevCake** dashboard and optional
alerts. Neither path is required for the other; this script is ops polish, not
a connectivity prerequisite.

Idempotency (honest): create-if-missing by name. An existing dashboard titled
DevCake is left untouched (panels are **not** updated in place — delete and
re-create, or edit in OO, when the panel schema changes). Alert/destination
POSTs treat an “already exists” body as success. Fail-loud on every other
error (A20). Never prints passwords.
"""

import base64
import json
import sys
import urllib.error
import urllib.request


def env(name, default=None):
    for line in open(".env"):
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    if default is not None:
        return default
    sys.exit(f"missing {name}")


OO = env("OO_URL", "http://localhost:5080")
ORG = env("OO_ORG", "default")   # must match the org the collector/app target
AUTH = base64.b64encode(f"{env('OO_ROOT_EMAIL')}:{env('OO_ROOT_PASSWORD')}".encode()).decode()
WEBHOOK = env("OO_ALERT_WEBHOOK", "")


def req(method, path, body=None, auth=None):
    r = urllib.request.Request(f"{OO}/api/{ORG}{path}", method=method,
                               data=json.dumps(body).encode() if body else None,
                               headers={"Authorization": f"Basic {auth or AUTH}",
                                        "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:400]}


def ensure_ingest_user():
    """Idempotently create the OO service account the stack authenticates
    with (collector, fluentbit, app push_oo_log).

    Primary path: app boot runs the same logic
    (``devcake.telemetry.oo_provision.ensure_oo_ingest_user``) so a filled
    .env + compose up is enough. This host-side copy remains for operators
    who want to repair credentials without restarting the app, and for the
    dashboard/alerts steps below.

    Live-verified on OO v0.91.1: accepted roles are `admin` and
    `service_account` ("member" is refused: "Custom roles not allowed");
    passwords need upper+lower+digit+special. NOTE (docs/14 §2 Zone B): OSS
    OpenObserve role separation is advisory — the real boundary is that Dev
    containers hold no OO credentials at all.

    Fail-loud contract: any failure exits non-zero."""
    email = env("OO_INGEST_EMAIL", "")
    password = env("OO_INGEST_PASSWORD", "")
    if not email.strip() or not password.strip():
        sys.exit("OO_INGEST_EMAIL / OO_INGEST_PASSWORD must be set in .env "
                 "(the OO service account)")
    ingest_auth = base64.b64encode(f"{email}:{password}".encode()).decode()

    def creds_work() -> bool:
        return "_error" not in req("GET", "/streams?type=logs", auth=ingest_auth)

    users = req("GET", "/users")
    if "_error" in users:
        sys.exit(f"cannot list OO users (root creds wrong? OO down?): {users}")
    if any(u.get("email") == email for u in (users.get("data") or [])):
        if creds_work():
            print(f"ingest user: {email} exists, credentials verified")
            return
        out = req("PUT", f"/users/{email}",
                  {"new_password": password, "change_password": True})
        if "_error" in out or not creds_work():
            sys.exit(f"ingest user {email} exists but .env password does not "
                     f"authenticate and resync failed: {out}")
        print(f"ingest user: {email} password resynced from .env")
        return
    out = req("POST", "/users", {"email": email, "password": password,
                                 "first_name": "DevCake", "last_name": "Ingest",
                                 "role": "service_account"})
    if "_error" in out:
        sys.exit(f"creating ingest user failed: {out}")
    if not creds_work():
        sys.exit(f"ingest user created but credentials do not authenticate — "
                 f"check OO_URL/OO_ORG ({OO}/api/{ORG})")
    print("ingest user:", out)


ensure_ingest_user()


def panel(pid, title, sql, ptype="line", x=0, y=0, w=12, h=8):
    return {
        "id": f"Panel_ID{pid}", "type": ptype, "title": title, "description": "",
        "config": {"show_legends": True, "legends_position": None, "decimals": 2},
        "queryType": "sql",
        "queries": [{"query": sql, "customQuery": True,
                     "fields": {"stream": "default", "stream_type": "traces",
                                "x": [], "y": [],
                                "filter": {"filterType": "group",
                                           "logicalOperator": "AND", "conditions": []}},
                     "config": {"promql_legend": ""}}],
        "layout": {"x": x, "y": y, "w": w, "h": h, "i": pid},  # i must be numeric
    }


DASHBOARD = {
    "version": 5,
    "title": "DevCake",
    "description": "Cost, reliability, and throughput — docs/12 §5",
    "role": "", "owner": "devcake",
    "tabs": [{
        "tabId": "default", "name": "v0",
        "panels": [
            # Cost / outcome panels constrain to run.finalize (docs/12 §3–§5;
            # tokens/cost and mission outcomes live there — not on dev.run).
            panel(1, "Cost per hour (USD, by dev type)",
                  "SELECT histogram(_timestamp, '1 hour') AS ts, devcake_dev_type, "
                  "SUM(devcake_cost_usd) AS cost FROM default "
                  "WHERE operation_name = 'run.finalize' AND devcake_cost_usd > 0 "
                  "GROUP BY ts, devcake_dev_type ORDER BY ts",
                  "line", 0, 0, 24, 9),
            panel(2, "Dev runs by outcome (daily)",
                  "SELECT histogram(_timestamp, '1 day') AS ts, devcake_outcome, "
                  "COUNT(*) AS runs FROM default WHERE operation_name = 'run.finalize' "
                  "GROUP BY ts, devcake_outcome ORDER BY ts",
                  "bar", 0, 9, 12, 9),
            panel(3, "Failure signals (kills, give-ups)",
                  "SELECT histogram(_timestamp, '1 hour') AS ts, operation_name, "
                  "COUNT(*) AS n FROM default WHERE operation_name IN "
                  "('watchdog.kill', 'mission.give_up') GROUP BY ts, operation_name ORDER BY ts",
                  "bar", 12, 9, 12, 9),
            # ADR-0021: estimated spend rides its own attribute — panel 1
            # stays "billed as reported by the harness", this one is the
            # labeled rate-card estimate (grok and friends)
            panel(4, "Estimated cost per hour (USD, by dev type — rate card)",
                  "SELECT histogram(_timestamp, '1 hour') AS ts, devcake_dev_type, "
                  "SUM(devcake_cost_usd_estimated) AS cost_estimated FROM default "
                  "WHERE operation_name = 'run.finalize' AND "
                  "devcake_cost_usd_estimated > 0 "
                  "GROUP BY ts, devcake_dev_type ORDER BY ts",
                  "line", 0, 18, 24, 9),
        ],
    }],
    "variables": {"list": []},
}

failures: list[str] = []   # fail-loud on EVERY error, not only ingest (A20)


def _failed(out) -> bool:
    """A real provisioning failure — an 'already exists' response on re-run
    is idempotent success, not an error."""
    if not (isinstance(out, dict) and "_error" in out):
        return False
    return "exist" not in str(out.get("_body", "")).lower()

existing = req("GET", "/dashboards")
names = [d.get("title") for d in (existing.get("dashboards") or [])]
if "DevCake" in names:
    print("dashboard 'DevCake' already exists — leaving it")
else:
    out = req("POST", "/dashboards", DASHBOARD)
    print("dashboard:", "created" if "_error" not in out else out)
    if _failed(out):
        failures.append(f"dashboard: {out}")

if not WEBHOOK:
    print("alerts: skipped (set OO_ALERT_WEBHOOK in .env to provision — docs/15 §6)")
    if failures:
        sys.exit("provision_oo FAILED: " + "; ".join(failures))
    sys.exit(0)

dest = req("POST", "/alerts/destinations",
           {"name": "devcake-webhook", "url": WEBHOOK, "method": "post",
            "skip_tls_verify": False, "template": "devcake-default"})
print("destination:", dest)
if _failed(dest):
    failures.append(f"destination: {dest}")
# Full documented alert set (docs/15 §6). Every query targets a
# span the app actually emits (verified against the tracer inventory):
# mission.give_up, watchdog.kill, audit.event (devcake_audit_action mirrors
# the audit log), breaker.trip, poll.instance PMO_TRANSIENT (per-instance
# child — not poll.cycle), forge.probe_transient, ingress.poison, and
# devcake_cost_usd on run.finalize only.
DAILY_COST_USD = float(env("OO_DAILY_COST_ALERT_USD", "50"))
for name, sql, period, threshold in [
    ("devcake-give-up", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "operation_name = 'mission.give_up'", 10, 1),
    ("devcake-kills", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "operation_name = 'watchdog.kill'", 10, 1),
    ("devcake-needs-human", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "operation_name = 'audit.event' AND "
     "devcake_audit_action = 'devcake_needs_human'", 15, 1),
    ("devcake-dev-auth-breaker", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "operation_name = 'breaker.trip'", 15, 1),
    ("devcake-pmo-forge-transient", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "(operation_name = 'poll.instance' AND devcake_outcome = 'PMO_TRANSIENT') "
     "OR operation_name = 'forge.probe_transient'", 15, 3),
    ("devcake-poison", "SELECT COUNT(*) as cnt FROM \"default\" WHERE "
     "operation_name = 'ingress.poison'", 10, 1),
    ("devcake-daily-cost", "SELECT SUM(CAST(devcake_cost_usd AS FLOAT)) as cnt "
     "FROM \"default\" WHERE operation_name = 'run.finalize' AND "
     "devcake_cost_usd IS NOT NULL", 60 * 24,
     DAILY_COST_USD),
]:
    out = req("POST", "/default/alerts",
              {"name": name, "stream_type": "traces", "stream_name": "default",
               "is_real_time": False,
               "query_condition": {"sql": sql, "type": "sql"},
               "trigger_condition": {"period": period, "threshold": threshold,
                                     "operator": ">=", "frequency": min(period, 60),
                                     "silence": 30},
               "destinations": ["devcake-webhook"], "enabled": True})
    print(f"alert {name}:", out)
    if _failed(out):
        failures.append(f"alert {name}: {out}")

if failures:
    sys.exit("provision_oo FAILED: " + "; ".join(failures))
