"""Dependency Edges (ADR-0053): the closure walk, the Chained Base
go-ahead truth table, the merge-setting preconditions, and the Based-on
PR-body zone. The walk/resolve tests run the real GitHubClient against the
in-memory GitHub, so the dependency endpoint's pagination and payload
parsing are exercised for real."""

from __future__ import annotations

import pytest
from fakegithub import FakeGitHub
from theozolith_worker import basedon, deps
from theozolith_worker.githubapi import GitHubClient, RepoMergeSettings


def make_env() -> tuple[FakeGitHub, GitHubClient]:
    fake = FakeGitHub()
    fake.register("tok", "worker-a")
    return fake, GitHubClient(fake.repo, "tok", transport=fake, sleep=lambda s: None)


def add_pr(fake: FakeGitHub, issue_number: int, labels: set[str], base: str = "main") -> int:
    """An open PR on the issue's deterministic branch, the way the pipeline
    creates one."""
    number = fake.create_issue(f"pr for #{issue_number}", "", labels)
    fake.pulls[number] = {
        "state": "open",
        "head": deps.branch_for(issue_number),
        "base": base,
    }
    return number


APPROVED = {"pr_ready", "needs_human"}


# -- the closure walk ----------------------------------------------------------


def test_walk_closure_is_topological_deterministic_and_walks_diamonds_once():
    fake, client = make_env()
    a = fake.create_issue("a", "", set())
    b = fake.create_issue("b", "", set())
    c = fake.create_issue("c", "", set())
    d = fake.create_issue("d", "", set())
    fake.add_blocked_by(d, c)  # declared out of number order: ties sort
    fake.add_blocked_by(d, b)
    fake.add_blocked_by(b, a)
    fake.add_blocked_by(c, a)

    calls: list[int] = []
    original = client.list_blocked_by
    client.list_blocked_by = lambda n: (calls.append(n), original(n))[1]  # type: ignore[method-assign]

    closure = deps.walk_closure(client, d)

    # Blockers before dependents, ties by ascending issue number.
    assert closure.order == (a, b, c, d)
    assert closure.edges == {d: (b, c), b: (a,), c: (a,), a: ()}
    assert set(closure.issues) == {a, b, c}
    # The diamond's shared blocker is read once, not once per path.
    assert sorted(calls) == [a, b, c, d]


def test_walk_closure_of_an_edge_less_issue_is_the_singleton_order():
    fake, client = make_env()
    number = fake.create_issue("standalone", "", set())
    assert deps.walk_closure(client, number).order == (number,)


def test_walk_closure_raises_on_a_cycle_with_the_path_named():
    fake, client = make_env()
    a = fake.create_issue("a", "", set())
    b = fake.create_issue("b", "", set())
    fake.add_blocked_by(a, b)
    fake.add_blocked_by(b, a)
    with pytest.raises(deps.DependencyCycleError) as excinfo:
        deps.walk_closure(client, a)
    assert excinfo.value.cycle == (a, b, a)
    assert f"#{a} -> #{b} -> #{a}" in str(excinfo.value)


def test_walk_closure_raises_on_a_cross_repo_edge_naming_the_repo():
    fake, client = make_env()
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, 999, repo="acme/elsewhere")
    with pytest.raises(deps.CrossRepoEdgeError) as excinfo:
        deps.walk_closure(client, dependent)
    assert excinfo.value.foreign_repo == "acme/elsewhere"
    assert "acme/elsewhere" in str(excinfo.value)
    assert "stand-in" in str(excinfo.value)


# -- the go-ahead truth table (ADR-0053) ---------------------------------------


def resolve(client, number) -> deps.ChainDecision:
    return deps.resolve(client, deps.walk_closure(client, number), "main")


def test_all_blockers_completed_resolves_ready_on_the_default_branch():
    fake, client = make_env()
    blocker = fake.create_issue("blocker", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, blocker)
    fake.close_issue(blocker, "completed")
    decision = resolve(client, dependent)
    assert decision.kind == deps.READY
    assert decision.base_branch == "main" and not decision.base_is_chained


def test_a_not_planned_blocker_is_malformed_naming_blocker_and_reason():
    fake, client = make_env()
    blocker = fake.create_issue("blocker", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, blocker)
    fake.close_issue(blocker, "not_planned")
    decision = resolve(client, dependent)
    assert decision.kind == deps.MALFORMED
    assert f"#{blocker}" in decision.reason and "not_planned" in decision.reason


def test_an_open_blocker_without_a_pr_means_wait():
    fake, client = make_env()
    blocker = fake.create_issue("blocker", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, blocker)
    decision = resolve(client, dependent)
    assert decision.kind == deps.WAIT
    assert f"#{blocker}" in decision.reason


@pytest.mark.parametrize(
    "labels",
    [
        {"pr_ready"},  # unreviewed: the aggressive knob is rejected
        {"needs_human"},
        {"pr_ready", "needs_human", "blocked"},  # human hold trumps
        set(),
    ],
)
def test_an_unapproved_or_blocked_blocker_pr_means_wait(labels):
    fake, client = make_env()
    blocker = fake.create_issue("blocker", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, blocker)
    add_pr(fake, blocker, labels)
    assert resolve(client, dependent).kind == deps.WAIT


