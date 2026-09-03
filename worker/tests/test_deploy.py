"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = REPO_ROOT / "worker" / "docker" / "Dockerfile.claude"
CONTROL_DOCKERFILE = REPO_ROOT / "control" / "docker" / "Dockerfile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


CLI_PIN_VERSION = "2.1.257"  # what the example's `cli` declares (flightdeck.toml)


def fake_resolve_cli(declared: str) -> dict:
    """The injected CLI Pin resolver seam (ADR-0055) — the npm twin of the
    faked registry digest resolver: the declared value resolves to itself
    (the example pins an exact version) with the full supported-platform
    map, keeping the suite hermetic."""
    from theozolith_worker.adapters import ClaudeAdapter

    return {
        "version": declared,
        "platforms": {
            key: {"package": package, "integrity": "sha512-" + "A" * 96}
            for key, package in ClaudeAdapter.CLI_PLATFORM_PACKAGES.items()
        },
    }


@pytest.fixture(scope="module")
def example_config(tmp_path_factory):
    """configs-example is a HUMAN Config Repo (ADR-0048): it becomes loadable
    by going through the real `theozolith config ingest` pipeline — which is
    itself the assertion that the shipped example ingests cleanly (knowledge
    compiles, lints pass, pins resolve). The example bases are tag-only and
    the deck pins a CLI, so the two registry round-trips (image digest, npm
    version/integrity) are the faked seams (the suite stays hermetic);
    everything else runs for real."""
    from theozolith_control.configrepo import load_config
    from theozolith_control.ingest import ingest

    pinned = tmp_path_factory.mktemp("pinned-build")
    ingest(
        str(DEPLOY / "configs-example"),
        pinned,
        resolve_digest=lambda ref: "sha256:" + "f" * 64,
        resolve_cli=fake_resolve_cli,
        log=lambda *_: None,
    )
    return load_config(pinned)


def test_compose_no_longer_runs_the_actors():
    """ADR-0013: run containers are created by the drivers, not compose."""
    assert not (DEPLOY / "docker-compose.yml").exists()


def test_dot_env_is_no_longer_a_user_facing_surface():
    """ADR-0023 deletion test: `.env`-driven setup is gone — no example
    file ships, the installer writes none, and the compose stub needs none.
    (Env vars survive only as validated expert overrides.)"""
    assert not (DEPLOY / ".env.example").exists()
    assert "/etc/theozolith/.env" not in (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "env_file" not in (DEPLOY / "compose" / "control.yml").read_text()
    # The dev-shape documentation kept its non-negotiables.
    readme = (DEPLOY / "README.md").read_text()
    assert "different GitHub identities" in readme  # no self-grading (ADR-0008)
    assert "VAR_FILE" in readme  # the secrets convention is documented


def test_systemd_units_exist_for_both_drivers():
    # One generic launcher per driver (ADR-0020): the unit names the built-in
    # worker type by ref, never a per-type console script.
    for role in ("implementer", "reviewer"):
        unit = (DEPLOY / "systemd" / f"theozolith-{role}.service").read_text()
        assert f"theozolith-driver builtin:{role}" in unit
        assert "EnvironmentFile=" in unit
        assert "Restart=on-failure" in unit
        assert "M3" in unit  # explicitly a convenience until daemon supervision


def test_run_image_contract():
    dockerfile = DOCKERFILE.read_text()
    # PID 1 is the harness; the actors never run in this image.
    assert 'ENTRYPOINT ["theozolith-harness"]' in dockerfile
    assert "theozolith-driver" not in re.findall(r"ENTRYPOINT.*|CMD.*", dockerfile)
    # Headless sessions (ADR-0019): no tmux anywhere in the run image — the
    # session is a one-shot process and the container is never an attach
    # target. The agent must not run as root.
    assert "tmux" not in dockerfile
    assert "USER ozolith" in dockerfile
    assert "OZOLITH_UID" in dockerfile  # job-dir ownership knob
    # Knowledge Source is baked at BUILD time (never at container start).
    assert "theozolith-knowledge bake" in dockerfile


def test_codex_run_image_contract():
    """The codex base image (ADR-0052) mirrors the run-image posture with
    its deliberate divergences: the CLI is PINNED to exactly the adapter's
    enforcement floor (the --json schema and rollout journal the parsers
    speak are experimental — the pin is the policy, the floor the
    backstop), and no knowledge bakes standalone (derived images bake the
    per-tool tree at the node)."""
    from theozolith_worker.adapters import CodexAdapter

    dockerfile = (REPO_ROOT / "worker" / "docker" / "Dockerfile.codex").read_text()
    assert 'ENTRYPOINT ["theozolith-harness"]' in dockerfile
    assert "tmux" not in dockerfile
    assert "USER ozolith" in dockerfile
    assert "OZOLITH_UID" in dockerfile
    pinned = re.search(r"npm install -g @openai/codex@(\d+\.\d+\.\d+)", dockerfile)
    assert pinned is not None, "the codex CLI install must be version-pinned"
    floor = ".".join(str(part) for part in CodexAdapter.MIN_ENFORCING_CLI)
    assert pinned.group(1) == floor  # bump both together, re-running spike #76
    assert "theozolith-knowledge bake" not in dockerfile


def test_ci_builds_the_run_container_images():
    """M2 brief: the CI must build the run-container images so image rot is
    caught (a PR #2 review finding, absorbed here; one matrix leg per Agent
    adapter since ADR-0052). The build runs through buildx
    (docker/build-push-action) with a GHA layer cache, one scope per image
    so the legs never evict each other's layers."""
    ci = CI.read_text()
    assert "docker/build-push-action" in ci
    job = yaml.safe_load(ci)["jobs"]["run-image"]
    legs = {leg["agent"]: leg for leg in job["strategy"]["matrix"]["include"]}
    assert legs["claude"]["dockerfile"] == "worker/docker/Dockerfile.claude"
    assert legs["codex"]["dockerfile"] == "worker/docker/Dockerfile.codex"
    assert legs["claude"]["cache_scope"] != legs["codex"]["cache_scope"]


def _publish_job() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]["publish-run-image"]


def _publish_step(job: dict, name_prefix: str) -> dict:
    steps = [s for s in job["steps"] if str(s.get("name", "")).startswith(name_prefix)]
    assert len(steps) == 1, (name_prefix, [s.get("name") for s in job["steps"]])
    return steps[0]


def _run_step_script(
    script: str, cwd: Path, env: dict[str, str]
) -> tuple[int, str, dict[str, str]]:
    """Execute a workflow step's `run:` script the way Actions does on Linux
    (bash -e, outputs collected via GITHUB_OUTPUT) — the publish-safety tests
    cover the scripts' BEHAVIOR, not their string shape."""
    output = cwd / "github-output"
    output.write_text("")
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", script],
        cwd=str(cwd),
        env={**os.environ, "GITHUB_OUTPUT": str(output), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = {}
    for line in output.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            outputs[key] = value
    return proc.returncode, proc.stdout + proc.stderr, outputs


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _gate_holds(condition: str, outputs: dict[str, dict[str, str]]) -> bool:
    """Evaluate a step-level `if` of the exact shape the publish job uses —
    `steps.<id>.outputs.<key> == '<value>'` clauses joined by && — against
    the outputs the EXECUTED scripts actually produced (a skipped step's
    outputs read as empty, as on Actions)."""
    for clause in condition.split("&&"):
        left, sep, right = clause.partition("==")
        assert sep, clause
        m = re.fullmatch(r"steps\.(\w+)\.outputs\.(\w+)", left.strip())
        assert m, clause
        if outputs.get(m.group(1), {}).get(m.group(2), "") != right.strip().strip("'"):
            return False
    return True


def _publish_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A bare `origin` plus a working clone, seeded with one commit on main
    — the ls-remote target the tip check resolves against."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "f").write_text("1\n")
    _commit(seed, "one")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return seed, clone


def _fake_registry(tmp_path: Path) -> tuple[Path, Path]:
    """A STATEFUL fake registry shared by fake `docker` and fake `curl`:
    `docker push` records the ref (a :main push fails under
    FAKE_MAIN_PUSH_FAIL — the lost-move scenario), `docker buildx
    imagetools create` re-points :main only at a recorded manifest (an
    unknown one fails like an unreadable manifest), and `curl` answers the
    write-once manifest HEAD from the same recorded state."""
    registry = tmp_path / "registry"
    registry.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  push)\n"
        '    case "$2" in\n'
        '      *:main) if [ -n "$FAKE_MAIN_PUSH_FAIL" ]; then\n'
        '        echo "connection reset while pushing $2" >&2; exit 1\n'
        "      fi ;;\n"
        "    esac\n"
        '    echo "$2" >> "$FAKE_REGISTRY/pushed"\n'
        "    ;;\n"
        "  buildx)\n"
        '    if [ "$2" != "imagetools" ] || [ "$3" != "create" ] || [ "$4" != "--tag" ]; then\n'
        "      exit 9\n"
        "    fi\n"
        '    if ! grep -qxF "$6" "$FAKE_REGISTRY/pushed" 2>/dev/null; then\n'
        '      echo "manifest unknown: $6" >&2; exit 1\n'
        "    fi\n"
        '    echo "$5 -> $6" > "$FAKE_REGISTRY/main"\n'
        "    ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *ghcr.io/token*) printf '%s' '{\"token\": \"t\"}' ;;\n"
        "  */manifests/*)\n"
        '    ref="${*##*/manifests/}"\n'
        '    if grep -qxF "${IMAGE}:${ref}" "$FAKE_REGISTRY/pushed" 2>/dev/null; then\n'
        "      printf '200'\n"
        "    else\n"
        "      printf '404'\n"
        "    fi ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)
    return fake_bin, registry


def test_ci_publishes_the_run_image_from_main_only():
    """ADR-0051: merges to main publish the moving :main tag plus a
    WRITE-ONCE :sha-<sha>. The publish is a SEPARATE job gated to main
    pushes — `packages: write` must never ride the check jobs, which run on
    every PR and branch push — and publication is serialized and monotonic:
    a main-specific concurrency group that QUEUES every pending run (FIFO,
    never replacing the pending publisher), a write-once existence check
    before any build, and a live tip re-resolution gating every registry
    write. An existing commit tag is recoverable state: the repair step
    re-points :main at it (current-tip runs only) instead of rebuilding.
    build-push-action never pushes anywhere in the workflow; the registry
    writes are the guarded `docker push` step (commit tag first) and the
    registry-side repair re-tag."""
    ci = CI.read_text()
    workflow = yaml.safe_load(ci)
    # The workflow-level push trigger stays unfiltered — the secret-scan job
    # must run on every branch and tag push (path relevance for the publish
    # lives INSIDE the job instead).
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"push": None, "pull_request": None}

    job = _publish_job()
    assert job["needs"] == "run-image"
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    # No other job — PR check jobs included — is granted packages access.
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    granted = [
        name
        for name, spec in workflow["jobs"].items()
        if isinstance(spec.get("permissions"), dict) and "packages" in spec["permissions"]
    ]
    assert granted == ["publish-run-image"]
    # Serialized publication that never cancels a possibly-current run
    # (obsolete runs supersede themselves at the tip check instead) and
    # never drops one from the queue: queue: max keeps every pending run
    # in FIFO order, so the current tip's run always gets its turn.
    # One concurrency group per image (ADR-0052 matrix): each registry
    # ref's monotonic FIFO ordering is its own.
    assert job["concurrency"] == {
        "group": "publish-run-image-main-${{ matrix.agent }}",
        "cancel-in-progress": False,
        "queue": "max",
    }
    legs = {leg["agent"]: leg for leg in job["strategy"]["matrix"]["include"]}
    assert set(legs) == {"claude", "codex"}
    assert legs["claude"]["dockerfile"] == "worker/docker/Dockerfile.claude"
    assert legs["codex"]["dockerfile"] == "worker/docker/Dockerfile.codex"
    assert job["env"]["IMAGE"] == "ghcr.io/snowfoxbuilds/theozolith-run-${{ matrix.agent }}"
    # No job in the workflow lets build-push-action push.
    assert "push: true" not in ci
    build = [s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/build-push")]
    assert len(build) == 1
    assert build[0]["with"]["push"] is False and build[0]["with"]["load"] is True
    assert build[0]["with"]["file"] == "${{ matrix.dockerfile }}"
    assert "${{ env.IMAGE }}:main" in build[0]["with"]["tags"]
    assert "${{ env.IMAGE }}:sha-${{ github.sha }}" in build[0]["with"]["tags"]
    # Ordering: relevance -> write-once -> build -> tip -> login -> push
    # -> repair.
    names = [str(s.get("name") or s.get("uses")) for s in job["steps"]]
    order = [
        next(i for i, name in enumerate(names) if name.startswith(prefix))
        for prefix in (
            "Did anything the image is built from change?",
            "Is the commit tag already published?",
            "docker/build-push-action",
            "Is this run still the tip of main?",
            "docker/login-action",
            "Push the commit tag",
            "Point :main at the already-published commit tag",
        )
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
    # The build path is gated on fresh=true; the tip check runs on BOTH the
    # fresh and the already-published path (it gates every registry write),
    # so it must not itself require fresh.
    assert "steps.writeonce.outputs.fresh == 'true'" in build[0]["if"]
    tip = _publish_step(job, "Is this run still the tip of main?")
    assert tip["if"] == "steps.relevant.outputs.publish == 'true'"
    login = job["steps"][order[4]]
    assert "steps.tip.outputs.current == 'true'" in login["if"]
    push = _publish_step(job, "Push the commit tag")
    assert "steps.relevant.outputs.publish == 'true'" in push["if"]
    assert "steps.writeonce.outputs.fresh == 'true'" in push["if"]
    assert "steps.tip.outputs.current == 'true'" in push["if"]
    repair = _publish_step(job, "Point :main at the already-published commit tag")
    assert "steps.relevant.outputs.publish == 'true'" in repair["if"]
    assert "steps.writeonce.outputs.fresh == 'false'" in repair["if"]
    assert "steps.tip.outputs.current == 'true'" in repair["if"]
    # The commit tag (the immutable record) lands before :main moves, and
    # the repair path re-tags registry-side — it never rebuilds or pushes
    # the commit tag.
    assert push["run"].index("sha-${GITHUB_SHA}") < push["run"].index(":main")
    assert "imagetools create" in repair["run"] and "docker push" not in repair["run"]


def test_ci_publish_relevance_check_skips_doc_only_pushes(tmp_path):
    """The in-job path check EXECUTED: a doc-only main push produces
    publish=false, a worker/ change publishes, and an unresolvable diff base
    (force-push, first push) fails open to publish."""
    script = _publish_step(_publish_job(), "Did anything the image is built from change?")["run"]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "worker").mkdir()
    (repo / "worker" / "f.py").write_text("x\n")
    base = _commit(repo, "base")
    (repo / "README.md").write_text("docs only\n")
    doc_only = _commit(repo, "docs")
    (repo / "worker" / "f.py").write_text("y\n")
    relevant = _commit(repo, "worker change")

    rc, _, outputs = _run_step_script(script, repo, {"BEFORE": base, "GITHUB_SHA": doc_only})
    assert rc == 0 and outputs == {"publish": "false"}
    rc, _, outputs = _run_step_script(script, repo, {"BEFORE": doc_only, "GITHUB_SHA": relevant})
    assert rc == 0 and outputs == {"publish": "true"}
    rc, _, outputs = _run_step_script(script, repo, {"BEFORE": "0" * 40, "GITHUB_SHA": relevant})
    assert rc == 0 and outputs == {"publish": "true"}  # fail-open


def test_ci_publish_write_once_check_never_republishes_a_commit_tag(tmp_path):
    """The write-once step EXECUTED against a scripted registry: an existing
    sha-<sha> manifest (HTTP 200) answers fresh=false — a rerun of the same
    commit never rebuilds or overwrites the tag; the repair lane re-points
    :main at it instead (covered end to end below) — a 404 lets the first
    publication proceed, and any other answer (or a failed token mint) fails
    the job loudly instead of publishing blind."""
    step = _publish_step(_publish_job(), "Is the commit tag already published?")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *ghcr.io/token*) [ -n "$FAKE_TOKEN_FAIL" ] && exit 22;'
        " printf '%s' '{\"token\": \"t\"}' ;;\n"
        "  */manifests/*) printf '%s' \"$FAKE_MANIFEST_CODE\" ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_TOKEN": "t",
        "IMAGE": "ghcr.io/snowfoxbuilds/theozolith-run-claude",
        "GITHUB_SHA": "a" * 40,
    }
    rc, out, outputs = _run_step_script(step["run"], tmp_path, {**env, "FAKE_MANIFEST_CODE": "200"})
    assert rc == 0 and outputs == {"fresh": "false"} and "write-once" in out
    rc, out, outputs = _run_step_script(step["run"], tmp_path, {**env, "FAKE_MANIFEST_CODE": "404"})
    assert rc == 0 and outputs == {"fresh": "true"}
    rc, out, outputs = _run_step_script(step["run"], tmp_path, {**env, "FAKE_MANIFEST_CODE": "503"})
    assert rc != 0 and "refusing to publish blind" in out and outputs == {}
    rc, out, outputs = _run_step_script(step["run"], tmp_path, {**env, "FAKE_TOKEN_FAIL": "1"})
    assert rc != 0 and "refusing to publish blind" in out and outputs == {}


