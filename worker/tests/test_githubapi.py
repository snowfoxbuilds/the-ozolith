from __future__ import annotations

import pytest
from fakegithub import FakeGitHub, rate_limited_response
from theozolith_worker.config import ConfigError, load_config
from theozolith_worker.githubapi import GitHubClient, GitHubError, Response


def make_client(fake: FakeGitHub, token: str = "tok-a", login: str = "worker-a") -> GitHubClient:
    fake.register(token, login)
    sleeps: list[float] = []
    client = GitHubClient(
        fake.repo, token, transport=fake, sleep=sleeps.append, clock=lambda: 1000.0
    )
    client.test_sleeps = sleeps  # type: ignore[attr-defined]
    return client


def test_issue_listing_filters_prs_and_paginates():
    fake = FakeGitHub()
    client = make_client(fake)
    for i in range(150):
        fake.create_issue(f"issue {i}", "", {"plan_ready"})
    fake.create_issue("unlabeled", "", set())
    pr_number = fake.create_issue("a pr", "", {"plan_ready"})
    fake.pulls[pr_number] = {"state": "open", "head": "b", "base": "main"}

    issues = client.list_open_issues("plan_ready")

    assert len(issues) == 150
    assert all(not issue.is_pr for issue in issues)


def test_secondary_rate_limit_honors_retry_after_and_recovers():
    """Acceptance 9 (unit): a secondary rate limit pauses the call, which then
    resumes and completes; the write happens exactly once."""
    fake = FakeGitHub()
    client = make_client(fake)
    number = fake.create_issue("t", "", set())
    fake.fail_next(
        lambda m, p: m == "POST" and p.endswith("/labels"),
        [rate_limited_response(retry_after=7), rate_limited_response(retry_after=9)],
    )

    client.add_labels(number, "in_progress")

    assert client.test_sleeps == [7.0, 9.0]  # type: ignore[attr-defined]
    assert fake.labels_of(number) == {"in_progress"}
    # The transcript records the one successful write, not the throttled tries.
    assert client.writes == [("POST", f"/repos/{fake.repo}/issues/{number}/labels")]


def test_primary_rate_limit_sleeps_until_reset():
    fake = FakeGitHub()
    client = make_client(fake)
    number = fake.create_issue("t", "", set())
    exhausted = Response(
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"},
        body=b'{"message": "API rate limit exceeded"}',
    )
    fake.fail_next(lambda m, p: m == "POST" and p.endswith("/labels"), [exhausted])

    client.add_labels(number, "pr_ready")

    # reset(1060) - clock(1000) + 1
    assert client.test_sleeps == [61.0]  # type: ignore[attr-defined]
    assert fake.labels_of(number) == {"pr_ready"}


def test_server_errors_back_off_exponentially():
    fake = FakeGitHub()
    client = make_client(fake)
    number = fake.create_issue("t", "", set())
    boom = Response(status=502, headers={}, body=b"bad gateway")
    fake.fail_next(lambda m, p: m == "POST" and p.endswith("/comments"), [boom, boom, boom])

    client.add_comment(number, "hello")

    assert client.test_sleeps == [2.0, 4.0, 8.0]  # type: ignore[attr-defined]


def test_permanent_client_error_raises_without_retry():
    fake = FakeGitHub()
    client = make_client(fake)
    with pytest.raises(GitHubError) as excinfo:
        client.get_issue(999)
    assert excinfo.value.status == 404
    assert client.test_sleeps == []  # type: ignore[attr-defined]


def test_create_pr_on_existing_head_returns_the_existing_pr():
    """Never a duplicate PR: a 422 for an existing head resolves to that PR."""
    fake = FakeGitHub()
    client = make_client(fake)
    first = client.create_pr(head="ozolith/issue-1", base="main", title="t", body="b")
    again = client.create_pr(head="ozolith/issue-1", base="main", title="t", body="b")
    assert again.number == first.number
    assert fake.open_pr_numbers() == [first.number]


# -- ADR-0053: creation-order draining and the dependency/chain reads ----------


