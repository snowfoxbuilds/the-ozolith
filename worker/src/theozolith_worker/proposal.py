"""The Output Proposal: the agent's only mutation surface (ADR-0046).

One schema-validated document per Run at ``output/proposal.json`` in the job
dir, written through the ``format-output`` CLI and applied post-exit by the
driver — the SOLE policy boundary. CLI validation is convenience: an agent
writing the file by hand changes nothing, because everything here is
re-checked driver-side. Forbidden mutations (base branch, issue state,
labels, other PRs) are unrepresentable — no field can express them, and
unknown fields are rejected. An absent optional field is a no-op, never a
clear.

Schemas are per worker type, keyed off the job manifest's mode. The schema
version is stamped into the manifest by the driver, asserted by the harness
before the session starts (a mismatch is a pre-session infra failure,
ADR-0016 — marked with :data:`SCHEMA_ERROR_PREFIX` so the driver never
mistakes it for harness breakage), asserted again by the CLI at first
invocation, and recorded in the proposal file itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_worker import jobdir
from theozolith_worker.decisions import (
    Decision,
    DecisionsSection,
    process_issues_from,
    section_from_dict,
)
from theozolith_worker.verdict import APPROVE, ESCALATE, GRADES, REVISE, Verdict

SCHEMA_VERSION = 1

PROPOSAL_FILE = "output/proposal.json"

# The pre-work schema-mismatch verdict (ADR-0046): anchored at the start of
# the harness's status error, exactly like the identity marker — a message
# merely containing it somewhere is not a schema verdict.
SCHEMA_ERROR_PREFIX = "schema-version: "

# Value kinds: how the CLI parses input and how validation checks shape.
LINE = "line"  # single-line non-empty string
TEXT = "text"  # multi-line non-empty string
ENUM = "enum"  # one of spec.choices
STRINGS = "strings"  # JSON list of non-empty strings
DECISION_ENTRIES = "decisions"  # JSON list of {"what": ..., "why": ...}
PROCESS_ENTRIES = "process-issues"  # JSON list of {"friction": ..., "suggested_fix": ...}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    choices: tuple[str, ...] = ()
    hint: str = ""


# Implementer (run mode): PR title/narrative, the Decisions-Section entries,
# and the required commit message (ADR-0046). The driver owns the "#N: "
# title prefix and the Closes line; these fields are the variable content.
RUN_FIELDS: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in (
        FieldSpec("pr-title", LINE, hint="descriptive PR title (the pipeline adds the #N: prefix)"),
        FieldSpec("pr-description", TEXT, hint="PR narrative (the pipeline composes the body)"),
        FieldSpec("commit-message", TEXT, hint="rich commit message; subject line + body"),
        FieldSpec("decisions", DECISION_ENTRIES, hint='[{"what": "...", "why": "..."}]'),
        FieldSpec("open-questions", STRINGS, hint="calls only a human can make"),
        FieldSpec("remaining-work", STRINGS, hint="what a follow-up round still needs"),
        FieldSpec("dead-ends", STRINGS, hint="approaches tried and abandoned"),
        FieldSpec(
            "process-issues",
            PROCESS_ENTRIES,
            hint='[{"friction": "...", "suggested_fix": "..."}] (advisory)',
        ),
    )
}

# Reviewer (review mode): the verdict enum plus its content. Invalid enum
# values and a final-round revise fail loud at WRITE time (ADR-0046,
# absorbing ADR-0014's validate-verdict harness job); the driver re-checks
# everything post-exit and still escalates one-strike on an invalid file.
REVIEW_FIELDS: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in (
        FieldSpec("verdict", ENUM, choices=(APPROVE, REVISE, ESCALATE)),
        FieldSpec("evidence", TEXT, hint="2-6 sentences citing files, criteria, decisions"),
        FieldSpec("deviation", ENUM, choices=GRADES, hint="approve only"),
        FieldSpec("risk", ENUM, choices=GRADES, hint="approve only"),
        FieldSpec("revised-plan", TEXT, hint="revise only: numbered, concrete steps"),
        FieldSpec("resume-commit", LINE, hint="revise only: commit SHA (empty = current head)"),
        FieldSpec("cherry-pick", STRINGS, hint="revise only: commit SHAs"),
        FieldSpec(
            "process-issues",
            PROCESS_ENTRIES,
            hint='[{"friction": "...", "suggested_fix": "..."}] (advisory)',
        ),
    )
}

_FIELDS_BY_MODE = {jobdir.MODE_RUN: RUN_FIELDS, jobdir.MODE_REVIEW: REVIEW_FIELDS}


class ProposalError(ValueError):
    """A proposal write or read that violates the schema."""


def fields_for(mode: str) -> dict[str, FieldSpec]:
    try:
        return _FIELDS_BY_MODE[mode]
    except KeyError:
        raise ProposalError(f"no output-proposal schema for mode {mode!r}") from None


def schema_mismatch(manifest_version: int) -> str | None:
    """The pre-work refusal, or None when the manifest's stamp matches the
    version this distribution speaks. 0 means an unstamped manifest (a
    driver older than the proposal channel) — equally a mismatch."""
    if manifest_version == SCHEMA_VERSION:
        return None
    return (
        f"{SCHEMA_ERROR_PREFIX}the job manifest stamps output-proposal schema"
        f" v{manifest_version} but this distribution speaks v{SCHEMA_VERSION}"
        " — driver and run image are out of step (pre-session infra failure,"
        " ADR-0016)"
    )


def schema_error_detail(message: str) -> str | None:
    """The detail of a schema-mismatch failure crossing the status.json
    channel, or None when ``message`` is not one. Anchored like the identity
    marker: it must BEGIN the status error (optionally behind the session
    layer's ``harness failed: `` wrapper)."""
    text = message.removeprefix("harness failed: ")
    if text.startswith(SCHEMA_ERROR_PREFIX):
        return text[len(SCHEMA_ERROR_PREFIX) :]
    return None


# -- the pending file ----------------------------------------------------------


def read_raw(job: Path) -> dict | None:
    """The proposal document as written, or None when absent/unparseable."""
    raw, _ = read_document(job)
    return raw


def read_document(job: Path) -> tuple[dict | None, str]:
    """The proposal document plus a precise problem statement when there is
    none — escalation forensics distinguish a session that wrote nothing
    from one that wrote garbage."""
    try:
        text = (job / PROPOSAL_FILE).read_text(encoding="utf-8")
    except OSError:
        return None, (
            "no output proposal was written (use format-output; run"
            " `format-output status` before finishing)"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"the output proposal is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "the output proposal is not a JSON object"
    return data, ""


def raw_text(job: Path) -> str | None:
    """The proposal file byte-for-byte (for evidence), or None when absent."""
    try:
        return (job / PROPOSAL_FILE).read_text(encoding="utf-8")
    except OSError:
        return None


def pending_fields(raw: dict | None) -> dict:
    fields = (raw or {}).get("fields")
    return fields if isinstance(fields, dict) else {}


def _parse_value(spec: FieldSpec, text: str) -> object:
    """Parse and shape-check one CLI-supplied value. Strict on purpose —
    write time is where the agent can still fix it."""
    if spec.kind in (LINE, TEXT, ENUM):
        value = text.strip("\n") if spec.kind != LINE else text.strip()
        if not value.strip():
            raise ProposalError(f"{spec.name} must not be empty")
        if spec.kind == LINE and "\n" in value:
            raise ProposalError(f"{spec.name} must be a single line")
        if spec.kind == ENUM and value not in spec.choices:
            raise ProposalError(f"{spec.name} must be one of {', '.join(spec.choices)}")
        return value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"{spec.name} takes a JSON array ({spec.hint}): {exc}") from None
    error = _list_shape_error(spec, parsed)
    if error:
        raise ProposalError(error)
    return parsed


def _list_shape_error(spec: FieldSpec, value: object) -> str | None:
    if not isinstance(value, list):
        return f"{spec.name} must be a JSON array ({spec.hint})"
    if spec.kind == STRINGS:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return f"{spec.name} must be an array of non-empty strings"
        return None
    key = "what" if spec.kind == DECISION_ENTRIES else "friction"
    optional = "why" if spec.kind == DECISION_ENTRIES else "suggested_fix"
    for item in value:
        if not isinstance(item, dict):
            return f"{spec.name} entries must be objects ({spec.hint})"
        if not isinstance(item.get(key), str) or not item[key].strip():
            return f"{spec.name} entries need a non-empty {key!r}"
        if not isinstance(item.get(optional, ""), str):
            return f"{spec.name} entry {optional!r} must be a string"
        unknown = set(item) - {key, optional}
        if unknown:
            return f"{spec.name} entries take only {key!r}/{optional!r}, not {sorted(unknown)}"
    return None


def write_field(job: Path, manifest: jobdir.Manifest, name: str, value_text: str) -> object:
    """One CLI write: parse, apply the write-time rules, persist atomically.
    Returns the parsed value. Raises ProposalError on any refusal."""
    specs = fields_for(manifest.mode)
    if name not in specs:
        raise ProposalError(
            f"unknown field {name!r} for {manifest.mode} mode; the schema allows:"
            f" {', '.join(specs)} (anything else is deliberately unrepresentable)"
        )
    value = _parse_value(specs[name], value_text)
    # The final-round rule, enforced where the agent can still change course
    # (ADR-0046 absorbing ADR-0014's validate-verdict job). The driver
    # re-checks post-exit either way.
    if name == "verdict" and value == REVISE and manifest.final_round:
        raise ProposalError(
            "final-round rule: this is the last budgeted review round —"
            " revise is unavailable (approve or escalate only)"
        )
    raw = read_raw(job) or {}
    fields = pending_fields(raw)
    fields[name] = value
    document = {
        "schema_version": SCHEMA_VERSION,
        "mode": manifest.mode,
        "fields": fields,
    }
    jobdir.atomic_write(job / PROPOSAL_FILE, json.dumps(document, indent=2, sort_keys=True))
    return value


def describe_value(spec: FieldSpec, value: object) -> str:
    """One fill-state cell: what `format-output` echoes per write and what
    the `status` table shows per field."""
    if value is None:
        return "empty"
    if spec.kind == ENUM:
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} entries" if len(value) != 1 else "1 entry"
    text = str(value)
    return f"{len(text)} chars" if len(text) != 1 else "1 char"


