"""Run execution, driver-side: one stateless, disposable attempt at one issue.

Every Run: fresh token-free clone, fresh run container, fresh agent context.
Resume state is exactly what the Reviewer designated — the PR branch at the
resume commit (plus cherry-picked commits) and the revised plan in the
verdict comment — nothing else survives between Runs (ADR-0008).

Context comes from GitHub at checkout, never from the dispatch grant
(ADR-0017 as amended, #52): every Run — local retries included — re-reads
the issue and PR context and materializes it as the Context Tree under the
job dir's ``input/``, filtered to OWNER/MEMBER authors (the authority
boundary; see contexttree). The prompt stays slim — rules, the issue body,
the revised plan on resume rounds, and a navigation guide — and the agent
discovers everything else in the tree; no discussion content is injected
and no relevance heuristic prunes it: the only filter is the authority
boundary, which also governs machine-verdict discovery.

The driver never executes repository code or model output (ADR-0013): the
agent session and every gate step run inside the ephemeral run container,
commissioned through the job directory; the driver performs the side effects
(commit, push, PR, labels) after sanitizing the container-touched checkout's
git metadata.

The agent's outputs leave the session as one Output Proposal (ADR-0046):
``output/proposal.json`` in the job dir, written through the format-output
CLI, validated and applied here post-exit — the sole policy boundary. The
driver composes the PR (``#N: `` title prefix, Closes line + narrative +
Decisions Section body) and commits with the proposed commit message plus a
provenance trailer; no fallback-generated message ever ships.

Run outcomes (ADR-0014, failure lane replaced by ADR-0016): a completed
session with commits ships the normal best-effort PR; a completed session
with zero commits but no-change reasoning in the proposal ships an
EMPTY PR (one driver-synthesized allow-empty commit) the Reviewer judges
like any other; everything else is a FAILED Run. The failed lane is local
retry (``execute_claim``): the driver keeps the claim and launches one full
second Run — new run_id, fresh clone and container, its own evidence
bundle — under a uniform budget across failure classes. A second
non-completion releases the claim and applies failed + needs_human with
both evidence links and each failure's class. Nothing is re-queued and no
state lives in comments.

One narrower lane joins it (ADR-0016 as amended by ADR-0046): a COMPLETED
session whose proposal fails validation gets exactly one completion retry —
new container, new run_id, its own evidence bundle, but the worktree and the
partially-filled proposal preserved, with a machine-generated error appendix
on the prompt. Capped at one and terminal: the retry ships or the claim
escalates. Implementer-only; the Reviewer's one-strike rule stands.

Evidence is the sole durable audit trail (ADR-0016): every Run pushes its
bundle, and a job directory is deleted only after that push confirmed —
otherwise it is parked beside the jobs dir for the boot-time evidence
sweep. The bundle's input half (prompt, issue metadata, Context Tree) comes
from a trusted snapshot frozen immediately before the container launches
(#52): input/ is agent-writable through the /job bind mount from launch
onward, so post-execution re-reads are never evidence.

One enumerated exception (M5, ADR-0019): when parking AND its
collision-safe fallback both fail, the completed directory is removed
unpublished — accepted evidence loss, because a completed dir left in the
jobs dir reads as an in-flight Run to queue-behind.
"""

from __future__ import annotations

import itertools
import json
import re
import secrets
import shutil
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

from theozolith_worker import (
    adapters,
    basedon,
    contexttree,
    decisions,
    deps,
    events,
    evidence,
    gitops,
    jobdir,
    proposal,
    verdict,
)
from theozolith_worker.bootstrap.vocabulary import (
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
    attempts_on,
)
from theozolith_worker.config import DriverConfig
from theozolith_worker.containers import (
    ContainerSpec,
    container_labels,
    run_container_name,
)
from theozolith_worker.deps import branch_for, issue_for_branch
from theozolith_worker.gate.pipeline import Finding, GateResult, run_gate
from theozolith_worker.githubapi import GitHubClient, Issue
from theozolith_worker.identity import identity_error_detail
from theozolith_worker.sessions import SessionError, SessionFactory
from theozolith_worker.sweep import TOMBSTONE_PREFIX, park_job_dir, pending_dir

# Paths in the checkout that are pipeline metadata, never repo content.
# (The in-worktree decisions file is gone — the Output Proposal lives in the
# job dir, outside the checkout, ADR-0046.)
EXCLUDED_METADATA = (".claude/settings.local.json",)

# Runs per claim: the original plus exactly one local retry (ADR-0016).
CLAIM_RUN_BUDGET = 2
# Delivery attempts for the claimed event that activates a grant (ADR-0017).
ACTIVATION_ATTEMPTS = 3

# Distinguishes Runs started within the same second by one driver process.
_RUN_SEQUENCE = itertools.count(1)


def new_run_id(config: DriverConfig) -> str:
    """run-id = <utc-timestamp>-<worker-id>-<seq> (ADR-0014)."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{config.worker_id}-{next(_RUN_SEQUENCE)}"


def _log(message: str) -> None:
    print(message, flush=True)


PROMPT_TEMPLATE = """\
You are an Implementer in TheOzolith agentic coding pipeline, executing one \
stateless, headless Run against the checked-out repository (your working \
directory). No human watches or steers this session; everything the pipeline \
needs from you must land in the working tree and your Output Proposal \
(written with the `format-output` CLI).

## Issue #{number}: {title}

{body}

{round_context}## Context tree

The issue and PR context — all comments and reviews from the repository's \
maintainers (authors GitHub reports as OWNER or MEMBER; content from any \
other author is removed before you see this tree, as a security boundary) — \
is on disk at `{job}/input/`, fetched fresh for this Run. Within that \
boundary the tree is complete and untruncated. Nothing beyond the issue \
body above is injected into this prompt: navigate the tree instead.

- `{job}/input/issue/comments/INDEX.md` — the maintainer issue comments, \
one line each; full text in the numbered files beside the index
- `{job}/input/issue/timeline.md` — issue events (labels, assignments, \
renames, references)
- `{job}/input/pr/` — present only when this Run resumes an existing PR: \
`body.md`, `conversation/` (maintainer PR comments, verdicts included), \
`review-comments/` (inline file/line comments), `reviews/`, `commits.md` \
(every PR commit), `checks.md` (check runs and commit statuses)
{deps_bullet}
Read each surface's `INDEX.md` first and open only the items you need. \
Comments may answer open questions, record human decisions, or narrow the \
scope — a human decision in a comment outranks the issue body above.

## Rules

- Implement the issue's acceptance criteria directly in the working tree.
- Never stop to ask questions. When a judgment call comes up, decide, then \
record the decision and its rationale. A separate Reviewer adjudicates your \
decisions afterwards.
- Everything the pipeline may publish for you goes through your Output \
Proposal, written with `format-output <field> <value>` (multi-line values: \
`-` reads stdin, or `--file <path>`; `view-output <field>` shows pending \
state). Nothing you run touches GitHub — the proposal is validated and \
applied only after you exit.
- `format-output commit-message` is REQUIRED every round. The pipeline \
commits your work with exactly this message (plus a provenance trailer) — \
no message is ever generated for you. Git history is the only context \
surface guaranteed to every future Run, so write it rich: a subject line, \
the what and why, key decisions with rationale, dead ends tried, \
constraints discovered. A weak message is a legitimate review finding.
- `format-output pr-title` (descriptive; the pipeline adds the `#N: ` \
prefix) and `format-output pr-description` (the PR narrative) are required \
on the round that creates the PR; on later rounds omit them to keep what \
exists.
- Fill the PR's Decisions Section through these fields (each takes a JSON \
array): `decisions` `[{{"what": "...", "why": "..."}}]` — judgment calls \
with rationale; `open-questions` — calls only a human can make; \
`remaining-work` — what a follow-up round still needs; `dead-ends` — \
approaches you tried and abandoned (so the next Run does not repeat them).
- `process-issues` (`[{{"friction": "...", "suggested_fix": "..."}}]`) is \
optional and advisory: observations about the PIPELINE itself (friction you \
hit — a confusing prompt, missing tooling, a flaky gate). Never findings \
about the change; it influences no verdict, label, or gate outcome.
- If you conclude that NO code change is needed, change nothing and record \
why in `decisions` and the commit message — the pipeline ships an empty PR \
carrying your reasoning for review. A run with no changes and no recorded \
reasoning is treated as a failure.
- Before you finish, run `format-output status` and make sure it reports \
the proposal as valid — an incomplete proposal cannot ship.
- Do not run git commit/push and do not open PRs; the pipeline owns version \
control and commits with your proposed message.
- Do not edit `.theozolith/gate.toml` or CI workflows unless the issue \
explicitly asks for it.
- When you are done, simply finish your reply and exit; the pipeline treats \
your process exit as completion and runs the quality gate.
"""

# The navigation-guide bullet for input/deps/, present only when the issue
# carries Dependency Edges (ADR-0053). Ends with the newline the template
# slot omits, so the edge-less prompt stays byte-identical.
DEPS_BULLET = (
    "- `{job}/input/deps/INDEX.md` — this issue's dependency closure "
    "(ADR-0053): every blocker issue and its PR, serialized like the "
    "surfaces above, in topological order. The Decisions Section in an "
    "unmerged blocker's PR body is the closest thing to that work's "
    "documentation.\n"
)

REVISED_PLAN_CONTEXT = """\
## Revised plan (round {round})

