"""ADR-0042 primitives (PR-3 of the plan): folds, step cards, notices and
the unfolding projection in feed.py. No call site changes yet — these
tests pin the shapes and the two invariants every later PR rests on: the
sentinel stays the last bytes of every rendered body, and every marker a
scan reads is found inside a fold on every syntax family."""
from datetime import datetime, timezone

import pytest

from devcake.domain.model import ActivityEntry, AttachmentRef
from devcake.domain.orchestrator import feed, freshness
from devcake.domain.orchestrator.feed import (FoldSection, StepCardParts,
                                              append_fold_section,
                                              card_header, cut_at_boundary,
                                              fold_sections, is_devcake_comment,
                                              record_section, render_fold,
                                              render_notice, render_step_card,
                                              strip_fold, unfold_entries,
                                              unquoted)
from devcake.domain.orchestrator.markers import (ANSWER_TOKEN_RE,
                                                 COMMENT_SENTINEL,
                                                 CONFLICT_MARKER,
                                                 FRESHNESS_MARKER,
                                                 MERGE_RETRY_MARKER,
                                                 REPLY_MARKER, STATUS_MARKER,
                                                 STEP_MARKER, answer_token,
                                                 discovery_in_keys,
                                                 discovery_posts,
                                                 discovery_receipts)
from devcake.ports.pmo import FOLD_DETAILS, FOLD_NONE, FOLD_PLUS

FAMILIES = (FOLD_DETAILS, FOLD_PLUS, FOLD_NONE)
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc)

TOKEN_REPORT = ("🧮 DevCake token report — step 2 (EXECUTE, senior-dev)\n"
                "model: m · input: 1 · output: 2\ncache read/write: 3/4\n"
                "cost: $3.0500\nextraction: end_event\nrun: T-1-2-EXECUTE-AAAAAA")
HARVEST = ("`devcake:discovery:v1 step=2 n=1`\n\n🔎 1 discovery from step 2 "
           "(EXECUTE) — leads for related missions, routed separately.\n\n"
           "`devcake:finding:v1 sha=0123456789ab`\n\n"
           "Full record attached: [DISCOVERY_2.md](https://files.example/DISCOVERY_2.md)\n\n"
           "**1.**\n> the config default changed\n> Evidence: src/config.py:42")
TRANSITION = ("🔀 DevCake opened/updated the pull request: "
              "https://forge.example/pr/8 — awaiting REVIEW.")


def _sections(*, run=True):
    out = [FoldSection("Answer", answer_token(2)),
           FoldSection("Token report", TOKEN_REPORT),
           FoldSection("Discoveries", HARVEST),
           FoldSection("Transition", TRANSITION)]
    if run:
        out.append(FoldSection("Run", "`T-1-2-EXECUTE-AAAAAA`"))
    return out


def _card(collapsible, *, url="https://files.example/2_EXECUTE.md",
          answer="Root cause: the skip gate.\n\nFix in the PR.", dump=None):
    return render_step_card(StepCardParts(
        seq=2, mission_type="EXECUTE", glyph="🔀", outcome_word="executed",
        result_line="Pull request https://forge.example/pr/8.",
        next_line="DevCake — REVIEW.", transcript_name="2_EXECUTE.md",
        transcript_url=url, sections=_sections(), answer_md=answer,
        transcript_md=dump, duration_s=12 * 60, cost_usd=3.05),
        collapsible=collapsible)


def _seal(body):
    return body + "\n\n" + COMMENT_SENTINEL


def _entry(body, ts=NOW, eid="c1", attachments=()):
    return ActivityEntry(ts=ts, author="cake", kind="comment", body=body,
                         entry_id=eid, attachments=list(attachments))


