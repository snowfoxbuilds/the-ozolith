"""The retry auditor (acceptance 3): attempt-N mismatches are flagged on the
Control Node and NEVER corrected on GitHub."""

from __future__ import annotations

from controlrig import ControlRig, FakeGitHubLite, review_event, run_event
from theozolith_control import auditor


def sweep(control: ControlRig, github: FakeGitHubLite):
    return auditor.sweep(control.store, github, log=lambda *_: None)


def test_artificial_mismatch_is_flagged_and_not_corrected(control, github):
    # Events say: one revise round happened (attempt-1 expected)…
    control.node_post("/api/v1/events", run_event(5, "pr-open", attempt=1, pr=11))
    control.node_post("/api/v1/events", review_event(11, 5, 1, "revise"))
    # …but someone hand-stamped attempt-2 on the PR.
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready", "attempt-2"})

    findings = sweep(control, github)

    assert [(f["pr"], f["expected"], f["actual"]) for f in findings] == [(11, 1, 2)]
    assert github.writes == [], "the auditor must never write to GitHub"
    stored = control.admin("GET", "/api/v1/audits").json()["audit_findings"]
    assert stored[0]["detail"].startswith("PR #11")


def test_consistent_labels_produce_no_findings(control, github):
    control.node_post("/api/v1/events", run_event(5, "pr-open", attempt=1, pr=11))
    control.node_post("/api/v1/events", review_event(11, 5, 1, "revise"))
    control.node_post("/api/v1/events", run_event(5, "pr-open", attempt=2, pr=11))
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready", "attempt-1"})
    assert sweep(control, github) == []


def test_run_attempts_alone_imply_expected_rounds(control, github):
    # A Run at attempt 3 implies two spent rounds even with no review events.
    control.node_post("/api/v1/events", run_event(5, "pr-open", attempt=3, pr=11))
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})  # no attempt label
    findings = sweep(control, github)
    assert [(f["expected"], f["actual"]) for f in findings] == [(2, 0)]


def test_prs_with_no_event_record_are_skipped(control, github):
    """A fresh Control Node has no grounds to audit history it never saw."""
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready", "attempt-2"})
    assert sweep(control, github) == []


def test_non_pipeline_prs_are_ignored(control, github):
    control.node_post("/api/v1/events", review_event(12, 6, 1, "revise"))
    github.add_pr(12, head_ref="feature/manual-work", labels={"attempt-1"})
    control.store.events()  # sanity: event exists
    findings = sweep(control, github)
    # Issue resolved via the event (issue 6): expected 1, actual 1 -> clean.
    assert findings == []


def test_findings_are_deduplicated_across_sweeps(control, github):
    control.node_post("/api/v1/events", review_event(11, 5, 1, "revise"))
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"attempt-3"})
    assert len(sweep(control, github)) == 1
    assert sweep(control, github) == []  # same mismatch, no new finding