def test_ci_publish_tip_check_blocks_a_stale_run(tmp_path):
    """The monotonic-publish step EXECUTED: once origin/main has advanced
    past the run's commit the step answers current=false (every registry
    write is gated on it — a stale run can push neither :main nor its
    commit tag, and cannot repair :main either), the tip's own run answers
    current=true, and an unresolvable main ref fails loudly."""
    script = _publish_step(_publish_job(), "Is this run still the tip of main?")["run"]
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "f").write_text("1\n")
    first = _commit(seed, "one")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (seed / "f").write_text("2\n")
    second = _commit(seed, "two")
    _git(seed, "push", "-q", "origin", "main")

    rc, out, outputs = _run_step_script(script, clone, {"GITHUB_SHA": first})
    assert rc == 0 and outputs == {"current": "false"} and "stale" in out
    rc, _, outputs = _run_step_script(script, clone, {"GITHUB_SHA": second})
    assert rc == 0 and outputs == {"current": "true"}

    empty_origin = tmp_path / "empty.git"
    empty_origin.mkdir()
    _git(empty_origin, "init", "-q", "--bare", "-b", "main")
    lonely = tmp_path / "lonely"
    _git(tmp_path, "clone", "-q", str(empty_origin), str(lonely))
    rc, out, _ = _run_step_script(script, lonely, {"GITHUB_SHA": first})
    assert rc != 0 and "refusing to publish blind" in out


def test_ci_publish_rerun_repairs_main_after_a_lost_move(tmp_path):
    """The recovery lane EXECUTED end to end against the stateful fake
    registry: run 1 publishes the commit tag but loses the :main move (the
    :main push dies mid-step); the rerun's write-once check answers
    fresh=false, the tip check still answers current=true, the push gate
    closes while the repair gate opens, and the repair step re-points :main
    at the published commit tag WITHOUT pushing it again. Repairing against
    a manifest the registry cannot serve fails loudly."""
    job = _publish_job()
    writeonce = _publish_step(job, "Is the commit tag already published?")["run"]
    tip = _publish_step(job, "Is this run still the tip of main?")["run"]
    push = _publish_step(job, "Push the commit tag")
    repair = _publish_step(job, "Point :main at the already-published commit tag")
    _, clone = _publish_fixture(tmp_path)
    sha = _git(clone, "rev-parse", "HEAD")
    fake_bin, registry = _fake_registry(tmp_path)
    image = "ghcr.io/snowfoxbuilds/theozolith-run-claude"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_TOKEN": "t",
        "IMAGE": image,
        "GITHUB_SHA": sha,
        "FAKE_REGISTRY": str(registry),
    }

    # Run 1: first publication — fresh and current, but the :main move dies
    # after the commit tag already landed.
    rc, _, once = _run_step_script(writeonce, clone, env)
    assert rc == 0 and once == {"fresh": "true"}
    rc, _, tips = _run_step_script(tip, clone, env)
    assert rc == 0 and tips == {"current": "true"}
    outputs = {"relevant": {"publish": "true"}, "writeonce": once, "tip": tips}
    assert _gate_holds(push["if"], outputs) and not _gate_holds(repair["if"], outputs)
    rc, out, _ = _run_step_script(push["run"], clone, {**env, "FAKE_MAIN_PUSH_FAIL": "1"})
    assert rc != 0 and "connection reset" in out
    assert (registry / "pushed").read_text() == f"{image}:sha-{sha}\n"
    assert not (registry / "main").exists()

    # Run 2 (rerun): the existing tag is recoverable state — never rebuilt
    # or re-pushed, and :main converges on it.
    rc, out, once = _run_step_script(writeonce, clone, env)
    assert rc == 0 and once == {"fresh": "false"} and "write-once" in out
    rc, _, tips = _run_step_script(tip, clone, env)
    assert rc == 0 and tips == {"current": "true"}
    outputs = {"relevant": {"publish": "true"}, "writeonce": once, "tip": tips}
    assert _gate_holds(repair["if"], outputs) and not _gate_holds(push["if"], outputs)
    rc, out, _ = _run_step_script(repair["run"], clone, env)
    assert rc == 0 and "repair" in out
    assert (registry / "main").read_text() == f"{image}:main -> {image}:sha-{sha}\n"
    # Across both runs the commit tag was pushed exactly once.
    assert (registry / "pushed").read_text() == f"{image}:sha-{sha}\n"

    # A repair whose source manifest the registry cannot serve fails loudly
    # instead of moving :main onto nothing.
    (registry / "pushed").write_text("")
    rc, out, _ = _run_step_script(repair["run"], clone, env)
    assert rc != 0 and "manifest unknown" in out


def test_ci_publish_existing_sha_for_a_stale_commit_writes_nothing(tmp_path):
    """An already-published commit tag on a run that is NO LONGER the tip:
    fresh=false and current=false, so the login gate and BOTH write gates
    (first publication and repair) evaluate closed — a stale run performs
    no registry write even on the recovery lane."""
    job = _publish_job()
    writeonce = _publish_step(job, "Is the commit tag already published?")["run"]
    tip = _publish_step(job, "Is this run still the tip of main?")["run"]
    push = _publish_step(job, "Push the commit tag")
    repair = _publish_step(job, "Point :main at the already-published commit tag")
    login = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/login"))
    seed, clone = _publish_fixture(tmp_path)
    first = _git(clone, "rev-parse", "HEAD")
    (seed / "f").write_text("2\n")
    _commit(seed, "two")
    _git(seed, "push", "-q", "origin", "main")
    fake_bin, registry = _fake_registry(tmp_path)
    image = "ghcr.io/snowfoxbuilds/theozolith-run-claude"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_TOKEN": "t",
        "IMAGE": image,
        "GITHUB_SHA": first,
        "FAKE_REGISTRY": str(registry),
    }
    # The stale commit's tag is already published (its own run pushed it
    # before losing the :main move, say) — then main advanced.
    (registry / "pushed").write_text(f"{image}:sha-{first}\n")

    rc, _, once = _run_step_script(writeonce, clone, env)
    assert rc == 0 and once == {"fresh": "false"}
    rc, out, tips = _run_step_script(tip, clone, env)
    assert rc == 0 and tips == {"current": "false"} and "stale" in out
    outputs = {"relevant": {"publish": "true"}, "writeonce": once, "tip": tips}
    assert not _gate_holds(login["if"], outputs)
    assert not _gate_holds(push["if"], outputs)
    assert not _gate_holds(repair["if"], outputs)
    # Nothing wrote: the recorded state is exactly what the test seeded.
    assert (registry / "pushed").read_text() == f"{image}:sha-{first}\n"
    assert not (registry / "main").exists()


def test_configs_example_bases_ride_the_moving_main_tag():
    """ADR-0051: every starter base references the CI-republished moving tag,
    tag-only (ADR-0048) — each ingest re-resolves the digest. A future bump
    that reverts SOME bases to a frozen tag would silently split the fleet's
    bases, so the sweep covers every worker type."""
    for toml_path in sorted((DEPLOY / "configs-example" / "worker-types").glob("*.toml")):
        for line in toml_path.read_text().splitlines():
            if line.startswith("base = "):
                assert line.rstrip().endswith(':main"'), (toml_path.name, line)


# -- M3 substrate artifacts -------------------------------------------------------


def test_nodedaemon_unit_enforces_kill_the_tree():
    """The unit is embedded in the installer (curl|bash needs no sidecar
    files); its cgroup and directory contract is unchanged — but there is
    no EnvironmentFile: configuration is the provisioned state dir."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "KillMode=control-group" in installer  # ADR-0013: no zombie processes
    assert "RuntimeDirectory=theozolith" in installer  # secrets tmpfs under /run
    assert "StateDirectory=theozolith" in installer
    assert "EnvironmentFile" not in installer
    assert "Restart=always" in installer
    assert "User=ozolith" in installer  # never root
    assert not (DEPLOY / "systemd" / "theozolith-nodedaemon.service").exists()


def test_installer_hands_off_to_provision_as_its_final_step():
    """ADR-0023 installer consolidation: the manual-configuration half is
    gone — the installer installs the distribution and unit, then runs
    `theozolith-nodedaemon provision <join-string>`; a run without a join
    string is refused (no fingerprint-less manual path)."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "theozolith-nodedaemon provision" in installer
    assert "ozjoin" in installer  # the join string is the one input
    assert "usermod -aG docker ozolith" in installer
    assert "read -r -s" not in installer  # no token prompting remains
    assert "theozolith join-token create" in installer  # the refusal says where to go
    # Steps after the last comment: pip install precedes provision.
    assert installer.index("pip install") < installer.index('provision "$JOIN"')


def test_control_compose_mounts_the_partitioned_home():
    compose = (DEPLOY / "compose" / "control.yml").read_text()
    assert "~/.theozolith}:/data" in compose  # ADR-0024: the one home
    assert "THEOZOLITH_DATA_DIR: /data" in compose
    assert "run --rm control init" in compose  # the unified first run (ADR-0023)
    assert "8443" in compose
    assert "6965:6965" in compose  # the bootstrap listener rides its own port
    # THEOZOLITH_REPO is retired as a Control Node setting (ADR-0056): the
    # control PAT alone enables coordination, keyed by the Pinned Build's
    # Bound Workspaces — the compose file must never pass it.
    assert "THEOZOLITH_REPO" not in compose
    assert "CONTROL_GITHUB_TOKEN" in compose


def test_ci_builds_the_control_image():
    ci = CI.read_text()
    assert "docker/build-push-action" in ci
    assert "file: control/docker/Dockerfile" in ci


