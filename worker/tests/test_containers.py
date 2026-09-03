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
    EngineError,
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
    with pytest.raises(ConfigError, match=r"\.claude or \.codex path.*ADR-0043"):
        load_config(
            {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "poison:/home/ozolith/.claude"},
            role="implementer",
        )
    # A .claude segment anywhere in the path is refused, not only the leaf.
    with pytest.raises(ConfigError, match=r"\.claude or \.codex path"):
        load_config(
            {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "poison:/home/ozolith/.claude/skills"},
            role="implementer",
        )


@pytest.mark.parametrize(
    "path",
    [
        # Ancestor mounts: a persistent volume above a protected .claude tree
        # sweeps the whole tree onto it.
        "/home/ozolith",
        "/home",
        "/",
        "/job",  # both session workspaces live under the mounted job dir
        "/job/checkout",
        "/job/work",
        # Normalization tricks: the engine mounts the CLEANED path, so these
        # are the same mounts as their canonical spellings.
        "/home/ozolith/",
        "/home//ozolith",
        "//home/ozolith",
        "/home/./ozolith",
        "/home/ozolith/.claude/..",
        "/home/ozolith/.cache/..",
        "/home/ozolith/.claude/",
        "/home//ozolith//.claude",
        "/home/ozolith/./.claude",
        "/opt/../home/ozolith/.claude",
        # Direct target and descendants (project-scoped included).
        "/job/checkout/.claude",
        "/job/work/.claude/settings",
        "/home/ozolith/.claude/skills/foo",
    ],
)
def test_cache_volumes_reject_persistence_bypasses(path):
    """ADR-0043 hardening: ancestor mounts, ``.``/``..`` and repeated-separator
    spellings, and project-scoped workspace ``.claude`` trees are all refused —
    not only the literal home-scoped path."""
    with pytest.raises(ConfigError, match="ADR-0043"):
        load_config(
            {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": f"poison:{path}"},
            role="implementer",
        )


def test_cache_volumes_are_stored_normalized():
    config = load_config(
        {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "c:/home//ozolith/./.cache/"},
        role="implementer",
    )
    assert config.cache_volumes == (("c", "/home/ozolith/.cache"),)


def test_cache_volumes_accept_the_default_cache_mount():
    """The shipped default (and .claude look-alikes that are not a whole
    segment) stay legal — the guard matches the .claude path component only."""
    config = load_config(
        {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "theozolith-cache:/home/ozolith/.cache"},
        role="implementer",
    )
    assert config.cache_volumes == (("theozolith-cache", "/home/ozolith/.cache"),)
    ok = load_config(
        {**_BASE_ENV, "THEOZOLITH_CACHE_VOLUMES": "c:/home/ozolith/.claudex"},
        role="implementer",
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
        # Both model credentials may ride together (either alone is enough):
        # the API key and the Claude Code OAuth token travel the same path.
        env={"ANTHROPIC_API_KEY": "sk-ant-secret", "CLAUDE_CODE_OAUTH_TOKEN": "oat-secret"},
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
    # Every secret is passed by NAME only; the value rides the CLI's env and
    # never appears in argv (labels, mounts, and the image ref included).
    for name, value in (
        ("ANTHROPIC_API_KEY", "sk-ant-secret"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "oat-secret"),
    ):
        assert name in argv
        assert all(value not in arg for arg in argv)
        assert entry["env"][name] == value


# -- fail-closed aliveness observation (#109, grilling 2026-09-02) ----------------


def scripted_docker(tmp_path: Path, plan: dict) -> Path:
    """A docker stand-in that returns scripted ``(rc, stdout, stderr)`` per
    subcommand. ``plan`` maps the docker subcommand (argv[0], e.g. ``inspect`` /
    ``wait``) to a list of ``[rc, stdout, stderr]``; each call to that subcommand
    pops the next response, holding on the last once the list is exhausted."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    count_path = tmp_path / "counts.json"
    count_path.write_text("{}", encoding="utf-8")
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"plan = json.load(open({str(plan_path)!r}))\n"
        f"counts = json.load(open({str(count_path)!r}))\n"
        "sub = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "responses = plan.get(sub, [[0, '', '']])\n"
        "i = counts.get(sub, 0)\n"
        "rc, out, err = responses[min(i, len(responses) - 1)]\n"
        "counts[sub] = i + 1\n"
        f"json.dump(counts, open({str(count_path)!r}, 'w'))\n"
        "sys.stdout.write(out)\n"
        "sys.stderr.write(err)\n"
        "sys.exit(rc)\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_alive_retries_a_transient_inspect_blip_then_reports_the_truth(tmp_path):
    """A non-definitive inspect failure (a transient dockerd 500) is NEVER read
    as 'not alive': the aliveness path retries bounded and returns the true
    aliveness once the blip clears — the whole point of #109 on the driver."""
    binary = scripted_docker(
        tmp_path,
        {
            "inspect": [
                [1, "", "Error response from daemon: 500 Internal Server Error"],  # blip
                [0, "true\n", ""],  # recovered: the container is running
            ]
        },
    )
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    assert engine.alive("ozolith-run-r1") is True  # the blip did not read as an exit
    assert len(slept) == 1  # retried once, with the injected (no-delay) sleep


def test_alive_reads_no_such_object_as_absent_without_retrying(tmp_path):
    """Docker's definitive no-such-object answer is evidence of absence (an
    exited --rm container): reported not-alive at once, with no retry."""
    binary = scripted_docker(
        tmp_path, {"inspect": [[1, "", "Error: No such object: ozolith-run-r1"]]}
    )
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    assert engine.alive("ozolith-run-r1") is False
    assert slept == []  # a definitive answer never retries


def test_alive_raises_after_exhausting_retries_on_an_unobservable_inspect(tmp_path):
    """When every attempt fails non-definitively, aliveness is unobservable —
    it RAISES EngineError rather than fabricate 'not alive'. The runner maps an
    escaping EngineError to the ADR-0016 infra lane."""
    binary = scripted_docker(
        tmp_path, {"inspect": [[1, "", "Error response from daemon: 500 Internal"]]}
    )
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    with pytest.raises(EngineError, match="unobservable"):
        engine.alive("ozolith-run-r1")
    assert len(slept) == 2  # 3 attempts -> 2 inter-attempt sleeps, no real delay


def test_wait_reads_a_removed_container_as_exited(tmp_path):
    """``docker wait`` failing on an already-removed --rm container resolves
    through the aliveness path: definitively absent -> exit 0."""
    binary = scripted_docker(
        tmp_path,
        {
            "wait": [[1, "", "Error: No such container: ozolith-run-r1"]],
            "inspect": [[1, "", "Error: No such object: ozolith-run-r1"]],
        },
    )
    engine = DockerEngine(binary=str(binary), sleep=lambda _s: None)
    assert engine.wait("ozolith-run-r1", 5.0) == 0


def test_wait_raises_rather_than_fabricate_an_exit_on_an_unobservable_inspect(tmp_path):
    """``docker wait`` failing while the container is UNOBSERVABLE must never be
    read as a clean exit 0 — it raises, so a finished Output Proposal is never
    discarded by a transient blip at wait time."""
    binary = scripted_docker(
        tmp_path,
        {
            "wait": [[1, "", "Error response from daemon: 500"]],
            "inspect": [[1, "", "Error response from daemon: 500"]],
        },
    )
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=lambda _s: None)
    with pytest.raises(EngineError):
        engine.wait("ozolith-run-r1", 5.0)


# -- exact inspect truth values: only `true`/`false` are trusted at a zero exit --
# `{{.State.Running}}` prints exactly `true` or `false` when the container
# exists. ONLY those two are definitive; a zero exit carrying anything else
# (blank, a partial line, an error string on stdout) is a failed read that
# proves nothing — retried like any other blip, NEVER read as absence.


def test_alive_reads_exact_false_as_definitive_absence_without_retrying(tmp_path):
    """A zero-exit inspect printing exactly ``false`` is docker's definitive
    'exists but not running' answer — absence, reported at once with no retry."""
    binary = scripted_docker(tmp_path, {"inspect": [[0, "false\n", ""]]})
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    assert engine.alive("ozolith-run-r1") is False
    assert slept == []  # a definitive answer never retries


@pytest.mark.parametrize("stdout", ["", "\n", "   \n", "Maybe\n", "error: broken pipe\n"])
def test_alive_retries_then_raises_on_a_zero_exit_non_boolean(tmp_path, stdout):
    """A zero exit whose stdout is neither ``true`` nor ``false`` — blank, a
    partial line, an error string the CLI mistakenly wrote to stdout — is a
    FAILED read, not an absence: it retries bounded and RAISES when persistent,
    never fabricating 'not alive' from a value that means nothing."""
    binary = scripted_docker(tmp_path, {"inspect": [[0, stdout, ""]]})
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    with pytest.raises(EngineError, match="unobservable"):
        engine.alive("ozolith-run-r1")
    assert len(slept) == 2  # 3 attempts -> 2 inter-attempt sleeps


def test_alive_recovers_from_a_zero_exit_non_boolean_then_reports_alive(tmp_path):
    """A zero-exit non-boolean value clears on retry: a blank/garbled read
    followed by a real ``true`` reports alive — the recovery the bounded retry
    exists for, on the exit-zero side rather than the non-zero side."""
    binary = scripted_docker(
        tmp_path,
        {"inspect": [[0, "\n", ""], [0, "true\n", ""]]},  # blank, then recovered
    )
    slept: list[float] = []
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=slept.append)
    assert engine.alive("ozolith-run-r1") is True
    assert len(slept) == 1  # retried once


def test_wait_does_not_read_a_zero_exit_non_boolean_inspect_as_exit_0(tmp_path):
    """``docker wait`` failing, then the aliveness fallback inspect returning a
    ZERO exit with a non-boolean value: wait() must not fabricate exit 0 from a
    garbled read — the value is unobservable, so it retries and RAISES (a
    finished Output Proposal is never discarded by a malformed inspect)."""
    binary = scripted_docker(
        tmp_path,
        {
            "wait": [[1, "", "Error response from daemon: 500"]],
            "inspect": [[0, "garbled-not-a-bool\n", ""]],
        },
    )
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=lambda _s: None)
    with pytest.raises(EngineError):
        engine.wait("ozolith-run-r1", 5.0)
