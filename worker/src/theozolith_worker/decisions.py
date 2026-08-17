"""The Decisions Section: the mandatory schema on every best-effort PR.

Fixed schema inherited from the former Handoff Doc (ADR-0003 as amended by
ADR-0008): decisions made with rationale, open questions, remaining work,
dead ends tried — plus the gate's structured findings, which the best-effort
contract records here when they are unresolvable.

The section is embedded in the PR description between markers, with the
machine-readable JSON in an HTML comment and the human-readable markdown
rendered from it. The agent hands the driver its half through the Output
Proposal's Decisions-Section fields (ADR-0046 — the in-worktree
``.theozolith/decisions.json`` is retired); a failed Run still ships its
evidence with a synthesized section saying what broke.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from theozolith_worker.gate.pipeline import Finding

BEGIN = "<!-- theozolith:decisions:begin -->"
END = "<!-- theozolith:decisions:end -->"
DATA_RE = re.compile(r"<!-- theozolith:decisions:data\n(.*?)\n-->", re.DOTALL)


@dataclass(frozen=True)
class Decision:
    what: str
    why: str


@dataclass(frozen=True)
class ProcessIssue:
    """One observation about the pipeline itself (2026-07-22 grilling):
    friction observed plus a suggested fix. Advisory only — process issues
    are never findings about the change and influence no verdict, label, or
    gate outcome; harvesting is manual in V1."""

    friction: str
    suggested_fix: str = ""


@dataclass
class DecisionsSection:
    decisions: list[Decision] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)
    gate_findings: list[Finding] = field(default_factory=list)
    process_issues: list[ProcessIssue] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _strings(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str | int | float)]


def _decisions(raw: object) -> list[Decision]:
    items: list[Decision] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if isinstance(entry, dict) and entry.get("what"):
            items.append(Decision(what=str(entry["what"]), why=str(entry.get("why", ""))))
        elif isinstance(entry, str):
            items.append(Decision(what=entry, why=""))
    return items


def process_issues_from(raw: object) -> list[ProcessIssue]:
    """Lenient by design: advisory content must never invalidate a file, so
    malformed entries are dropped, never errors (shared with verdict.py)."""
    items: list[ProcessIssue] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if isinstance(entry, dict) and entry.get("friction"):
            items.append(
                ProcessIssue(
                    friction=str(entry["friction"]),
                    suggested_fix=str(entry.get("suggested_fix", "")),
                )
            )
        elif isinstance(entry, str) and entry:
            items.append(ProcessIssue(friction=entry))
    return items


def section_from_dict(data: dict) -> DecisionsSection:
    return DecisionsSection(
        decisions=_decisions(data.get("decisions")),
        open_questions=_strings(data.get("open_questions")),
        remaining_work=_strings(data.get("remaining_work")),
        dead_ends=_strings(data.get("dead_ends")),
        gate_findings=[
            Finding(**entry) for entry in data.get("gate_findings", []) if isinstance(entry, dict)
        ],
        process_issues=process_issues_from(data.get("process_issues")),
    )


def _bullets(items: list[str], empty: str) -> list[str]:
    return [f"- {item}" for item in items] or [f"- {empty}"]


def render(section: DecisionsSection) -> str:
    """The Decisions Section block for a PR description."""
    lines = [BEGIN, "## Decisions", "", "### Decisions made"]
    if section.decisions:
        lines += [
            f"- **{d.what}** — {d.why}" if d.why else f"- **{d.what}**" for d in section.decisions
        ]
    else:
        lines += ["- none recorded"]
    lines += ["", "### Open questions"]
    lines += _bullets(section.open_questions, "none")
    lines += ["", "### Remaining work"]
    lines += _bullets(section.remaining_work, "none")
    lines += ["", "### Dead ends tried"]
    lines += _bullets(section.dead_ends, "none")
    lines += ["", "### Gate findings"]
    if section.gate_findings:
        lines += [
            f"- [{f.step}] {f.severity}{' (auto-fixed)' if f.fixed else ''}: {f.summary}"
            for f in section.gate_findings
        ]
    else:
        lines += ["- none"]
    # Advisory pipeline observations (2026-07-22 grilling): rendered only
    # when present — an absent or empty field renders nothing at all.
    if section.process_issues:
        lines += ["", "### Process issues"]
        lines += [render_process_issue(issue) for issue in section.process_issues]
    lines += [
        "",
        "<!-- theozolith:decisions:data",
        section.to_json(),
        "-->",
        END,
    ]
    return "\n".join(lines)


def render_process_issue(issue: ProcessIssue) -> str:
    """One bullet, shared by the PR body and the verdict comment."""
    if issue.suggested_fix:
        return f"- {issue.friction} — suggested fix: {issue.suggested_fix}"
    return f"- {issue.friction}"


def upsert(pr_body: str, section: DecisionsSection) -> str:
    """Replace (or append) the Decisions Section in a PR description."""
    block = render(section)
    begin = pr_body.find(BEGIN)
    end = pr_body.find(END)
    if begin != -1 and end != -1:
        return pr_body[:begin] + block + pr_body[end + len(END) :]
    if pr_body.strip():
        return f"{pr_body.rstrip()}\n\n{block}\n"
    return block + "\n"


def parse(pr_body: str) -> DecisionsSection | None:
    """Extract the machine copy of the Decisions Section from a PR body."""
    match = DATA_RE.search(pr_body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return section_from_dict(data) if isinstance(data, dict) else None


def section_text(pr_body: str) -> str:
    """The human-readable Decisions Section text (for the Reviewer prompt)."""
    begin = pr_body.find(BEGIN)
    end = pr_body.find(END)
    if begin == -1 or end == -1:
        return ""
    text = pr_body[begin + len(BEGIN) : end]
    return DATA_RE.sub("", text).strip()


def fallback_section(reason: str) -> DecisionsSection:
    """The section a Run ships when the agent recorded nothing usable."""
    return DecisionsSection(
        open_questions=[f"No agent-recorded decisions: {reason}"],
        remaining_work=["Review the diff without a decision record for this round."],
    )