def test_listings_drain_in_creation_order_against_a_newest_first_default():
    """Both dispatch listings pass sort=created&direction=asc; the fake's
    default order is real GitHub's newest-first, so a dropped sort param
    is an observable inversion, not something the fake papers over."""
    fake = FakeGitHub()
    client = make_client(fake)
    numbers = [fake.create_issue(f"issue {i}", "", {"plan_ready"}) for i in range(3)]
    pr_numbers = []
    for i in range(3):
        n = fake.create_issue(f"pr {i}", "", {"pr_ready"})
        fake.pulls[n] = {"state": "open", "head": f"b{i}", "base": "main"}
        pr_numbers.append(n)

    assert [i.number for i in client.list_open_issues("plan_ready")] == numbers
    assert [i.number for i in client.list_open_prs_by_label("pr_ready")] == pr_numbers
    # The fake's own default really is newest-first (the raw route).
    raw = fake(
        "GET",
        f"/repos/{fake.repo}/issues?state=open&labels=plan_ready",
        {"Authorization": "Bearer tok-a"},
        None,
    ).json()
    assert [item["number"] for item in raw] == list(reversed(numbers))


def test_list_blocked_by_paginates_and_round_trips_state_and_repo():
    fake = FakeGitHub()
    client = make_client(fake)
    dependent = fake.create_issue("dependent", "", set())
    blockers = []
    for i in range(150):  # crosses the 100-per-page boundary
        blocker = fake.create_issue(f"blocker {i}", "", set())
        fake.add_blocked_by(dependent, blocker)
        blockers.append(blocker)
    fake.close_issue(blockers[0], "completed")
    fake.close_issue(blockers[1], "not_planned")

    result = client.list_blocked_by(dependent)

    assert [issue.number for issue in result] == blockers
    assert (result[0].state, result[0].state_reason) == ("closed", "completed")
    assert (result[1].state, result[1].state_reason) == ("closed", "not_planned")
    assert result[2].state == "open" and result[2].state_reason == ""
    # Same-repo edges parse to the client's own repo.
    assert all(issue.repo == fake.repo for issue in result)


def test_list_blocked_by_carries_the_foreign_repo_on_cross_repo_edges():
    fake = FakeGitHub()
    client = make_client(fake)
    dependent = fake.create_issue("dependent", "", set())
    fake.add_blocked_by(dependent, 999, repo="acme/elsewhere")
    (blocker,) = client.list_blocked_by(dependent)
    assert blocker.number == 999
    assert blocker.repo == "acme/elsewhere"


def test_repo_merge_settings_parse_and_the_incomplete_payload():
    fake = FakeGitHub()
    client = make_client(fake)
    settings = client.repo_merge_settings()
    assert settings.complete
    assert settings.merge_commit_allowed and settings.delete_branch_on_merge
    assert not settings.squash_allowed and not settings.rebase_allowed

    fake.merge_settings["allow_squash_merge"] = True
    assert client.repo_merge_settings().squash_allowed

    # GitHub omits the allow_* fields for tokens that cannot see them:
    # complete=False, and callers treat that as preconditions-failed.
    del fake.merge_settings["allow_merge_commit"]
    assert not client.repo_merge_settings().complete


def test_branch_head_returns_the_tip_and_none_on_a_deleted_branch(tmp_path):
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch", "main", str(remote)], check=True
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch", "main"], cwd=seed, check=True)
    (seed / "f").write_text("x")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "seed"], cwd=seed, check=True)
    subprocess.run(
        ["git", "push", "-q", str(remote), "HEAD:refs/heads/ozolith/issue-7"], cwd=seed, check=True
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True, text=True, check=True
    ).stdout.strip()

    fake = FakeGitHub(git_dir=remote)
    client = make_client(fake)
    assert client.branch_head("ozolith/issue-7") == sha
    # A deleted branch is the healthy retarget signal, not an error.
    assert client.branch_head("ozolith/issue-8") is None


def test_find_pr_by_head_prefers_open_across_states():
    fake = FakeGitHub()
    client = make_client(fake)
    first = client.create_pr(head="ozolith/issue-1", base="main", title="t1", body="")
    fake.merge_pr(first.number)
    second = client.create_pr(head="ozolith/issue-1", base="main", title="t2", body="")

    assert client.find_pr_by_head("ozolith/issue-1").number == second.number
    fake.close_pr(second.number)
    # No open match: the newest closed one wins.
    assert client.find_pr_by_head("ozolith/issue-1").number == second.number
    assert client.find_pr_by_head("ozolith/never") is None