# -- required fields and validation --------------------------------------------


def required_fields(mode: str, round_number: int) -> tuple[str, ...]:
    """The unconditionally required fields. commit-message on every round;
    pr-title/pr-description only on the round that creates the PR (on resume
    rounds an absent field means keep-what-exists, ADR-0046). Conditional
    requirements (approve needs grades, revise needs a plan) live in
    :func:`validate_review`."""
    if mode == jobdir.MODE_RUN:
        if round_number <= 1:
            return ("pr-title", "pr-description", "commit-message")
        return ("commit-message",)
    return ("verdict", "evidence")


def _document_errors(raw: dict | None, mode: str) -> list[str]:
    """Structural refusals that apply to the document as a whole."""
    if raw is None:
        return [
            "no output proposal was written (use format-output; run"
            " `format-output status` before finishing)"
        ]
    errors = []
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(f"proposal schema_version {version!r} is not {SCHEMA_VERSION}")
    stamped = raw.get("mode")
    if stamped != mode:
        errors.append(f"proposal mode {stamped!r} is not {mode!r}")
    if not isinstance(raw.get("fields"), dict):
        errors.append("proposal carries no fields object")
    unknown_keys = set(raw) - {"schema_version", "mode", "fields"}
    if unknown_keys:
        errors.append(f"unknown proposal keys {sorted(unknown_keys)}")
    return errors