A Reviewer judged the previous round of this work; the branch you are on is \
its designated resume state, resumed from commit `{resume}`. Execute this \
revised plan exactly:

{plan}

"""

# Appended when the checkout base is an unmerged blocker's branch (a
# Chained-Base Run, ADR-0053): the blocker's work is present but under its
# own separate review — the prompt contract keeps this session from
# "fixing" it out from under that review.
CHAINED_BASE_CONTEXT = """\


## Chained base

Your checkout is based on `{base_branch}` — it includes UNMERGED work from \
issue #{blocker}{pr_ref}, which is under its own separate review. Treat that \
work's interfaces as authoritative: build on them exactly as they stand. Do \
not refactor, restyle, or "fix" the blocker's code — even where it looks \
wrong. If a defect there genuinely blocks you, record it as a Decisions \
Section entry (`decisions` or `open-questions`) and proceed best-effort.
"""

# The machine-generated error appendix on a completion retry (ADR-0016 as
# amended by ADR-0046): fill-only instruction, soft enforcement — churn the
# retry session does make is reviewable like anything else.
COMPLETION_RETRY_CONTEXT = """\


## Completion retry: finish the previous session's Output Proposal

A previous session completed the work in this working tree but exited with \
an invalid Output Proposal. The current work is unfinished, missing: \
{missing}.