def test_no_tailscale_anywhere_in_product_code_or_deploy():
    """NODE-SUBSTRATE.md: Tailscale is a private-side deployment detail — never
    in product code, product IMAGES, or deploy scripts. Per-container tailnet
    identity enters only via a worker type's setup instructions in the Config
    Repo (ADR-0043); the ONE sanctioned home for the string is
    ``deploy/configs-example/**`` (and its README), which this scan
    deliberately does not touch. The product Dockerfiles are in scope: a
    tailscaled baked into a base image would violate the doctrine just as
    surely as product source would. PR #35's generic product corrections
    (entrypoint-override container commands, cross-UID secret delivery) land
    inside this scan's scope — this test staying green is what proves they
    carry no Tailscale-specific behavior or strings."""
    for component in ("worker", "control", "nodedaemon", "knowledge"):
        for path in (REPO_ROOT / component / "src").rglob("*.py"):
            assert "tailscale" not in path.read_text().lower(), path
    for dockerfile in (DOCKERFILE, CONTROL_DOCKERFILE):
        assert "tailscale" not in dockerfile.read_text().lower(), dockerfile
    for name in ("install-nodedaemon.sh", "compose/control.yml", "README.md"):
        assert "tailscale" not in (DEPLOY / name).read_text().lower(), name


def test_configs_example_parses_and_places_the_builtin_stacks(example_config):
    """The starter Config Repo must stay valid THROUGH INGEST (ADR-0048):
    worker/reviewer as process Stacks, the Flight Deck as a container Stack
    (ADR-0013/0019). Control is never a Stack — the substrate never
    supervises its own control plane (ADR-0035) — so the example must not
    carry one."""
    config = example_config
    kinds = {stack.name: stack.kind for stack in config.stacks}
    assert kinds == {
        "implementer": "process",
        "reviewer": "process",
        # The second-adapter reviewer (ADR-0052), staged stopped beside the
        # claude one (the pr_ready race is documented, not routed).
        "codex-review": "process",
        "flightdeck": "container",
        # The custom-driver example (ADR-0042): a drivers/<name> worker type
        # resolves to process kind and the generic launcher, staged stopped.
        "hello-logger": "process",
    }
    # The starter ships PIN-LESS (ADR-0051): the update flow (`theozolith
    # build`/`update`) owns the product pin, and ingest preserves it —
    # declaring product.toml here would revert real deployments' pins.
    assert config.product_version == ""
    assert "claude-dev" in config.worker_types
    # The example knowledge tree compiled at ingest, pinned PER TOOL
    # (ADR-0052); both claude types share the claude-view pin, the codex
    # reviewer joins the codex-view pin of the SAME tree.
    assert config.worker_types["claude-dev"].knowledge == "knowledge/claude-dev"
    assert (
        config.worker_types["claude-dev"].knowledge_pin
        == config.worker_types["claude-review"].knowledge_pin
        != ""
    )
    codex_review = config.worker_types["codex-review"]
    assert codex_review.knowledge == "knowledge/claude-dev"
    assert codex_review.knowledge_pin not in ("", config.worker_types["claude-dev"].knowledge_pin)
    # The recipe carries the adapter-derived bake target; the daemon COPYs
    # where it is told (ADR-0052).
    recipe = codex_review.recipe_wire()
    assert recipe["knowledge_tool"] == "codex"
    assert recipe["knowledge_target"] == "/home/ozolith/.codex/"
    # The custom driver resolves to the one launcher with a drivers/<name> ref
    # (ADR-0042), and its module is present so the load did not fault.
    hello = next(s for s in config.stacks if s.name == "hello-logger")
    assert hello.command == "theozolith-driver drivers/hello_logger"
    assert hello.state == "stopped"
    assert (REPO_ROOT / "deploy" / "configs-example" / "drivers" / "hello_logger.py").is_file()
    # Every example Stack is STAGED (stopped): the fill-in placeholders would
    # be refused by ingest on a running Stack (ADR-0048).
    assert {stack.state for stack in config.stacks} == {"stopped"}
    # The Implementer Stack's node gets exactly its referenced secrets (the
    # worker type owns them, ADR-0044).
    implementer = next(s for s in config.stacks if s.name == "implementer")
    assert config.secret_names_for(implementer.node) >= {"github-implementer", "anthropic-api-key"}
    # Desired state renders (compose text inlines) for every placed node.
    for node in {stack.node for stack in config.stacks}:
        state = config.desired_state_for(node)
        assert state["commit"]
    # ADR-0019: run containers are never attach targets — no process Stack
    # carries an attach command (the parser enforces it; this pins the
    # example). The Flight Deck is the attach target, under its own
    # dedicated machine-identity secret, distinct from every driver PAT.
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    assert flightdeck.attach and "tmux" in flightdeck.attach
    driver_secrets = {
        name
        for stack in config.stacks
        if stack.kind == "process"
        for name in stack.secrets.values()
    }
    assert flightdeck.secrets["GITHUB_TOKEN"] == "flightdeck-github-token"
    assert "flightdeck-github-token" not in driver_secrets


def test_configs_example_flightdeck_knowledge_wiring(example_config):
    """ADR-0048 (amending ADR-0043) + issue #31: the example Flight Deck wires
    per-instance runtime state, the READ-ONLY bind of the node's applied
    knowledge export, and a per-instance tailnet identity volume; bakes the
    knowledge symlinks into flightdeck-start; and keeps the carve-out
    Flight-Deck-only. The tailscale half is landed behind the #31 gate
    evidence (uid-1000, userspace networking, no added capabilities)."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")

    # Per-instance state + logs + tailnet identity (resolved from {stack});
    # the SHARED read-only knowledge/policy/cli binds that are deliberately
    # NOT per-instance — and nothing else. Mounted at the stable PARENT the
    # Node Daemon maintains, so a tree swap or CLI re-point never recreates
    # the container.
    assert set(flightdeck.volumes) == {
        "flightdeck-logs:/var/log/flightdeck",
        "flightdeck-claude-state:/home/ozolith/.claude",
        "/var/lib/theozolith/knowledge:/var/lib/theozolith/knowledge:ro",
        "/var/lib/theozolith/policy:/var/lib/theozolith/policy:ro",
        "/var/lib/theozolith/cli:/var/lib/theozolith/cli:ro",
        "flightdeck-tailscale-state:/var/lib/tailscale",
        "flightdeck-workspace:/workspace",
    }

    wt = config.worker_types["flightdeck"]
    # The deck SELECTS its tree via the worker-type knowledge field (ADR-0048
    # amendment): validated like a driver's (pin joined at load), delivered
    # as control-injected env — and NEVER baked: the wire recipe carries
    # empty knowledge fields (the state volume shadows ~/.claude), so a
    # content edit moves the pin without touching the image identity.
    assert wt.knowledge == "knowledge/claude-dev"
    assert wt.knowledge_pin == config.worker_types["claude-dev"].knowledge_pin
    recipe = wt.recipe_wire()
    assert recipe["knowledge"] == "" and recipe["knowledge_pin"] == ""
    assert flightdeck.env["THEOZOLITH_KNOWLEDGE_TREE"] == "claude-dev"
    script = "\n".join(wt.setup)

    # The writable clone is RETIRED (ADR-0048): no clone-init, no knowledge
    # URL — the symlinks resolve into the mounted COMPILED tree, selected at
    # container start from the injected env (no hardcoded tree name), and
    # the script fails loud when the selected tree is unavailable. The
    # replace idiom is portable (no GNU-only ln -T): a real directory at a
    # destination is refused, a stale link or file is relinked.
    assert "clone-init" not in script
    # github.com may appear ONLY for the workspace/identity wiring (the gh
    # apt source and the derived noreply commit identity) — knowledge itself
    # is never fetched from a remote (the retired ADR-0043 clone).
    for line in script.splitlines():
        if "github.com" in line:
            assert "cli.github.com" in line or "users.noreply.github.com" in line, line
    assert "ln -sfnT" not in script and "ln -sfn" not in script
    assert "knowledge/claude-dev" not in script  # the selection is env, not a literal
    assert '"${THEOZOLITH_KNOWLEDGE_TREE:?' in script
    assert 'KNOWLEDGE_TREE_DIR="/var/lib/theozolith/knowledge/${THEOZOLITH_KNOWLEDGE_TREE}"' in (
        script
    )
    assert 'if [ ! -d "$KNOWLEDGE_TREE_DIR" ]; then' in script
    assert "link_knowledge" in script and 'ln -s "$1" "$2"' in script
    assert "for entry in skills agents workflows CLAUDE.md; do" in script
    assert '"/home/ozolith/.claude/$entry"' in script

    # The carve-out is Flight-Deck-only: no OTHER stack or worker type mounts
    # the knowledge or policy exports, any .claude path, or a tailnet identity.
    for stack in config.stacks:
        if stack.name == "flightdeck":
            continue
        for volume in stack.volumes:
            assert "knowledge" not in volume and ".claude" not in volume, (stack.name, volume)
            assert "policy" not in volume, (stack.name, volume)
            assert "tailscale" not in volume, (stack.name, volume)
    for name, other in config.worker_types.items():
        if name == "flightdeck":
            continue
        for volume in other.volumes:
            assert "knowledge" not in volume and ".claude" not in volume, (name, volume)
            assert "policy" not in volume, (name, volume)
            assert "tailscale" not in volume, (name, volume)


def test_configs_example_flightdeck_policy_wiring(example_config):
    """ADR-0055: the example Flight Deck declares an Agent Policy tree,
    control injects the bare tree name (never a literal in the script), the
    read-only policy bind rides at the stable PARENT, and the deck recipe's
    policy fields stay EMPTY — a policy content edit never rebuilds the deck
    image or recreates the container; only reselecting the tree does
    (through the injected env)."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    wt = config.worker_types["flightdeck"]
    assert wt.policy == "policy/claude-defaults"
    assert wt.policy_pin == config.worker_types["claude-dev"].policy_pin != ""
    recipe = wt.recipe_wire()
    assert recipe["policy"] == "" and recipe["policy_pin"] == ""
    assert flightdeck.env["THEOZOLITH_POLICY_TREE"] == "claude-defaults"
    assert "/var/lib/theozolith/policy:/var/lib/theozolith/policy:ro" in flightdeck.volumes

    script = "\n".join(wt.setup)
    assert "policy/claude-defaults" not in script  # the selection is env, not a literal
    assert 'POLICY_TREE_DIR="/var/lib/theozolith/policy/${THEOZOLITH_POLICY_TREE}"' in script
    assert 'if [ ! -d "$POLICY_TREE_DIR" ]; then' in script
    assert "sudo mkdir -p /etc/claude-code" in script
    assert "sudo rm -f /etc/claude-code/managed-settings.d" in script
    assert 'sudo ln -s "$POLICY_TREE_DIR" /etc/claude-code/managed-settings.d' in script

    # The BAKE path is exercised by the driver types: claude-dev declares the
    # same tree, its recipe carries the reference and pin, and the identity
    # gained exactly the conditional keys (nothing pins example tags, so the
    # tag moving with the drop-in content is fine).
    claude_dev = config.worker_types["claude-dev"].recipe_wire()
    assert claude_dev["policy"] == "policy/claude-defaults"
    assert claude_dev["policy_pin"] == config.worker_types["claude-dev"].policy_pin


def test_configs_example_flightdeck_tmpfs_mount(example_config):
    """#109: the example Flight Deck declares a RAM-backed tmpfs /tmp so its
    heavy scratch I/O lands off the overlay writable layer (the layer whose
    size walk raced `docker ps` into a transient 500). The worker-type field is
    driverless-only, resolves onto the Stack verbatim (container paths only, no
    {stack} substitution), and — being node-side runtime state, not image
    identity — never enters the wire recipe."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    wt = config.worker_types["flightdeck"]
    assert wt.tmpfs == ("/tmp:size=8g",)
    assert flightdeck.tmpfs == ("/tmp:size=8g",)  # resolved onto the Stack verbatim
    # tmpfs is runtime state, not image identity: the recipe carries no such key.
    assert "tmpfs" not in wt.recipe_wire()


def test_configs_example_flightdeck_github_workspace_wiring(example_config):
    """snow-maker container-setup parity (dev-dockers/setup.sh + Dockerfile):
    the deck ships the GitHub CLI from GitHub's own apt repo plus the
    interactive tool set, authenticates the dedicated no-merge machine
    identity from the delivered secret FILE on every start (the value never
    enters argv or env), derives the commit identity from the token account,
    and clones the Stack-bound workspace once onto the per-instance volume."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    wt = config.worker_types["flightdeck"]
    setup = "\n".join(wt.setup)

    # The Stack's per-placement workspace binding (ADR-0047) resolves into
    # the deck's env; the checkout target volume is per-instance and its
    # mountpoint is seeded ozolith-owned (setup runs as root).
    assert flightdeck.env["THEOZOLITH_REPO"] == "acme/sandbox"
    assert "flightdeck-workspace:/workspace" in flightdeck.volumes
    seed = next(s for s in wt.setup if s.startswith("mkdir -p"))
    assert "/workspace" in seed and "chown ozolith:ozolith" in seed

    # gh rides GitHub's own apt repo (keyring over TLS, signature-verified
    # packages), alongside the snow-maker interactive tool set.
    assert "cli.github.com/packages/githubcli-archive-keyring.gpg" in wt.setup[0]
    for tool in ("gh", "jq", "ripgrep", "less", "procps", "vim", "nano", "openssh-client"):
        assert f" {tool}" in wt.setup[0], tool

    # Identity + workspace wiring in the start script: auth by FILE path
    # only, gh as the git credential helper, machine-derived commit
    # identity, clone only when the checkout is absent, and the fail-loud
    # misconfiguration (workspace bound, token not).
    for needed in (
        'gh auth login --with-token < "$GITHUB_TOKEN_FILE"',
        "gh auth setup-git",
        "gh auth status",
        'git config --global user.name "$GH_LOGIN"',
        '"${GH_ID}+${GH_LOGIN}@users.noreply.github.com"',
        'gh repo clone "$THEOZOLITH_REPO" "$WORKSPACE_DIR"',
        "restore the flightdeck-github-token binding",
    ):
        assert needed in setup, needed


