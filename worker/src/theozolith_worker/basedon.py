"""The driver-owned "Based on #N at `<sha>`" PR-body zone (ADR-0053).

A chained dependent's PR targets its blocker's branch; this zone is the
durable record of that base — blocker issue and the tip SHA at checkout —
and the human-gate warning (GitHub does not structurally prevent merging
the dependent first, which would merge it into the blocker's branch). It
is the same HTML-comment machine-block class as ``theozolith:verdict``:
driver-written, never model prose, and parseable after a lost control DB.

Writes happen only through the driver's ship path (``upsert_zone`` on the
composed PR body); this module itself performs no GitHub calls. Parsing is
tolerant and never raises — PR bodies are agent-adjacent input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BasedOn:
    issue: int  # the blocker whose branch the PR is based on
    sha: str  # that branch's tip at checkout


# Newlines are \r?\n-tolerant throughout: a human edit in GitHub's web UI
# can round-trip the body to CRLF, and a zone the regexes stop matching
# would stack a duplicate per ship round instead of replacing.
ZONE_RE = re.compile(r"<!-- theozolith:based-on\r?\n(.*?)\r?\n-->", re.DOTALL)

# Removal/replacement strips warning lines and machine blocks
# INDEPENDENTLY (all occurrences): a human edit can separate or reword
# around them, and a stale "merge #N first" instruction surviving on a
# retargeted-to-main PR would direct the human gate in the wrong order.
_WARNING_STRIP_RE = re.compile(r"\*\*Based on #\d+ at `[^`\n]*`\*\*[^\n]*(?:\r?\n)*")
_BLOCK_STRIP_RE = re.compile(r"<!-- theozolith:based-on\r?\n.*?\r?\n-->(?:\r?\n)*", re.DOTALL)


def render_zone(issue: int, sha: str) -> str:
    """The warning first — it is what the merging human must read — then
    the machine block."""
    warning = (
        f"**Based on #{issue} at `{sha}`** — merge #{issue} first; merging this PR"
        f" first would merge it into #{issue}'s branch."
    )
    block = json.dumps({"issue": issue, "sha": sha}, sort_keys=True)
    return f"{warning}\n\n<!-- theozolith:based-on\n{block}\n-->"


def upsert_zone(body: str, based_on: BasedOn | None) -> str:
    """Replace, prepend, or remove the zone. ``None`` removes it — the
    healthy retarget-to-main shape after the blocker merges (ADR-0053).
    The zone leads the body so the merge-order warning is the first thing
    a human reads."""
    remainder = _BLOCK_STRIP_RE.sub("", _WARNING_STRIP_RE.sub("", body or ""))
    remainder = remainder.lstrip("\r\n")
    if based_on is None:
        return remainder
    zone = render_zone(based_on.issue, based_on.sha)
    return f"{zone}\n\n{remainder}" if remainder else zone


def parse_zone(body: str) -> BasedOn | None:
    """The recorded base, or None on anything malformed — tolerant by
    design; a mangled body is a human problem, never a crash."""
    match = ZONE_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    issue, sha = data.get("issue"), data.get("sha")
    if not isinstance(issue, int) or isinstance(issue, bool) or not isinstance(sha, str):
        return None
    return BasedOn(issue=issue, sha=sha)
