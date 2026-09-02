"""Fail-closed docker observation + the tmpfs Stack field (#109).

The observation doctrine (NODE-SUBSTRATE.md, grilling 2026-09-02): a failed
docker read is NEVER evidence of absence — a listing raises rather than coerce
to empty, nothing destructive follows a failed read, the heartbeat reports an
unobservable Stack `unknown` with its evidence rows withheld, and every
single-image replacement logs its reason AND emits a namespaced event. The
tmpfs field rides the container spec (fingerprint + `--tmpfs` argv) as node-side
runtime state, conditionally hashed so a tmpfs-less fleet never churns.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
from daemonrig import Rig, container_stack, desired, process_stack
from theozolith_nodedaemon.dockerctl import DockerCtl, DockerError
from theozolith_nodedaemon.stacks import WireStack


def heartbeat_response(stacks, images=None, commands=None) -> dict:
    return {"commands": commands or [], "config": desired(stacks, images)}


def _converge(rig: Rig, *stacks: dict) -> None:
    rig.control.heartbeat_answers.append(heartbeat_response(list(stacks)))
    rig.daemon.once()


def _last_heartbeat(rig: Rig) -> dict:
    beats = [req for _m, path, req, _a in rig.control.transcript if path == "/heartbeats"]
    return beats[-1]


def _replace_events(rig: Rig) -> list[dict]:
    return [e for e in rig.control.events if e.get("type") == "theozolith.stack-replace"]


# -- real DockerCtl: fail-closed listings + the no-size ps format ----------------


def test_dockerctl_ps_raises_on_a_nonzero_exit():
    """A failed ``docker ps`` (a transient dockerd 500, the #109 root cause) is
    never coerced to an empty listing — it RAISES, so no consumer reads it as
    'no containers'. Both label-scoped listings share ``_ps``."""

    def runner(args, timeout=None, env=None):
        return subprocess.CompletedProcess(args, 1, "", "Error response from daemon: 500 ...")

    ctl = DockerCtl(runner=runner)
    with pytest.raises(DockerError):
        ctl.stack_containers("flightdeck")
    with pytest.raises(DockerError):
        ctl.run_containers()


def test_dockerctl_ps_format_requests_no_container_size():
    """The ps argv pins the explicit per-field format, NOT ``{{json .}}``: the
    whole-struct form makes the CLI request ``size=1`` (the racy overlay-snapshot
    walk). No ``.Size`` is referenced anywhere in the argv."""
    calls: list[list[str]] = []

    def runner(args, timeout=None, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.stack_containers("flightdeck")
    (ps,) = [c for c in calls if c[1] == "ps"]
    fmt = ps[ps.index("--format") + 1]
    assert "{{json .}}" not in fmt  # never the whole-struct form
    assert ".Size" not in " ".join(ps)  # nothing requests container size
    for field in ("Names", "State", "Status", "Labels"):
        assert f"{{{{json .{field}}}}}" in fmt  # explicit per-field, same row shape


def test_dockerctl_ps_parses_the_explicit_field_format():
    """The explicit format still parses to the exact row shape consumers read:
    name/state/status plus flattened ``label=value`` pairs."""
    line = json.dumps(
        {
            "Names": "ozolith-stack-flightdeck",
            "State": "running",
            "Status": "Up 3 hours",
            "Labels": "theozolith.stack=flightdeck,theozolith.spec=abc",
        }
    )

    def runner(args, timeout=None, env=None):
        return subprocess.CompletedProcess(args, 0, line + "\n", "")

    ctl = DockerCtl(runner=runner)
    (row,) = ctl.stack_containers("flightdeck")
    assert row["name"] == "ozolith-stack-flightdeck"
    assert row["state"] == "running"
    assert row["status"] == "Up 3 hours"
    assert row["theozolith.stack"] == "flightdeck"
    assert row["theozolith.spec"] == "abc"


def test_dockerctl_compose_ps_raises_on_a_nonzero_exit():
    """``compose_ps`` is fail-closed exactly like ``_ps``."""

    def runner(args, timeout=None, env=None):
        return subprocess.CompletedProcess(args, 1, "", "compose ps failed")

    ctl = DockerCtl(runner=runner)
    with pytest.raises(DockerError):
        ctl.compose_ps("ozolith-flightdeck")


def test_dockerctl_run_stack_container_emits_tmpfs_per_entry():
    """Each tmpfs entry rides the run argv as ``--tmpfs <entry>`` in declared
    order, before the image (docker's own ``--tmpfs`` value syntax)."""
    calls: list[list[str]] = []

    def runner(args, timeout=None, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.run_stack_container(
        "deck",
        "img:1",
        env_files={},
        env={},
        ports=[],
        volumes=["v:/data"],
        tmpfs=["/tmp:size=8g", "/scratch"],
        spec="s",
    )
    run = next(c for c in calls if c[1] == "run")
    idx = [i for i, a in enumerate(run) if a == "--tmpfs"]
    assert [run[i + 1] for i in idx] == ["/tmp:size=8g", "/scratch"]
    assert all(i < run.index("img:1") for i in idx)  # every mount before the image


def test_dockerctl_run_stack_container_without_tmpfs_emits_none():
    """A tmpfs-less run carries no ``--tmpfs`` at all (default behavior)."""
    calls: list[list[str]] = []

    def runner(args, timeout=None, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.run_stack_container("deck", "img:1", env_files={}, env={}, ports=[], volumes=[])
    run = next(c for c in calls if c[1] == "run")
    assert "--tmpfs" not in run


# -- fail-closed convergence: a failed read recreates nothing --------------------


def test_ps_failure_during_converge_does_not_recreate_the_container(rig: Rig):
    """The #109 fix, end to end: a ps failure during a converge pass raises into
    the per-Stack reconcile catch BEFORE any teardown — the running container is
    untouched, the failure is logged and emitted, and the other Stacks in the
    same pass still converge."""
    good = container_stack("flightdeck", image="img:fd")
    other = process_stack("batch", command="my-batch --run", env={})
    _converge(rig, good, other)
    assert "ozolith-stack-flightdeck" in rig.docker.stacks

    rig.docker.removed.clear()
    rig.logs.clear()
    rig.docker.fail_stack_containers.add("flightdeck")  # its ps now fails
    _converge(rig, good, other)

    # The running container is untouched — no removal, no recreate.
    assert "ozolith-stack-flightdeck" not in rig.docker.removed
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == "img:fd"
    # The failure surfaced: reconcile-failed log + a theozolith.error event.
    assert any("stack flightdeck: reconcile failed" in line for line in rig.logs)
    assert any(e.get("error_class") == "DockerError" for e in rig.control.events)
    # The unrelated Stack still converged in the same pass.
    assert rig.daemon._supervisor.alive("batch")


# -- honest heartbeat under a failed observation ---------------------------------


def test_heartbeat_reports_unknown_and_withholds_rows_on_a_failed_listing(rig: Rig):
    """A failed per-Stack listing in the heartbeat path aborts nothing: the
    Stack is reported ``unknown`` with its container-evidence rows withheld
    (attach targets must never resolve from unverified state), the other Stacks
    report normally, and a ``docker-observation-failed`` event names it."""
    good = container_stack("gooddeck", image="img:good")
    failing = container_stack("flightdeck", image="img:fd")
    proc = process_stack("batch", command="my-batch --run", env={})
    _converge(rig, good, failing, proc)  # everything healthy first
    rig.control.transcript.clear()

    rig.docker.fail_stack_containers.add("flightdeck")
    _converge(rig, good, failing, proc)

    beat = _last_heartbeat(rig)
    by_name = {s["name"]: s for s in beat["stacks"]}
    assert by_name["flightdeck"]["state"] == "unknown"  # never stopped/absent
    assert "observation failed" in by_name["flightdeck"]["detail"]
    assert by_name["gooddeck"]["state"] == "running"  # other container normal
    assert by_name["batch"]["state"] == "running"  # process normal
    # Evidence rows withheld for the unobserved Stack, present for the healthy one.
    with_rows = {r["stack"] for r in beat["stack_containers"]}
    assert "flightdeck" not in with_rows
    assert "gooddeck" in with_rows
    obs = [e for e in rig.control.events if e.get("error_class") == "docker-observation-failed"]
    assert any("flightdeck" in e["message"] for e in obs)


def test_heartbeat_reports_empty_run_containers_on_a_failed_listing(rig: Rig):
    """A failed run-container listing keeps the ``run_containers`` key present
    (the channel invariant asserts the exact key set) with an empty value for
    that beat, and surfaces the same ``docker-observation-failed`` event."""
    proc = process_stack("batch", command="my-batch --run", env={})
    rig.docker.fail_run_containers = True
    _converge(rig, proc)

    beat = _last_heartbeat(rig)
    assert "run_containers" in beat  # key stays present
    assert beat["run_containers"] == []
    obs = [e for e in rig.control.events if e.get("error_class") == "docker-observation-failed"]
    assert any("run-container listing failed" in e["message"] for e in obs)


# -- visible replacements: a reason line + a namespaced event every recreate -----


def _compose_stack(name: str, **overrides) -> dict:
    files = [{"name": "compose/base.yml", "content": "services: {}\n"}]
    return container_stack(name, image="", ports=[], compose_files=files, **overrides)


def test_first_create_emits_a_no_running_container_replace_event(rig: Rig):
    _converge(rig, container_stack("flightdeck", image="img:1"))
    (event,) = [e for e in _replace_events(rig) if e["stack"] == "flightdeck"]
    assert event["reason"] == "no-running-container"
    assert event["component"] == "node-daemon"
    assert event["node"] == "box1"


def test_spec_mismatch_emits_a_replace_event_with_old_and_wanted(rig: Rig):
    _converge(rig, container_stack("flightdeck", image="img:1"))
    applied = rig.docker.stacks["ozolith-stack-flightdeck"]["theozolith.spec"]
    rig.control.transcript.clear()
    _converge(rig, container_stack("flightdeck", image="img:2"))
    (event,) = [e for e in _replace_events(rig) if e["stack"] == "flightdeck"]
    assert event["reason"] == "spec-mismatch"
    wanted = rig.docker.stacks["ozolith-stack-flightdeck"]["theozolith.spec"]
    assert applied in event["detail"] and wanted in event["detail"]  # old -> wanted


def test_missing_spec_label_emits_a_replace_event(rig: Rig):
    # A pre-existing running container with no applied-spec label (older daemon,
    # a manual start): reconciled once, and the recreate names the reason.
    rig.docker.stacks["ozolith-stack-flightdeck"] = {
        "stack": "flightdeck",
        "image": "stale:0",
        "state": "running",
    }
    _converge(rig, container_stack("flightdeck", image="img:1"))
    (event,) = [e for e in _replace_events(rig) if e["stack"] == "flightdeck"]
    assert event["reason"] == "missing-spec-label"


def test_compose_to_single_image_emits_a_compose_transition_event(rig: Rig):
    _converge(rig, _compose_stack("svc"))
    rig.control.transcript.clear()
    _converge(rig, container_stack("svc", image="ghcr.io/x/s:1"))
    (event,) = [e for e in _replace_events(rig) if e["stack"] == "svc"]
    assert event["reason"] == "compose-transition"


def test_single_image_to_compose_emits_a_compose_transition_event(rig: Rig):
    _converge(rig, container_stack("svc", image="ghcr.io/x/s:1"))
    rig.control.transcript.clear()
    _converge(rig, _compose_stack("svc"))
    (event,) = [e for e in _replace_events(rig) if e["stack"] == "svc"]
    assert event["reason"] == "compose-transition"


def test_a_healthy_converged_pass_emits_no_replace_event(rig: Rig):
    _converge(rig, container_stack("flightdeck", image="img:1"))
    rig.control.transcript.clear()
    _converge(rig, container_stack("flightdeck", image="img:1"))  # unchanged: no churn
    assert _replace_events(rig) == []


# -- tmpfs in the container fingerprint (conditional, ADR-0052/0055 precedent) ----


def test_tmpfs_change_recreates_the_container_once(rig: Rig):
    """Adopting a tmpfs mount changes the fingerprint and recreates exactly once,
    then stays stable — the quiet-moment recreate that belongs to the rollout."""
    _converge(rig, container_stack("flightdeck", tmpfs=[]))
    rig.docker.removed.clear()
    _converge(rig, container_stack("flightdeck", tmpfs=["/tmp:size=1g"]))
    assert "ozolith-stack-flightdeck" in rig.docker.removed  # recreated once
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["tmpfs"] == ["/tmp:size=1g"]

    rig.docker.removed.clear()
    _converge(rig, container_stack("flightdeck", tmpfs=["/tmp:size=1g"]))
    assert "ozolith-stack-flightdeck" not in rig.docker.removed  # stable thereafter


def test_tmpfs_less_fingerprint_is_byte_identical_to_the_pre_change_value(rig: Rig):
    """A tmpfs-less Stack's fingerprint carries no ``tmpfs`` key: it is the exact
    pre-#109 serialization, so the daemon upgrade alone recreates nothing."""
    stack = WireStack.from_wire(container_stack("flightdeck", image="img:1", ports=["8443:8443"]))
    assert stack.tmpfs == ()
    expected = hashlib.sha256(
        json.dumps(
            {
                "image": stack.image,
                "command": stack.command,
                "env": stack.env,
                "secrets": stack.secrets,
                "ports": list(stack.ports),
                "volumes": list(stack.volumes),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert rig.daemon._container_fingerprint(stack) == expected


def test_tmpfs_fingerprint_key_present_only_when_non_empty(rig: Rig):
    """The ``tmpfs`` key enters the fingerprint only when non-empty, and its
    presence is the sole difference from the tmpfs-less serialization."""
    without = WireStack.from_wire(container_stack("flightdeck", image="img:1", tmpfs=[]))
    withtmp = WireStack.from_wire(
        container_stack("flightdeck", image="img:1", tmpfs=["/tmp:size=1g"])
    )
    assert rig.daemon._container_fingerprint(without) != rig.daemon._container_fingerprint(withtmp)
    expected_with = hashlib.sha256(
        json.dumps(
            {
                "image": withtmp.image,
                "command": withtmp.command,
                "env": withtmp.env,
                "secrets": withtmp.secrets,
                "ports": list(withtmp.ports),
                "volumes": list(withtmp.volumes),
                "tmpfs": ["/tmp:size=1g"],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert rig.daemon._container_fingerprint(withtmp) == expected_with
