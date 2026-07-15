# 17 — Positioning: What This Is, and How We Say It

> **Audience:** anyone writing outward-facing words about this project — README,
> website, pitch, launch post. The README is the applied version of this doc.
> **Status:** adopted at v0 close; **aligned with the product security contract**
> in [`14-security.md`](14-security.md). Outward copy must not claim a stronger
> posture than `14`. The product name is **provisional** (§6).

## 1. The core insight

Every AI-developer product asks you to adopt a new place of work — a chat
window, a special IDE, someone else's web app. This one doesn't. Its entire
control surface is the task board you already run. You manage AI developers
exactly the way you manage human ones: write a ticket, get work back on a pull
request, approve it or send it back with notes.

**Motto: "You never operate it."**  
(Long form: *You never operate it. You write a ticket; finished work comes back
on a PR — with a receipt.*)

“Never operate it” means **no day-to-day chat babysitting** — not “no trust
decisions.” You still choose who can write tickets, protect the default branch,
and whether auto-merge is on. Those are operator duties (`14` §9).

The second pillar is **autonomy with receipts.** Every step posts its transcript
and token bill to the ticket; dispatches, kills, sweeps, and PMO writes are
traced. **Recommended** config uses a different Dev Type for REVIEW than
EXECUTE (warned when shared — not a hard invariant). **Unless you turn on
auto-merge**, a human merges before Done. Autonomy you can audit is the only
autonomy worth shipping — receipts do not make agents injection-proof (`14` §3).

**Every sentence that claims autonomy must earn a trust clause** (§1a).

### 1a. Trust clauses (normative for all outward copy)

Any pitch, README line, or launch post that claims autonomy, “reviewed,” or
“ships” must be consistent with all of the following (detail in `14`):

1. **Self-hosted dedicated host** — not multi-tenant SaaS; control plane ≅ host root.
2. **Ticket and repo writers are in the trust boundary** — they can steer agents that hold forge/model credentials.
3. **Default-branch protection is the operator’s job** — the app warns; the forge enforces.
4. **Human merge unless auto-merge** — default off; enabling is an explicit choice.
5. **Independent AI review is recommended configuration**, not a guaranteed invariant.

Forbidden overclaims:

- “Credentials never leave your machine” without noting Dev open egress / injection residual.
- “Independent review by default” as a hard product guarantee.
- Implying sandboxed multi-customer isolation.

## 2. The pitches

**The hero line (10 words):**
> You never operate it. Your task board just gets done.

**One-liner (for "what is this repo?"):**
> DevCake staffs your task board with AI developers whose work arrives on a
> PR — billed, transcribed, and ready for you (or auto-merge) to ship.

**Ten seconds:**
> Write a ticket in your task board. An AI crew sizes it up, plans it, builds
> it; a second role can review it like a skeptical senior engineer; you approve
> a finished pull request. Every step comes with a receipt. You run it on your
> own machine.

**Thirty seconds (the elevator):**
> You know how software teams track work — a board of tickets, each one a task?
> DevCake staffs that board with AI developers. You write a ticket in plain
> language and stay in the tool you already use; DevCake triages it, plans it,
> writes the code, and — when you assign a separate review role — a second AI
> looks at the work skeptically before you merge. What comes back is a pull
> request plus a receipt for every step and what it cost. It runs on a machine
> you control, with your own AI subscriptions. Nothing reaches main without
> your green light unless you turn on auto-merge and accept that path. You
> don't babysit a chat UI. You run your board — and you still own branch
> protection and who can write tickets.

**The kitchen-table version (zero tech vocabulary):**
> It's a robot work crew for my to-do list. I write down what I want in plain
> words. The robots figure out how big the job is, make a plan, and do the
> work — and I can have another robot inspect it before I say yes. I look at
> the finished thing and approve it. Every job comes with an itemized receipt.
> It runs on my computer; people who can edit the to-do list can steer the
> robots, so I keep that list tight.

**Two minutes (engineers):** the thirty-second pitch, then:
> Under the hood it's deliberately boring: your PMO system (Linear in v0) is
> the single source of truth, and labels form the state machine — triage, plan,
> execute, review. Each step runs as a disposable Docker container wrapping a
> real coding harness — Claude Code, Grok Build, or Codex — on subscriptions
> you already pay for. Dagu holds `docker.sock` on a dedicated host; that is
> intentional, not an accident (`14`). There are no locks: a crashed agent holds
> nothing, and a human edit to a ticket always beats an in-flight agent. "Done"
> means *merged* — never before. Everything is traced in OpenTelemetry down to
> token counts; a large test suite pins behavioral invariants. The security
> contract is adult-operator: prompt injection is in scope of “ticket writers
> are trusted,” and the real supply-chain defense is branch protection and
> human merge. One Bake + compose brings up the stack on loopback.

## 3. Message house

| | |
|---|---|
| **Roof (motto)** | *You never operate it.* (no chat babysitting — not “no trust work”) |
| **Pillar 1 — No new interface** | Your task board is the product. Labels are the controls; tickets are the conversations; your merge (or auto-merge) is the deploy button. |
| **Pillar 2 — Autonomy with receipts** | Every step posts transcript + token bill. Traced and audited. Independent AI review is **recommended config** (warned if violated). “Done” never lies about merge. |
| **Pillar 3 — Your models, your box** | Self-hosted on a **dedicated** machine. Mix Claude/Grok/Codex per role. Control plane does not ship your secrets to us; agents with open egress can still exfiltrate if injected — defend the supply chain (`14`). |
| **Foundation** | Verified, not vibed: acceptance path tickets → PRs, GitHub and GitLab, invariant tests, redaction of app-mediated posts. Security contract in `14`. |

## 4. Tone guide

- **Concrete beats grand.** "A second AI reviews the work" — never "leveraging
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

## 6. The name (open decision)

"DevCake" is provisional. Criteria for the real name: says *delegated work*
or *board* at first hearing; two syllables preferred; survives being said
aloud in a serious meeting; no collisions with known dev-tools.

| Candidate | Why | Risk |
|---|---|---|
| **DevCake** (keep) | Warm, memorable, "piece of cake" = the promise; layers = the pipeline; 🍰 already ours | Reads playful; "Dev-" prefix is crowded |
| **Boardhand** | A hand for your board — ranch-hand/deckhand worker DNA; motto-compatible ("hands for the board you already run") | Invented word; needs a beat to land |
| **Nightcrew** | The work happens while you sleep; honest and evocative | Implies only-async; slightly ops-flavored |
| **Punchlist** | Construction term: the list a contractor finishes and the owner inspects — exactly our loop | Existing small products use it |
| **Ticketsmith** | Forges finished work from tickets; craft connotation | Three syllables; smith-naming is common |

Recommendation: decide before anything public. Renaming touches the label
namespace (`DEVCAKE-*`), repo, and docs — a one-day, find-and-replace-plus-
migration job (label rename procedure already exists in ADR-0004).