The working tree and the partially-filled proposal are preserved exactly as \
that session left them. Do NOT redo or rework the change: review the \
existing state (`git status`, `git diff`, `view-output <field>`), fill in \
what is missing with `format-output`, confirm with `format-output status`, \
and exit.
"""


@dataclass
class CompletionCarryover:
    """The enumerated no-carryover exception (ADR-0016 as amended by
    ADR-0046): everything one completion retry inherits from a completed Run
    whose Output Proposal failed validation. The worktree is parked in a
    dot-prefixed sibling of the job dirs (ignored by queue-behind and the
    evidence sweep) until the retry Run adopts it; the agent session itself
    is never preserved."""

    checkout: Path  # the preserved worktree, parked outside any job dir
    proposal_text: str | None  # the pending proposal file, byte-for-byte
    errors: list[str]  # what validation rejected — quoted in the appendix
    base_branch: str
    # The base tip recorded at the original checkout: the retry re-ships
    # against the carried base — worktree and Based-on zone alike — and
    # never re-resolves the chain (the worktree embodies the base; ADR-0053).
    base_sha: str
    force: bool  # push disposition the preserved worktree embodies


@dataclass
class RunReport:
    run_id: str
    issue: int
    round: int
    phase: str = "claimed"  # claimed | checkout | agent | gate | empty-pr | pr-open | failed
    pr_number: int | None = None
    branch: str = ""
    head: str = ""
    container: str = ""
    agent_outcome: str = ""  # completed | timed out | session died | harness-failed | unknown
    gate_findings: int = 0
    reason: str = ""  # failed Runs: what broke
    # ADR-0016 uniform budget classes: timeout | session-died | harness |
    # no-changes | infra ("" for completed Runs). ADR-0045 adds identity:
    # the static checks failed before launch or the fail-loud monitor
    # detected an off-identity session (killed mid-run) — the Run is
    # invalid. ADR-0046 adds completion: the session completed but its
    # Output Proposal failed validation — the one class whose retry
    # preserves the worktree.
    failure_class: str = ""
    # Set only on a completion-classed failure that successfully parked its
    # worktree: what execute_claim hands the one completion retry.
    carryover: CompletionCarryover | None = None
    evidence_pushed: bool = False
    # True only on the compound failure: the push failed AND both parking
    # attempts failed, so the completed job dir was removed unpublished
    # (accepted evidence loss, M5 — never a false in-flight signal).
    evidence_discarded: bool = False
    notes: list[str] = field(default_factory=list)


class _RunFailed(Exception):
    """Internal control flow: this Run is a FAILED Run (ADR-0016)."""

    def __init__(self, reason: str, failure_class: str):
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


@dataclass
class _RunContext:
    """What the failure path needs from however far the Run body got."""

    base_branch: str = ""
    # The base branch's tip as this Run checked it out — the Based-on
    # zone's recorded SHA when the base is a blocker branch (ADR-0053).
    base_sha: str = ""
    gate: GateResult = field(default_factory=GateResult)
    section: decisions.DecisionsSection | None = None
    # The push disposition the checkout ended up with (reviewer-designated
    # reset or stale-branch overwrite): a completion retry must carry it.
    force: bool = False
    # Set when a completed session's proposal failed validation: the error
    # list a completion retry's prompt appendix quotes (ADR-0016 as amended).
    completion_errors: list[str] | None = None
    # The pre-launch input snapshot (#52): captured immediately before the
    # container starts, so evidence never re-reads input the agent could
    # have rewritten through the /job bind mount. None means no container
    # was ever launched — the job dir's input is still driver-authored.
    trusted_input: dict[str, bytes] | None = None


def render_run_prompt(
    issue: Issue,
    round_number: int,
    revised: verdict.Verdict | None,
    *,
    chained: str = "",
    deps_present: bool = False,
) -> str:
    """Both round shapes (#52): rules + issue body + the navigation guide,
    plus the revised plan and resume commit on resume rounds, plus the
    Chained-base section when the base is a blocker branch (``chained``,
    pre-rendered from CHAINED_BASE_CONTEXT; ADR-0053) and the input/deps
    bullet when the issue carries Dependency Edges. Discussion content is
    never injected — every authorized comment lives in the Context Tree.

    ``schema_version`` surface (ADR-0054): the production implementer prompt
    renderer, exposed through ``theozolith_worker.api`` so a bench driver
    replays the exact bytes this driver writes to ``input/prompt.md``."""
    round_context = ""
    if revised is not None and revised.verdict == verdict.REVISE and revised.revised_plan:
        round_context = REVISED_PLAN_CONTEXT.format(
            round=round_number,
            resume=revised.resume_commit or "(the branch head)",
            plan=revised.revised_plan,
        )
    deps_bullet = DEPS_BULLET.format(job=jobdir.CONTAINER_JOB_PATH) if deps_present else ""
    return (
        PROMPT_TEMPLATE.format(
            number=issue.number,
            title=issue.title,
            body=issue.body or "(no body)",
            round_context=round_context,
            job=jobdir.CONTAINER_JOB_PATH,
            deps_bullet=deps_bullet,
        )
        + chained
    )


def _exclude_metadata(workdir: Path) -> None:
    """Keep pipeline metadata out of commits, surviving agent tampering."""
    exclude = workdir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("".join(f"{path}\n" for path in EXCLUDED_METADATA), encoding="utf-8")


def _read_output(job: Path, relpath: str) -> str:
    try:
        return (job / relpath).read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_stats(config: DriverConfig, job: Path) -> adapters.StreamStats:
    """Counters from the structured output stream (ADR-0019): token usage
    (None when the stream carries none) and the observed model — telemetry's
    model source now that selection is baked into the image (ADR-0045)."""
    return adapters.stream_stats(config.adapter, job / jobdir.TRANSCRIPT_FILE)


def _write_issue_metadata(job: Path, issue: Issue, *, round_number: int) -> None:
    jobdir.atomic_write(
        job / "input" / "issue.json",
        json.dumps(
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "labels": sorted(issue.labels),
                "round": round_number,
            },
            indent=2,
            sort_keys=True,
        ),
    )


def _has_reasoning(section: decisions.DecisionsSection | None) -> bool:
    """Did the agent record anything that can justify a no-change Run?"""
    return section is not None and bool(
        section.decisions or section.open_questions or section.remaining_work or section.dead_ends
    )


def _no_change_intro(section: decisions.DecisionsSection) -> str:
    lines = ["This Run concluded that **no code change is needed**. The Worker's reasoning:", ""]
    if section.decisions:
        lines += [f"- **{d.what}**" + (f" — {d.why}" if d.why else "") for d in section.decisions]
    else:
        lines += [f"- {item}" for item in section.open_questions + section.dead_ends]
    return "\n".join(lines)


def commit_message_with_trailer(proposed: str, run_id: str, issue_number: int, round_n: int) -> str:
    """The proposed commit message plus the provenance trailer (ADR-0046).
    The previously generated ``Run <id> for #<N> (round <r>)`` subject is
    demoted to these trailer facts; the agent-authored message is the
    archival copy of the round's context — no fallback ever ships."""
    return (
        f"{proposed.rstrip()}\n"
        "\n"
        f"Ozolith-Run: {run_id}\n"
        f"Ozolith-Issue: #{issue_number}\n"
        f"Ozolith-Round: {round_n}\n"
    )


def compose_pr_body(
    issue_number: int,
    narrative: str,
    section: decisions.DecisionsSection,
    *,
    no_change_intro: str = "",
) -> str:
    """Zone composition (ADR-0046): Closes line + narrative + Decisions
    Section. The driver owns the frame; the proposal supplies the content."""
    zones = [f"Closes #{issue_number}."]
    if no_change_intro:
        zones.append(no_change_intro)
    if narrative.strip():
        zones.append(narrative.strip())
    return decisions.upsert("\n\n".join(zones), section)


def execute_run(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    *,
    run_id: str | None = None,
    log=_log,
    sink: events.EventSink | None = None,
    completion: CompletionCarryover | None = None,
) -> RunReport:
    """One Run on one claimed issue. Assumes the claim already exists on
    GitHub (written by the Control Node, ADR-0017). Never raises: every
    failure mode returns a failed report under the uniform budget.

    ``completion`` makes this Run the one completion retry (ADR-0016 as
    amended): the preserved worktree and pending proposal are adopted
    instead of a fresh clone, and the prompt carries the error appendix."""
    run_id = run_id or new_run_id(config)
    sink = sink or events.make_sink(config, log)
    job = jobdir.create_job_dir(config.jobs_dir, run_id)
    # Issue metadata lands BEFORE any slow work (clone can take minutes):
    # a driver death from here on leaves a job dir the boot sweep can file
    # under the correct runs/issue-N path (ADR-0016). Rewritten with the
    # final round number once the checkout settles it.
    _write_issue_metadata(job, issue, round_number=1)
    report = RunReport(
        run_id=run_id,
        issue=issue.number,
        round=1,
        branch=branch_for(issue.number),
        container=run_container_name(run_id),
    )
    context = _RunContext()
    try:
        _run_to_pr(
            config, client, issue, session_factory, job, report, context, log, sink, completion
        )
    except _RunFailed as failed:
        _fail_run(config, issue, report, job, context, failed, log, sink)
        # The evidence (diffstat included) is pushed; NOW the worktree can
        # leave the doomed job dir. Only a first miss parks one — the retry
        # itself never stacks another carryover (capped at one, terminal).
        if (
            failed.failure_class == "completion"
            and completion is None
            and context.completion_errors is not None
        ):
            report.carryover = _stash_completion_carryover(config, job, report, context, log)
    except Exception as exc:  # pre/post-session driver-side breakage
        # An internal failure, not a Run outcome: container-start and GitHub
        # write failures land here — summarize for the dashboard errors
        # panel (2026-07-21 grilling) before the failed-Run machinery runs.
        events.emit_error(
            sink,
            config,
            error_class=type(exc).__name__,
            message=f"run {run_id}: driver-side failure: {exc}",
            context=traceback.format_exc(),
        )
        _fail_run(
            config,
            issue,
            report,
            job,
            context,
            _RunFailed(f"driver-side failure: {exc}", "infra"),
            log,
            sink,
        )
    finally:
        if report.evidence_pushed:
            shutil.rmtree(job, ignore_errors=True)
            # The trusted input snapshot goes only after the job dir is
            # fully gone: if removal left remnants, the sweep may push this
            # bundle path again, and it must keep using the snapshot — a
            # tampered remnant input must never overwrite good evidence.
            if not job.exists():
                evidence.discard_input_snapshot(Path(config.jobs_dir), run_id)
        else:
            # The bundle is the sole durable audit trail: never delete a job
            # dir whose push is unconfirmed — the evidence sweep retries it.
            # Parked in the -pending sibling immediately, so the Node
            # Daemon's queue-behind in-flight signal never reads a finished
            # Run's retained evidence as a live Run (ADR-0019). The trusted
            # input snapshot stays with it: the sweep builds the retried
            # bundle from that snapshot, never from the retained job dir.
            _park_for_sweep(config, job, report, log)
            if report.evidence_discarded:
                evidence.discard_input_snapshot(Path(config.jobs_dir), run_id)
    return report


def _park_failure_record(log, event: str, report: RunReport, source: Path, destination: Path, exc):
    """One structured parking-failure record (M5): machine-greppable with
    everything an operator needs to find what was (or was not) saved."""
    log(
        "evidence parking failed: "
        + json.dumps(
            {
                "event": event,
                "run_id": report.run_id,
                "issue": report.issue,
                "source": str(source),
                "destination": str(destination),
                "error": str(exc),
            },
            sort_keys=True,
        )
    )


def _park_for_sweep(config: DriverConfig, job: Path, report: RunReport, log) -> None:
    """Move a retained job dir into the sweep's parking sibling. Failure
    ladder (M5): the plain atomic rename; then a structured failure record
    plus a collision-safe unique destination; then a second structured
    record and REMOVAL of the completed directory from the active jobs dir
    — evidence loss in that compound case is explicitly accepted, because
    a completed dir left in place reads as an in-flight Run to the Node
    Daemon's queue-behind signal. Never raises: escalation must proceed."""
    try:
        kept = park_job_dir(config, job)
        log(f"run {job.name}: job directory kept for the evidence sweep ({kept})")
        return
    except OSError as exc:
        _park_failure_record(
            log, "theozolith.evidence-park-failed", report, job, pending_dir(config) / job.name, exc
        )
    # The unique suffix keeps -<worker-id>- in the name, so the sweep still
    # recognizes the parked dir as this driver's to push.
    fallback = f"{job.name}-parked-{secrets.token_hex(4)}"
    try:
        kept = park_job_dir(config, job, target_name=fallback)
        log(f"run {job.name}: job directory kept for the evidence sweep ({kept})")
        return
    except OSError as exc:
        _park_failure_record(
            log, "theozolith.evidence-lost", report, job, pending_dir(config) / fallback, exc
        )
    shutil.rmtree(job, ignore_errors=True)
    if not job.exists():
        report.evidence_discarded = True
        log(
            f"run {job.name}: parking failed twice — the completed job directory is removed"
            " unpublished (accepted evidence loss) so queue-behind never mistakes it for an"
            " active Run"
        )
        return
    # Removal left remnants (e.g. container-owned files the driver cannot
    # unlink). A tombstone rename WITHIN the jobs dir needs only write
    # permission on the jobs dir itself, never on the remnants — and the
    # dot-prefixed form is skipped by both queue-behind's in-flight signal
    # and the sweep, so it can no longer read as a live Run.
    tombstone = job.with_name(f"{TOMBSTONE_PREFIX}{job.name}-{secrets.token_hex(4)}")
    try:
        job.rename(tombstone)
    except OSError as exc:
        # Unwritable jobs dir: the one state with nothing left to try. The
        # dir stays under its Run name — the sweep may still publish it, so
        # the comment's retained branch stays truthful — but queue-behind
        # will defer until an operator clears it. Record exactly that.
        _park_failure_record(log, "theozolith.evidence-remnants", report, job, tombstone, exc)
        log(
            f"run {job.name}: parking failed twice AND removal left remnants at {job} —"
            " queue-behind may read it as in-flight until an operator clears it"
        )
        return
    report.evidence_discarded = True
    shutil.rmtree(tombstone, ignore_errors=True)  # best-effort; the name is inert either way
    log(
        f"run {job.name}: parking failed twice and removal left remnants — tombstoned as"
        f" {tombstone.name} (accepted evidence loss; ignored by queue-behind and the sweep;"
        " clear it manually)"
    )


def _stash_completion_carryover(
    config: DriverConfig,
    job: Path,
    report: RunReport,
    context: _RunContext,
    log,
) -> CompletionCarryover | None:
    """Park the completed-but-unproposed worktree for the one completion
    retry (ADR-0016 as amended by ADR-0046). Runs strictly AFTER the failed
    Run's evidence push (the diffstat needs the worktree in place), and moves
    it into a dot-prefixed sibling of the job dirs — invisible to
    queue-behind and the evidence sweep — so the job dir itself can be
    cleaned normally. None (logged) when parking fails: the claim then takes
    the ordinary local-retry lane instead."""
    workdir = job / jobdir.CHECKOUT_DIR
    if not workdir.is_dir() or not context.completion_errors:
        return None
    holding = Path(config.jobs_dir) / f".completion-{report.run_id}"
    shutil.rmtree(holding, ignore_errors=True)
    target = holding / jobdir.CHECKOUT_DIR
    try:
        holding.mkdir(parents=True, exist_ok=True)
        workdir.rename(target)
    except OSError as exc:
        shutil.rmtree(holding, ignore_errors=True)
        log(
            f"run {report.run_id}: completion carryover could not be parked ({exc});"
            " falling back to the full local retry (fresh clone)"
        )
        return None
    return CompletionCarryover(
        checkout=target,
        proposal_text=proposal.raw_text(job),
        errors=list(context.completion_errors),
        base_branch=context.base_branch,
        base_sha=context.base_sha,
        force=context.force,
    )


def _walk_closure(client: GitHubClient, issue_number: int) -> deps.DependencyClosure:
    """The one closure walk per Run (ADR-0053), shared by base derivation
    and the input/deps Context Tree. Its typed errors — a cycle, a
    cross-repo edge — fail the Run loudly as infra on EVERY round shape:
    the driver-side backstop against a graph a human malformed after
    dispatch."""
    try:
        return deps.walk_closure(client, issue_number)
    except (deps.DependencyCycleError, deps.CrossRepoEdgeError) as exc:
        raise _RunFailed(f"dependency closure unresolvable at checkout: {exc}", "infra") from exc


def _derive_checkout_base(client: GitHubClient, closure: deps.DependencyClosure) -> str:
    """The fresh-round checkout base (ADR-0053): the dependency chain is
    re-resolved live at checkout and the Run proceeds with current truth —
    every blocker merged means the default branch; a live approved chain
    means its current tip. A closure or chain that does not resolve to a
    base (dispatch-to-checkout drift, or a driver launched outside
    dispatch) fails the Run loudly as infra — the driver never guesses a
    base. Chain preconditions are deliberately NOT re-checked here: the
    grant already asserted them (deps.chain_preconditions is dispatch's
    job)."""
    default = client.default_branch()
    if len(closure.order) == 1:
        return default  # edge-less: the exact pre-ADR-0053 base
    decision = deps.resolve(client, closure, default)
    if decision.kind in (deps.WAIT, deps.MALFORMED):
        raise _RunFailed(
            f"dependency chain unresolvable at checkout ({decision.kind}): {decision.reason}",
            "infra",
        )
    return decision.base_branch


# The only base branches the ship path will fetch or publish against are
# names the pipeline itself derives (the default branch, ozolith/issue-N);
# this guard additionally refuses any repository-derived name git could
# parse as an option (a leading dash) or that carries shell-hostile bytes.
_SAFE_REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/+-]*")