def test_an_approved_chain_resolves_chained_on_the_tip():
    fake, client = make_env()
    b1 = fake.create_issue("b1", "", set())
    b2 = fake.create_issue("b2", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, b2)
    fake.add_blocked_by(b2, b1)
    add_pr(fake, b1, APPROVED, base="main")
    add_pr(fake, b2, APPROVED, base=deps.branch_for(b1))

    decision = resolve(client, dependent)

    assert decision.kind == deps.CHAINED
    assert decision.base_is_chained
    assert decision.tip_issue == b2
    assert decision.base_branch == deps.branch_for(b2)


def test_a_completed_blocker_plus_an_approved_open_one_chains_on_the_open_tip():
    fake, client = make_env()
    done = fake.create_issue("done", "", set())
    live = fake.create_issue("live", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, done)
    fake.add_blocked_by(dependent, live)
    fake.close_issue(done, "completed")
    add_pr(fake, live, APPROVED)
    decision = resolve(client, dependent)
    assert decision.kind == deps.CHAINED and decision.tip_issue == live


def test_parallel_open_blocker_lines_mean_wait():
    """Fan-in: two approved blockers each based on main — two tips."""
    fake, client = make_env()
    b1 = fake.create_issue("b1", "", set())
    b2 = fake.create_issue("b2", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, b1)
    fake.add_blocked_by(dependent, b2)
    add_pr(fake, b1, APPROVED, base="main")
    add_pr(fake, b2, APPROVED, base="main")
    decision = resolve(client, dependent)
    assert decision.kind == deps.WAIT
    assert "parallel open blocker lines" in decision.reason


def test_a_blocker_pr_on_a_foreign_base_means_wait():
    fake, client = make_env()
    blocker = fake.create_issue("blocker", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, blocker)
    add_pr(fake, blocker, APPROVED, base="feature-x")
    decision = resolve(client, dependent)
    assert decision.kind == deps.WAIT
    assert "feature-x" in decision.reason


def test_a_base_cycle_beside_a_clean_line_means_wait_not_a_partial_chain():
    """One tip exists, but two blockers point base_refs at each other: the
    tip's chain does not cover them, so chaining would drop their work."""
    fake, client = make_env()
    b1 = fake.create_issue("b1", "", set())
    b2 = fake.create_issue("b2", "", set())
    b3 = fake.create_issue("b3", "", set())
    dependent = fake.create_issue("dependent", "", set())
    for blocker in (b1, b2, b3):
        fake.add_blocked_by(dependent, blocker)
    add_pr(fake, b1, APPROVED, base=deps.branch_for(b2))
    add_pr(fake, b2, APPROVED, base=deps.branch_for(b1))
    add_pr(fake, b3, APPROVED, base="main")
    assert resolve(client, dependent).kind == deps.WAIT


# -- the merge-setting preconditions -------------------------------------------


def settings(**overrides) -> RepoMergeSettings:
    values = dict(
        merge_commit_allowed=True,
        squash_allowed=False,
        rebase_allowed=False,
        delete_branch_on_merge=True,
        complete=True,
    )
    values.update(overrides)
    return RepoMergeSettings(**values)


def test_chain_preconditions_pass_on_the_chain_enabled_workspace():
    assert deps.chain_preconditions(settings()) is None


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"merge_commit_allowed": False}, "merge commits disabled"),
        ({"squash_allowed": True}, "squash merge enabled"),
        ({"rebase_allowed": True}, "rebase merge enabled"),
        ({"delete_branch_on_merge": False}, "delete-branch-on-merge disabled"),
        ({"complete": False}, "unreadable with this token"),
    ],
)
def test_chain_preconditions_name_each_failing_setting(overrides, named):
    reason = deps.chain_preconditions(settings(**overrides))
    assert reason is not None and reason.startswith("chaining off")
    assert named in reason


def test_chain_preconditions_name_every_failure_at_once():
    reason = deps.chain_preconditions(
        settings(squash_allowed=True, rebase_allowed=True, delete_branch_on_merge=False)
    )
    assert "squash merge enabled" in reason
    assert "rebase merge enabled" in reason
    assert "delete-branch-on-merge disabled" in reason


# -- the Based-on zone (basedon) ------------------------------------------------


def test_zone_render_parse_round_trip():
    sha = "ab" * 20
    body = basedon.render_zone(7, sha)
    assert basedon.parse_zone(body) == basedon.BasedOn(issue=7, sha=sha)
    # The human-gate warning is present and names the merge order.
    assert f"**Based on #7 at `{sha}`**" in body
    assert "merge #7 first" in body