def test_merged_fields_round_trip_on_both_endpoint_shapes():
    fake = FakeGitHub()
    client = make_client(fake)
    pr = client.create_pr(head="ozolith/issue-2", base="main", title="t", body="")
    assert client.get_pull(pr.number).merged is False
    fake.merge_pr(pr.number)
    # The single-PR endpoint sends the merged boolean...
    merged = client.get_pull(pr.number)
    assert merged.merged and merged.state == "closed"
    assert merged.merge_commit_sha == "merge-of-ozolith/issue-2"
    # ...list endpoints omit it and the client derives it from merged_at.
    listed = client.find_pr_by_head("ozolith/issue-2")
    assert listed.merged and listed.merge_commit_sha == "merge-of-ozolith/issue-2"


def test_list_open_prs_returns_creation_order():
    fake = FakeGitHub()
    client = make_client(fake)
    first = client.create_pr(head="b1", base="main", title="t1", body="")
    second = client.create_pr(head="b2", base="main", title="t2", body="")
    fake.close_pr(first.number)
    third = client.create_pr(head="b3", base="main", title="t3", body="")
    assert [pr.number for pr in client.list_open_prs()] == [second.number, third.number]


def test_update_pr_patches_the_base():
    fake = FakeGitHub()
    client = make_client(fake)
    pr = client.create_pr(head="ozolith/issue-3", base="ozolith/issue-2", title="t", body="")
    client.update_pr(pr.number, base="main")
    assert client.get_pull(pr.number).base_ref == "main"


def test_remove_label_tolerates_absent_label():
    fake = FakeGitHub()
    client = make_client(fake)
    number = fake.create_issue("t", "", set())
    client.remove_label(number, "pr_ready")  # no raise


def test_assign_order_reflects_event_timeline():
    fake = FakeGitHub()
    client = make_client(fake)
    number = fake.create_issue("t", "", set())
    fake.force_assign(number, "worker-b")
    fake.force_assign(number, "worker-a")
    assert client.assign_order(number) == ["worker-b", "worker-a"]


CONTROL_ENV = {"CONTROL_NODE_URL": "https://control.invalid:8443"}


def test_config_reads_var_file_convention(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("s3cret\n")
    config = load_config(
        {"THEOZOLITH_REPO": "acme/sandbox", "GITHUB_TOKEN_FILE": str(token_file), **CONTROL_ENV},
        role="implementer",
    )
    assert config.token == "s3cret"
    # Tokenless by design: the PAT never lands in a checkout's .git/config.
    assert config.clone_url == "https://github.com/acme/sandbox.git"
    assert "s3cret" not in config.clone_url


def test_config_role_prefixed_variables_route_one_shared_env():
    """One .env serves both drivers: IMPLEMENTER_*/REVIEWER_* win over generics."""
    env = {
        "THEOZOLITH_REPO": "acme/sandbox",
        "IMPLEMENTER_GITHUB_TOKEN": "tok-w",
        "REVIEWER_GITHUB_TOKEN": "tok-r",
        "WORKER_ID": "worker-9",
        "POLL_SECONDS": "5",
        **CONTROL_ENV,
    }
    worker = load_config(env, role="implementer")
    reviewer = load_config(env, role="reviewer")
    assert worker.token == "tok-w" and reviewer.token == "tok-r"
    assert worker.stack == "implementer" and reviewer.stack == "reviewer"
    assert worker.worker_id == "worker-9"
    assert worker.poll_seconds == 5.0


def test_config_requires_repo_token_and_control_node():
    with pytest.raises(ConfigError):
        load_config({}, role="implementer")
    with pytest.raises(ConfigError):
        load_config({"THEOZOLITH_REPO": "acme/sandbox"}, role="implementer")
    # ADR-0017: no Control Node = no claim path = not a runnable driver.
    with pytest.raises(ConfigError, match="CONTROL_NODE_URL"):
        load_config(
            {"THEOZOLITH_REPO": "acme/sandbox", "GITHUB_TOKEN": "x"},
            role="implementer",
        )


def test_config_forwards_either_claude_credential():
    """The Claude adapter authenticates with an API key OR an OAuth token (or
    both) — whichever is supplied is forwarded into the run container; neither
    is required at load time."""
    base = {"THEOZOLITH_REPO": "acme/sandbox", "GITHUB_TOKEN": "x", **CONTROL_ENV}

    api_only = load_config({**base, "ANTHROPIC_API_KEY": "sk-ant"}, role="implementer")
    assert api_only.agent_env == {"ANTHROPIC_API_KEY": "sk-ant"}

    oauth_only = load_config({**base, "CLAUDE_CODE_OAUTH_TOKEN": "oat-tok"}, role="implementer")
    assert oauth_only.agent_env == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-tok"}

    both = load_config(
        {**base, "ANTHROPIC_API_KEY": "sk-ant", "CLAUDE_CODE_OAUTH_TOKEN": "oat-tok"},
        role="implementer",
    )
    assert both.agent_env == {
        "ANTHROPIC_API_KEY": "sk-ant",
        "CLAUDE_CODE_OAUTH_TOKEN": "oat-tok",
    }

    neither = load_config(base, role="implementer")
    assert neither.agent_env == {}


def test_config_oauth_token_honors_var_file_convention(tmp_path):
    """The OAuth token arrives like every other secret — via <NAME>_FILE from
    tmpfs — never as a literal in the environment."""
    token_file = tmp_path / "oauth"
    token_file.write_text("oat-from-file\n")
    config = load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "x",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE": str(token_file),
            **CONTROL_ENV,
        },
        role="implementer",
    )
    assert config.agent_env == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-from-file"}