def _resolve_deleted_base(client: GitHubClient, base_branch: str, default: str) -> str:
    """Follow a deleted chained base to its live successor (ADR-0053): the
    branch's PR — resolved across ALL states — must PROVE the merge, and
    the dependent follows the merged PR's actual ``base_ref``, layer by
    layer through any already-merged-and-deleted chain, until a live branch
    or the default branch. Branch absence alone is never merge proof: an
    absent, closed-unmerged, or otherwise unverifiable blocker PR fails the
    Run loudly as infra — the driver never publishes against a guessed
    base, and never guesses the repository default."""
    walked = [base_branch]
    while True:
        gone = walked[-1]
        pr = client.find_pr_by_head(gone)
        if pr is None:
            raise _RunFailed(
                f"chained base {gone} no longer exists and no PR for it can be found"
                " — cannot prove it merged; refusing to publish against a guessed base",
                "infra",
            )
        if not pr.merged:
            raise _RunFailed(
                f"chained base {gone} no longer exists but its PR #{pr.number} is"
                f" {pr.state} and not merged — a deleted branch without merge proof is"
                " unverifiable; refusing to publish against a guessed base",
                "infra",
            )
        successor = pr.base_ref
        if successor == default:
            return default
        if issue_for_branch(successor) is None:
            raise _RunFailed(
                f"merged blocker PR #{pr.number} targeted {successor!r}, outside the"
                " pipeline's branch naming — refusing to follow it to a base",
                "infra",
            )
        if successor in walked:
            raise _RunFailed(
                "the merged-base chain loops"
                f" ({' -> '.join([*walked, successor])}); refusing to publish against"
                " a guessed base",
                "infra",
            )
        walked.append(successor)
        if client.branch_head(successor) is not None:
            return successor


def _verify_ship_base(workdir: Path, base_branch: str, base_sha: str, auth: dict[str, str]) -> None:
    """The retargeted-base containment proof (ADR-0053): fetch the resolved
    base — refusing a repository-derived name git could parse as an option
    — and require the Run's recorded base commit to be an ancestor of its
    fresh tip; anything else would fold blocker-owned changes into the PR
    diff. The fetch also refreshes the checkout's ``origin/<base>`` ref, so
    the evidence diffstat runs against the tip the PR actually targets."""
    if not _SAFE_REF_RE.fullmatch(base_branch):
        raise _RunFailed(f"resolved base branch {base_branch!r} is not a safe ref name", "infra")
    try:
        gitops.fetch(workdir, base_branch, env=auth)
    except gitops.GitError as exc:
        raise _RunFailed(f"cannot fetch the resolved base {base_branch}: {exc}", "infra") from exc
    if not gitops.is_ancestor(workdir, base_sha, f"origin/{base_branch}"):
        raise _RunFailed(
            f"the recorded base commit {base_sha} is not contained in the resolved"
            f" base {base_branch} — publishing against it would fold blocker-owned"
            " changes into this PR's diff; refusing",
            "infra",
        )