def test_configs_example_flightdeck_tailscale_wiring(example_config):
    """Issue #31: the tailscale half of the example, landed behind the gate
    evidence recorded on the issue. Static binaries are pinned by version AND
    sha256 with a FAIL-CLOSED placeholder (human-entered, never ingest-
    computed — ADR-0048); the auth key enters as a named secret only; the
    hostname is per-placement Stack env; no capability or device passthrough
    exists anywhere to grant (the worker-type schema has no such field — this
    test pins the example's side of that doctrine)."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    wt = config.worker_types["flightdeck"]
    setup = "\n".join(wt.setup)

    # Fail-closed binary pinning: the checksum is verified before install, and
    # the shipped placeholder can never match a real download.
    assert "pkgs.tailscale.com" in setup
    assert "sha256sum -c" in setup
    assert "TS_SHA256=0000000000000000000000000000000000000000000000000000000000000000" in setup
    assert setup.index("sha256sum -c") < setup.index("install -m 0755")

    # The pinned release is the one the #31 gate evidence was produced with
    # (the spikes/issue-31-tailscale-uid1000 harness runs the identical
    # archive), and it may never regress below the 1.98.9 security floor.
    version = re.search(r"TS_VERSION=(\d+)\.(\d+)\.(\d+)", setup)
    assert version, "TS_VERSION must be pinned major.minor.patch in the setup step"
    assert version.group(0) == "TS_VERSION=1.102.2"
    assert tuple(map(int, version.groups())) >= (1, 98, 9)

    # Userspace daemon, uid-1000 statedir, no capability grant anywhere.
    # sudo itself is sanctioned in the deck for in-session installs
    # (snow-maker parity — see the dedicated test), but the tailnet
    # lifecycle must never be escalated: the #31 gate evidence covers the
    # unprivileged uid-1000 daemon only.
    assert "--tun=userspace-networking" in setup
    assert "chown ozolith:ozolith" in setup and "/var/lib/tailscale" in setup
    for forbidden in ("cap_add", "NET_ADMIN", "/dev/net/tun", "privileged"):
        assert forbidden not in setup, forbidden
    for line in setup.splitlines():
        if "tailscale" in line:
            assert "sudo" not in line, f"tailscale must never run under sudo: {line}"

    # The key is a named secret delivered as TS_AUTHKEY_FILE; the hostname is
    # per-placement Stack env, present on the example Stack.
    assert flightdeck.secrets["TS_AUTHKEY"] == "flightdeck-tailscale-authkey"
    assert flightdeck.env["FLIGHTDECK_TS_HOSTNAME"] == "flightdeck-box1"
    # Only the file path is ever referenced — the value has no other route in.
    assert "TS_AUTHKEY_FILE" in setup


def test_configs_example_flightdeck_sudo_for_in_session_installs(example_config):
    """snow-maker parity: ozolith gets PASSWORDLESS sudo via a 0440
    sudoers.d drop-in baked with the base tooling, so a session installs
    software (build-essential included) without an operator round-trip.
    This is root within the container NAMESPACE only — the substrate grants
    no capability (pinned by the tailscale-wiring test, which also pins
    that the tailnet daemon itself is never escalated)."""
    setup = example_config.worker_types["flightdeck"].setup
    assert " sudo " in setup[0]
    assert 'echo "ozolith ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ozolith' in setup[0]
    assert "chmod 0440 /etc/sudoers.d/ozolith" in setup[0]
    # snow-maker's networking pair rides along (inspection works; netfilter
    # mutation still needs a capability the schema cannot grant).
    for pkg in ("iproute2", "iptables"):
        assert pkg in setup[0], pkg


def test_configs_example_flightdeck_ssh_username_alias(example_config):
    """Bare `ssh <hostname>` support: the worker type carries an operator-
    edited FLIGHTDECK_SSH_USER setup variable that bakes the name as a
    SECOND /etc/passwd entry at the ozolith uid — non-root Tailscale SSH
    compares uids numerically (tailssh.go, the pinned v1.102.2 release), so
    an alias suffices, and a runtime env never could (the deck runs
    unprivileged per the #31 doctrine and /etc/passwd is root-owned, so the
    name must exist before the build drops back to USER ozolith)."""
    setup = "\n".join(example_config.worker_types["flightdeck"].setup)
    assert "FLIGHTDECK_SSH_USER=ozolith" in setup  # the shipped no-op default
    assert 'if [ "$FLIGHTDECK_SSH_USER" != ozolith ]' in setup
    # The alias shares the ozolith uid/gid and home — DERIVED, never
    # hardcoded (the base image accepts an OZOLITH_UID build arg).
    assert "useradd --non-unique" in setup
    assert '--uid "$(id -u ozolith)"' in setup
    assert '--gid "$(id -g ozolith)"' in setup
    assert "--home-dir /home/ozolith" in setup
    assert "--no-create-home" in setup


def test_configs_example_flightdeck_tmux_conf_is_verbatim_snow_maker(tmp_path, example_config):
    """The baked /etc/tmux.conf carries the snow-maker mosh+tmux settings
    VERBATIM — the Ms terminal-override in particular is fragile (the
    obvious simpler form makes tparm fail silently and tmux emits nothing),
    so the directives are pinned byte-exact by EXECUTING the generator step,
    never by grepping fragments."""
    setup = example_config.worker_types["flightdeck"].setup
    generators = [s for s in setup if "/etc/tmux.conf" in s]
    assert len(generators) == 1
    dest = tmp_path / "tmux.conf"
    command = generators[0].replace("/etc/tmux.conf", str(dest))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    directives = [
        line for line in dest.read_text().splitlines() if line and not line.startswith("#")
    ]
    assert directives == [
        "set -g mouse on",
        "set -g set-clipboard on",
        "set -as terminal-features ',*:clipboard'",
        r"set -as terminal-overrides ',*:Ms=\E]52;%?%p1%l%t%p1%s%ec%;;%p2%s\007'",
        "set -g mode-keys vi",
        "set -g history-limit 50000",
        "bind -T copy-mode-vi v     send-keys -X begin-selection",
        "bind -T copy-mode-vi y     send-keys -X copy-selection-and-cancel",
        "bind -T copy-mode-vi Enter send-keys -X copy-selection-and-cancel",
        "bind -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-selection-and-cancel",
        "bind -T root MouseDown2Pane paste-buffer -p",
    ]


def test_configs_example_flightdeck_shell_defaults_and_mosh(tmp_path, example_config):
    """snow-maker parity for the interactive surface: mosh plus a
    server-side UTF-8 locale (mosh-server refuses to start without one
    matching the client's forwarded LANG), and a baked profile whose
    CLAUDE_CONFIG_DIR puts .claude.json on the claude-state volume. The
    profile generator is EXECUTED, and the result sourced under set -eu —
    the LANG default must neither error when LANG is unset nor clobber a
    client-forwarded value."""
    setup = example_config.worker_types["flightdeck"].setup
    for needed in ("mosh", "locales", "locale-gen", "en_US.UTF-8"):
        assert needed in setup[0], needed
    generators = [s for s in setup if "/etc/profile.d/flightdeck.sh" in s]
    assert len(generators) == 1
    profile = tmp_path / "flightdeck.sh"
    bashrc = tmp_path / "bash.bashrc"
    runtime_env = tmp_path / "flightdeck-env"
    command = generators[0].replace("/etc/profile.d/flightdeck.sh", str(profile))
    command = command.replace("/etc/bash.bashrc", str(bashrc))
    command = command.replace("/home/ozolith/.flightdeck-env", str(runtime_env))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    # tmux windows run non-login shells: bash.bashrc must source the SAME
    # file the login-shell profile.d path serves.
    assert bashrc.read_text() == f". {profile}\n"
    content = profile.read_text()
    assert "export CLAUDE_CONFIG_DIR=/home/ozolith/.claude" in content
    assert "alias claude-long='claude --dangerously-skip-permissions'" in content
    source_fresh = f'set -eu; unset LANG; . "{profile}"; printf %s "$LANG:$CLAUDE_CONFIG_DIR"'
    probe = subprocess.run(
        ["/bin/sh", "-c", source_fresh],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout == "en_US.UTF-8:/home/ozolith/.claude"
    probe = subprocess.run(
        ["/bin/sh", "-c", f'set -eu; LANG=de_DE.UTF-8; . "{profile}"; printf %s "$LANG"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout == "de_DE.UTF-8"  # forwarded locale wins over the default
    # The profile sources the runtime env file flightdeck-start rewrites on
    # every start (the workspace checkout path), and interactive LOGIN
    # shells — a tailscale ssh/mosh session — open in the workspace, like
    # snow-maker's dev-env.sh. Plain sh / non-login shells never cd (tmux
    # windows already start in the checkout via new-session -c), and the
    # guard stays set -eu-safe with the env file absent (probes above).
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime_env.write_text(f"export WORKSPACE_DIR={workspace}\n")
    probe = subprocess.run(
        ["/bin/sh", "-c", f'set -eu; . "{profile}"; printf %s "$WORKSPACE_DIR:$PWD"'],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert probe.stdout == f"{workspace}:{tmp_path}"  # exported, but no cd
    probe = subprocess.run(
        ["bash", "--noprofile", "--norc", "-lic", f'. "{profile}"; printf %s "$PWD"'],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert probe.stdout == str(workspace)  # login+interactive shells cd in


def test_flightdeck_tailscale_version_matches_the_gate_harness(example_config):
    """The version the example ships must be the version the #31 gate harness
    (spikes/issue-31-tailscale-uid1000, PR #34) actually tested — gate evidence
    for one binary says nothing about another. Both sides fetch the same
    archive from the same URL; the harness additionally pins the real official
    SHA-256 (the example's stays a fail-closed placeholder by design)."""
    spike_dockerfile = REPO_ROOT / "spikes" / "issue-31-tailscale-uid1000" / "Dockerfile"
    if not spike_dockerfile.exists():
        pytest.skip("gate harness (PR #34) not present in this checkout yet")
    setup = "\n".join(example_config.worker_types["flightdeck"].setup)
    example = re.search(r"TS_VERSION=(\d+\.\d+\.\d+)", setup)
    spike = re.search(r"TS_VERSION=(\d+\.\d+\.\d+)", spike_dockerfile.read_text())
    assert example and spike, "both sides must pin TS_VERSION major.minor.patch"
    assert example.group(1) == spike.group(1)


# -- flightdeck-start: the generated script is EXECUTED, not just grepped ---------


def _generate_flightdeck_start(tmp_path: Path, config) -> Path:
    """Run the worker type's script-writing setup entry in a real /bin/sh —
    exactly what the image build does — with the baked destination redirected
    into tmp_path, and return the generated script."""
    wt = config.worker_types["flightdeck"]
    generators = [s for s in wt.setup if "/usr/local/bin/flightdeck-start" in s]
    assert len(generators) == 1
    dest = tmp_path / "flightdeck-start"
    command = generators[0].replace("/usr/local/bin/flightdeck-start", str(dest))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    assert dest.stat().st_mode & 0o111, "flightdeck-start must be executable"
    return dest


POLICY_DOC = '{"attribution": {"sessionUrl": false}}\n'


def _sandboxed_script(script: Path, sandbox: Path) -> Path:
    """Rewrite the generated script's absolute paths into a sandbox so it can
    run as the test user; the command sequence is untouched. A passthrough
    `sudo` stub lands in the sandbox bin dir (the policy wiring escalates for
    the root-owned managed drop-in link, ADR-0055; in the sandbox the command
    just runs as the test user)."""
    content = script.read_text()
    content = content.replace("/home/ozolith", str(sandbox / "home"))
    content = content.replace("/var/log/flightdeck", str(sandbox / "log"))
    content = content.replace("/var/lib/tailscale", str(sandbox / "tsstate"))
    content = content.replace("/var/lib/theozolith/knowledge", str(sandbox / "knowledge"))
    content = content.replace("/var/lib/theozolith/policy", str(sandbox / "policy"))
    content = content.replace("/var/lib/theozolith/cli", str(sandbox / "cli"))
    content = content.replace("/opt/theozolith-deck/bin", str(sandbox / "deck-bin"))
    content = content.replace("/etc/claude-code", str(sandbox / "claude-code"))
    content = content.replace("/etc/theozolith", str(sandbox / "etc"))
    content = content.replace("/workspace", str(sandbox / "workspace"))
    rewritten = sandbox / "start"
    rewritten.write_text(content)
    rewritten.chmod(0o755)
    (sandbox / "home" / ".claude").mkdir(parents=True)  # the state-volume mountpoint
    (sandbox / "tsstate").mkdir()  # the tailscale-state volume mountpoint
    (sandbox / "workspace").mkdir()  # the workspace volume mountpoint
    # The applied knowledge tree the deck's selection resolves to (ADR-0048):
    # present by default so the fail-loud gate passes; the unavailable-tree
    # test removes it.
    (sandbox / "knowledge" / "claude-dev").mkdir(parents=True)
    # The applied Agent Policy tree (ADR-0055), same posture.
    (sandbox / "policy" / "claude-defaults").mkdir(parents=True)
    (sandbox / "policy" / "claude-defaults" / "attribution.json").write_text(POLICY_DOC)
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    return rewritten


def _stub(bin_dir: Path, name: str, exit_code: int) -> Path:
    """A recording stand-in: appends its argv to <name>.calls, exits fixed."""
    calls = bin_dir / f"{name}.calls"
    stub = bin_dir / name
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return calls


def _tailscale_stub(bin_dir: Path, *, status_code: int = 0, up_code: int = 0) -> Path:
    """A `tailscale` CLI stand-in that distinguishes the readiness probe
    (`status --json`) from `up`, so tests can drive each lifecycle branch."""
    calls = bin_dir / "tailscale.calls"
    stub = bin_dir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        f'*" up "*|*" up") exit {up_code} ;;\n'
        f"*) exit {status_code} ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return calls


def _tailscale_timeout_stub(bin_dir: Path) -> Path:
    """A `tailscale` stand-in modelling the REAL CLI's contract on a tailnet
    that never reaches Running state: `up` WITHOUT a --timeout flag blocks
    indefinitely (the CLI default is an infinite wait), `up` WITH one returns
    non-zero at the bound (emulated instantly for test speed). The readiness
    probe answers ready. A script that drops the native flag therefore hangs
    here and fails the test's OUTER deadline instead of passing."""
    calls = bin_dir / "tailscale.calls"
    stub = bin_dir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        '*" up "*|*" up")\n'
        '  case "$*" in\n'
        '  *--timeout=*) echo "timeout waiting for Tailscale service" >&2; exit 1 ;;\n'
        "  *) exec /bin/sleep 60 ;;\n"
        "  esac ;;\n"
        "*) exit 0 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return calls


def _tailscaled_stub(
    bin_dir: Path, *, lifespan: str | None, writes_state: Path | None = None
) -> tuple[Path, Path]:
    """A `tailscaled` stand-in launched in the background by the script.
    lifespan None = exit immediately (a daemon that dies on startup); a
    duration = stay alive that long under /bin/sleep (immune to the test's
    `sleep` stub), recording its pid so tests can verify the EXIT trap
    reaped it. writes_state mimics the real daemon writing its machine key
    into the statedir at launch — BEFORE auth-key registration completes —
    which is exactly why a non-empty state file is not proof of enrollment."""
    calls = bin_dir / "tailscaled.calls"
    pid_file = bin_dir / "tailscaled.pid"
    body = f'#!/bin/sh\necho "$@" >> "{calls}"\necho $$ > "{pid_file}"\n'
    if writes_state is not None:
        body += f'echo machine-key-material > "{writes_state / "tailscaled.state"}"\n'
    body += "exit 0\n" if lifespan is None else f"exec /bin/sleep {lifespan}\n"
    stub = bin_dir / "tailscaled"
    stub.write_text(body)
    stub.chmod(0o755)
    return calls, pid_file


def _gh_stub(bin_dir: Path, *, auth_code: int = 0, clone_code: int = 0) -> Path:
    """A `gh` CLI stand-in: records every argv, captures the token piped to
    `auth login` from STDIN (the real flow — the value must never ride argv),
    answers the identity queries with a fixed machine account, and emulates
    `repo clone` by materializing (or, on failure, not materializing) the
    target checkout."""
    calls = bin_dir / "gh.calls"
    stub = bin_dir / "gh"
    clone_action = f"exit {clone_code}" if clone_code else 'mkdir -p "$4/.git"'
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$1 $2" in\n'
        f'"auth login") cat > "{bin_dir}/gh.token"; exit {auth_code} ;;\n'
        '"api user") case "$*" in *login*) echo testbot ;; *) echo 12345 ;; esac ;;\n'
        f'"repo clone") {clone_action} ;;\n'
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return calls


def _tmux_stub(bin_dir: Path, *, has_session_code: int) -> Path:
    """A `tmux` stand-in; has-session's exit code drives the supervisor loop
    (0 = session alive, 1 = session gone). Besides the flat `tmux.calls`
    line, every invocation is recorded boundary-preserving into `tmux.argv`
    (`argc N` then one `arg <value>` line each) — `echo "$@"` flattens a
    multi-word command argument into indistinguishable words, and the baked
    model contract is exactly about tmux receiving ONE command string."""
    calls = bin_dir / "tmux.calls"
    argv = bin_dir / "tmux.argv"
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        f'echo "argc $#" >> "{argv}"\n'
        f'for a in "$@"; do echo "arg $a" >> "{argv}"; done\n'
        f'case "$1" in has-session) exit {has_session_code} ;; esac\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return calls


def _tmux_invocations(bin_dir: Path) -> list[list[str]]:
    """The argv of every recorded tmux invocation, boundaries intact."""
    lines = (bin_dir / "tmux.argv").read_text().splitlines()
    invocations: list[list[str]] = []
    index = 0
    while index < len(lines):
        head = lines[index]
        assert head.startswith("argc "), head
        argc = int(head.split()[1])
        args = lines[index + 1 : index + 1 + argc]
        assert len(args) == argc and all(arg.startswith("arg ") for arg in args)
        invocations.append([arg[len("arg ") :] for arg in args])
        index += 1 + argc
    return invocations


def _instant_sleep(bin_dir: Path) -> None:
    """Neutralize the script's wait/supervision intervals so bounded loops run
    at test speed. Stub daemons dodge this by calling /bin/sleep directly."""
    stub = bin_dir / "sleep"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)


def _run_start(
    script: Path, bin_dir: Path, *, timeout: float | None = None, **extra_env: str | None
) -> subprocess.CompletedProcess:
    """The container-start env: the knowledge and policy selections ride as
    the control-injected THEOZOLITH_KNOWLEDGE_TREE / THEOZOLITH_POLICY_TREE
    (ADR-0048/0055) — defaulted here so every lifecycle test starts like a
    resolved deck Stack; pass ``None`` to UNSET a variable (the
    missing-selection and policy-less-deck tests)."""
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "THEOZOLITH_KNOWLEDGE_TREE": "claude-dev",
        "THEOZOLITH_POLICY_TREE": "claude-defaults",
    }
    env.pop("TS_AUTHKEY_FILE", None)
    env.pop("GITHUB_TOKEN_FILE", None)
    env.pop("THEOZOLITH_REPO", None)
    # The CLI Pin selection (ADR-0055) stays UNSET by default: a pinless deck
    # keeps today's behavior exactly; the CLI lifecycle tests opt in.
    env.pop("THEOZOLITH_WORKER_TYPE", None)
    for key, value in extra_env.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=timeout)


