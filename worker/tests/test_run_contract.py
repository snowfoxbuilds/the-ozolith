"""Run Contract conformance (ADR-0054; BENCH-CONTRACT.md).

The exposed run-contract surface is ``schema_version`` surface under the
no-silent-breaks promise: a bench driver replays the production prompt
renderers, PR-body composition, and gate sequence through
``theozolith_worker.api`` and must get byte-identical output. These tests
pin that surface — byte-stability goldens (sha256 over the rendered bytes:
the literal moves ONLY with a deliberate template change, which is a
``schema_version``-owned, changelog-noted event), proposal validation
through the api names, schema-version mismatch refusal, and the synthetic
round-one Review Run construction the bench contract requires (no live
GitHub PR anywhere).
"""

from __future__ import annotations

import hashlib
import subprocess

from theozolith_worker import api

ISSUE = api.Issue(
    number=7,
    title="Add retry logic",
    body="Implement retries for the fetcher.",
    labels={"plan_ready"},
    assignees=[],
    is_pr=False,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- byte-stable prompt rendering (both modes) ----------------------------------


def test_run_prompt_round_one_bytes_are_golden():
    """GOLDEN: the round-one implementer prompt for a fixed issue. Moves only
    with a deliberate template change — never drift."""
    prompt = api.render_run_prompt(ISSUE, 1, None)
    assert "## Issue #7: Add retry logic" in prompt
    assert "## Context tree" in prompt and "## Rules" in prompt
    assert "## Revised plan" not in prompt and "## Chained base" not in prompt
    assert "input/deps/INDEX.md" not in prompt  # edge-less prompt stays bare
    assert _sha(prompt) == "cf5d31c2d8b7c99c214abd8f49b806572b7039daeb824a7ebd4bc3545f3960cf"


def test_run_prompt_resume_round_bytes_are_golden():
    """GOLDEN: the resume-round shape — revised plan + resume commit, and the
    branch-head fallback when the verdict names no resume commit."""
    revised = api.Verdict(
        verdict=api.REVISE,
        round=1,
        revised_plan="1. Fix the retry cap.",
        resume_commit="abc1234",
    )
    prompt = api.render_run_prompt(ISSUE, 2, revised)
    assert "## Revised plan (round 2)" in prompt
    assert "resumed from commit `abc1234`" in prompt
    assert _sha(prompt) == "614441c68c9298181d33ef286f397b77135eb3f7b44b746960f407a341597803"

    bare = api.Verdict(verdict=api.REVISE, round=1, revised_plan="1. Fix the retry cap.")
    fallback = api.render_run_prompt(ISSUE, 2, bare)
    assert "resumed from commit `(the branch head)`" in fallback
    assert _sha(fallback) == "90bb179ede4afa3e61900e2c1bf8692063573d98d90b36a1d0ed5201e20c41a1"


def test_review_prompt_round_one_bytes_are_golden():
    """GOLDEN: the round-one review prompt under the bench pin (round 1 of
    budget 3 — production first-round conditions, BENCH-CONTRACT.md)."""
    prompt = api.render_review_prompt(ISSUE, head_sha="0123abc", round_number=1, round_budget=3)
    assert "This verdict closes round 1 of 3." in prompt
    assert "LAST budgeted round" not in prompt
    assert "## Chained base" not in prompt and "input/deps/INDEX.md" not in prompt
    assert _sha(prompt) == "6694be43b5fb8e1b89b23da95535b94a7e52107c3bc92646823858949bd7fe3e"


def test_review_prompt_final_round_swaps_in_the_revise_forbidden_rule():
    """GOLDEN: the final budgeted round carries the revise-forbidden rule —
    the very distortion the bench round pin exists to avoid."""
    prompt = api.render_review_prompt(ISSUE, head_sha="0123abc", round_number=3, round_budget=3)
    assert "This is the LAST budgeted round: revise is unavailable" in prompt
    assert _sha(prompt) == "2b11dc51deb9aa93126e906624d29423f58f54b30f6b8d07a6ae3d48457115a9"


def test_prompt_flags_add_their_sections():
    """The deps bullet and the chained sections are flag-driven additions on
    top of the golden bases (their full bytes ride the goldens above)."""
    deps = api.render_run_prompt(ISSUE, 1, None, deps_present=True)
    assert "/job/input/deps/INDEX.md" in deps
    review_deps = api.render_review_prompt(
        ISSUE, head_sha="0123abc", round_number=1, round_budget=3, deps_present=True
    )
    assert "/job/input/deps/INDEX.md" in review_deps
    chained = api.render_review_prompt(
        ISSUE,
        head_sha="0123abc",
        round_number=1,
        round_budget=3,
        based_on=api.BasedOn(issue=5, sha="basesha1"),
    )
    assert "## Chained base" in chained and "#5's UNMERGED branch" in chained


# -- PR-body composition and the commit trailer ---------------------------------


def _section() -> api.DecisionsSection:
    return api.DecisionsSection(
        decisions=[api.Decision(what="Chose exponential backoff", why="bounded load")],
        open_questions=["Cap at 5 or 10 attempts?"],
    )


def test_pr_body_composition_bytes_are_golden():
    """GOLDEN: Closes line + narrative + Decisions Section, wrapped in the
    driver-owned Based-on zone — exactly what the production driver would
    publish for this proposal (ADR-0046/0053)."""
    body = api.upsert_zone(
        api.compose_pr_body(7, "This adds retries to the fetcher.", _section()),
        api.BasedOn(issue=5, sha="basesha1"),
    )
    assert body.count("Closes #7.") == 1
    assert body.index("Based on #5") < body.index("Closes #7.")
    assert "Chose exponential backoff" in body
    assert _sha(body) == "ec226a2dda0c9739f2e5683166731e71bf9a6b6b16e8fc7ed661a631f374c18f"


def test_commit_message_trailer_is_golden():
    assert api.commit_message_with_trailer(
        "Add retry logic\n\nDetails and rationale.", "run-1", 7, 1
    ) == (
        "Add retry logic\n"
        "\n"
        "Details and rationale.\n"
        "\n"
        "Ozolith-Run: run-1\n"
        "Ozolith-Issue: #7\n"
        "Ozolith-Round: 1\n"
    )


def test_base_md_bytes_are_golden():
    pr = api.PullRequest(
        number=41,
        title="#7: Add retry logic",
        body="",
        head_ref="ozolith/issue-7",
        head_sha="0123abc",
        base_ref="main",
        labels=set(),
        state="open",
    )
    assert api.render_base_md(pr, "basecommit1", None) == (
        "# PR base\n\n- base-ref: main\n- base-commit: basecommit1\n"
    )
    assert api.render_base_md(pr, "basecommit1", api.BasedOn(issue=5, sha="basesha1")) == (
        "# PR base\n\n- base-ref: main\n- base-commit: basecommit1\n"
        "- based-on-issue: 5\n- based-on-sha: basesha1\n"
    )


# -- gate ordering through the api ----------------------------------------------


def test_gate_step_order_is_the_published_sequence(tmp_path):
    """STEP_ORDER is contract: canonical steps first in test -> docs -> lint
    order, extra declared steps after, sorted."""
    assert api.STEP_ORDER == ("test", "docs", "lint")
    gate = tmp_path / ".theozolith"
    gate.mkdir()
    (gate / "gate.toml").write_text(
        '[steps.lint]\nrun = "l"\n[steps.zeta]\nrun = "z"\n'
        '[steps.test]\nrun = "t"\n[steps.alpha]\nrun = "a"\n',
        encoding="utf-8",
    )
    assert [s.name for s in api.load_steps(tmp_path)] == ["test", "lint", "alpha", "zeta"]
    result = api.run_gate(tmp_path, lambda command, timeout: (True, ""))
    assert result.steps_run == ["test", "lint", "alpha", "zeta"] and result.clean


# -- proposal validation and schema_version through the api ----------------------


def _document(mode: str, fields: dict) -> dict:
    return {"schema_version": api.SCHEMA_VERSION, "mode": mode, "fields": fields}


def test_proposal_validation_via_api_names():
    assert api.SCHEMA_VERSION == 1
    assert api.required_fields(api.MODE_RUN, 1) == ("pr-title", "pr-description", "commit-message")
    assert api.required_fields(api.MODE_RUN, 2) == ("commit-message",)
    assert api.required_fields(api.MODE_REVIEW, 1) == ("verdict", "evidence")

    proposal, errors = api.validate_run(
        _document(api.MODE_RUN, {"pr-title": "t", "pr-description": "d", "commit-message": "m"}),
        round_number=1,
    )
    assert errors == [] and isinstance(proposal, api.RunProposal)
    missing, errors = api.validate_run(_document(api.MODE_RUN, {}), round_number=1)
    assert missing is None and errors


def test_final_round_revise_is_refused_via_api():
    verdict, reason = api.validate_review(
        _document(
            api.MODE_REVIEW,
            {"verdict": "revise", "evidence": "e", "revised-plan": "1. redo"},
        ),
        round_number=3,
        final_round=True,
        default_resume="HEAD",
        bundle_url="",
    )
    assert verdict is None and "final-round" in reason


def test_schema_version_mismatch_is_refused():
    """A proposal stamped with a foreign schema_version never validates —
    the no-silent-breaks floor of the whole Run Contract."""
    document = _document(
        api.MODE_RUN, {"pr-title": "t", "pr-description": "d", "commit-message": "m"}
    )
    document["schema_version"] = api.SCHEMA_VERSION + 1
    proposal, errors = api.validate_run(document, round_number=1)
    assert proposal is None
    assert any("schema_version" in error for error in errors)


# -- synthetic round-one Review Run construction (no live GitHub PR) -------------


def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def test_synthetic_round_one_review_job_is_constructible_without_github(tmp_path):
    """BENCH-CONTRACT.md constructibility requirement: a Review Run job dir
    from a synthetic issue + a git branch + an implementer-contract output,
    at full production shape — the snapshot carries the synthetic PR and its
    commits enumerated by the production enumerator, ``write_tree`` emits
    the COMPLETE first-round PR surface set (body, empty conversation /
    review-comment / review indexes, commits, checks), base.md /
    changed-files.md / signals.md come from the driver's own git reads, the
    prompt from the production renderer, and the manifest pins round 1 of
    3. No GitHub client anywhere."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "fetcher.py").write_text("def fetch():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-qb", "ozolith/issue-7")
    (repo / "fetcher.py").write_text("def fetch(retries=3):\n    return 1\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "add retries")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    # The implementer-contract output feeds PR-body composition verbatim.
    body = api.upsert_zone(
        api.compose_pr_body(7, "This adds retries to the fetcher.", _section()), None
    )
    pr = api.PullRequest(
        number=41,
        title="#7: Add retry logic",
        body=body,
        head_ref="ozolith/issue-7",
        head_sha=head_sha,
        base_ref="main",
        labels=set(),
        state="open",
    )

    # The production serializer writes every PR surface — including the
    # commits the production enumerator reads from the synthetic branch.
    job = tmp_path / "job"
    snapshot = api.ContextSnapshot(
        issue=ISSUE,
        pr=pr,
        pr_commits=api.git_pr_commits(repo, base_sha, "HEAD"),
    )
    api.write_tree(job / "input", snapshot)
    pr_input = job / "input" / "pr"
    name_status = _git(repo, "diff", "--name-status", base_sha, "HEAD").strip()
    numstat = _git(repo, "diff", "--numstat", base_sha, "HEAD").strip()
    signals = api.signals_from_git(numstat.splitlines(), name_status.splitlines())
    api.atomic_write(pr_input / "base.md", api.render_base_md(pr, base_sha, None))
    api.atomic_write(
        pr_input / "changed-files.md", (name_status + "\n") if name_status else "(none)\n"
    )
    api.atomic_write(pr_input / "signals.md", signals.render() + "\n")
    prompt = api.render_review_prompt(ISSUE, head_sha=head_sha, round_number=1, round_budget=3)
    api.atomic_write(job / api.PROMPT_FILE, prompt)
    manifest = api.Manifest(
        run_id="bench-review-1",
        mode=api.MODE_REVIEW,
        adapter="claude",
        round=1,
        round_budget=3,
        schema_version=api.SCHEMA_VERSION,
    )
    api.write_manifest(job, manifest)

    # The COMPLETE workspace-parity PR surface set (#85): what a production
    # first-round Review Run materializes, nothing missing, nothing extra.
    assert sorted(p.name for p in pr_input.iterdir()) == [
        "base.md",
        "body.md",
        "changed-files.md",
        "checks.md",
        "commits.md",
        "conversation",
        "review-comments",
        "reviews",
        "signals.md",
    ]
    assert "Closes #7." in (pr_input / "body.md").read_text(encoding="utf-8")
    assert "fetcher.py" in (pr_input / "changed-files.md").read_text(encoding="utf-8")
    # First round: every conversation surface exists and is EMPTY — the
    # exact shape a reviewer sees before any human comment lands.
    for surface, label in (
        ("conversation", "PR conversation"),
        ("review-comments", "PR review comments"),
        ("reviews", "PR reviews"),
    ):
        assert [p.name for p in (pr_input / surface).iterdir()] == ["INDEX.md"]
        index = (pr_input / surface / "INDEX.md").read_text(encoding="utf-8")
        assert index == f"# {label} (0)\n\n(none)\n"
    commits = (pr_input / "commits.md").read_text(encoding="utf-8")
    assert commits.startswith("# PR commits (1)")
    assert head_sha in commits and "add retries" in commits
    checks = (pr_input / "checks.md").read_text(encoding="utf-8")
    assert f"# Checks on {head_sha}" in checks
    assert "## Check runs (0)" in checks and "## Commit statuses (0)" in checks
    assert (job / "input" / "issue" / "body.md").is_file()
    assert (job / "input" / "issue" / "comments" / "INDEX.md").is_file()
    loaded = api.read_manifest(job)
    assert loaded == manifest and loaded.final_round is False and loaded.serve_jobs is False
    written = (job / api.PROMPT_FILE).read_text(encoding="utf-8")
    assert written == prompt and f"`{head_sha}`" in written
