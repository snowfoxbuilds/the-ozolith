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