def _run_to_pr(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    job: Path,
    report: RunReport,
    context: _RunContext,
    log,
    sink: events.EventSink,
    completion: CompletionCarryover | None = None,
) -> None:
    """The Run body: checkout, agent session, gate, ship. Raises _RunFailed
    for every non-shipping outcome (ADR-0016 classification)."""
    workdir = job / jobdir.CHECKOUT_DIR
    branch = report.branch
    login = client.viewer_login()
    email = f"{login}@users.noreply.github.com"
    auth = gitops.auth_env(config.token)

    # The checkout base is derived BEFORE the clone (ADR-0053), so the PR
    # lookup moves ahead of it. Resume rounds take the base from the PR's
    # own base_ref — GitHub retargets a chained PR to the default branch
    # when its blocker merges, and the checkout (and evidence diffstat)
    # must follow — never from clone HEAD. Fresh rounds re-resolve the
    # dependency chain live. A completion retry keeps its carryover base
    # and skips all of it: the preserved worktree embodies the base.
    existing_pr = client.find_open_pr_by_head(branch)
    if completion is None:
        closure = _walk_closure(client, issue.number)
    else:
        # The one-shot completion retry is terminal (ADR-0016 as amended):
        # it needs no base derivation (the carryover embodies the base) and
        # the closure feeds only the advisory input/deps tree — a graph a
        # human malformed mid-claim must not discard preserved completed
        # work, so the walk degrades to edge-less with a recorded note
        # instead of failing the retry.
        try:
            closure = deps.walk_closure(client, issue.number)
        except (deps.DependencyCycleError, deps.CrossRepoEdgeError) as exc:
            closure = deps.DependencyClosure(
                order=(issue.number,), edges={issue.number: ()}, issues={}
            )
            report.notes.append(
                f"dependency closure unresolvable on the completion retry ({exc});"
                " input/deps omitted"
            )
    if completion is not None:
        context.base_branch = completion.base_branch
        context.base_sha = completion.base_sha
    elif existing_pr is not None:
        context.base_branch = existing_pr.base_ref
    else:
        context.base_branch = _derive_checkout_base(client, closure)
    context.force = completion.force if completion is not None else False

    if completion is None:
        # Reference clone off the node-local mirror (#51): same disposable,
        # self-contained checkout a full clone produced — --dissociate severs
        # every tie to the mirror — but the per-Run download is a ref
        # advertisement instead of the whole history. The mirror update
        # precedes the reference clone, so a freshly pushed blocker tip is
        # present for a chained base. The mirror is an optimization, never
        # load-bearing for Run success (#56): any mirror-path failure —
        # trust validation refusals, git failures, and per-operation timeout
        # expiry alike — falls back ONCE to a full network clone, after one
        # advisory telemetry event. The mirror itself is left in place (the
        # next Run's _ensure_mirror_locked heals or replaces it). Only when
        # the fallback also fails does the Run land in the pre-session infra
        # lane (ADR-0016), exactly as a full-clone failure always has.
        try:
            gitops.clone_with_mirror(
                config.clone_url,
                config.mirrors_dir,
                workdir,
                branch=context.base_branch,
                env=auth,
                timeout=config.git_timeout_seconds,
            )
        except gitops.GitError as exc:
            events.emit_error(
                sink,
                config,
                error_class="mirror-fallback",
                message=(
                    f"run {report.run_id} (issue #{issue.number}): mirror-backed"
                    f" checkout failed, falling back to a full clone: {exc}"
                ),
            )
            # Never trust partial clone debris left by the failed attempt.
            shutil.rmtree(workdir, ignore_errors=True)
            gitops.clone(
                config.clone_url,
                workdir,
                branch=context.base_branch,
                env=auth,
                timeout=config.git_timeout_seconds,
            )
    else:
        # The enumerated carryover (ADR-0016 as amended by ADR-0046): the
        # preserved worktree — uncommitted completed work included — IS this
        # Run's checkout. No branch dance and no reviewer-designated reset
        # may run below: any reset here would wipe exactly the work the
        # retry exists to ship.
        completion.checkout.rename(workdir)
        shutil.rmtree(completion.checkout.parent, ignore_errors=True)
    gitops.sanitize_checkout(workdir, config.clone_url)
    _exclude_metadata(workdir)
    if completion is None:
        # The base tip exactly as this Run checked it out, recorded before
        # any branch dance: the Based-on zone's SHA (ADR-0053).
        context.base_sha = gitops.head_sha(workdir)
    report.phase = "checkout"
    # The grant carried claim authority only (ADR-0017 as amended, #52): the
    # issue and PR context is re-read here, fresh, on every Run — local
    # retries and the completion retry included — authority-filtered to
    # OWNER/MEMBER authors, and materialized as the Context Tree. The
    # granted issue snapshot froze at dispatch time; from here on the fresh
    # one is the issue.
    snapshot = contexttree.fetch_snapshot(client, issue.number, existing_pr)
    issue = snapshot.issue
    revised: verdict.Verdict | None = None
    if existing_pr is not None:
        report.round = attempts_on(existing_pr.labels) + 1
        report.pr_number = existing_pr.number
        gitops.fetch(workdir, branch, env=auth)
        if completion is None:
            # The zone SHA a resume round records is the base commit the
            # PR's history actually CONTAINS — the merge base of the base
            # tip (clone HEAD) and the fetched PR head — never the base
            # branch's current tip: the branch is resumed, not rebased, so
            # a blocker that advanced since the last round must still read
            # as drift against the recorded SHA (the janitor lane, #82),
            # not be silently masked by a refreshed value.
            context.base_sha = gitops.git(["merge-base", "HEAD", "FETCH_HEAD"], workdir)
        # The PR commit snapshot comes from this trusted checkout, not the
        # 250-capped REST endpoint (#52 amendment) — enumerated at the
        # fetched PR head, BEFORE any reviewer-designated reset, so the
        # recorded range is the PR as it stood at checkout no matter where
        # the branch is reset below.
        snapshot = replace(
            snapshot,
            pr_commits=contexttree.git_pr_commits(
                workdir, f"origin/{existing_pr.base_ref}", "FETCH_HEAD"
            ),
        )
        # The machine verdict stays a driver-internal reset detail, selected
        # only from the authority-filtered conversation (#52 amendment): a
        # verdict block in an unauthorized comment can never designate the
        # resume state. The agent reads the same filtered conversation from
        # the tree.
        found = verdict.latest_verdict(snapshot.pr_conversation)
        revised = found[0] if found is not None else None
        if completion is None:
            gitops.checkout_branch(workdir, branch, create=True)
            gitops.reset_hard(workdir, "FETCH_HEAD")
            if revised is not None and revised.resume_commit:
                if gitops.commit_exists(workdir, revised.resume_commit):
                    if revised.resume_commit != gitops.head_sha(workdir):
                        gitops.reset_hard(workdir, revised.resume_commit)
                        context.force = True
                    if revised.cherry_pick:
                        gitops.cherry_pick(workdir, login, email, *revised.cherry_pick)
                else:
                    report.notes.append(
                        f"designated resume commit {revised.resume_commit} not found; "
                        "resuming from branch head"
                    )
    elif completion is None:
        gitops.checkout_branch(workdir, branch, create=True)
        if gitops.ref_exists(workdir, f"origin/{branch}"):
            # A previous Run pushed but crashed before opening the PR.
            # That state was never Reviewer-designated: overwrite it — but
            # re-verify first. The pre-clone PR lookup is now strictly
            # older than the clone's refs, so a resurfaced zombie Run
            # (the residual ADR-0016 hole) can push AND open its PR inside
            # the clone window; a branch a live PR references must never
            # be force-overwritten on a stale reading.
            if client.find_open_pr_by_head(branch) is not None:
                raise _RunFailed(
                    f"a PR for {branch} appeared during checkout; refusing to overwrite"
                    " — the retry resumes it",
                    "infra",
                )
            context.force = True
            report.notes.append("stale branch from a crashed Run overwritten (no PR referenced it)")
    contexttree.write_tree(job / "input", snapshot)
    # The dependency closure as Context Tree (ADR-0053): every closure
    # member serialized like the primary under input/deps/, from the same
    # walk that derived the base — the Decisions Section of an unmerged
    # blocker's PR is the closest thing to that work's documentation.
    has_deps = len(closure.order) > 1
    if has_deps:
        contexttree.write_deps(
            job / "input",
            contexttree.fetch_deps(client, closure, issue.number, workdir=workdir),
            closure,
        )

    # A non-default base names a blocker branch: this is a Chained-Base Run
    # (ADR-0053) — the prompt carries the blocker-interfaces contract and
    # the ship path records the Based-on zone. Uniform across round shapes:
    # fresh rounds resolved the chain above; resume rounds read the PR's
    # base_ref; a completion retry carries its base.
    chained_tip = (
        issue_for_branch(context.base_branch)
        if context.base_branch != client.default_branch()
        else None
    )
    chained_section = ""
    if chained_tip is not None:
        blocker_pr = client.find_open_pr_by_head(context.base_branch)
        chained_section = CHAINED_BASE_CONTEXT.format(
            base_branch=context.base_branch,
            blocker=chained_tip,
            pr_ref=f" (PR #{blocker_pr.number})" if blocker_pr is not None else "",
        )

    prompt = render_run_prompt(
        issue, report.round, revised, chained=chained_section, deps_present=has_deps
    )
    if completion is not None:
        # Main prompt plus the machine-generated error appendix (ADR-0016 as
        # amended): fill-only instruction, soft enforcement.
        prompt += COMPLETION_RETRY_CONTEXT.format(missing="; ".join(completion.errors))
        report.notes.append("completion retry: worktree and pending proposal preserved")
        if completion.proposal_text is not None:
            jobdir.atomic_write(job / proposal.PROPOSAL_FILE, completion.proposal_text)
    jobdir.atomic_write(job / jobdir.PROMPT_FILE, prompt)
    _write_issue_metadata(job, issue, round_number=report.round)
    manifest = jobdir.Manifest(
        run_id=report.run_id,
        mode=jobdir.MODE_RUN,
        adapter=config.adapter,
        workdir=jobdir.CHECKOUT_DIR,
        agent_timeout_seconds=config.agent_timeout_seconds,
        round=report.round,
        schema_version=proposal.SCHEMA_VERSION,
    )
    jobdir.write_manifest(job, manifest)
    spec = ContainerSpec(
        name=run_container_name(report.run_id),
        image=config.run_image,
        labels=container_labels(report.run_id, config.stack),
        mounts=((str(job), jobdir.CONTAINER_JOB_PATH),),
        volumes=config.cache_volumes,
        env=dict(config.agent_env),  # never the GitHub PAT (ADR-0013)
        user=config.container_user,
    )

    # The last driver act before the container exists: freeze the input for
    # evidence (#52). From launch onward input/ is agent-writable via the
    # /job bind mount, so every bundle — live, failed, or boot-swept — is
    # built from this snapshot, never from a post-execution re-read.
    context.trusted_input = evidence.capture_input_snapshot(
        Path(config.jobs_dir), job, report.run_id
    )
    session = session_factory(spec, job, manifest)
    session.launch()
    report.phase = "agent"
    outcome: jobdir.AgentOutcome | None = None
    harness_error = ""
    gate = GateResult()
    reporter = events.ProgressReporter(
        config, sink, job, issue=issue.number, run_id=report.run_id, attempt=report.round
    )
    try:
        with reporter:
            try:
                outcome = session.wait_for_agent()
                report.agent_outcome = outcome.describe()
            except SessionError as exc:
                harness_error = str(exc)
                report.agent_outcome = "harness-failed"
            if outcome is not None and outcome.completed:
                try:
                    gate = run_gate(
                        workdir,
                        runner=lambda command, timeout: session.run_job("gate", command, timeout),
                    )
                except SessionError as exc:
                    # The agent's work is already in the checkout: ship it
                    # best-effort with the gate failure recorded as a finding.
                    gate = GateResult(
                        findings=[
                            Finding(
                                step="gate",
                                severity="error",
                                summary=f"gate infrastructure failed: {exc}",
                            )
                        ]
                    )
                report.phase = "gate"
                report.gate_findings = len(gate.findings)
                context.gate = gate
                sink.emit(
                    events.run_event(
                        config,
                        issue=issue.number,
                        run_id=report.run_id,
                        phase=events.PHASE_GATE,
                        attempt=report.round,
                    )
                )
    finally:
        session.finish()

    # The container touched the checkout: distrust its git metadata.
    gitops.sanitize_checkout(workdir, config.clone_url)
    _exclude_metadata(workdir)

    # -- Run-outcome classification (ADR-0014/0016) ---------------------------

    if outcome is None or not outcome.completed:
        if harness_error:
            # ADR-0045: the harness marks identity-gate failures (preflight,
            # gate, mid-run drift) with a distinct prefix — a policy problem,
            # not harness breakage, and retrying it burns the same budget
            # against the same policy. Anchored, never substring: an error
            # merely quoting the marker is not an identity verdict.
            if identity_error_detail(harness_error) is not None:
                raise _RunFailed(harness_error, "identity")
            # ADR-0046: the schema-version refusal fails strictly pre-work —
            # driver and run image are out of step, a pre-session infra
            # failure (ADR-0016), never harness breakage. Same anchoring.
            if proposal.schema_error_detail(harness_error) is not None:
                raise _RunFailed(harness_error, "infra")
            raise _RunFailed(harness_error, "harness")
        if outcome is not None and outcome.timed_out:
            raise _RunFailed("agent session timed out", "timeout")
        if outcome is not None and outcome.session_died:
            raise _RunFailed("agent session died", "session-died")
        raise _RunFailed(
            f"agent session {(outcome or jobdir.AgentOutcome()).describe()}", "harness"
        )

    # The Output Proposal is the session's entire result (ADR-0046); the
    # driver re-validates it here no matter what the in-session CLI said.
    validated, errors = proposal.validate_run_job(job, round_number=report.round)
    if validated is None:
        # The completion lane (ADR-0016 as amended): evidence still carries
        # whatever Decisions-Section content the agent did record.
        section = proposal.lenient_section(proposal.read_raw(job))
        section.gate_findings = gate.findings
        context.section = section
        context.completion_errors = errors
        raise _RunFailed(f"output proposal invalid: {'; '.join(errors)}", "completion")
    section = validated.section
    section.gate_findings = gate.findings
    context.section = section

    message = commit_message_with_trailer(
        validated.commit_message, report.run_id, issue.number, report.round
    )
    committed = gitops.commit_all(workdir, message, login, email)
    empty = not committed
    if empty and not _has_reasoning(section):
        raise _RunFailed("run completed with no commits and no no-change reasoning", "no-changes")
    if empty:
        # A concluded no-change Run still ships: one allow-empty commit —
        # carrying the proposed message, which records why nothing changed —
        # reasoning in the PR body, judged like any PR (ADR-0014).
        gitops.commit_empty(workdir, message, login, email)
    report.head = gitops.head_sha(workdir)

    gitops.push(workdir, branch, force=context.force, env=auth)

    # -- ship-time base reconciliation (ADR-0053) ------------------------------
    # The blocker can merge (branch auto-deleted: the delete-branch-on-merge
    # posture chaining requires) DURING the agent session — the EXPECTED
    # human act, since chained dispatch demands an approved-and-awaiting-
    # merge blocker. Every ship path — fresh Run, resume round, completion
    # retry — therefore reconciles its base against current GitHub truth
    # immediately before composing the PR. An existing PR is reloaded
    # (GitHub retargets it to the merged PR's base branch when its base is
    # deleted, and may have done so mid-session); a recorded base branch
    # that no longer exists must be PROVEN merged — its PR resolved across
    # all states — and is followed to that PR's actual base branch, layer
    # by layer through any already-merged chain, until a live branch or the
    # default branch. Branch absence alone is never merge proof, and the
    # driver never publishes against a guessed base.
    default = client.default_branch()
    current_pr = client.get_pull(existing_pr.number) if existing_pr is not None else None
    effective_base = current_pr.base_ref if current_pr is not None else context.base_branch
    if current_pr is not None and effective_base != context.base_branch:
        report.notes.append(
            f"PR #{current_pr.number} was retargeted from {context.base_branch} to"
            f" {effective_base} during the session"
        )
    if effective_base != default and issue_for_branch(effective_base) is None:
        raise _RunFailed(
            f"PR base {effective_base!r} is neither the default branch nor an"
            " ozolith/issue-N blocker branch — a human retarget owns this PR;"
            " refusing to ship against a base outside the pipeline's naming",
            "infra",
        )
    retargeted_by_walk = False
    if effective_base != default and client.branch_head(effective_base) is None:
        resolved = _resolve_deleted_base(client, effective_base, default)
        report.notes.append(
            f"chained base {effective_base} was merged and deleted during the"
            f" session; the PR {'retargets to' if current_pr is not None else 'opens against'}"
            f" {resolved}"
        )
        effective_base = resolved
        retargeted_by_walk = current_pr is not None
    if effective_base != context.base_branch:
        # The diff must stay scoped to the dependent layer: prove the
        # recorded base commit is contained in the resolved target before
        # publishing against it, and refresh the checkout's origin/<base>
        # ref so the evidence diffstat runs against the fresh tip.
        _verify_ship_base(workdir, effective_base, context.base_sha, auth)
        context.base_branch = effective_base
    ship_tip = issue_for_branch(effective_base) if effective_base != default else None
    # The Based-on zone (ADR-0053) is driver-owned — composed from the
    # checkout's own base record, never from the Output Proposal — and
    # refreshed on EVERY ship round: removed only when the effective base
    # is truly the default branch, otherwise warning for the actual
    # remaining blocker. The recorded SHA stays the base commit this Run's
    # history actually CONTAINS (containment in the effective base was just
    # verified) — never a refreshed tip, which would mask drift (#82).
    based_on = (
        basedon.BasedOn(issue=ship_tip, sha=context.base_sha) if ship_tip is not None else None
    )
    intro = _no_change_intro(section) if empty else ""
    if current_pr is None:
        # The driver owns the number prefix; the proposal owns the words.
        title = f"#{issue.number}: {validated.pr_title}"
        composed = basedon.upsert_zone(
            compose_pr_body(issue.number, validated.pr_description, section, no_change_intro=intro),
            based_on,
        )
        pr = client.create_pr(head=branch, base=context.base_branch, title=title, body=composed)
        if pr.base_ref != context.base_branch or pr.title != title or pr.body != composed:
            # The 422 fallback reused an existing PR (a lookup-to-create
            # race): converge it on this Run's composed content — base,
            # title, AND body (any one differing is enough), so a raced
            # chained PR never enters review without its Based-on zone and
            # merge-order warning, and never under the twin's title. Safe:
            # resume rounds never reach create_pr, so the PR predates any
            # review round (ADR-0053).
            retargeted = pr.base_ref != context.base_branch
            client.update_pr(
                pr.number,
                title=title,
                body=composed,
                base=context.base_branch if retargeted else None,
            )
            report.notes.append(
                f"create-PR fallback found PR #{pr.number} based on {pr.base_ref};"
                f" retargeted to the derived base {context.base_branch} and converged"
                " title/body"
                if retargeted
                else f"create-PR fallback found PR #{pr.number}; converged title/body"
            )
    else:
        pr = current_pr
        # Absent field = no-op, never clear (ADR-0046): a resume round that
        # proposes no title/narrative keeps the PR's existing ones and only
        # the Decisions Section is replaced. The body baseline is the
        # RELOADED PR's — the session-stale copy may predate a mid-session
        # human edit or retarget.
        body = (
            compose_pr_body(issue.number, validated.pr_description, section, no_change_intro=intro)
            if validated.pr_description
            else decisions.upsert(pr.body, section)
        )
        client.update_pr(
            pr.number,
            title=f"#{issue.number}: {validated.pr_title}" if validated.pr_title else None,
            body=basedon.upsert_zone(body, based_on),
            # A base the walk resolved past a deleted branch is PATCHed;
            # GitHub's own retarget already moved the PR and needs none.
            base=context.base_branch if retargeted_by_walk else None,
        )
    report.pr_number = pr.number
    client.add_labels(pr.number, PR_READY)
    report.phase = "empty-pr" if empty else "pr-open"
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=report.run_id,
            phase=events.PHASE_PR_OPEN,
            attempt=report.round,
            pr=pr.number,
        )
    )

    report.evidence_pushed = _push_run_evidence(config, report, issue, job, context, log, sink)


