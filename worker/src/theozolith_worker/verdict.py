"""Reviewer verdicts: the review-comment format both actors speak.

Every Reviewer verdict lands as one PR comment: human-readable evidence and
plan up top, plus a machine block the next Run parses. The machine block
carries the whole schema — verdict, grades, the revised plan, and the resume
designation (a resume commit ID, optionally followed by cherry-picked
commits) that defines the only state carried into the next Run (ADR-0008).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from theozolith_worker.githubapi import Comment

APPROVE = "approve"
REVISE = "revise"
ESCALATE = "escalate"

MACHINE_RE = re.compile(r"<!-- theozolith:verdict\n(.*?)\n-->", re.DOTALL)


@dataclass
class Verdict:
    verdict: str  # approve | revise | escalate
    round: int  # review round this verdict closes (1-based)
    evidence: str = ""  # evidence-citing rationale
    deviation: str | None = None  # low | medium | high (approve)
    risk: str | None = None  # low | medium | high (approve)
    revised_plan: str = ""  # revise
    resume_commit: str = ""  # revise: branch state the next Run starts from
    cherry_pick: list[str] = field(default_factory=list)  # revise, optional
    bundle_url: str = ""  # evidence bundle link (escalate: mandatory)


def render_comment(v: Verdict) -> str:
    lines = [f"### Reviewer verdict: {v.verdict} (round {v.round})", ""]
    if v.verdict == APPROVE:
        lines += [f"Deviation: **{v.deviation}** · Risk: **{v.risk}**", ""]
    if v.evidence:
        lines += [v.evidence, ""]
    if v.verdict == REVISE:
        lines += ["#### Revised plan", "", v.revised_plan or "(none given)", ""]
        resume = v.resume_commit or "(current head)"
        lines += [f"Resume from commit `{resume}`."]
        if v.cherry_pick:
            picks = ", ".join(f"`{sha}`" for sha in v.cherry_pick)
            lines += [f"Then cherry-pick: {picks}."]
        lines += [""]
    if v.bundle_url:
        lines += [f"Evidence bundle: {v.bundle_url}", ""]
    lines += [
        "<!-- theozolith:verdict",
        json.dumps(asdict(v), indent=2, sort_keys=True),
        "-->",
    ]
    return "\n".join(lines)


def parse_comment(body: str) -> Verdict | None:
    match = MACHINE_RE.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in (APPROVE, REVISE, ESCALATE):
        return None
    return Verdict(
        verdict=data["verdict"],
        round=int(data.get("round", 0)),
        evidence=str(data.get("evidence", "")),
        deviation=data.get("deviation"),
        risk=data.get("risk"),
        revised_plan=str(data.get("revised_plan", "")),
        resume_commit=str(data.get("resume_commit", "")),
        cherry_pick=[str(sha) for sha in data.get("cherry_pick", [])],
        bundle_url=str(data.get("bundle_url", "")),
    )


def latest_verdict(comments: list[Comment]) -> tuple[Verdict, Comment] | None:
    """The most recent verdict comment on a PR, if any."""
    for comment in reversed(comments):
        verdict = parse_comment(comment.body)
        if verdict is not None:
            return verdict, comment
    return None


def comments_after(comments: list[Comment], marker: Comment | None) -> list[Comment]:
    """Comments later than ``marker`` (all comments when marker is None)."""
    if marker is None:
        return list(comments)
    index = next((i for i, c in enumerate(comments) if c.id == marker.id), -1)
    return list(comments[index + 1 :])
