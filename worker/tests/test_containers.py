"""Container conventions and docker CLI invocation (via a recording binary)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from theozolith_worker.containers import (
    ContainerSpec,
    DockerEngine,
    container_labels,
    review_container_name,
    review_session_name,
    run_container_name,
    run_session_name,
)


def test_naming_and_label_conventions():
    assert run_container_name("20260716T1200-w1-1") == "ozolith-run-20260716T1200-w1-1"
    assert review_container_name(41, 2) == "ozolith-review-41-round-2"
    assert run_session_name("r1") == "run-r1"
    assert review_session_name(41, 2) == "review-41-round-2"
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
