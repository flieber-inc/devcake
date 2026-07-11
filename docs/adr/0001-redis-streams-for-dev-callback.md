# ADR-0001 — Redis Streams for the Dev → App Channel

**Status:** accepted (v0).

## Context

Devs run as ephemeral sibling containers and must deliver artifacts (transcript, token report, `result.json`) and liveness signals to the main app, and occasionally request data (the activity feed). The mission doc places Redis in the architecture as the mediator of Dev↔app traffic, and separately mentions Devs having "access to some endpoint that is able to update/communicate with the PMO System" — which could be read as a second, HTTP channel.

## Decision

One channel: **Redis Streams** (`devcake:ingress` with a consumer group; per-run reply streams for request/reply). The "endpoint" is the `devcake-relay` CLI speaking this protocol; the app is the sole PMO client (INV-4). Protocol in `09-messaging.md`.

## Alternatives considered

- **HTTP callbacks to the app** — requires the app to be up at Dev completion; every Dev image reimplements retry/queue logic; no replay after an app crash mid-finalization.
- **Both HTTP and Redis** — two contracts to keep consistent for no added capability.
- **Direct PMO writes from Devs** — spreads idempotency/compare-and-transition logic across four harness environments and lets a malfunctioning harness corrupt PMO state.

## Consequences

Devs can finish while the app is down and nothing is lost (streams are durable, AOF-persisted). At-least-once delivery requires idempotent consumption — provided by finalization's per-side-effect keys. Redis becomes a hard runtime dependency of Dev runs; acceptable, it is already in the stack.