def test_upsert_zone_prepends_replaces_and_removes():
    body = "Closes #9.\n\nThe narrative."
    with_zone = basedon.upsert_zone(body, basedon.BasedOn(7, "a" * 40))
    assert with_zone.startswith("**Based on #7")
    assert "Closes #9." in with_zone

    # Replace: a refreshed SHA supersedes the old zone — exactly one block.
    refreshed = basedon.upsert_zone(with_zone, basedon.BasedOn(7, "b" * 40))
    assert refreshed.count("theozolith:based-on") == 1
    assert basedon.parse_zone(refreshed) == basedon.BasedOn(7, "b" * 40)
    assert "a" * 40 not in refreshed

    # Remove: the healthy retarget-to-main shape — warning gone too.
    removed = basedon.upsert_zone(refreshed, None)
    assert basedon.parse_zone(removed) is None
    assert "Based on" not in removed
    assert removed.startswith("Closes #9.")


def test_upsert_zone_on_an_empty_body_is_just_the_zone():
    body = basedon.upsert_zone("", basedon.BasedOn(3, "c" * 40))
    assert basedon.parse_zone(body) == basedon.BasedOn(3, "c" * 40)
    assert basedon.upsert_zone("", None) == ""


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no zone at all",
        "<!-- theozolith:based-on\nnot json\n-->",
        '<!-- theozolith:based-on\n["a", "list"]\n-->',
        '<!-- theozolith:based-on\n{"issue": "7", "sha": "x"}\n-->',  # wrong types
        '<!-- theozolith:based-on\n{"issue": true, "sha": "x"}\n-->',
        '<!-- theozolith:based-on\n{"sha": "x"}\n-->',
    ],
)
def test_parse_zone_is_tolerant_and_never_raises(body):
    assert basedon.parse_zone(body) is None


def test_zone_survives_an_agent_mangled_warning_line():
    """The machine block alone is authoritative: a body whose warning text
    was edited away still parses, and upsert still deduplicates."""
    sha = "d" * 40
    mangled = (
        "some prose\n\n<!-- theozolith:based-on\n" + f'{{"issue": 5, "sha": "{sha}"}}' + "\n-->"
    )
    assert basedon.parse_zone(mangled) == basedon.BasedOn(5, sha)
    replaced = basedon.upsert_zone(mangled, basedon.BasedOn(5, "e" * 40))
    assert replaced.count("theozolith:based-on") == 1
    assert basedon.parse_zone(replaced) == basedon.BasedOn(5, "e" * 40)


def test_walk_closure_prunes_beneath_closed_blockers():
    """A closed blocker's own edges constrained work that is over: its
    subtree never judges a live dependent, so historical edges beneath
    long-completed blockers cannot poison new work."""
    fake, client = make_env()
    ancient = fake.create_issue("ancient", "", set())
    done = fake.create_issue("done", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, done)
    fake.add_blocked_by(done, ancient)
    fake.close_issue(done, "completed")
    fake.close_issue(ancient, "not_planned")  # would malform if walked

    closure = deps.walk_closure(client, dependent)

    assert closure.order == (done, dependent)
    assert ancient not in closure.issues
    assert closure.edges[done] == ()  # pruned, never fetched
    assert deps.resolve(client, closure, "main").kind == deps.READY


def test_a_stale_cycle_beneath_a_completed_blocker_never_raises():
    fake, client = make_env()
    a = fake.create_issue("a", "", set())
    b = fake.create_issue("b", "", set())
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, a)
    fake.add_blocked_by(a, b)
    fake.add_blocked_by(b, a)
    fake.close_issue(a, "completed")
    closure = deps.walk_closure(client, dependent)
    assert deps.resolve(client, closure, "main").kind == deps.READY


def test_zone_round_trips_a_crlf_body():
    """GitHub's web editor can round-trip a body to CRLF; the zone must
    keep parsing and keep REPLACING — a non-matching regex would stack a
    duplicate zone per ship round."""
    sha = "f" * 40
    body = basedon.upsert_zone("Closes #9.", basedon.BasedOn(7, sha))
    crlf = body.replace("\n", "\r\n")
    assert basedon.parse_zone(crlf) == basedon.BasedOn(7, sha)
    replaced = basedon.upsert_zone(crlf, basedon.BasedOn(7, "0" * 40))
    assert replaced.count("theozolith:based-on") == 1
    assert basedon.parse_zone(replaced) == basedon.BasedOn(7, "0" * 40)
    removed = basedon.upsert_zone(crlf, None)
    assert "Based on" not in removed and "theozolith:based-on" not in removed
    assert "Closes #9." in removed


def test_removal_strips_a_warning_separated_from_its_block():
    """A human edit between warning and block must not let a stale
    'merge #N first' instruction survive the retarget-to-main removal."""
    body = basedon.upsert_zone("Closes #9.", basedon.BasedOn(7, "a" * 40))
    edited = body.replace(
        "\n\n<!-- theozolith:based-on", "\n\nA human note.\n\n<!-- theozolith:based-on"
    )
    removed = basedon.upsert_zone(edited, None)
    assert "merge #7 first" not in removed
    assert "theozolith:based-on" not in removed
    assert "A human note." in removed and "Closes #9." in removed