# ── fold grammar ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fam", FAMILIES)
def test_fold_round_trips_sections_on_every_family(fam):
    secs = [FoldSection("Token report", TOKEN_REPORT),
            FoldSection("Routing receipts",
                        "🧭 Discovery routing receipts:\n`devcake:discovery-routed:v1 step=2 to=-`",
                        at=LATER)]
    body = "head line\n\n" + render_fold(secs, collapsible=fam)
    head, inner = strip_fold(_seal(body))
    assert head == "head line"
    got = fold_sections(inner)
    assert [(s.title, s.body, s.at) for s in got] == \
        [(s.title, s.body, s.at) for s in secs]
    assert "Details — token report · routing receipts" in body


def test_section_line_never_matches_head_or_legacy_lines():
    for line in ("**Result:** Pull request x.", "**Next:** DevCake — REVIEW.",
                 "✋ **Needs you.** The plan needs approval.",
                 "**1.**", "🧮 DevCake token report — step 2 (EXECUTE, d)",
                 "▸ **lowercase**", "**Token report**"):
        assert feed.SECTION_RE.match(line) is None, line
    assert feed.SECTION_RE.match("▸ **Token report**")
    hit = feed.SECTION_RE.match("▸ **Routing receipts** · at=2026-09-09T15:30:00Z")
    assert hit and hit.group("at") == "2026-09-09T15:30:00Z"


def test_append_fold_section_extends_creates_and_refuses_paged():
    base = _card(FOLD_DETAILS)
    late = FoldSection("Routing receipts",
                       "`devcake:discovery-routed:v1 step=2 to=T-9`", at=LATER)
    grown = append_fold_section(_seal(base), late, collapsible=FOLD_DETAILS)
    assert COMMENT_SENTINEL not in grown          # sentinel-free: _edit re-seals
    titles = [s.title for s in fold_sections(strip_fold(grown)[1])]
    assert titles == ["Answer", "Token report", "Discoveries", "Transition",
                      "Run", "Routing receipts"]
    assert "· routing receipts" in grown           # summary re-rendered
    # a pre-card anchor (no fold) gains one
    legacy = _seal("🧾 DevCake transcript `1_ONBOARD.md` (run `r`) — attached: [1_ONBOARD.md](u)")
    created = append_fold_section(legacy, late, collapsible=FOLD_PLUS)
    assert created.startswith("🧾 DevCake transcript") and "+++ Details" in created
    assert fold_sections(strip_fold(created)[1])[0].title == "Routing receipts"
    with pytest.raises(ValueError):
        append_fold_section("Part 1 of 2\n\n" + base, late, collapsible=FOLD_DETAILS)


# ── the step card ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("fam", FAMILIES)
def test_card_invariants_on_every_family(fam):
    body = _seal(_card(fam))
    assert is_devcake_comment(body)                         # sentinel last
    assert not freshness._is_material(body)                 # never trips the gate
    assert len(STEP_MARKER.findall(unquoted(body))) == 1    # one file token
    text = unquoted(body)
    assert discovery_posts(text) == [(2, 1)]
    assert ANSWER_TOKEN_RE.search(text).group(1) == "2"
    assert body.startswith("🔀 Step 2 · EXECUTE · executed · 12 min · $3.05\n\n> Root cause")
    assert "Transcript: `2_EXECUTE.md` — [download](https://files.example/2_EXECUTE.md)" in body
    assert "<!--" not in body                               # no HTML markers


def test_every_scanner_finds_its_marker_inside_a_fold():
    secs = [FoldSection("Markers", "\n".join([
        "`devcake:conflict-resolve:2`", "`devcake:freshness-rereview:3`",
        MERGE_RETRY_MARKER, "`devcake:discovery-routed:v1 step=1 to=T-4`",
        "`devcake:discovery-in:v1 src=T-7 step=2`"]))]
    for fam in FAMILIES:
        text = unquoted(_seal("head\n\n" + render_fold(secs, collapsible=fam)))
        assert max(int(m.group(1)) for m in CONFLICT_MARKER.finditer(text)) == 2
        assert max(int(m.group(1)) for m in FRESHNESS_MARKER.finditer(text)) == 3
        assert MERGE_RETRY_MARKER in text
        assert discovery_receipts(text) == {(1, "T-4")}
        assert discovery_in_keys(text) == {("T-7", 2)}


