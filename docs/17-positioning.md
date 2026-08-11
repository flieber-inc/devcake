# 17 — Positioning: What This Is, and How We Say It

> **Audience:** anyone writing outward-facing words about this project — README,
> website, pitch, launch post. The README is the applied version of this doc;
> the full argument it compresses is [`19-thesis.md`](19-thesis.md).
> **Status:** adopted at v0 close; last revised 2026-08-11 — §1, §1c, §6.
> **Aligned with the product security contract**
> in [`14-security.md`](14-security.md). Outward copy must not claim a stronger
> posture than `14`.

## 1. The core insight

DevCake is a **meta-harness** — a CLI agent orchestrator. It does not replace
Claude Code, Grok Build, or Codex; it **staffs** them. Those tools are model
harnesses: one session between a model and its tools. DevCake is the outer
envelope that turns board work into sequenced, isolated, accountable harness
runs — swap the workers without changing the control plane.

Most AI-developer products ask you to manage each task in a chat window, a
special IDE, or someone else's work queue. DevCake keeps the **day-to-day
Mission interface** on the task board you already run: write a ticket, get work
back on a pull request, approve it or send it back with notes. Its bundled
admin UI is the separate configuration and operations surface — health, runs,
credentials, and maintenance — not another Mission queue.

**Motto: "Your board is the interface."**  
(Long form: *Your board is the interface. Tickets dispatch work; labels steer
it; finished work comes back on a PR — with a receipt.*)

There is no second day-to-day work queue — and no pretense that the work runs
itself.
You self-host it, you decide who writes tickets, you protect the default
branch, and you hold the merge button. That ownership is the deal, stated
plainly: it is *why* the output can be trusted, not fine print to walk back
(`14` §9; recurring duties in [`18-operator-contract.md`](18-operator-contract.md)).

The second pillar is **autonomy with receipts.** Every step posts its transcript
and token bill to the ticket; dispatches, kills, sweeps, and PMO writes are
traced. REVIEW is always a pipeline stage; the **reviewer token** (app-only
formal forge approval) is the security-relevant second identity when branch
protection requires it. **Unless you turn on auto-merge**, a human merges
before Done. Autonomy you can audit is the only autonomy worth shipping —
receipts do not make agents injection-proof (`14` §3).

**Every sentence that claims autonomy must earn a trust clause** (§1a).

### 1a. Trust clauses (normative for all outward copy)

Any pitch, README line, or launch post that claims autonomy, “reviewed,” or
“ships” must be consistent with all of the following (detail in `14`):

1. **Self-hosted dedicated host** — not multi-tenant SaaS; control plane ≅ host root.
2. **Ticket and repo writers are in the trust boundary** — they can steer agents that hold forge/model credentials.
3. **Default-branch protection is the operator’s job** — the app warns; the forge enforces.
4. **Human merge unless auto-merge** — default off; enabling is an explicit choice.
5. **Reviewer token is recommended for formal forge approval** under branch protection (app-only; never given to a Dev) — not the same as staffing a different Dev Type for the REVIEW stage.

Forbidden overclaims:

- “Credentials never leave your machine” without noting Dev open egress / injection residual.
- Implying that a separate REVIEW Dev Type is a security control or second identity on the forge.
- Implying sandboxed multi-customer isolation.

### 1b. When to use it — and when not (normative for all outward copy)

Fit is part of the pitch. Every outward description of the product should make
both lists recognizable within the first minute.

**Use DevCake when:**

- Your team already runs real work through a task board (Linear in-tree today)
  and the board has discipline: tickets are written to be picked up, and
  labels mean things.
- One adult operator can own a dedicated machine and the trust envelope that
  comes with it — team membership, branch protection, credentials, backups
  ([`18-operator-contract.md`](18-operator-contract.md)).
- You want **delegation with receipts** — transcripts, token bills, traces —
  more than a chat copilot at your elbow.

**Don't use DevCake when:**

- You want a hosted, zero-ops service. There isn't one; self-hosting on your
  own box *is* the trust model (`14` §2).
- There is no board discipline to inherit. DevCake amplifies the board you
  run; it cannot invent one for you.
- You need multi-tenant isolation, or agents that stay safe under malicious
  ticket writers — ticket writers are inside the trust boundary (`14` §3).
- You want pair programming in an editor. This is delegation, not
  companionship.

### 1c. The clean room (what the envelope means)

As machine workers improve, the scarce thing stops being capability and
becomes **accountability** — specifying, checking, and accounting for work —
and that is what the envelope supplies, domain-free. The clean-room analogy is
precise, not decorative: fabs do not hope dust stays out; they build rooms
that do not care which process runs inside. Contamination control, one-way
pressure, QA-before-release, and batch records map one-to-one onto context
contaminants, read-only valves, REVIEW, and receipts. **DevCake is the clean
room for delegated deep work** — the meta-harness *is* the clean room; the
inner harness is the process that runs inside it.

| Clean-room concept | DevCake mechanism |
|---|---|
| Contamination control | Context hygiene: fresh containers, curated mounts, quoting quarantine, activity-mirror discipline (ADR-0014/0025) |
| One-way pressure / valves | Read-only upstream context — credential scope plus instruction; the only kernel-RO mount is the provision-phase source mirror (ADR-0025) |
| QA before release | REVIEW as an always-on pipeline stage, plus the operator/forge merge doctrine |
| Batch records / genealogy | Transcripts, token reports, run records, feed posts, activity repos — the accountability trail (INV-5) |

