# 17 — Positioning: What This Is, and How We Say It

> **Audience:** anyone writing outward-facing words about this project — README,
> website, pitch, launch post. The README is the applied version of this doc.
> **Status:** adopted at v0 close (2026-07-11). The product name is **provisional**
> (§6); "DevCake" is used throughout as a placeholder.

## 1. The core insight

Every AI-developer product asks you to adopt a new place of work — a chat
window, a special IDE, someone else's web app. This one doesn't. Its entire
control surface is the task board you already run. You manage AI developers
exactly the way you manage human ones: write a ticket, get reviewed work back,
approve it or send it back with notes.

**Motto: "You never operate it."**
(Long form, for a website hero: *You never operate it. You write a ticket;
finished, reviewed work comes back — with a receipt.*)

The second pillar makes the first one safe: **autonomy with receipts.** Every
step an agent takes posts its full transcript and its token bill to the ticket
it worked on; every action is traceable end to end; nothing reaches your main
branch without an independent, deliberately skeptical AI review — and, unless
you switch on auto-merge, your explicit approval. Autonomy you can audit is the
only autonomy worth shipping.

**Every sentence that claims autonomy must earn a trust clause.** That's the
tone rule in miniature.

## 2. The pitches

**The hero line (10 words):**
> You never operate it. Your task board just gets done.

**One-liner (for "what is this repo?"):**
> DevCake staffs your task board with AI developers whose work arrives
> reviewed, billed, and ready to approve.

**Ten seconds:**
> Write a ticket in your task board. An AI crew sizes it up, plans it, builds
> it; a second AI reviews it like a skeptical senior engineer; you approve a
> finished pull request. Every step comes with a receipt.

**Thirty seconds (the elevator):**
> You know how software teams track work — a board of tickets, each one a task?
> DevCake staffs that board with AI developers. You write a ticket in plain
> language and stay in the tool you already use; DevCake triages it, plans it,
> writes the code, and then a *second* AI reviews the work skeptically before
> you ever see it. What comes back is a finished, reviewed pull request — plus
> a receipt showing every step it took and what each one cost. It runs on your
> own machine, with your own AI subscriptions, and nothing ships without your
> green light unless you decide to trust it that far. You never operate the
> system. You just run your board.

**The kitchen-table version (zero tech vocabulary — the "wife test"):**
> It's a robot work crew for my to-do list. I write down what I want in plain
> words. The robots figure out how big the job is, make a plan, and do the
> work — and a second robot inspects everything before I ever see it. I just
> look at the finished thing and say yes or no. And every job comes with an
> itemized receipt.

**Two minutes (engineers):** the thirty-second pitch, then:
> Under the hood it's deliberately boring: your PMO system (Linear in v0) is
> the single source of truth, and four labels form the whole state machine —
> triage, plan, execute, review. Each step runs as a disposable Docker
> container wrapping a real coding harness — Claude Code, Grok Build, or Codex,
> mixed per role, on the subscriptions you already pay for. There are no locks:
> a crashed agent holds nothing, and a human edit to a ticket always beats an
> in-flight agent. "Done" means *merged* — never before. Everything is traced
> in OpenTelemetry down to the token counts, a 200+-test suite pins the core
> invariants, and the release gate is an acceptance script that takes fresh
> tickets to merged PRs with zero human input. One docker-compose brings up
> the whole thing.

## 3. Message house

| | |
|---|---|
| **Roof (motto)** | *You never operate it.* |
| **Pillar 1 — No new interface** | Your task board is the product. Labels are the controls; tickets are the conversations; your approval is the deploy button. |
| **Pillar 2 — Autonomy with receipts** | Every step posts its transcript and its token bill to the ticket. Every action is a trace. Nothing merges un-reviewed; "Done" never lies. |
| **Pillar 3 — Your models, your box** | Self-hosted in one compose file. Mix Claude/Grok/Codex per role, on your own subscriptions. Your credentials never leave your machine. |
| **Foundation** | Verified, not vibed: unattended acceptance 2/2 (tickets → merged PRs), both GitHub and GitLab, invariant test suite, live-proven secret redaction. |

## 4. Tone guide

- **Concrete beats grand.** "A second AI reviews the work" — never "leveraging
  multi-agent orchestration."
- **Banned words:** revolutionary, supercharge, unleash, 10x, game-changing,
  "AI-powered" as an adjective.
- **Autonomy earns trust clauses.** Never state what it does alone without
  stating how you check it.
- **Numbers only when true and verified.** "2/2 unattended acceptance runs" is
  allowed *because we ran them*.
- **Warm, not cute.** The 🍰 can smile; the sentences stay in work clothes.
- **The reader is smart and busy.** No paragraph survives if its first sentence
  wouldn't.

## 5. The field (why this is different, honestly)

Devin-style autonomous engineers, Copilot-style coding agents, and open-source
agent frameworks all share two assumptions we reject: that you'll come to
*their* interface, and that you'll trust output you can't itemize. Our answers:
the interface is your existing board (adopt one label to try it, remove it to
stop); and every unit of work is transcribed, billed, reviewed, and traced. We
are also deliberately **model-plural** — judgment roles and volume roles can run
different vendors' models, swapped in config, priced on subscriptions you
already own. What we don't claim: that it replaces engineers (it drafts and
reviews; you decide), or that it's magic (the receipts exist precisely so you
can see when it isn't).

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