def test_cut_at_boundary_prefers_paragraph_then_sentence():
    text = "First paragraph. Still first.\n\nSecond paragraph is long. " * 40
    cut, was_cut = cut_at_boundary(text, 300)
    assert was_cut and cut.endswith("first.") or cut.endswith("long.")
    assert len(cut) <= 300
    no_para = "A sentence here. " * 40
    cut, was_cut = cut_at_boundary(no_para, 200)
    assert was_cut and cut.endswith(".") and len(cut) <= 200
    assert cut_at_boundary("short", 100) == ("short", False)
    cut, was_cut = cut_at_boundary("x" * 500, 100)
    assert was_cut and len(cut) == 100


def test_card_header_omits_unknowns():
    assert card_header("✅", 3, "REVIEW", "approved") == "✅ Step 3 · REVIEW · approved"
    assert card_header("✅", 3, "REVIEW", "approved", 42, None) == \
        "✅ Step 3 · REVIEW · approved · 42 s"
    assert card_header("✅", 3, "REVIEW", "approved", 100 * 60, 0.5) == \
        "✅ Step 3 · REVIEW · approved · 1 h 40 min · $0.50"


def test_inline_transcript_section_is_the_dump_for_step_files():
    dump = "turn 1\n\nturn 2 with `devcake:discovery:v1 step=9 n=9` quoted"
    body = _seal(_card(FOLD_DETAILS, url=None, dump=dump))
    assert "Transcript: `2_EXECUTE.md` (inline, in the details below)" in body
    assert discovery_posts(unquoted(body)) == [(2, 1)]    # the quoted dump never counts
    files = feed.coalesced_step_files([_entry(body)])
    assert [(n, c) for n, c, _ in files] == [("2_EXECUTE.md", dump)]
    # a pre-card inline transcript still reconstructs as before
    legacy = _seal("🧾 DevCake transcript `1_ONBOARD.md` (run `r`)\n\n"
                   + feed.blockquote("---\n\n" + dump))
    assert feed.coalesced_step_files([_entry(legacy)])[0][1] == dump


# ── notices ─────────────────────────────────────────────────────────────────

def test_notice_carries_the_record_and_unfolds_to_it():
    legacy = ("🧩 Auto-merge hit a merge conflict on https://forge.example/pr/8 "
              "— back to EXECUTE. `devcake:conflict-resolve:1`")
    body = render_notice(feed.directive_lead("🧩", "Conflict-resolve directive"),
                         "The merge conflicted; the next Dev syncs the branch.",
                         sections=[record_section(_seal(legacy))],
                         collapsible=FOLD_PLUS)
    sealed = _seal(body)
    assert is_devcake_comment(sealed)
    assert CONFLICT_MARKER.search(unquoted(sealed)).group(1) == "1"
    assert unquoted(sealed).count("conflict-resolve:1") == 1   # never doubled
    [proj] = unfold_entries([_entry(sealed)])
    assert proj.body == _seal(legacy)


# ── the projection ──────────────────────────────────────────────────────────

def _card_entry(fam, **kw):
    refs = [AttachmentRef(url="https://files.example/2_EXECUTE.md", name="2_EXECUTE.md"),
            AttachmentRef(url="https://files.example/DISCOVERY_2.md", name="DISCOVERY_2.md")]
    return _entry(_seal(_card(fam, **kw)), attachments=refs)


@pytest.mark.parametrize("fam", FAMILIES)
def test_card_unfolds_into_the_legacy_sequence(fam):
    out = unfold_entries([_card_entry(fam)])
    assert [p.body for p in out] == [
        _seal("🧾 DevCake transcript `2_EXECUTE.md` (run `T-1-2-EXECUTE-AAAAAA`) — "
              "attached: [2_EXECUTE.md](https://files.example/2_EXECUTE.md)\n\n"
              "> Root cause: the skip gate.\n>\n> Fix in the PR."),
        _seal(REPLY_MARKER + "\n\n> Root cause: the skip gate.\n>\n> Fix in the PR."),
        _seal(TOKEN_REPORT), _seal(HARVEST), _seal(TRANSITION)]
    assert all(is_devcake_comment(p.body) for p in out)
    assert all(p.ts == NOW and p.entry_id == "c1" for p in out)
    assert [a.name for a in out[0].attachments] == ["2_EXECUTE.md"]
    assert [a.name for a in out[3].attachments] == ["DISCOVERY_2.md"]
    assert out[1].attachments == [] and out[2].attachments == []
    assert out[0].source is not None