The analogy stops where `14` stops: a clean room for *work quality* is not a
multi-tenant sandbox, and receipts do not make agents injection-proof
(`14` §3).

## 2. The pitches

**The hero line (10 words):**
> Your board is the interface. Tickets in, pull requests out.

**One-liner (for "what is this repo?"):**
> DevCake staffs your task board with AI developers whose work arrives on a
> PR — billed, transcribed, and ready for you (or auto-merge) to ship.

**Ten seconds:**
> Write a ticket in your task board. An AI crew sizes it up, plans it, builds
> it, and a review step judges the PR; you approve a finished pull request.
> Every step comes with a receipt. You run it on your own machine.

**Thirty seconds (the elevator):**
> You know how software teams track work — a board of tickets, each one a task?
> DevCake staffs that board with AI developers. You write a ticket in plain
> language and stay in the tool you already use; DevCake triages it, plans it,
> writes the code, and a review step judges the PR before anything is treated
> as shippable. What comes back is a pull request plus a receipt for every
> step and what it cost. It runs on a machine you control, with your own AI
> subscriptions. Nothing reaches main without your green light unless you turn
> on auto-merge and accept that path. Your board is the interface — no chat UI
> to babysit. And you own the shop: branch protection, who can write tickets,
> the reviewer token when you want formal forge approval, and the merge button.

**The kitchen-table version (zero tech vocabulary):**
> It's a robot work crew for my to-do list. I write down what I want in plain
> words. The robots figure out how big the job is, make a plan, do the work,
> and inspect the result before I say yes. I look at the finished thing and
> approve it. Every job comes with an itemized receipt. It runs on my computer;
> people who can edit the to-do list can steer the robots, so I keep that list
> tight.

**Two minutes (engineers):** the thirty-second pitch, then:
> Under the hood it's deliberately boring: your PMO system (Linear in v0) is
> the single source of truth, and labels form the state machine — triage, plan,
> execute, review. Each step runs as a disposable Docker container wrapping a
> real coding harness — Claude Code, Grok Build, or Codex — using the
> subscription or API credentials you provide. Dagu holds `docker.sock` on a
> dedicated host; that is intentional, not an accident (`14`). There are no
> persistent per-Mission leases or checkouts: a crashed agent holds nothing,
> while process-local locks only serialize dispatch and maintenance. A human
> edit to a ticket always beats an in-flight agent. "Done"
> means *merged* — never before. Everything is traced in OpenTelemetry down to
> token counts; a large test suite pins behavioral invariants. The security
> contract is adult-operator: prompt injection is in scope of “ticket writers
> are trusted,” and the real supply-chain defense is branch protection and
> human merge. One Bake + compose brings up the stack on loopback.

## 3. Message house

| | |
|---|---|
| **Roof (motto)** | *Your board is the interface.* (board-native day-to-day Mission work — and you own the trust envelope) |
| **Pillar 1 — Board-native Mission work** | Your task board is the day-to-day Mission interface. Labels are the controls; tickets are the conversations; your merge (or auto-merge) is the deploy button. The admin UI remains the configuration and operations surface. |
| **Pillar 2 — Autonomy with receipts** | Every step posts transcript + token bill. Traced and audited. REVIEW is always a pipeline stage; formal forge approval uses the **reviewer token** when configured. “Done” never lies about merge. |
| **Pillar 3 — Your box, your rules** | Self-hosted on a **dedicated** machine; you own the trust envelope — team membership, branch protection, the merge button, backups (`18`). Mix Claude/Grok/Codex per role. Control plane does not ship your secrets to us; agents with open egress can still exfiltrate if injected — defend the supply chain (`14`). |
| **Foundation** | Verified, not vibed: acceptance path tickets → PRs, GitHub, GitLab, and Gitea, invariant tests, redaction of app-mediated posts. Security contract in `14`. |

## 4. Tone guide

- **Concrete beats grand.** "A review step judges the PR" — never "leveraging
  multi-agent orchestration."
- **Banned words:** revolutionary, supercharge, unleash, 10x, game-changing,
  "AI-powered" as an adjective.
- **Autonomy earns trust clauses** (§1a). Never state what it does alone without
  stating how you check it — and what the operator still owns.
- **Numbers only when true and verified.**
- **Warm, not cute.** The 🍰 can smile; the sentences stay in work clothes.
- **The reader is smart and busy.** No paragraph survives if its first sentence
  wouldn't.
- **Receipts ≠ sandbox.** Never imply that transcripts make malicious tickets safe.

## 5. The field (why this is different, honestly)

Devin-style autonomous engineers, Copilot-style coding agents, and open-source
agent frameworks often assume you'll come to *their* interface, or that you'll
trust output you can't itemize. Our answers: the interface is your existing
board (adopt one label to try it, remove it to stop); and every unit of work is
transcribed, billed, and traced. We are deliberately **model-plural** — judgment
roles and volume roles can run different vendors' models on subscriptions you
already own.

What we don't claim: that it replaces engineers (it drafts and reviews; you
decide); that it's magic (receipts exist so you can see when it isn't); or that
it is a multi-tenant secure sandbox (it is a powerful agent on **your** host —
`14`).

The long-form version of this argument — evidence status stated per claim,
falsifiers included — is [`19-thesis.md`](19-thesis.md).

## 6. The name

The product name is **DevCake**. "Piece of cake" is the promise; the layers
are the pipeline; the 🍰 is ours. The name can smile — the sentences stay in
work clothes (§4).