def test_config_forwards_no_credentials_for_a_non_claude_adapter():
    """Only the resolved adapter's credentials are forwarded, so a non-Claude
    worker never receives a Claude token even if one sits in the environment."""
    config = load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "x",
            "THEOZOLITH_ADAPTER": "future",
            "ANTHROPIC_API_KEY": "sk-ant",
            "CLAUDE_CODE_OAUTH_TOKEN": "oat-tok",
            **CONTROL_ENV,
        },
        role="implementer",
    )
    assert config.adapter == "future"
    assert config.agent_env == {}


def test_agent_env_never_carries_a_model_selection():
    """The last loophole (ADR-0045): the run container's env is exactly the
    supplied spend credentials — no ANTHROPIC_MODEL, no THEOZOLITH_MODEL —
    so nothing in the container's environment can steer the CLI off the
    image's baked model."""
    config = load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "x",
            "ANTHROPIC_API_KEY": "spend-key",
            **CONTROL_ENV,
        },
        role="implementer",
    )
    assert config.agent_env == {"ANTHROPIC_API_KEY": "spend-key"}


@pytest.mark.parametrize("name", ["IMPLEMENTER_MODEL", "THEOZOLITH_MODEL"])
def test_config_rejects_the_removed_model_env_loudly(name):
    """ADR-0045: the model env chain is gone — a leftover export must fail
    loudly, not silently run a model the operator no longer controls."""
    with pytest.raises(ConfigError, match=rf"{name} is removed \(ADR-0045\)"):
        load_config(
            {
                "THEOZOLITH_REPO": "acme/sandbox",
                "GITHUB_TOKEN": "x",
                name: "claude-opus-5",
                **CONTROL_ENV,
            },
            role="implementer",
        )


def test_fake_rejects_unknown_token():
    fake = FakeGitHub()
    client = GitHubClient(fake.repo, "unknown", transport=fake, sleep=lambda s: None)
    with pytest.raises(GitHubError) as excinfo:
        client.viewer_login()
    assert excinfo.value.status == 401


def test_config_forwards_the_codex_plan_credential():
    """The codex adapter's single credential is the plan-auth document; only
    the resolved adapter's names are forwarded — a codex worker never
    receives a Claude token and vice versa (ADR-0013/0052)."""
    config = load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "x",
            "THEOZOLITH_ADAPTER": "codex",
            "CODEX_AUTH_JSON": '{"tokens": {}}',
            "ANTHROPIC_API_KEY": "sk-ant",
            **CONTROL_ENV,
        },
        role="reviewer",
    )
    assert config.adapter == "codex"
    assert config.agent_env == {"CODEX_AUTH_JSON": '{"tokens": {}}'}