def _assert_daemon_reaped(pid_file: Path) -> None:
    """The EXIT trap must kill the backgrounded tailscaled on every path."""
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 15)  # do not leak the stub past the test
    raise AssertionError("tailscaled survived flightdeck-start exiting")


def test_flightdeck_start_generation_is_literal_until_runtime(tmp_path, example_config):
    """The generator is one classic-Dockerfile-safe printf; the script it emits
    must carry every runtime expansion UNTOUCHED by the build — the hostname,
    the key path, and the daemon pid expand at container start, never at image
    build (an issue #31 test requirement)."""
    script = _generate_flightdeck_start(tmp_path, example_config).read_text()
    lines = script.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == "set -eu"  # fail-fast: a failed step exits the container
    # Runtime values survived generation literally — a build-time expansion
    # would have resolved $! to nothing and the variables to empty strings.
    assert "TAILSCALED_PID=$!" in lines
    assert '--hostname="$FLIGHTDECK_TS_HOSTNAME"' in script
    assert '--auth-key="file:${TS_AUTHKEY_FILE}"' in script
    # ADR-0045 §4: the baked-model command substitution also survives
    # generation LITERALLY — the model file is read at container start, never
    # expanded into the script at image build — and the whole launch command
    # is one quoted tmux argument, guarded by the file's non-emptiness.
    assert "if [ -s /etc/theozolith/model ]; then" in script
    assert (
        "tmux new-session -d -s flightdeck"
        ' -c "${WORKSPACE_DIR:-/home/ozolith}"'
        ' "claude --model \\"$(cat /etc/theozolith/model)\\""' in script
    )
    # ... and the only variables in the script are its own runtime ones
    # (LANG is the default-only probe of the client-forwarded locale;
    # CLAUDE_CONFIG/tmp are the seed path and its atomic-publish temp;
    # GITHUB_TOKEN_FILE/THEOZOLITH_REPO arrive from the secret machinery and
    # the resolved Stack env, GH_*/WORKSPACE_DIR are derived at start).
    assert set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", script)) == {
        "FLIGHTDECK_TS_HOSTNAME",
        "THEOZOLITH_KNOWLEDGE_TREE",
        "KNOWLEDGE_TREE_DIR",
        "THEOZOLITH_POLICY_TREE",
        "POLICY_TREE_DIR",
        "THEOZOLITH_WORKER_TYPE",
        "CLI_ROOT",
        "CLI_DESIRED",
        "CLI_ENTRY",
        "PATH",
        "entry",
        "CLAUDE_CONFIG",
        "tmp",
        "LANG",
        "GITHUB_TOKEN_FILE",
        "THEOZOLITH_REPO",
        "WORKSPACE_DIR",
        "GH_LOGIN",
        "GH_ID",
        "TS_AUTHKEY_FILE",
        "TS_ENROLL",
        "TS_TRIES",
        "TAILSCALED_PID",
    }
    # Order is the issue #31 lifecycle: the knowledge selection gate and
    # symlinks first (into the read-only mount, ADR-0048 — no clone step
    # exists anymore), then the enrollment decision — read from the
    # Ozolith-owned COMPLETION MARKER, never from tailscaled.state — BEFORE
    # the daemon launches, readiness before `up`, tmux last, and a supervisor
    # — never the removed draft's `exec tmux wait-for`.
    assert "clone-init" not in script
    assert (
        script.index("KNOWLEDGE_TREE_DIR=")
        < script.index("link_knowledge")
        < script.index("POLICY_TREE_DIR=")  # policy wiring before the tailnet path (ADR-0055)
        < script.index("CLAUDE_CONFIG=")  # the seed decides before the tailnet path
        < script.index("gh auth login")  # identity + workspace before the tailnet path
        < script.index("gh repo clone")
        < script.index(".theozolith-enrolled-v1 ]")
        < script.index("tailscaled --tun=userspace-networking")
        < script.index(" up --ssh")
        < script.index("new-session")
        < script.index("has-session")
    )
    # The decision predicate is the final marker name, never the state file or
    # the promotion temp; promotion is atomic (tmp write, then same-volume mv)
    # and strictly AFTER a successful `up` — so no interruption can leave a
    # false success marker, and tailscaled.state is never deleted or rewritten.
    assert "if [ -f /var/lib/tailscale/.theozolith-enrolled-v1 ]" in script
    assert "-s /var/lib/tailscale/tailscaled.state ]" not in script
    # Nothing on the TAILNET-IDENTITY volume is ever deleted; the only rms in
    # the script are the portable symlink replace under ~/.claude (never a
    # real directory — the guard above it refuses those), which GNU ln -sfnT
    # performed implicitly before, the same replace idiom for the managed
    # drop-in link (ADR-0055; a real directory refuses first), and the seed
    # subshell's cleanup trap on its own mktemp file (the published
    # .claude.json itself is never removed).
    for line in lines:
        if "rm " in line:
            assert "tailscale" not in line, line
            assert (
                'rm -f "$2"' in line
                or 'rm -f \\"$tmp\\"' in line
                or "sudo rm -f /etc/claude-code/managed-settings.d" in line
            ), line
    assert (
        script.index(" up --ssh")
        < script.index("> /var/lib/tailscale/.theozolith-enrolled-v1.tmp")
        < script.index("mv /var/lib/tailscale/.theozolith-enrolled-v1.tmp")
        < script.index("new-session")
    )
    assert "wait-for" not in script
    assert lines[-1] == "exit 0"
    # No unbounded loop anywhere: the readiness loop carries its bound, and
    # `up` is never wrapped in a loop.
    for line in lines:
        if " up --ssh" in line:
            assert not line.strip().startswith(("until", "while")), line


def test_flightdeck_start_up_attempts_are_natively_bounded(tmp_path, example_config):
    """`tailscale up` waits for Running state FOREVER by default, so "one
    attempt" alone never guaranteed prompt failure. BOTH up commands — fresh
    enrollment and marker-present reuse — must carry the CLI's NATIVE
    --timeout=30s, and the bound must be that flag, never an external
    `timeout` process wrapping the command."""
    script = _generate_flightdeck_start(tmp_path, example_config).read_text()
    up_commands = [
        line
        for line in script.splitlines()
        if line.strip().startswith("tailscale ") and " up " in line
    ]
    assert len(up_commands) == 2
    for line in up_commands:
        assert "--timeout=30s" in line, line
        # The filter above already proves `tailscale` is the command itself,
        # not an argument to an external `timeout` wrapper.
    assert not any(line.strip().startswith("timeout ") for line in script.splitlines())


def test_flightdeck_start_unavailable_tree_fails_before_the_daemon(tmp_path, example_config):
    """ADR-0048 amendment: the selected knowledge tree is a HARD prerequisite
    — when the node has not converged a distribution carrying it (or the tree
    was retired), the deck fails loud BEFORE tailscaled ever launches, and
    the daemon's reconcile loop owns the retry — recreating the deck on a later
    pass (~heartbeat cadence). Silently starting without skills is exactly what
    this replaces."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    shutil.rmtree(sandbox / "knowledge" / "claude-dev")  # not converged yet
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "not available on this node" in proc.stderr
    assert not daemon_calls.exists()
    assert not (sandbox / "home" / ".claude" / "skills").exists()


def test_flightdeck_start_missing_selection_fails_before_the_daemon(tmp_path, example_config):
    """Without the control-injected THEOZOLITH_KNOWLEDGE_TREE (a deck run
    outside a resolved worker-type Stack), the script refuses to guess."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        THEOZOLITH_KNOWLEDGE_TREE=None,
    )
    assert proc.returncode != 0
    assert "THEOZOLITH_KNOWLEDGE_TREE" in proc.stderr
    assert not daemon_calls.exists()