def field_errors(fields: dict, mode: str, round_number: int) -> list[str]:
    """Missing required fields plus shape errors on whatever is present —
    the driver-side check and the completion-retry appendix source alike.
    process-issues stays lenient (advisory content must never invalidate a
    proposal — malformed entries are dropped at composition, never errors)."""
    specs = fields_for(mode)
    errors = []
    for name in sorted(set(fields) - set(specs)):
        errors.append(f"unknown field {name!r} (the {mode} schema allows: {', '.join(specs)})")
    for name in required_fields(mode, round_number):
        if name not in fields:
            errors.append(f"{name} (missing)")
    for name, value in fields.items():
        spec = specs.get(name)
        if spec is None or spec.kind == PROCESS_ENTRIES:
            continue
        if spec.kind in (LINE, TEXT, ENUM):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} (must be a non-empty string)")
            elif spec.kind == LINE and "\n" in value:
                errors.append(f"{name} (must be a single line)")
            elif spec.kind == ENUM and value not in spec.choices:
                errors.append(f"{name} (must be one of {', '.join(spec.choices)})")
        else:
            shape = _list_shape_error(spec, value)
            if shape:
                errors.append(f"{name} ({shape})")
    return errors


@dataclass(frozen=True)
class RunProposal:
    """The validated Implementer proposal, ready for the driver to apply."""

    commit_message: str
    pr_title: str = ""  # "" on resume rounds: keep the existing title
    pr_description: str = ""  # "" on resume rounds: keep the existing body
    section: DecisionsSection = field(default_factory=DecisionsSection)


def _section_from(fields: dict) -> DecisionsSection:
    return DecisionsSection(
        decisions=[
            Decision(what=item["what"], why=item.get("why", ""))
            for item in fields.get("decisions", [])
        ],
        open_questions=list(fields.get("open-questions", [])),
        remaining_work=list(fields.get("remaining-work", [])),
        dead_ends=list(fields.get("dead-ends", [])),
        process_issues=process_issues_from(fields.get("process-issues")),
    )


