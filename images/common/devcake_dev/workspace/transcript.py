"""Transcript assembly helpers."""
from __future__ import annotations

import json

def with_session(text: str, dump: str) -> str:
    """Failure-path transcripts: append the session dump when one exists."""
    return text + (f"\n\n## Session transcript\n\n{dump}" if dump else "")


def assemble_transcript(seq, mtype, run_id, dev_type, harness, token_report,
                        dump, result_text, result) -> str:
    """ADR-0014 D1: the attachment doc — header, the FULL session dump (all
    assistant-visible text), the outcome JSON. `## Agent report` (last message
    alone) appears only when no dump exists; the feed comment carries the last
    message, so the attachment need not repeat it. result=None (failure paths)
    drops the Outcome section."""
    body = (f"## Session transcript\n\n{dump}\n\n" if dump
            else f"## Agent report\n\n{result_text}\n\n")
    return (
        f"# {seq}_{mtype} — run {run_id}\n\n"
        f"**Dev:** {dev_type} ({harness}) · "
        f"**turns:** {token_report.get('num_turns', '—')} · "
        f"**duration:** {token_report.get('duration_ms', '—')} ms\n\n"
        + body
        + (f"## Outcome\n\n```json\n{json.dumps(result, indent=2)}\n```\n"
           if result is not None else ""))