def test_flightdeck_start_replaces_stale_links_but_refuses_a_real_directory(
    tmp_path, example_config
):
    """The portable replace idiom keeps GNU ln -sfnT's safety: a stale symlink
    or plain file at a ~/.claude destination is replaced, a REAL directory
    (state-volume debris) fails the start loudly instead of being deleted."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")
    claude = sandbox / "home" / ".claude"
    (claude / "skills").symlink_to(sandbox / "elsewhere")  # stale link
    (claude / "CLAUDE.md").write_text("stale file\n")

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    # Both stale entries were relinked into the selected tree before the
    # (deliberately dying) stub daemon failed the container.
    assert daemon_calls.exists()
    assert os.readlink(claude / "skills") == str(sandbox / "knowledge" / "claude-dev" / "skills")
    assert os.readlink(claude / "CLAUDE.md") == str(
        sandbox / "knowledge" / "claude-dev" / "CLAUDE.md"
    )

    (claude / "agents").unlink()
    (claude / "agents").mkdir()  # a real directory must never be deleted
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "refusing to symlink over it" in proc.stderr
    assert (claude / "agents").is_dir() and not (claude / "agents").is_symlink()


def _policy_run(tmp_path, example_config):
    """A sandboxed script plus the minimal env for a run that reaches (and
    passes) the policy wiring before the deliberately dying daemon stub fails
    the container — the policy sections run strictly before tailscaled."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")
    return sandbox, bin_dir, script, daemon_calls, key_file


def test_flightdeck_start_links_managed_settings_into_the_policy_tree(tmp_path, example_config):
    """ADR-0055 §8: the managed drop-in dir becomes a symlink into the
    selected exported tree (via the passwordless-sudo stub), and the drop-in
    reads through it — the path the CLI resolves at launch."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert daemon_calls.exists()  # the wiring completed and the daemon launched
    link = sandbox / "claude-code" / "managed-settings.d"
    assert os.readlink(link) == str(sandbox / "policy" / "claude-defaults")
    assert (link / "attribution.json").read_text() == POLICY_DOC


def test_flightdeck_start_unconverged_policy_tree_fails_before_the_daemon(tmp_path, example_config):
    """ADR-0055 §6: a SELECTED policy tree the node has not converged fails
    the start loudly BEFORE tailscaled launches — the daemon's reconcile loop
    owns the retry, recreating the deck on a later pass (~heartbeat cadence);
    a deck never runs under silently missing policy."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    shutil.rmtree(sandbox / "policy" / "claude-defaults")  # not converged yet
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "policy tree" in proc.stderr and "not available on this node" in proc.stderr
    assert not daemon_calls.exists()
    assert not (sandbox / "claude-code" / "managed-settings.d").exists()


def test_flightdeck_start_without_policy_selection_skips_wiring_and_starts(
    tmp_path, example_config
):
    """A policy-less deck is today's deck: no selection injected, the section
    skips with a logged note, and the start proceeds (here to the
    deliberately dying daemon stub — past the policy section)."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
        THEOZOLITH_POLICY_TREE=None,
    )
    assert daemon_calls.exists()  # the skip is not a failure
    assert "no Agent Policy selected" in proc.stdout + proc.stderr
    assert not (sandbox / "claude-code").exists()  # the wiring never ran


def test_flightdeck_start_refuses_a_real_directory_at_the_managed_settings_path(
    tmp_path, example_config
):
    """A REAL directory at the link destination (image debris, a hand edit)
    is never deleted — same posture as link_knowledge (ADR-0055)."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    real = sandbox / "claude-code" / "managed-settings.d"
    real.mkdir(parents=True)
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "refusing to symlink over it" in proc.stderr
    assert real.is_dir() and not real.is_symlink()
    assert not daemon_calls.exists()


def test_flightdeck_policy_content_edit_lands_on_the_next_launch(tmp_path, example_config):
    """The ADR-0055 lifecycle obligation: after a successful start, a policy
    CONTENT edit exchanged under the mounted parent — exactly the daemon's
    child swap, with the script NOT re-run — is readable through the managed
    drop-in path the CLI resolves at its next launch, with no container-spec
    input (env, volumes, image) having changed."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert daemon_calls.exists()
    link = sandbox / "claude-code" / "managed-settings.d"
    target_before = os.readlink(link)
    assert (link / "attribution.json").read_text() == POLICY_DOC

    # The daemon's exchange: stage a v2 tree, rename the old child aside,
    # rename the staged tree in (parent inode stable, script not re-run).
    parent = sandbox / "policy"
    v2 = '{"attribution": {"sessionUrl": true}}\n'
    staging = parent / ".claude-defaults.tmp"
    staging.mkdir()
    (staging / "attribution.json").write_text(v2)
    os.replace(parent / "claude-defaults", parent / ".claude-defaults.retired")
    os.replace(staging, parent / "claude-defaults")

    # The next `claude` launch resolves the SAME link to the new content —
    # nothing about the container spec changed (the link target path is
    # byte-identical; env/volumes/image are config-level facts pinned by
    # test_configs_example_flightdeck_policy_wiring).
    assert os.readlink(link) == target_before
    assert (link / "attribution.json").read_text() == v2


def test_flightdeck_start_missing_hostname_fails_before_the_daemon(tmp_path, example_config):
    """FLIGHTDECK_TS_HOSTNAME comes from the Stack [env]; without it the
    container fails with a naming message before tailscaled ever launches."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)

    proc = _run_start(script, bin_dir)
    assert proc.returncode != 0
    assert "FLIGHTDECK_TS_HOSTNAME" in proc.stderr
    assert not daemon_calls.exists()


def test_flightdeck_start_missing_authkey_fails_fast_before_the_daemon(tmp_path, example_config):
    """Issue #31 lifecycle point 2: enrollment due (completion marker absent)
    with the auth-key secret absent fails fast with a DISTINCT
    restore-the-mapping message, BEFORE tailscaled launches (point 1) — and a
    non-empty tailscaled.state left by a failed prior attempt must not dodge
    that check: without the marker, the decision is still "enroll"."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    # Debris of a rejected first enrollment: state present, no marker.
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "restore the TS_AUTHKEY binding" in proc.stderr
    assert not daemon_calls.exists()  # decided before launching, not after
    assert not tmux_calls.exists()


def test_flightdeck_start_fresh_enrollment_consumes_the_key_by_path_only(tmp_path, example_config):
    """The success path end-to-end over an empty state volume (a first start,
    or the volume after deliberate state loss — both correctly route to
    enrollment): knowledge symlinks, enrollment via file:$TS_AUTHKEY_FILE
    (the VALUE never enters any argv), atomic completion-marker promotion,
    tmux session with transcript piping — and on a clean session end, exit 0
    with the EXIT trap reaping the daemon (issue #31 lifecycle points 6/7)."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    tmux_calls = _tmux_stub(bin_dir, has_session_code=1)  # session already over

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    home = sandbox / "home"
    # The symlinks target the COMPILED trees on the read-only mount (ADR-0048).
    for link, target in (
        (".claude/skills", "claude-dev/skills"),
        (".claude/agents", "claude-dev/agents"),
        (".claude/workflows", "claude-dev/workflows"),
        (".claude/CLAUDE.md", "claude-dev/CLAUDE.md"),
    ):
        assert os.readlink(home / link) == str(sandbox / "knowledge" / target), link
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--ssh" in up_lines[0]
    assert "--hostname=flightdeck-test" in up_lines[0]
    assert f"--auth-key=file:{key_file}" in up_lines[0]
    # The key VALUE never appears in any recorded argv — path form only.
    for calls in (ts_calls, tmux_calls):
        assert "SECRETVALUE" not in calls.read_text(), calls
    tmux_lines = tmux_calls.read_text().splitlines()
    assert tmux_lines[0].startswith("new-session -d -s flightdeck")
    assert tmux_lines[1].startswith("pipe-pane -o -t flightdeck")
    # Success promoted the completion marker atomically: the final name
    # exists, the promotion temp does not survive.
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_marker_present_reuses_identity_without_the_key(tmp_path, example_config):
    """Issue #31 lifecycle point 1's other branch: a PROMOTED completion
    marker (a successful prior enrollment) routes to `up` WITHOUT the auth
    key — so the remove-the-mapping hardening (no TS_AUTHKEY_FILE at all)
    keeps working for enrolled instances across restarts."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--auth-key" not in up_lines[0]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_seeds_claude_config_and_env(tmp_path, example_config):
    """snow-maker parity in the start script: CLAUDE_CONFIG_DIR and a LANG
    default are exported BEFORE the session launches (the tmux server, and
    with it every window, inherits them), and a FRESH state volume is seeded
    with an onboarding-complete .claude.json — PRIVATE (0600 regardless of
    the caller's umask: after a /login this file is the credential store) —
    while an existing config is NEVER rewritten: a restart over retained
    state preserves it byte-for-byte, mode included."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    # A tmux stand-in that records the environment the server would inherit.
    env_file = bin_dir / "tmux.env"
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'case "$1" in new-session) env > "{env_file}" ;; has-session) exit 1 ;; esac\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    # The widest-open umask the script could inherit: a plain redirection
    # would land the seed world-writable, so the 0600 below proves the
    # mktemp+chmod path, not the process default.
    old_umask = os.umask(0)
    try:
        proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test", LANG=None)
    finally:
        os.umask(old_umask)
    assert proc.returncode == 0, proc.stderr
    session_env = env_file.read_text().splitlines()
    assert f"CLAUDE_CONFIG_DIR={sandbox / 'home' / '.claude'}" in session_env
    assert "LANG=en_US.UTF-8" in session_env
    config = sandbox / "home" / ".claude" / ".claude.json"
    assert config.read_text() == '{"hasCompletedOnboarding": true}\n'
    assert json.loads(config.read_text()) == {"hasCompletedOnboarding": True}
    assert config.stat().st_mode & 0o777 == 0o600
    assert list(config.parent.glob(".claude.json.seed.*")) == []  # temp published, not left
    _assert_daemon_reaped(daemon_pid)

    # Second start over the same volume: the seed runs only when NO path
    # exists, so a config with real credential state survives byte-for-byte
    # with its mode, and a forwarded LANG outranks the default.
    raw = '{"hasCompletedOnboarding": true, "custom": 1}'
    config.write_text(raw)
    config.chmod(0o640)
    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test", LANG="de_DE.UTF-8")
    assert proc.returncode == 0, proc.stderr
    assert config.read_text() == raw
    assert config.stat().st_mode & 0o777 == 0o640
    assert "LANG=de_DE.UTF-8" in env_file.read_text().splitlines()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_claude_seed_preserves_a_zero_byte_config(tmp_path, example_config):
    """A zero-byte .claude.json is an EXISTING regular file (say, truncated
    by an interrupted CLI write), never a fresh volume: the seed must leave
    it exactly alone — the retired `! -s` predicate would have rewritten it."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)
    config = sandbox / "home" / ".claude" / ".claude.json"
    config.touch()
    config.chmod(0o644)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert config.read_bytes() == b""
    assert config.stat().st_mode & 0o777 == 0o644
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_claude_seed_refuses_irregular_paths(tmp_path, example_config):
    """A pre-existing symlink (a credential-write redirection target), a
    dangling symlink, or a directory at the .claude.json path fails the
    start non-zero with a clear message BEFORE tailscaled ever launches —
    never followed, never replaced — and a symlink target is untouched."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    config = sandbox / "home" / ".claude" / ".claude.json"
    target = sandbox / "elsewhere.json"
    target.write_text("innocent bystander")

    def clear() -> None:
        if config.is_symlink():
            config.unlink()
        elif config.is_dir():
            config.rmdir()

    for shape in ("symlink", "dangling-symlink", "directory"):
        clear()
        if shape == "symlink":
            config.symlink_to(target)
        elif shape == "dangling-symlink":
            config.symlink_to(sandbox / "nonexistent")
        else:
            config.mkdir()
        proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
        assert proc.returncode == 1, shape
        assert "is not a regular file" in proc.stderr, shape
    assert target.read_text() == "innocent bystander"  # never written through
    assert not daemon_calls.exists()  # refused before the tailnet lifecycle
    assert not tmux_calls.exists()


def test_flightdeck_start_claude_seed_publication_failure_cleans_up(tmp_path, example_config):
    """An injected publication failure (mv stubbed to fail) exits non-zero
    with the seeding error, creates NO final config, leaves NO temp debris
    (the subshell EXIT trap owns cleanup), and never reaches the tailnet
    lifecycle."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    _stub(bin_dir, "mv", exit_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "seeding" in proc.stderr and "failed" in proc.stderr
    state_dir = sandbox / "home" / ".claude"
    assert not (state_dir / ".claude.json").exists()
    assert list(state_dir.glob(".claude.json.seed.*")) == []
    assert not daemon_calls.exists()


def test_flightdeck_start_baked_model_launches_the_session_with_the_flag(tmp_path, example_config):
    """ADR-0045 §4 end-to-end: with the image-baked model file present, the
    generated script (produced by the REAL worker-type setup command) starts
    the session as `claude --model "claude-fable-5"` — delivered to tmux as
    exactly ONE shell-command argument, so the whole launch command runs in
    the session instead of tmux misparsing the flag as its own — while the
    rest of the lifecycle (enrollment by key path, transcript piping, clean
    daemon reaping) behaves exactly as without a model."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "etc").mkdir()  # the sandboxed /etc/theozolith
    (sandbox / "etc" / "model").write_text("claude-fable-5\n")
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    tmux_calls = _tmux_stub(bin_dir, has_session_code=1)  # session already over

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    invocations = _tmux_invocations(bin_dir)
    assert invocations[0] == [
        "new-session",
        "-d",
        "-s",
        "flightdeck",
        "-c",
        str(sandbox / "home"),  # no workspace bound -> the session opens at home
        'claude --model "claude-fable-5"',
    ]
    assert invocations[1][:4] == ["pipe-pane", "-o", "-t", "flightdeck"]
    assert "transcript.log" in invocations[1][4]
    # The key VALUE stays out of every recorded argv on this branch too.
    assert "SECRETVALUE" not in tmux_calls.read_text()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_absent_model_file_launches_bare_claude(tmp_path, example_config):
    """The compatibility branch (ADR-0045 §4): NO model file in the image —
    a model-less worker type, or a pre-§4 base — must launch exactly `claude`
    as the session command, with no --model flag and no empty argument."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert _tmux_invocations(bin_dir)[0] == [
        "new-session",
        "-d",
        "-s",
        "flightdeck",
        "-c",
        str(sandbox / "home"),
        "claude",
    ]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_empty_model_file_launches_bare_claude(tmp_path, example_config):
    """An EMPTY model file takes the same bare-`claude` branch as an absent
    one (the guard is `-s`, non-empty): a build that materialized nothing
    must never produce `claude --model ""`."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "etc").mkdir()
    (sandbox / "etc" / "model").write_text("")
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert _tmux_invocations(bin_dir)[0] == [
        "new-session",
        "-d",
        "-s",
        "flightdeck",
        "-c",
        str(sandbox / "home"),
        "claude",
    ]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_failed_enrollment_is_retried_with_the_key_not_reused(
    tmp_path, example_config
):
    """The defect the completion marker exists to fix: tailscaled writes its
    machine key BEFORE auth-key registration completes, so a REJECTED first
    enrollment leaves a non-empty tailscaled.state behind. The next start
    over that same state volume must take the enrollment branch again
    (file:$TS_AUTHKEY_FILE — the marker is absent), never the keyless reuse
    branch it cannot recover from; a successful retry then promotes the
    marker."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60", writes_state=sandbox / "tsstate")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=1)  # rejected key
    _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    first = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert first.returncode == 1
    assert "enrollment failed" in first.stderr
    state = sandbox / "tsstate" / "tailscaled.state"
    assert state.read_text()  # the failed attempt left a NON-EMPTY state file
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1").exists()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    _assert_daemon_reaped(daemon_pid)

    # Second start, same state volume, corrected/working key: the absent
    # marker routes to enrollment WITH the key, and success promotes it.
    ts_calls.unlink()
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)
    second = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert second.returncode == 0, second.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert f"--auth-key=file:{key_file}" in up_lines[0]
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_interrupted_promotion_is_not_a_marker(tmp_path, example_config):
    """A leftover promotion temp file — an interruption between `up` success
    and the same-volume mv — must NOT count as enrolled: only the final
    marker name flips the decision, so the next start re-enrolls with the
    reusable key (documented as safe) and re-promotes."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").write_text("enrolled")
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert f"--auth-key=file:{key_file}" in up_lines[0]  # enrollment, not reuse
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_daemon_death_before_ready_fails_promptly(tmp_path, example_config):
    """Issue #31 lifecycle point 3: while waiting for the LocalAPI, a daemon
    that already exited is detected and fails the container — the wait is
    never served out against a corpse, and `up` is never attempted."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")  # reuse branch
    _tailscaled_stub(bin_dir, lifespan=None)  # dies immediately
    ts_calls = _tailscale_stub(bin_dir, status_code=1)  # LocalAPI never answers
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "exited before its LocalAPI answered" in proc.stderr
    assert not any(" up " in c for c in ts_calls.read_text().splitlines())
    assert not tmux_calls.exists()


def test_flightdeck_start_readiness_wait_is_bounded(tmp_path, example_config):
    """Issue #31 lifecycle point 3/4: a live daemon whose LocalAPI never
    answers exhausts a BOUNDED wait and fails the container — no unbounded
    loop, no `up` attempt against a dead socket."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")  # reuse branch
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=1)  # never ready
    _instant_sleep(bin_dir)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "not ready after 30 tries" in proc.stderr
    assert not any(" up " in c for c in ts_calls.read_text().splitlines())
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_enrollment_failure_is_permanent_not_retried(tmp_path, example_config):
    """Issue #31 lifecycle point 4: a failing `tailscale up` (invalid/expired
    key, rejected flags) gets exactly ONE attempt and fails the container
    promptly — the removed draft's invisible `until ... sleep 5` loop must
    never come back. The daemon's reconcile loop owns any retry, recreating the
    deck on a later pass (~heartbeat cadence)."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-EXPIRED\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=1)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "enrollment failed" in proc.stderr
    assert sum(" up " in c for c in ts_calls.read_text().splitlines()) == 1
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_fresh_enrollment_timeout_fails_finitely(tmp_path, example_config):
    """A tailnet that never reaches Running must not hang a fresh enrollment:
    readiness succeeds, the single `up` attempt hits the CLI's native 30s
    bound, and the container exits non-zero within a finite interval — the
    stub blocks any `up` lacking a --timeout flag, so a script that drops the
    bound fails this test's outer deadline. The timeout must prevent marker
    promotion (no final marker, no promotion temp) and tmux startup, retain
    the daemon's state debris for the Docker-owned retry, and reach the EXIT
    trap so tailscaled is reaped."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60", writes_state=sandbox / "tsstate")
    ts_calls = _tailscale_timeout_stub(bin_dir)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(
        script,
        bin_dir,
        timeout=20,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "enrollment failed" in proc.stderr
    assert sum(" up " in c for c in ts_calls.read_text().splitlines()) == 1
    # The daemon's machine-key debris is retained; neither the final marker
    # nor the promotion temp may exist after a timed-out enrollment.
    assert (sandbox / "tsstate" / "tailscaled.state").read_text()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1").exists()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_reuse_timeout_fails_finitely_preserving_identity(
    tmp_path, example_config
):
    """The marker-present branch gets the same native bound: a stalled
    tailnet fails the container within a finite interval after ONE keyless
    `up` attempt, with no destructive recovery — the completion marker and
    tailscaled.state are preserved unchanged for the Docker-owned retry,
    tmux never starts, and the EXIT trap reaps the daemon."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_timeout_stub(bin_dir)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(script, bin_dir, timeout=20, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "up on existing state failed" in proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--auth-key" not in up_lines[0]
    assert (sandbox / "tsstate" / "tailscaled.state").read_text() == "machine-key-material"
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").read_text() == "enrolled"
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_daemon_death_after_start_fails_the_container(tmp_path, example_config):
    """Issue #31 lifecycle point 5: after the session is up, a dying tailscaled
    must fail the container — never a nominally healthy container with dead
    one-hop access (the removed draft's `exec tmux wait-for` defect)."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _tailscaled_stub(bin_dir, lifespan="1")  # dies shortly after startup
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=0)  # the session stays alive
    _instant_sleep(bin_dir)  # the supervisor polls at test speed

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "tailscaled exited" in proc.stderr


