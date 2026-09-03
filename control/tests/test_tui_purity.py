"""M9 enforcement: the Operator TUI is a pure API consumer (acceptance 1),
the stdlib components cannot reach it (acceptance 9), the `top` subcommand
rides the one admin resolution path, and the model derivations align with
the REAL server documents end to end."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest
from controlrig import ControlRig
from theozolith_control import statuscli
from theozolith_control import tui as tui_package
from theozolith_control.cli import main as cli_main
from theozolith_control.tui import model

REPO_ROOT = Path(__file__).resolve().parents[2]
TUI_DIR = Path(tui_package.__file__).parent

# The TUI's whole world: stdlib, Textual (+ its Rich), and its own package.
# No store, no secret store, no web surface, no sqlite3, no subprocess, no
# pty — if a datum is not in the two API documents, the TUI cannot have it.
TUI_ALLOWED_ROOTS = set(sys.stdlib_module_names) | {"textual", "rich", "theozolith_control"}


def _imports(source: Path):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.module or ""


def test_tui_module_tree_imports_only_textual_and_its_own_package():
    """Acceptance 1's structural half: every import anywhere under tui/
    (function scope included) is stdlib, textual/rich, or theozolith_control
    **.tui** — the store, the secret store, the web surface, sqlite3,
    subprocess, and pty are unreachable from this tree by construction."""
    sources = sorted(TUI_DIR.rglob("*.py"))
    assert sources, "the tui package went missing"
    for source in sources:
        for module in _imports(source):
            root = module.partition(".")[0]
            assert root in TUI_ALLOWED_ROOTS, f"{source.name}: forbidden import {module!r}"
            if root == "theozolith_control":
                assert module.startswith("theozolith_control.tui"), (
                    f"{source.name}: {module!r} — the TUI may import only its own"
                    " subpackage; every other control module can reach the databases"
                )


def test_tui_sources_never_name_the_database_files():
    """No file reads of cache.db/store.db (acceptance 1) — not even the
    names appear; the data dir layout is invisible from the TUI."""
    for source in sorted(TUI_DIR.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        for name in ("cache.db", "store.db", "master.key", "sqlite"):
            assert name not in text, f"{source.name} mentions {name!r}"


def test_stdlib_components_cannot_import_the_tui_or_textual():
    """Acceptance 9: worker/, nodedaemon/, and knowledge/ stay stdlib-only
    — no source imports textual (or any control module), and no pyproject
    declares it. Their own suites enforce full stdlib closures; this pins
    the M9-specific boundary."""
    for component in ("worker", "nodedaemon", "knowledge"):
        for source in sorted((REPO_ROOT / component / "src").rglob("*.py")):
            for module in _imports(source):
                root = module.partition(".")[0]
                assert root != "textual", f"{component}: {source.name} imports textual"
                assert root != "theozolith_control", (
                    f"{component}: {source.name} imports the control package"
                )
        pyproject = tomllib.loads(
            (REPO_ROOT / component / "pyproject.toml").read_text(encoding="utf-8")
        )
        deps = " ".join(pyproject.get("project", {}).get("dependencies", []))
        assert "textual" not in deps, f"{component}/pyproject.toml declares textual"


def test_textual_is_declared_only_by_control():
    pyproject = tomllib.loads(
        (REPO_ROOT / "control" / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps = [d for d in pyproject["project"]["dependencies"] if d.startswith("textual")]
    assert deps, "control/pyproject.toml must declare the textual exception (ADR-0015)"


# -- the `top` subcommand rides cli._admin_env (the ONE resolution path) --------


def test_top_resolves_url_token_ca_through_the_status_path(monkeypatch):
    seen = {}

    def fake_resolve(url_flag, ca_flag, environ=None):
        seen["flags"] = (url_flag, ca_flag)
        return "https://127.0.0.1:8443", "tok", "/tmp/ca.pem"

    def fake_run_top(url, token, ca):
        seen["target"] = (url, token, ca)
        return 0

    monkeypatch.setattr(statuscli, "resolve_target", fake_resolve)
    monkeypatch.setattr("theozolith_control.tui.app.run_top", fake_run_top)
    assert cli_main(["--url", "https://forwarded:9", "top"]) == 0
    assert seen["flags"] == ("https://forwarded:9", None)
    assert seen["target"] == ("https://127.0.0.1:8443", "tok", "/tmp/ca.pem")


def test_top_refuses_like_every_admin_subcommand_when_unresolvable(monkeypatch):
    def refuse(url_flag, ca_flag, environ=None):
        raise statuscli.TargetError("no admin token")

    monkeypatch.setattr(statuscli, "resolve_target", refuse)
    with pytest.raises(SystemExit) as caught:
        cli_main(["top"])
    assert "no admin token" in str(caught.value)


def test_top_help_documents_panels_keys_and_cadence(capsys):
    """The delegated layout/keybinding/cadence decision is documented in
    --help (M9 brief)."""
    with pytest.raises(SystemExit) as caught:
        cli_main(["top", "--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "panels:" in text and "keys:" in text and "refresh:" in text
    assert "attach" in text and "5s" in text


# -- schema alignment: the model over the REAL server documents -----------------

DECK_TOML = """\
kind = "container"
node = "box1"
image = "ghcr.io/x/deck:1.0@sha256:%s"
attach = ["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "tmux", "attach"]
""" % ("0" * 64)


def test_model_derivations_align_with_the_real_state_document(control: ControlRig):
    """The rig round-trip (the test_status_cli precedent): a real heartbeat
    and Config Repo feed /api/v1/state; the TUI model resolves the attach
    command, stack convergence, and node health from that document alone."""
    control.write_config("stacks/deck.toml", DECK_TOML)
    control.heartbeat(
        stacks=[{"name": "deck", "kind": "container", "state": "running", "detail": ""}],
        stack_containers=[
            {"name": "flight-deck-1", "stack": "deck", "state": "running", "status": "Up"}
        ],
    )
    state = control.admin("GET", "/api/v1/state").json()

    assert model.node_rows(state)[0].health == "ok"
    deck = next(r for r in model.stack_rows(state) if r.name == "deck")
    assert deck.converged and deck.actual == "running"

    result = model.attach_command(state, "box1", "flight-deck-1")
    assert result.ok, result.reason
    assert result.command == "ssh box1 -t docker exec -it flight-deck-1 tmux attach"

    # Stale heartbeat evidence refuses — on the SERVER clock the document
    # itself carries (the rig clock advances; no local time involved).
    control.clock.advance(200)
    stale = control.admin("GET", "/api/v1/state").json()
    refused = model.attach_command(stale, "box1", "flight-deck-1")
    assert not refused.ok and "stale" in refused.reason


def test_model_runs_align_with_the_real_events_documents(control: ControlRig):
    """Run + progress events ingested through the real API reduce to the
    same run detail the dashboard computes server-side — through the
    RunIndex's real cursor walk (small pages force multiple fetches), with
    the worker's failure_class round-tripping the channel verbatim
    (ADR-0040 amendment)."""
    for issue, run_id, phase, extra in (
        (3, "r1", "claimed", {}),
        (3, "r1", "failed", {"failure_class": "timeout"}),
        (7, "r2", "claimed", {"attempt": 2}),
        (7, "r2", "gate", {"attempt": 2}),
    ):
        control.node_post(
            "/api/v1/events",
            {
                "type": "theozolith.run",
                "worker": "worker-a",
                "node": "box1",
                "stack": "worker",
                "repo": "acme/sandbox",
                "issue": issue,
                "run_id": run_id,
                "phase": phase,
                **extra,
            },
        )
    control.node_post(
        "/api/v1/events",
        {
            "type": "theozolith.run.progress",
            "worker": "worker-a",
            "node": "box1",
            "stack": "worker",
            "issue": 7,
            "run_id": "r2",
            "attempt": 2,
            "phase": "agent",
            "elapsed_seconds": 480.0,
            "tool_calls": 44,
            "tokens": 9000,
            "transcript_bytes": 65536,
            "transcript_tail": "...tail...",
        },
    )
    state = control.admin("GET", "/api/v1/state").json()

    def fetch(type_name: str):
        def page(cursor):
            query = f"type={type_name}&limit=2" + (f"&cursor={cursor}" if cursor else "")
            return control.admin("GET", f"/api/v1/events?{query}").json()

        return page

    index = model.RunIndex()
    index.refresh(fetch("theozolith.run"), fetch("theozolith.run.progress"))
    assert not index.truncated
    rows = {r.issue: r for r in model.run_rows(index, state["repo"])}
    assert set(rows) == {3, 7}  # issue 3's latest event sat beyond page one
    live = rows[7]
    assert live.phase == "gate" and not live.terminal
    assert live.tool_calls == 44 and live.transcript_bytes == 65536
    assert live.elapsed_seconds == 480.0
    assert model.timeout_budget_seconds(state, live.stack) == 3600.0
    failed = rows[3]
    assert failed.terminal and failed.phase == "failed"
    assert failed.failure_class == "timeout"  # the channel carries it verbatim