def lenient_section(raw: dict | None) -> DecisionsSection:
    """Best-effort Decisions Section from whatever fields are pending —
    evidence fidelity for a proposal that FAILED validation (the completion
    lane still bundles what the agent did record). Never a validation path."""
    fields = pending_fields(raw)
    return section_from_dict(
        {
            "decisions": fields.get("decisions"),
            "open_questions": fields.get("open-questions"),
            "remaining_work": fields.get("remaining-work"),
            "dead_ends": fields.get("dead-ends"),
            "process_issues": fields.get("process-issues"),
        }
    )


def validate_run(raw: dict | None, *, round_number: int) -> tuple[RunProposal | None, list[str]]:
    """Strict driver-side validation of an Implementer proposal. Returns
    (proposal, []) or (None, errors) — the error list is what the
    completion-retry appendix quotes (ADR-0016 as amended)."""
    errors = _document_errors(raw, jobdir.MODE_RUN)
    if errors:
        return None, errors
    fields = pending_fields(raw)
    errors = field_errors(fields, jobdir.MODE_RUN, round_number)
    if errors:
        return None, errors
    return (
        RunProposal(
            commit_message=fields["commit-message"],
            pr_title=fields.get("pr-title", ""),
            pr_description=fields.get("pr-description", ""),
            section=_section_from(fields),
        ),
        [],
    )


def validate_run_job(job: Path, *, round_number: int) -> tuple[RunProposal | None, list[str]]:
    """:func:`validate_run` against the job dir's pending file, with the
    precise absent/garbled diagnosis when there is no document."""
    raw, problem = read_document(job)
    if problem:
        return None, [problem]
    return validate_run(raw, round_number=round_number)


def validate_review_job(
    job: Path,
    *,
    round_number: int,
    final_round: bool,
    default_resume: str,
    bundle_url: str,
) -> tuple[Verdict | None, str]:
    """:func:`validate_review` against the job dir's pending file, with the
    precise absent/garbled diagnosis when there is no document."""
    raw, problem = read_document(job)
    if problem:
        return None, problem
    return validate_review(
        raw,
        round_number=round_number,
        final_round=final_round,
        default_resume=default_resume,
        bundle_url=bundle_url,
    )


def validate_review(
    raw: dict | None,
    *,
    round_number: int,
    final_round: bool,
    default_resume: str,
    bundle_url: str,
) -> tuple[Verdict | None, str]:
    """Strictly validate a Reviewer proposal into the Verdict the driver
    publishes. Returns (verdict, "") or (None, reason). Strict by design
    (ADR-0014's rules carried forward against the proposal, ADR-0046): an
    invalid proposal must be reliably detected so the driver applies no
    PR-side state and escalates one-strike. The final-round rule is enforced
    here as well as at write time — the driver is the boundary."""
    errors = _document_errors(raw, jobdir.MODE_REVIEW)
    if not errors:
        fields = pending_fields(raw)
        errors = field_errors(fields, jobdir.MODE_REVIEW, round_number)
    if errors:
        return None, "; ".join(errors)

    kind = fields["verdict"]
    deviation = fields.get("deviation")
    risk = fields.get("risk")
    if kind == APPROVE:
        if deviation not in GRADES:
            return None, f"approve requires deviation in {GRADES}, got {deviation!r}"
        if risk not in GRADES:
            return None, f"approve requires risk in {GRADES}, got {risk!r}"
    if kind == REVISE:
        if final_round:
            return None, (
                "final-round rule: a revise at the last budgeted round would "
                "commission a Run with no remaining review round"
            )
        if not str(fields.get("revised-plan", "")).strip():
            return None, "revise requires a non-empty revised-plan"

    return (
        Verdict(
            verdict=kind,
            round=round_number,
            evidence=fields["evidence"].strip(),
            deviation=deviation if deviation in GRADES else None,
            risk=risk if risk in GRADES else None,
            revised_plan=str(fields.get("revised-plan", "")),
            resume_commit=str(fields.get("resume-commit", "")) or default_resume,
            cherry_pick=[sha for sha in fields.get("cherry-pick", []) if sha],
            bundle_url=bundle_url,
            # Advisory: parsed leniently — malformed entries are dropped,
            # never a validation failure that could trigger an escalation.
            process_issues=process_issues_from(fields.get("process-issues")),
        ),
        "",
    )
