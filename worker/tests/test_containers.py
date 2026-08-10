"""Container conventions and docker CLI invocation (via a recording binary)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from theozolith_worker.config import ConfigError, load_config
from theozolith_worker.containers import (
    ContainerSpec,
    DockerEngine,
    container_labels,
    review_container_name,
    run_container_name,
)

_BASE_ENV = {
    "THEOZOLITH_REPO": "acme/sandbox",
    "GITHUB_TOKEN": "tok",
    "CONTROL_NODE_URL": "https://control.invalid:8443",
}


def test_cache_volumes_reject_a_claude_target():
    """ADR-0043: no config-reachable channel may mount live knowledge into a
    Run — a cache volume aimed at a .claude path is refused, closing the
    prompt-injection persistence channel. The Flight-Deck symlink carve-out is
    the only exception and never touches run containers."""
    with pytest.raises(ConfigError, match=r"\.claude path.*ADR-0043"):
        load_config(
            {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "poison:/home/ozolith/.claude"},
            role="implementer",
            default_model="claude-sonnet-5",
        )
    # A .claude segment anywhere in the path is refused, not only the leaf.
    with pytest.raises(ConfigError, match=r"\.claude path"):
        load_config(
            {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "poison:/home/ozolith/.claude/skills"},
            role="implementer",
            default_model="claude-sonnet-5",
        )


def test_cache_volumes_accept_the_default_cache_mount():
    """The shipped default (and .claude look-alikes that are not a whole
    segment) stay legal — the guard matches the .claude path component only."""
    config = load_config(
        {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "theozolith-cache:/home/ozolith/.cache"},
        role="implementer",
        default_model="claude-sonnet-5",
    )
    assert config.cache_volumes == (("theozolith-cache", "/home/ozolith/.cache"),)
    ok = load_config(
        {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "c:/home/ozolith/.claudex"},
        role="implementer",
        default_model="claude-sonnet-5",
    )
    assert ok.cache_volumes == (("c", "/home/ozolith/.claudex"),)


def test_naming_and_label_conventions():
    assert run_container_name("20260716T1200-w1-1") == "ozolith-run-20260716T1200-w1-1"
    assert review_container_name(41, 2) == "ozolith-review-41-round-2"
    assert container_labels("r1", "worker") == {
        "theozolith.run-id": "r1",
        "theozolith.owner": "worker",
    }


def fake_docker(tmp_path: Path) -> Path:
    """A docker stand-in that records argv and env, then succeeds."""
    record = tmp_path / "record.jsonl"
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
        "print('ok')\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_docker_launch_flags_and_secret_env_handling(tmp_path):
    binary = fake_docker(tmp_path)
    engine = DockerEngine(binary=str(binary))
    job = tmp_path / "job"
    job.mkdir()
    spec = ContainerSpec(
        name="ozolith-run-r1",
        image="theozolith-run-claude:local",
        labels=container_labels("r1", "worker"),
        mounts=((str(job), "/job"),),
        volumes=(("theozolith-cache", "/home/ozolith/.cache"),),
        env={"ANTHROPIC_API_KEY": "sk-ant-secret"},
        user="1000:1000",
    )

    engine.launch(spec)

    entry = json.loads((tmp_path / "record.jsonl").read_text().splitlines()[0])
    argv = entry["argv"]
    assert argv[:4] == ["run", "--detach", "--rm", "--init"]
    assert argv[argv.index("--name") + 1] == "ozolith-run-r1"
    assert "theozolith.run-id=r1" in argv and "theozolith.owner=worker" in argv
    assert f"{job.resolve()}:/job" in argv
    assert "theozolith-cache:/home/ozolith/.cache" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[-1] == "theozolith-run-claude:local"
    # The secret is passed by NAME only; the value rides the CLI's env.
    assert "ANTHROPIC_API_KEY" in argv
    assert all("sk-ant-secret" not in arg for arg in argv)
    assert entry["env"]["ANTHROPIC_API_KEY"] == "sk-ant-secret"