def _fail_run(
    config: DriverConfig,
    issue: Issue,
    report: RunReport,
    job: Path,
    context: _RunContext,
    failed: _RunFailed,
    log,
    sink: events.EventSink,
) -> None:
    """The FAILED-Run path (ADR-0016): evidence and the failed event, nothing
    else. The claim stays with the driver — retry or escalation is
    ``execute_claim``'s call; no state is stamped into comments."""
    report.phase = "failed"
    report.reason = failed.reason
    report.failure_class = failed.failure_class
    if context.section is None:
        context.section = decisions.fallback_section(failed.reason)
    report.evidence_pushed = _push_run_evidence(config, report, issue, job, context, log, sink)
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=report.run_id,
            phase=events.PHASE_FAILED,
            attempt=report.round,
            failure_class=report.failure_class,
        )
    )
    log(f"run {report.run_id} failed [{report.failure_class}]: {report.reason}")


def _push_run_evidence(
    config: DriverConfig,
    report: RunReport,
    issue: Issue,
    job: Path,
    context: _RunContext,
    log,
    sink: events.EventSink | None = None,
) -> bool:
    """Every Run pushes its bundle (ADR-0014) — normal, empty-PR, and failed
    Runs alike. True only when the push confirmed (ADR-0016: the job dir
    outlives an unconfirmed push)."""
    workdir = job / jobdir.CHECKOUT_DIR
    diffstat = ""
    if context.base_branch and workdir.is_dir():
        try:
            diffstat = gitops.diff_stat(workdir, f"origin/{context.base_branch}")
        except Exception as exc:
            diffstat = f"(diffstat unavailable: {exc})"
    section = context.section or decisions.fallback_section(
        report.reason or "no decisions recorded"
    )
    prefix = evidence.run_dir(issue.number, report.run_id)
    transcript = _read_output(job, jobdir.TRANSCRIPT_FILE)
    stats = _run_stats(config, job)
    files: dict[str, str | bytes] = {
        f"{prefix}/run.json": json.dumps(
            {
                "run_id": report.run_id,
                "worker_id": config.worker_id,
                "stack": config.stack,
                "adapter": config.adapter,
                # ADR-0045: the config-time model string is gone from the
                # driver; identity is the run image (its tag covers the baked
                # model) and "model" is what the session stream reported.
                "run_image": config.run_image,
                # The node's git, the variable #56 makes irrelevant to Run
                # success — recorded so a fleet-wide git behavior change is
                # attributable from the bundle alone ("" = git cannot report).
                "git_version": gitops.git_version(),
                "model": stats.model,
                "model_note": stats.model_note,
                # The harness's identity record (ADR-0045): expected vs
                # observed model/effort, check status, category, violation,
                # gap notes. None on an image that bakes no identity. Values
                # are category strings and model/effort names only — never
                # credentials or settings.
                "identity": jobdir.read_identity(job),
                "container": report.container,
                "issue": issue.number,
                "round": report.round,
                "pr": report.pr_number,
                "branch": report.branch,
                "head": report.head,
                "phase": report.phase,
                "agent_outcome": report.agent_outcome,
                "tokens": stats.tokens,
                "reason": report.reason,
                "failure_class": report.failure_class,
                "notes": report.notes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        f"{prefix}/findings.json": json.dumps(
            [f.__dict__ for f in context.gate.findings], indent=2, sort_keys=True
        )
        + "\n",
        f"{prefix}/decisions.json": section.to_json() + "\n",
        # The headless session's structured output stream: the Run's full
        # audit trail (ADR-0019).
        f"{prefix}/transcript.txt": transcript or "(empty)\n",
        f"{prefix}/diffstat.txt": diffstat + "\n",
    }
    # The Output Proposal as the session left it (ADR-0046) — for a
    # completion-classed failure this is the partial file the retry inherits.
    raw_proposal = proposal.raw_text(job)
    if raw_proposal is not None:
        files[f"{prefix}/proposal.json"] = (
            raw_proposal if raw_proposal.endswith("\n") else raw_proposal + "\n"
        )
    # The exact input the Run saw (#52): prompt, issue metadata, and the
    # authorized Context Tree, byte-for-byte under their relative paths —
    # from the pre-launch trusted snapshot whenever a container launched.
    # The job-dir fallback covers only pre-launch failures (clone, fetch),
    # where no agent has ever had write access to input/.
    input_files = (
        context.trusted_input
        if context.trusted_input is not None
        else evidence.input_artifacts(job)
    )
    files.update({f"{prefix}/{rel}": content for rel, content in input_files.items()})
    try:
        evidence.push_bundle(
            config.clone_url,
            files,
            message=f"Evidence: run {report.run_id} (issue #{issue.number})",
            author_name=config.worker_id,
            author_email=f"{config.worker_id}@theozolith.invalid",
            env=gitops.auth_env(config.token),
        )
    except Exception as exc:
        # Never fail the Run on evidence, but never swallow it silently: a
        # structured record (ADR-0022) so operators and log scrapers see
        # exactly which bundle is missing and why.
        report.notes.append(f"evidence push failed: {exc}")
        log(
            "evidence push failed: "
            + json.dumps(
                {
                    "event": "theozolith.evidence-push-failed",
                    "run_id": report.run_id,
                    "issue": issue.number,
                    "bundle": prefix,
                    "attempts": evidence.PUSH_ATTEMPTS,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        if sink is not None:
            events.emit_error(
                sink,
                config,
                error_class=type(exc).__name__,
                message=f"evidence push failed for run {report.run_id}: {exc}",
                context=f"bundle {prefix}, {evidence.PUSH_ATTEMPTS} attempts",
            )
        return False
    return True


# -- the claim lane: local retry, then escalation (ADR-0016) -------------------


def render_claim_escalation(repo: str, issue_number: int, reports: list[RunReport]) -> str:
    lines = [
        f"All {len(reports)} Runs for this claim failed; the retry budget is spent (ADR-0016).",
        "",
    ]
    for report in reports:
        line = f"- Run `{report.run_id}` — **{report.failure_class}**: {report.reason}"
        if report.evidence_pushed:
            url = evidence.run_evidence_url(repo, issue_number, report.run_id)
            line += f" ([evidence]({url}))"
        elif report.evidence_discarded:
            # Never a dead link and never a false promise (ADR-0019/M5):
            # the compound parking failure discarded this bundle for good.
            line += (
                f" — evidence bundle `{evidence.run_dir(issue_number, report.run_id)}` was"
                " lost (push failed after bounded retries and the job directory could not"
                " be parked for boot-sweep recovery; loss accepted over a false in-flight"
                " signal)"
            )
        else:
            # Never a dead link (ADR-0019): name the bundle path the boot
            # sweep will publish to, best-effort, and say why it is absent.
            line += (
                f" — evidence bundle `{evidence.run_dir(issue_number, report.run_id)}` is not"
                " yet published (push failed after bounded retries; the job directory is"
                " retained for boot-sweep recovery)"
            )
        lines.append(line)
    lines += [
        "",
        f"The claim is released and escalated: `{FAILED}` + `{NEEDS_HUMAN}`."
        f" A human re-queues by removing `{FAILED}` and restoring `{PLAN_READY}`.",
    ]
    return "\n".join(lines)


def execute_claim(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    *,
    log=_log,
    sink: events.EventSink | None = None,
) -> list[RunReport]:
    """Everything one granted claim owes: up to CLAIM_RUN_BUDGET full Runs
    (the original and one local retry) plus at most one completion retry
    (ADR-0016 as amended by ADR-0046), then release + failed + needs_human
    with complete forensics. Returns the reports, one per Run executed."""
    sink = sink or events.make_sink(config, log)

    def _emit_claimed(run_id: str) -> bool:
        claimed = events.run_event(
            config, issue=issue.number, run_id=run_id, phase=events.PHASE_CLAIMED
        )
        return sink.emit(claimed)

    reports: list[RunReport] = []
    full_runs = 0
    completion_used = False
    carryover: CompletionCarryover | None = None
    while True:
        run_id = new_run_id(config)
        if not reports:
            # The activation handshake (ADR-0017): without a landed claimed
            # event the Control Node releases this grant after ~60s; running
            # anyway would fork ownership. Walk away and let it.
            if not any(_emit_claimed(run_id) for _ in range(ACTIVATION_ATTEMPTS)):
                log(
                    f"issue #{issue.number}: claimed event never reached the Control Node;"
                    " abandoning the grant (it will be released, ADR-0017)"
                )
                return reports
        else:
            # The claim is already activated: a retry's claimed event is
            # best-effort visibility, never a gate.
            _emit_claimed(run_id)
        is_completion_retry = carryover is not None
        if not is_completion_retry:
            full_runs += 1
        report = execute_run(
            config,
            client,
            issue,
            session_factory,
            run_id=run_id,
            log=log,
            sink=sink,
            completion=carryover,
        )
        carryover = None
        reports.append(report)
        if report.phase != "failed":
            return reports
        if is_completion_retry:
            # Capped at one and terminal (ADR-0016 as amended): the
            # completion retry ships or the claim escalates — a second miss
            # (or any other retry failure) never re-enters the local lane.
            break
        if (
            report.failure_class == "completion"
            and not completion_used
            and report.carryover is not None
        ):
            completion_used = True
            carryover = report.carryover
            log(
                f"run {report.run_id} completed with an invalid output proposal; keeping the"
                " claim and launching the one completion retry — worktree and pending"
                " proposal preserved (ADR-0016 as amended by ADR-0046)"
            )
            continue
        if full_runs >= CLAIM_RUN_BUDGET:
            break
        log(
            f"run {report.run_id} failed [{report.failure_class}]; keeping the claim and"
            " launching the one local retry (ADR-0016)"
        )
    _escalate_claim(config, client, issue, reports, log, sink)
    return reports


def _escalate_claim(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    reports: list[RunReport],
    log,
    sink: events.EventSink,
) -> None:
    """The local-retry budget is spent: release the claim and hand the issue
    to a human with both failures' forensics (ADR-0016). failed means
    execution broke — autopsy the evidence; only a human removes it.

    Write order matters: the escalation labels and forensics land BEFORE the
    claim is stripped, so a GitHub failure mid-sequence can never leave the
    issue label-less and invisible — the worst partial outcome is an
    escalated issue still wearing its claim, which a human sees either way.
    """
    client.add_labels(issue.number, FAILED, NEEDS_HUMAN)
    client.add_comment(issue.number, render_claim_escalation(config.repo, issue.number, reports))
    me = client.viewer_login()
    fresh = client.get_issue(issue.number)
    if me in fresh.assignees:
        client.remove_assignee(issue.number, me)
    client.remove_label(issue.number, IN_PROGRESS)
    last = reports[-1]
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=last.run_id,
            phase=events.PHASE_ESCALATED,
            attempt=last.round,
            failure_class=last.failure_class,
        )
    )
    log(
        f"issue #{issue.number}: local-retry budget spent; claim released and escalated"
        f" ({FAILED} + {NEEDS_HUMAN})"
    )