def test_cut_answer_unfolds_with_the_legacy_cut_notes():
    long = "Sentence one is here. " * 200
    out = unfold_entries([_card_entry(FOLD_DETAILS, answer=long)])
    assert out[0].body.count("… (truncated — full text in the attachment)") == 1
    assert "full text in the step transcript on this issue" in out[1].body
    assert "full text in the attachment" not in out[1].body


def test_pointer_only_card_unfolds_without_an_answer_entry():
    parts = StepCardParts(seq=1, mission_type="ONBOARD", glyph="📋",
                          outcome_word="triaged", result_line="Needs a plan.",
                          next_line="DevCake — PLAN.", transcript_name="1_ONBOARD.md",
                          transcript_url="u", sections=[
                              FoldSection("Token report", TOKEN_REPORT),
                              FoldSection("Run", "`r`")])
    body = _seal(render_step_card(parts, collapsible=FOLD_DETAILS))
    out = unfold_entries([_entry(body)])
    assert [p.body for p in out] == [
        _seal("🧾 DevCake transcript `1_ONBOARD.md` (run `r`) — attached: [1_ONBOARD.md](u)"),
        _seal(TOKEN_REPORT)]


def test_late_sections_unfold_at_their_own_time_after_later_entries():
    card = _seal(_card(FOLD_DETAILS))
    grown = _seal(append_fold_section(card, FoldSection(
        "Routing receipts", "🧭 receipts\n`devcake:discovery-routed:v1 step=2 to=-`",
        at=LATER), collapsible=FOLD_DETAILS))
    human = ActivityEntry(ts=datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc),
                          author="felipe", kind="comment", body="please hurry",
                          entry_id="h1")
    out = unfold_entries([_entry(grown), human])
    assert out[-2].body == "please hurry" and out[-1].body.startswith("🧭 receipts\n")
    assert out[-1].ts == LATER and out[-1].entry_id == "c1"


def test_status_comment_is_omitted_and_others_pass_through():
    status = _seal("📌 **T-1 — status**\n\n" + render_fold(
        [FoldSection("Record", STATUS_MARKER)], collapsible=FOLD_PLUS))
    legacy = _seal("✅ REVIEW approved; PR merged (u). Mission done.")
    human = ActivityEntry(ts=NOW, author="felipe", kind="comment",
                          body="> quoting `devcake:v1`\n\nmy own words")
    out = unfold_entries([_entry(status, eid="s1"), _entry(legacy, eid="c2"), human])
    assert [p.body for p in out] == [legacy, human.body]
    assert feed.is_status_comment(status) and feed.find_status_entry(
        [_entry(legacy, eid="c2"), _entry(status, eid="s1")]) == "s1"


def test_paged_card_unfolds_from_its_joined_pages():
    dump = "line\n" * 400
    body = _card(FOLD_DETAILS, url=None, dump=dump)
    pages = feed.split_vendor_comments(body, 1200)
    assert len(pages) >= 3
    entries = [_entry(_seal(p), eid=f"p{i}") for i, p in enumerate(pages)]
    out = unfold_entries(entries)
    assert out[0].body.startswith("🧾 DevCake transcript `2_EXECUTE.md` (run `T-1-2-EXECUTE-AAAAAA`)\n\n> ---")
    assert [p.entry_id for p in out] == ["p0"] * len(out)
    files = feed.coalesced_step_files(entries)
    assert [(n, c) for n, c, _ in files] == [("2_EXECUTE.md", dump.rstrip("\n"))] \
        or [(n, c) for n, c, _ in files] == [("2_EXECUTE.md", dump)]
