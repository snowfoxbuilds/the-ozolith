"""The Decisions Section: the mandatory schema on every best-effort PR.

Fixed schema inherited from the former Handoff Doc (ADR-0003 as amended by
ADR-0008): decisions made with rationale, open questions, remaining work,
dead ends tried — plus the gate's structured findings, which the best-effort
contract records here when they are unresolvable.

The section is embedded in the PR description between markers, with the
machine-readable JSON in an HTML comment and the human-readable markdown
rendered from it. The agent hands the Worker its half by writing
``.theozolith/decisions.json`` in the worktree; a Run that fails to produce a
parseable file still ships — the Worker synthesizes a section saying so (the
gate and the contract never block PR creation).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from theozolith_worker.gate.pipeline import Finding

BEGIN = "<!-- theozolith:decisions:begin -->"
END = "<!-- theozolith:decisions:end -->"
DATA_RE = re.compile(r"<!-- theozolith:decisions:data\n(.*?)\n-->", re.DOTALL)

AGENT_FILE = Path(".theozolith") / "decisions.json"


@dataclass(frozen=True)
class Decision:
    what: str
    why: str


@dataclass
class DecisionsSection:
    decisions: list[Decision] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)
    gate_findings: list[Finding] = field(default_factory=list)

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


def section_from_dict(data: dict) -> DecisionsSection:
    return DecisionsSection(
        decisions=_decisions(data.get("decisions")),
        open_questions=_strings(data.get("open_questions")),
        remaining_work=_strings(data.get("remaining_work")),
        dead_ends=_strings(data.get("dead_ends")),
        gate_findings=[
            Finding(**entry) for entry in data.get("gate_findings", []) if isinstance(entry, dict)
        ],
    )


def read_agent_decisions(worktree: Path) -> DecisionsSection | None:
    """Parse the agent-written decisions file, or None if absent/invalid."""
    path = worktree / AGENT_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return section_from_dict(data)


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
    lines += [
        "",
        "<!-- theozolith:decisions:data",
        section.to_json(),
        "-->",
        END,
    ]
    return "\n".join(lines)


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
