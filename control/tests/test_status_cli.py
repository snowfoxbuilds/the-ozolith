"""``theozolith status`` (ADR-0039): degraded-reason precedence, exit
codes 0/1/2, the --json parsing contract, the pure-API-consumer rule (no
subprocess, ever), and the stdlib-only import closure."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from controlrig import ControlRig
from theozolith_control import statuscli

NOW = 1_000_000.0


def _args(**over) -> SimpleNamespace:
    values = {"url": "https://203.0.113.5", "ca": None, "json_output": False}
    values.update(over)
    return SimpleNamespace(**values)


def _environ(tmp_path: Path) -> dict[str, str]:
    return {
        "THEOZOLITH_DATA_DIR": str(tmp_path / "status-home"),
        "THEOZOLITH_ADMIN_TOKEN": "admin-token",
    }


def state_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "now": NOW,
        "nodes": [
            {"name": "box1", "version": "0.3.0", "registered_at": NOW - 900, "last_seen": NOW - 10}
        ],
        "stacks": [
            {
                "node": "box1",
                "name": "worker",
                "kind": "process",
                "state": "stopped",
                "detail": "",
                "updated_at": NOW - 10,
            }
        ],
        "desired_stacks": [
            {"node": "box1", "name": "worker", "kind": "process", "state": "stopped"}
        ],
        "node_health": [],
        "product_pin": "0.3.0",
        "run_containers": [],
        "stack_containers": [],
        "images": [],
        "commands": [],
        "provisioned_nodes": [{"node": "box1", "created_at": NOW - 900}],
        "unregistered_nodes": [],
    }
    doc.update(over)
    return doc


def no_errors() -> dict[str, Any]:
    return {"events": [], "next_cursor": None, "evicted": False}


def _fake_fetch(state: dict[str, Any], errors: dict[str, Any] | None = None):
    def fetch(url: str, path: str, token: str, ca: str | None):
        assert token == "admin-token"
        if path.startswith("/api/v1/state"):
            return state
        assert path.startswith("/api/v1/events?")
        return errors if errors is not None else no_errors()

    return fetch


def _run(tmp_path, state, errors=None, **arg_over) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = statuscli.run(
        _args(**arg_over),
        environ=_environ(tmp_path),
        fetch=_fake_fetch(state, errors),
        out=lines.append,
    )
    return code, lines


# -- healthy and the individual degraded conditions (acceptance 8) ---------------


def test_healthy_stopped_by_desire_exits_zero(tmp_path):
    """Acceptance 5's status slice: a heartbeating node with a Stack
    stopped by desire is healthy — exit 0, no reasons."""
    code, lines = _run(tmp_path, state_doc())
    assert code == 0
    assert lines[0] == "healthy"
    joined = "\n".join(lines)
    assert "box1" in joined and "stopped" in joined


def test_stale_node_degrades(tmp_path):
    state = state_doc()
    state["nodes"][0]["last_seen"] = NOW - 151
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert lines[0].startswith("degraded: node box1 stale")


def test_quarantined_node_degrades(tmp_path):
    state = state_doc(
        node_health=[
            {
                "node": "box1",
                "consecutive_failures": 2,
                "quarantined": 1,
                "reason": "2 consecutive failed Runs",
                "since": NOW - 60,
            }
        ]
    )
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert lines[0].startswith("degraded: node box1 quarantined")


def test_offpin_node_degrades(tmp_path):
    state = state_doc(product_pin="0.4.0")
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert "off-pin" in lines[0] and "0.4.0" in lines[0]


def test_stack_off_desired_state_degrades_both_directions(tmp_path):
    # Desired running, actual stopped.
    state = state_doc(
        desired_stacks=[{"node": "box1", "name": "worker", "kind": "process", "state": "running"}]
    )
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert "off desired state" in lines[0] and "desired running" in lines[0]
    # Desired running, never reported.
    state = state_doc(
        stacks=[],
        desired_stacks=[{"node": "box1", "name": "worker", "kind": "process", "state": "running"}],
    )
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert "not reported" in lines[0]
    # Desired stopped, actual running (a drained-forgotten twin).
    state = state_doc()
    state["stacks"][0]["state"] = "running"
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert "desired stopped, actual running" in lines[0]


def test_recent_errors_degrade(tmp_path):
    errors = {
        "events": [
            {
                "id": 9,
                "type": "theozolith.error",
                "received_at": NOW - 60,
                "node": "box1",
                "component": "node-daemon",
                "payload": {
                    "type": "theozolith.error",
                    "node": "box1",
                    "component": "node-daemon",
                    "error_class": "RuntimeError",
                    "message": "docker died",
                },
            }
        ],
        "next_cursor": None,
        "evicted": False,
    }
    code, lines = _run(tmp_path, state_doc(), errors)
    assert code == 1
    assert "theozolith.error" in lines[0] and "docker died" in lines[0]


def test_degraded_precedence_orders_the_reasons(tmp_path):
    """ADR-0039: quarantined > stale > off-pin > stack-off-desired >
    recent errors > incomplete error history; the first line is the
    highest-precedence reason."""
    state = state_doc(
        product_pin="0.4.0",
        node_health=[{"node": "box1", "quarantined": 1, "reason": "halted", "since": NOW}],
        desired_stacks=[{"node": "box1", "name": "worker", "kind": "process", "state": "running"}],
    )
    state["nodes"][0]["last_seen"] = NOW - 500
    errors = {
        "events": [{"id": 1, "type": "theozolith.error", "received_at": NOW, "payload": {}}],
        "next_cursor": None,
        "evicted": True,
    }
    reasons = statuscli.evaluate(state, errors)
    kinds = [
        "quarantined"
        if "quarantined" in r
        else "stale"
        if "stale" in r
        else "off-pin"
        if "off-pin" in r
        else "stack"
        if "off desired state" in r
        else "incomplete"
        if "incomplete" in r
        else "errors"
        for r in reasons
    ]
    assert kinds == ["quarantined", "stale", "off-pin", "stack", "errors", "incomplete"]
    code, lines = _run(tmp_path, state, errors)
    assert code == 1
    assert lines[0].startswith("degraded: node box1 quarantined")


def test_incomplete_error_history_never_reads_healthy(tmp_path):
    """M8 amendment: an otherwise-healthy fleet whose recent-error window
    reports evicted history is never an unqualified healthy — exit 1 with
    the incompleteness stated explicitly, in the table and in --json."""
    errors = {"events": [], "next_cursor": None, "evicted": True}
    code, lines = _run(tmp_path, state_doc(), errors)
    assert code == 1
    assert lines[0].startswith("degraded: recent-error history is incomplete")
    assert "may miss failures" in lines[0]

    code, lines = _run(tmp_path, state_doc(), errors, json_output=True)
    assert code == 1
    doc = json.loads(lines[0])
    assert doc["status"] == "degraded"  # cannot be mistaken for complete health
    assert any("incomplete" in reason for reason in doc["reasons"])
    assert doc["errors"]["evicted"] is True  # the raw document rides along


# -- coordination surfaces (ADR-0056) --------------------------------------------


def test_coordinating_line_lists_the_bound_workspaces(tmp_path):
    code, lines = _run(tmp_path, state_doc(repos=["acme/app", "acme/infra"]))
    assert code == 0
    assert "coordinating: acme/app, acme/infra" in "\n".join(lines)


def test_no_bound_workspaces_reads_explicitly(tmp_path):
    code, lines = _run(tmp_path, state_doc())  # no repos key
    assert code == 0
    assert "coordinating: (no Bound Workspaces)" in "\n".join(lines)


def test_dispatch_pause_degrades_with_its_repo_and_reason(tmp_path):
    """A per-repo dispatch pause degrades the verdict and prints a paused
    table (ADR-0056), below node-health and above advisory telemetry."""
    state = state_doc(
        repos=["acme/app"],
        dispatch_pauses=[
            {
                "repo": "acme/app",
                "reason": "GitHub 502",
                "first_seen": NOW - 30,
                "last_seen": NOW - 5,
            }
        ],
    )
    reasons = statuscli.evaluate(state, no_errors())
    assert reasons == ["dispatch paused for acme/app: GitHub 502"]
    code, lines = _run(tmp_path, state)
    assert code == 1
    joined = "\n".join(lines)
    assert lines[0] == "degraded: dispatch paused for acme/app: GitHub 502"
    assert "REPO" in joined and "GitHub 502" in joined  # the paused table renders


def test_unbound_obligations_render_without_degrading_the_verdict(tmp_path):
    """Unbinding a repo leaves visible operator-owned obligations (ADR-0056):
    they render in a table but are NOT a health verdict — surfaced, never a
    health downgrade (the janitor writes no GitHub over them either)."""
    state = state_doc(
        repos=["acme/app"],
        unbound_obligations=[
            {
                "kind": "grant",
                "repo": "acme/gone",
                "ref": "acme/gone#7",
                "reason": "pending grant to worker-a awaiting activation",
                "since": NOW - 600,
            }
        ],
    )
    assert statuscli.evaluate(state, no_errors()) == []  # surfaced, not degraded
    code, lines = _run(tmp_path, state)
    assert code == 0  # healthy verdict despite the obligation
    joined = "\n".join(lines)
    assert "unbound coordination obligations" in joined
    assert "acme/gone#7" in joined
    assert "pending grant to worker-a awaiting activation" in joined


# -- exit 2: the read failed (acceptance 8) --------------------------------------


def test_unreachable_prints_target_class_and_hints(tmp_path):
    def refuse(url, path, token, ca):
        raise statuscli.Unreachable(url, "ConnectionRefusedError", "connection refused")

    lines: list[str] = []
    code = statuscli.run(_args(), environ=_environ(tmp_path), fetch=refuse, out=lines.append)
    assert code == 2
    joined = "\n".join(lines)
    assert "https://203.0.113.5" in joined
    assert "ConnectionRefusedError" in joined
    assert "systemctl status theozolith-control.service" in joined
    assert "docker ps" in joined


def test_unreachable_json_shape(tmp_path):
    def refuse(url, path, token, ca):
        raise statuscli.Unreachable(url, "HTTP 401", "admin token required")

    lines: list[str] = []
    code = statuscli.run(
        _args(json_output=True), environ=_environ(tmp_path), fetch=refuse, out=lines.append
    )
    assert code == 2
    doc = json.loads(lines[0])
    assert set(doc) == {"status", "dial_target", "error_class", "hints"}
    assert doc["status"] == "unreachable"
    assert doc["error_class"] == "HTTP 401"


def test_unresolvable_target_exits_two(tmp_path):
    code, lines = _run(tmp_path, state_doc(), url=None)
    assert code == 2
    assert "CONTROL_NODE_URL" in "\n".join(lines)


# -- the --json parsing contract (acceptance 8) ----------------------------------


def test_json_emits_the_raw_documents_verbatim(tmp_path):
    state = state_doc()
    errors = no_errors()
    code, lines = _run(tmp_path, state, errors, json_output=True)
    assert code == 0
    doc = json.loads(lines[0])
    # The documented schema: exactly these keys, raw documents untouched.
    assert set(doc) == {"status", "reasons", "state", "errors"}
    assert doc["status"] == "healthy"
    assert doc["reasons"] == []
    assert doc["state"] == state
    assert doc["errors"] == errors


# -- pure API consumer: no subprocess, ever (acceptance 8) -----------------------


def test_status_makes_no_subprocess_or_system_calls(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("status must never spawn a process (ADR-0039)")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr("os.system", explode)
    code, _ = _run(tmp_path, state_doc())
    assert code == 0


def test_statuscli_source_never_imports_process_machinery():
    """AST-level guard (the nodedaemon check's discipline): statuscli
    imports no subprocess/pty/shutil anywhere, function scope included."""
    source = Path(statuscli.__file__).read_text(encoding="utf-8")
    forbidden = {"subprocess", "shutil", "pty", "pexpect"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots = {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = set() if node.level else {(node.module or "").partition(".")[0]}
        else:
            continue
        assert not roots & forbidden, f"statuscli imports {roots & forbidden}"


# -- stdlib-only (acceptance 9): the import closure ------------------------------


def test_statuscli_import_closure_is_stdlib_only():
    """Importing statuscli in a clean interpreter loads none of control/'s
    dependency exceptions — the whole chain (settings, controltoml, origin,
    worker.config) is stdlib (ADR-0039)."""
    probe = (
        "import sys; import theozolith_control.statuscli;"
        " heavy = sorted({m.split('.')[0] for m in sys.modules} &"
        " {'cryptography', 'fastapi', 'uvicorn', 'jinja2', 'multipart',"
        "  'starlette', 'pydantic', 'httpx'});"
        " raise SystemExit('non-stdlib in status closure: ' + ', '.join(heavy) if heavy else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_statuscli_constants_match_their_sources():
    """The constants statuscli redeclares to stay stdlib-only must track
    the modules that own them."""
    from theozolith_control import tls
    from theozolith_control.web import views

    assert statuscli.CA_FILE_NAME == tls.CA_FILE
    assert statuscli.STALE_AFTER_SECONDS == views.STALE_AFTER_SECONDS


# -- end to end against the real app (schema alignment) --------------------------


def test_status_reads_the_real_state_document(tmp_path, control: ControlRig):
    """The rig round-trip: server-side /api/v1/state and /api/v1/events
    feed evaluate() and the table — a healthy node with a stopped-by-desire
    scaffold Stack exits 0 (M8 acceptance 5)."""
    control.write_config("product.toml", '[product]\nversion = "0.3.0"\n')
    control.write_config(
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\nstate = "stopped"\ncommand = "sleep 30"\n',
    )
    control.heartbeat(
        stacks=[{"name": "worker", "kind": "process", "state": "stopped", "detail": ""}]
    )

    def fetch(url, path, token, ca):
        answer = control.client.get(path, headers={"Authorization": f"Bearer {token}"})
        if answer.status_code >= 400:
            raise statuscli.Unreachable(url, f"HTTP {answer.status_code}", answer.text)
        return answer.json()

    lines: list[str] = []
    code = statuscli.run(_args(), environ=_environ(tmp_path), fetch=fetch, out=lines.append)
    assert code == 0, lines
    assert lines[0] == "healthy"
    joined = "\n".join(lines)
    assert "worker" in joined and "stopped" in joined

    # Quarantine flips it to degraded through the same real documents.
    control.store.record_event(
        {"type": "theozolith.run", "node": "box1", "issue": 1, "run_id": "r1", "phase": "failed"}
    )
    control.store.record_event(
        {"type": "theozolith.run", "node": "box1", "issue": 1, "run_id": "r2", "phase": "failed"}
    )
    lines = []
    code = statuscli.run(_args(), environ=_environ(tmp_path), fetch=fetch, out=lines.append)
    assert code == 1
    assert lines[0].startswith("degraded: node box1 quarantined")


def test_status_reflects_real_eviction_through_the_events_api(tmp_path, control: ControlRig):
    """The rig round-trip for the amendment: status degrades exactly when
    ITS error query is incomplete — a progress-only eviction leaves it
    healthy; an evicted error row inside the window degrades it."""

    def fetch(url, path, token, ca):
        answer = control.client.get(path, headers={"Authorization": f"Bearer {token}"})
        if answer.status_code >= 400:
            raise statuscli.Unreachable(url, f"HTTP {answer.status_code}", answer.text)
        return answer.json()

    def run_status() -> tuple[int, list[str]]:
        lines: list[str] = []
        code = statuscli.run(_args(), environ=_environ(tmp_path), fetch=fetch, out=lines.append)
        return code, lines

    # A recent progress-only eviction: unrelated to the error query —
    # status stays healthy (no false degradation from unrelated eviction).
    control.store.record_event(
        {
            "type": "theozolith.run.progress",
            "worker": "worker-a",
            "node": "box1",
            "issue": 1,
            "run_id": "r1",
            "transcript_tail": "x" * 1000,
        }
    )
    assert control.store.evict_progress(budget_bytes=1) == 1
    code, lines = run_status()
    assert code == 0, lines
    assert lines[0] == "healthy"

    # An evicted theozolith.error row inside the 15-minute window: the
    # error evidence itself is incomplete — degraded, stated explicitly.
    control.store.record_event(
        {
            "type": "theozolith.error",
            "node": "box1",
            "component": "node-daemon",
            "error_class": "RuntimeError",
            "message": "m" * 1500,
            "context": "",
        }
    )
    assert control.store.evict_progress(budget_bytes=1) == 1
    code, lines = run_status()
    assert code == 1
    assert lines[0].startswith("degraded: recent-error history is incomplete")


def test_status_subcommand_is_wired_with_json_flag(tmp_path, monkeypatch, capsys):
    """The CLI carries the subcommand and flag; the handler is statuscli.run."""
    from theozolith_control.cli import main as cli_main

    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["json"] = args.json_output
        return 0

    monkeypatch.setattr(statuscli, "run", fake_run)
    assert cli_main(["status", "--json"]) == 0
    assert seen["json"] is True


# -- CLI Pin convergence rows (ADR-0055) --------------------------------------------


def _cli_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "node": "box1",
        "worker_type": "flightdeck",
        "tool": "claude",
        "desired": "2.1.257",
        "applied": "2.1.250",
        "converged": 0,
        "error_class": "",
        "error_message": "",
        "updated_at": NOW - 10,
    }
    row.update(over)
    return row


def test_nonconverged_cli_pin_degrades_with_the_specified_wording(tmp_path):
    state = state_doc(cli_status=[_cli_row(error_class="CliIntegrityMismatch")])
    code, lines = _run(tmp_path, state)
    assert code == 1
    assert lines[0] == (
        "degraded: worker type flightdeck on box1 cli pin not converged:"
        " desired 2.1.257, applied 2.1.250 (last failure: CliIntegrityMismatch)"
    )
    # No applied version and no recorded failure both render honestly.
    state = state_doc(cli_status=[_cli_row(applied="")])
    _code, lines = _run(tmp_path, state)
    assert "applied none" in lines[0] and "last failure" not in lines[0]


def test_converged_cli_pin_is_healthy_and_renders_the_table(tmp_path):
    state = state_doc(cli_status=[_cli_row(applied="2.1.257", converged=1)])
    code, lines = _run(tmp_path, state)
    assert code == 0
    joined = "\n".join(lines)
    assert "cli pins:" in joined
    header = next(line for line in lines if "WORKER-TYPE" in line)
    assert "NODE" in header and "DESIRED" in header and "LAST FAILURE" in header
    row = next(line for line in lines if "flightdeck" in line)
    assert "2.1.257" in row and " ok" in row
    # No rows -> no table (the section only prints when it has content).
    _code, lines = _run(tmp_path, state_doc())
    assert "cli pins:" not in "\n".join(lines)


def test_cli_pin_reason_precedence_sits_between_offhash_and_stacks(tmp_path):
    """After off-hash, before stack-off-desired (ADR-0055 telemetry)."""
    state = state_doc(
        config_drivers_hash="c" * 64,
        cli_status=[_cli_row(error_class="CliDownloadFailed")],
        desired_stacks=[{"node": "box1", "name": "worker", "kind": "process", "state": "running"}],
    )
    state["nodes"][0]["drivers_hash"] = "d" * 64
    state["nodes"][0]["drivers_hash_reported"] = 1
    reasons = statuscli.evaluate(state, no_errors())
    kinds = [
        "off-hash"
        if "off-hash" in r
        else "cli"
        if "cli pin" in r
        else "stack"
        if "off desired state" in r
        else "other"
        for r in reasons
    ]
    assert kinds == ["off-hash", "cli", "stack"]


def test_json_carries_the_cli_status_rows_verbatim(tmp_path):
    state = state_doc(cli_status=[_cli_row(error_class="CliArchiveInvalid")])
    code, lines = _run(tmp_path, state, json_output=True)
    assert code == 1
    document = json.loads(lines[0])
    assert document["state"]["cli_status"] == state["cli_status"]
    assert any("cli pin not converged" in reason for reason in document["reasons"])