def test_flightdeck_start_github_auth_and_first_start_clone(tmp_path, example_config):
    """snow-maker setup.sh parity, end-to-end in a real /bin/sh: with the
    GITHUB_TOKEN secret bound and a workspace resolved, the start
    authenticates gh from the secret FILE (the token rides stdin, never
    argv), wires gh as the git credential helper, derives the machine
    commit identity from the token account, clones the workspace ONCE onto
    the volume, hands the path to interactive shells via ~/.flightdeck-env,
    and opens the tmux session inside the checkout. A second start over the
    same volume re-authenticates but never re-clones."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    token_file = sandbox / "ghtoken"
    token_file.write_text("github_pat_SECRETTOKEN\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)  # session already over
    gh_calls = _gh_stub(bin_dir)
    git_calls = _stub(bin_dir, "git", exit_code=0)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        GITHUB_TOKEN_FILE=str(token_file),
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 0, proc.stderr
    workspace = sandbox / "workspace" / "sandbox"
    gh_lines = gh_calls.read_text().splitlines()
    assert gh_lines == [
        "auth login --with-token",
        "auth setup-git",
        "auth status",
        "api user --jq .login",
        "api user --jq .id",
        f"repo clone acme/sandbox {workspace}",
    ]
    # The token reached gh via STDIN and appears in no recorded argv.
    assert (bin_dir / "gh.token").read_text() == "github_pat_SECRETTOKEN\n"
    assert "SECRETTOKEN" not in gh_calls.read_text()
    assert "SECRETTOKEN" not in git_calls.read_text()
    # Machine-derived commit identity — never a human's.
    git_lines = git_calls.read_text().splitlines()
    assert "config --global user.name testbot" in git_lines
    assert "config --global user.email 12345+testbot@users.noreply.github.com" in git_lines
    assert (workspace / ".git").is_dir()
    env_file = sandbox / "home" / ".flightdeck-env"
    assert env_file.read_text() == f"export WORKSPACE_DIR={workspace}\n"
    assert _tmux_invocations(bin_dir)[0] == [
        "new-session",
        "-d",
        "-s",
        "flightdeck",
        "-c",
        str(workspace),
        "claude",
    ]
    _assert_daemon_reaped(daemon_pid)

    # Second start, same volume: the checkout exists -> auth again, clone
    # never (the session owns the working tree; no automatic fetch either).
    gh_calls.unlink()
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        GITHUB_TOKEN_FILE=str(token_file),
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 0, proc.stderr
    rerun_lines = gh_calls.read_text().splitlines()
    assert "auth login --with-token" in rerun_lines
    assert not any(line.startswith("repo clone") for line in rerun_lines)
    assert not any("fetch" in line or "pull" in line for line in git_calls.read_text().splitlines())
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_workspace_without_token_fails_before_the_daemon(tmp_path, example_config):
    """A bound workspace with no GITHUB_TOKEN secret is a refused
    misconfiguration (the clone could not authenticate): fail loud with a
    restore-the-binding message BEFORE tailscaled ever launches — never a
    deck that silently starts without its repo."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    gh_calls = _gh_stub(bin_dir)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 1
    assert "GITHUB_TOKEN" in proc.stderr and "flightdeck-github-token" in proc.stderr
    assert not gh_calls.exists()  # nothing to authenticate with
    assert not daemon_calls.exists()
    assert not tmux_calls.exists()


def test_flightdeck_start_tokenless_repoless_deck_starts_bare(tmp_path, example_config):
    """With NEITHER binding, the deck starts bare with a logged note: no gh
    invocation at all (no `gh` stub is on PATH — a stray call would fail the
    run loudly), an empty runtime env file, and the session opens at home."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert "no GITHUB_TOKEN bound" in proc.stderr
    assert (sandbox / "home" / ".flightdeck-env").read_text() == ""
    assert _tmux_invocations(bin_dir)[0][4:6] == ["-c", str(sandbox / "home")]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_workspace_debris_is_refused_not_deleted(tmp_path, example_config):
    """Non-checkout debris at the workspace target (volume leftovers, a
    half-removed tree) fails the start loudly BEFORE the tailnet lifecycle —
    never deleted, never cloned over, never silently adopted."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    token_file = sandbox / "ghtoken"
    token_file.write_text("github_pat_x\n")
    debris = sandbox / "workspace" / "sandbox"
    debris.mkdir(parents=True)
    (debris / "leftover.txt").write_text("precious")
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    _gh_stub(bin_dir)
    _stub(bin_dir, "git", exit_code=0)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        GITHUB_TOKEN_FILE=str(token_file),
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 1
    assert "not a git checkout" in proc.stderr
    assert (debris / "leftover.txt").read_text() == "precious"  # untouched
    assert not daemon_calls.exists()
    assert not tmux_calls.exists()


def test_flightdeck_start_failed_auth_or_clone_fails_the_container(tmp_path, example_config):
    """gh failures are container failures (the daemon's reconcile loop owns the
    retry, recreating the deck on a later pass ~heartbeat cadence): a rejected
    token stops the start at auth, a failed clone stops it at the clone — both
    BEFORE tailscaled launches, so a deck never runs half-authenticated or
    half-materialized."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    token_file = sandbox / "ghtoken"
    token_file.write_text("github_pat_x\n")
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    _stub(bin_dir, "git", exit_code=0)

    _gh_stub(bin_dir, auth_code=1)  # rejected token
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        GITHUB_TOKEN_FILE=str(token_file),
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 1
    assert not daemon_calls.exists()

    gh_calls = _gh_stub(bin_dir, clone_code=1)  # auth fine, clone fails
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        GITHUB_TOKEN_FILE=str(token_file),
        THEOZOLITH_REPO="acme/sandbox",
    )
    assert proc.returncode == 1
    assert any(line.startswith("repo clone") for line in gh_calls.read_text().splitlines())
    assert not (sandbox / "workspace" / "sandbox").exists()
    assert not daemon_calls.exists()


# -- repo mirror cache provisioning + retention (#51 amendment) ---------------------


def test_installer_provisions_the_mirror_cache_root():
    """The trusted cache root exists before the service ever starts —
    service-user owned, no group/world write — so the drivers' fail-closed
    runtime validation passes on the happy path."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    line = (
        "install -d -m 0750 -o ozolith -g ozolith /var/tmp/theozolith /var/tmp/theozolith/mirrors"
    )
    assert line in installer
    # Provisioned before the unit is registered or provision could start it.
    assert installer.index(line) < installer.index("systemctl daemon-reload")


