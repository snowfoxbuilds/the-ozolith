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