def test_cleanup_removes_the_default_mirror_cache_root():
    """Uninstall removes the node-shared scratch root (job dirs + mirror
    cache — full repo history, possibly private refs) only after the daemon
    and its drivers are down, and tells the operator that a custom
    THEOZOLITH_MIRRORS_DIR is theirs to remove separately."""
    readme = (DEPLOY / "README.md").read_text()
    cleanup = readme[readme.index("## Cleanup / deletion test") :]
    assert "sudo rm -rf /var/tmp/theozolith" in cleanup
    assert cleanup.index("systemctl disable --now theozolith-nodedaemon") < cleanup.index(
        "sudo rm -rf /var/tmp/theozolith"
    )
    assert "THEOZOLITH_MIRRORS_DIR" in cleanup


def test_mirror_cache_docs_state_the_retention_and_trust_facts():
    """Operator docs carry the #51 amendment doctrine: caches are never
    backup/restore inputs, they hold repo history (possibly private refs),
    ownership/mode rules are stated, and the timeout knob is documented."""
    readme = (DEPLOY / "README.md").read_text()
    assert "never backed up and never restored" in readme
    assert "may include private refs" in readme
    assert "THEOZOLITH_GIT_TIMEOUT_SECONDS" in readme
    assert "no group/world write" in readme


# -- the CLI Pin launch path (ADR-0055): shim + flightdeck-start, EXECUTED --------


def test_configs_example_flightdeck_cli_wiring(example_config):
    """Config level: the example deck pins the CLI, the pin resolves through
    the injected seam to the full platform map, the resolved Stack carries
    the control-injected selection, and none of it is image identity."""
    from theozolith_worker.adapters import ClaudeAdapter

    wt = example_config.worker_types["flightdeck"]
    assert wt.cli == CLI_PIN_VERSION
    assert wt.cli_version == CLI_PIN_VERSION
    assert set(wt.cli_platforms) == set(ClaudeAdapter.CLI_PLATFORM_PACKAGES)
    recipe = wt.recipe_wire()
    assert recipe["cli_tool"] == "claude" and recipe["cli_version"] == CLI_PIN_VERSION
    flightdeck = next(s for s in example_config.stacks if s.name == "flightdeck")
    assert flightdeck.env["THEOZOLITH_WORKER_TYPE"] == "flightdeck"
    # The example's pin matches the run image's own pinned CLI (one version
    # to reason about) and sits above the adapter's floor.
    dockerfile = DOCKERFILE.read_text()
    assert f"@anthropic-ai/claude-code@{CLI_PIN_VERSION}" in dockerfile
    script = "\n".join(wt.setup)
    assert "DISABLE_AUTOUPDATER=1" in script
    assert "/opt/theozolith-deck/bin/claude" in script


def test_profile_prepends_the_shim_path_only_when_pinned(tmp_path, example_config):
    """The /etc/profile.d block is conditional on the injected selection: a
    pinless shell keeps today's PATH and no DISABLE_AUTOUPDATER; a pinned
    one gets the shim FIRST on PATH and the autoupdater disabled."""
    setup = example_config.worker_types["flightdeck"].setup
    generator = next(s for s in setup if "/etc/profile.d/flightdeck.sh" in s)
    profile = tmp_path / "flightdeck.sh"
    command = generator.replace("/etc/profile.d/flightdeck.sh", str(profile))
    command = command.replace("/etc/bash.bashrc", str(tmp_path / "bash.bashrc"))
    command = command.replace("/home/ozolith/.flightdeck-env", str(tmp_path / "env"))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    pinless = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'set -eu; unset THEOZOLITH_WORKER_TYPE; . "{profile}";'
            ' printf %s "$PATH:${DISABLE_AUTOUPDATER:-unset}"',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "/opt/theozolith-deck/bin" not in pinless.stdout
    assert pinless.stdout.endswith(":unset")
    pinned = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'set -eu; THEOZOLITH_WORKER_TYPE=flightdeck; . "{profile}";'
            ' printf %s "$PATH:${DISABLE_AUTOUPDATER:-unset}"',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert pinned.stdout.startswith("/opt/theozolith-deck/bin:")
    assert pinned.stdout.endswith(":1")


def _generate_deck_shim(tmp_path: Path, config, sandbox: Path) -> Path:
    """Run the shim-baking setup entry in a real /bin/sh with the baked
    destination and the cli store redirected into the sandbox."""
    wt = config.worker_types["flightdeck"]
    generators = [s for s in wt.setup if "/opt/theozolith-deck/bin/claude" in s]
    assert len(generators) == 1
    deck_bin = sandbox / "deck-bin"
    command = generators[0].replace("/opt/theozolith-deck/bin", str(deck_bin))
    command = command.replace("/var/lib/theozolith/cli", str(sandbox / "cli"))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    shim = deck_bin / "claude"
    assert shim.stat().st_mode & 0o111, "the claude shim must be executable"
    return shim


def _cli_version_binary(store: Path, version: str, *, body: str | None = None) -> Path:
    """A fake installed CLI at <store>/claude/<version>/claude, recording its
    argv boundary-preserving into <store>/<version>.argv."""
    record = store / f"{version}.argv"
    target = store / "claude" / version / "claude"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        body
        or (
            "#!/bin/sh\n"
            f'echo "argc $#" >> "{record}"\n'
            f'for a in "$@"; do echo "arg $a" >> "{record}"; done\n'
            "exit 0\n"
        )
    )
    target.chmod(0o755)
    return record


def _cli_records(
    store: Path, *, wt: str = "flightdeck", desired: str | None = None, entry: str | None = None
) -> None:
    """The daemon-maintained pin records: a .desired text record and/or a
    .current relative symlink (ADR-0055 export contract)."""
    by_type = store / "claude" / "by-type"
    by_type.mkdir(parents=True, exist_ok=True)
    if desired is not None:
        (by_type / f"{wt}.desired").write_text(desired + "\n")
    if entry is not None:
        link = by_type / f"{wt}.current"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f"../{entry}")


def _run_shim(shim: Path, *args: str, **env_over):
    env = {**os.environ, "THEOZOLITH_WORKER_TYPE": "flightdeck"}
    for key, value in env_over.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([str(shim), *args], env=env, capture_output=True, text=True)


def test_deck_shim_refuses_every_preconvergence_state_with_no_fallback(tmp_path, example_config):
    """PIN-STRICT at every launch (ADR-0055): a missing selection, a missing
    desired record, a missing entry, and a stale entry each refuse loudly —
    and neither the previous export's binary nor a later-on-PATH decoy
    `claude` is EVER invoked as a fallback."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    store = sandbox / "cli"
    shim = _generate_deck_shim(tmp_path, example_config, sandbox)
    previous = _cli_version_binary(store, "2.1.250")  # the previous export
    decoy_dir = sandbox / "decoy-bin"
    decoy_dir.mkdir()
    decoy_calls = decoy_dir / "claude.calls"
    decoy = decoy_dir / "claude"
    decoy.write_text(f'#!/bin/sh\necho "$@" >> "{decoy_calls}"\nexit 0\n')
    decoy.chmod(0o755)
    path_with_decoy = f"{shim.parent}:{decoy_dir}:{os.environ['PATH']}"

    proc = _run_shim(shim, THEOZOLITH_WORKER_TYPE=None, PATH=path_with_decoy)
    assert proc.returncode != 0 and "THEOZOLITH_WORKER_TYPE" in proc.stderr

    proc = _run_shim(shim, PATH=path_with_decoy)  # no records at all
    assert proc.returncode == 1
    assert "no desired CLI record" in proc.stderr

    _cli_records(store, desired="2.1.257")  # desired, not yet installed
    proc = _run_shim(shim, PATH=path_with_decoy)
    assert proc.returncode == 1
    assert "not converged" in proc.stderr and "desired 2.1.257" in proc.stderr

    _cli_records(store, desired="2.1.257", entry="2.1.250")  # stale entry
    proc = _run_shim(shim, PATH=path_with_decoy)
    assert proc.returncode == 1
    assert "never the previous export, never the image CLI" in proc.stderr
    assert not previous.exists()  # the entry-target binary was never invoked
    assert not decoy_calls.exists()  # ...and neither was the decoy


def test_deck_shim_execs_exactly_the_desired_version_with_argv(tmp_path, example_config):
    """Post-convergence the shim execs the VERSION-ADDRESSED binary with argv
    passed through boundary-preserving, and `claude` on PATH resolves to the
    shim ahead of any later entry."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    store = sandbox / "cli"
    shim = _generate_deck_shim(tmp_path, example_config, sandbox)
    record = _cli_version_binary(store, "2.1.257")
    _cli_records(store, desired="2.1.257", entry="2.1.257")

    proc = _run_shim(shim, "--model", "claude-fable-5", "two words")
    assert proc.returncode == 0, proc.stderr
    assert record.read_text().splitlines() == [
        "argc 3",
        "arg --model",
        "arg claude-fable-5",
        "arg two words",
    ]
    record.unlink()
    # PATH resolution: `claude` finds the shim first (the profile prepend).
    env = {
        **os.environ,
        "THEOZOLITH_WORKER_TYPE": "flightdeck",
        "PATH": f"{shim.parent}:{os.environ['PATH']}",
    }
    proc = subprocess.run(
        ["/bin/sh", "-c", "claude --version-probe"], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "arg --version-probe" in record.read_text()


def test_deck_shim_running_session_survives_a_bump(tmp_path, example_config):
    """ADR-0055 point 6: the shim execs the version-addressed path, so a
    running session holds its binary through a later re-point while the NEXT
    launch gets the new version."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    store = sandbox / "cli"
    shim = _generate_deck_shim(tmp_path, example_config, sandbox)
    pid_file = store / "running.pid"
    _cli_version_binary(
        store,
        "2.1.257",
        body=f'#!/bin/sh\necho $$ > "{pid_file}"\nexec /bin/sleep 60\n',
    )
    _cli_records(store, desired="2.1.257", entry="2.1.257")
    env = {**os.environ, "THEOZOLITH_WORKER_TYPE": "flightdeck"}
    session = subprocess.Popen([str(shim)], env=env)
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            time.sleep(0.05)
        pid = int(pid_file.read_text())

        # The bump: the daemon installs the new version, rewrites desired,
        # and atomically re-points the entry — the running session lives on.
        new_record = _cli_version_binary(store, "2.1.258")
        _cli_records(store, desired="2.1.258", entry="2.1.258")
        os.kill(pid, 0)  # raises if the session died with the re-point
        proc = _run_shim(shim)
        assert proc.returncode == 0, proc.stderr
        assert new_record.exists()  # the next launch ran the NEW version
        os.kill(pid, 0)  # still alive after the new launch too
    finally:
        session.terminate()
        session.wait(timeout=10)


def test_flightdeck_start_unconverged_cli_pin_fails_before_the_daemon(tmp_path, example_config):
    """ADR-0055 §6: a pinned deck whose node has not converged the CLI export
    refuses the container start loudly — before tailscaled, before tmux —
    and the daemon's reconcile loop owns the retry, recreating the deck on a
    later pass (~heartbeat cadence); neither the previous export nor the image
    CLI ever runs."""
    sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
        THEOZOLITH_WORKER_TYPE="flightdeck",
    )
    assert proc.returncode == 1
    assert "no desired CLI record" in proc.stderr
    assert not daemon_calls.exists()

    _cli_records(sandbox / "cli", desired="2.1.257", entry="2.1.250")
    _cli_version_binary(sandbox / "cli", "2.1.250")
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
        THEOZOLITH_WORKER_TYPE="flightdeck",
    )
    assert proc.returncode == 1
    assert "CLI pin not converged" in proc.stderr
    assert "never the previous export, never the image CLI" in proc.stderr
    assert not daemon_calls.exists()


def test_flightdeck_start_pinned_deck_exports_the_launch_path_to_the_session(
    tmp_path, example_config
):
    """With a CONVERGED pin the start proceeds, and the tmux session — and so
    every window — inherits the shim-first PATH and DISABLE_AUTOUPDATER=1;
    the session command still says `claude`, resolved through PATH to the
    shim."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    _cli_version_binary(sandbox / "cli", "2.1.257")
    _cli_records(sandbox / "cli", desired="2.1.257", entry="2.1.257")
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    env_record = bin_dir / "tmux.envprobe"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        f'echo "PATH=$PATH" >> "{env_record}"\n'
        f'echo "DISABLE_AUTOUPDATER=${{DISABLE_AUTOUPDATER:-unset}}" >> "{env_record}"\n'
        'case "$1" in has-session) exit 1 ;; esac\n'
        "exit 0\n"
    )
    tmux.chmod(0o755)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
        THEOZOLITH_WORKER_TYPE="flightdeck",
    )
    assert proc.returncode == 0, proc.stderr
    probe = env_record.read_text()
    assert f"PATH={sandbox / 'deck-bin'}:" in probe
    assert "DISABLE_AUTOUPDATER=1" in probe
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_pinless_deck_keeps_todays_behavior(tmp_path, example_config):
    """Without the injected selection the CLI section skips with a note: no
    PATH prepend, no DISABLE_AUTOUPDATER, the image CLI stays in use, and
    the start proceeds (here to the deliberately dying daemon stub)."""
    _sandbox, bin_dir, script, daemon_calls, key_file = _policy_run(tmp_path, example_config)
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert daemon_calls.exists()  # the skip is not a failure
    assert "no CLI pin for this deck" in proc.stdout + proc.stderr
