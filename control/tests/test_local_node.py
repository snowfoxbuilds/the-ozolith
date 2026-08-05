"""``init --with-local-node`` (ADR-0037): pre-flight refusals, the
node-unit drift guard against install-nodedaemon.sh, the internal join
orchestration (standard provision grammar, loopback addr, token consumed,
join string never shown), and the stage-don't-deploy scaffold."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from controlrig import make_settings
from theozolith_control import localnode
from theozolith_control.configrepo import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
NODEDAEMON_EXEC = "/opt/theozolith/bin/theozolith-nodedaemon"


def _ok(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


# -- pre-flight (before any state is written) ------------------------------------


def test_preconditions_require_docker_and_the_daemon_cli():
    with pytest.raises(SystemExit, match="docker"):
        localnode.ensure_preconditions(which=lambda name: None)
    # docker present, daemon CLI missing: refusal with remediation — a root
    # setup path never pip-installs on its own.
    with pytest.raises(SystemExit, match=r"build\.py"):
        localnode.ensure_preconditions(
            which=lambda name: "/usr/bin/docker" if name == "docker" else None
        )


def test_with_local_node_refuses_inside_a_container(tmp_path, monkeypatch):
    from theozolith_control.cli import main as cli_main

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr("theozolith_control.cli._running_in_container", lambda: True)
    with pytest.raises(SystemExit, match="bare-metal root"):
        cli_main(["init", "--ip", "127.0.0.1", "--with-local-node"])
    assert not (tmp_path / "home" / "secrets").exists()  # refused before any state


# -- the node unit: one body, two writers, drift-tested --------------------------


def test_node_unit_matches_the_installer_heredoc():
    """install-nodedaemon.sh (remote boxes) and localnode (the local one)
    write the same unit; ExecStart is the parameterized seam. Drift in
    either direction fails here."""
    installer = (REPO_ROOT / "deploy" / "install-nodedaemon.sh").read_text(encoding="utf-8")
    match = re.search(r"<<'UNIT'\n(.*?)\nUNIT\n", installer, re.DOTALL)
    assert match, "install-nodedaemon.sh no longer embeds the unit heredoc"
    heredoc = match.group(1) + "\n"
    assert localnode.render_node_unit(NODEDAEMON_EXEC) == heredoc


def test_install_node_daemon_lays_down_user_dir_and_unit(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _ok()

    unit_path = tmp_path / "theozolith-nodedaemon.service"
    localnode.install_node_daemon(
        NODEDAEMON_EXEC,
        runner=runner,
        unit_path=unit_path,
        state_dir=tmp_path / "state",
        log=lambda _: None,
    )
    assert calls[0][:2] == ["useradd", "--system"]
    assert ["usermod", "-aG", "docker", "ozolith"] in calls
    assert any(c[0] == "install" and str(tmp_path / "state") in c for c in calls)
    assert calls[-1] == ["systemctl", "daemon-reload"]
    unit = unit_path.read_text()
    assert f"ExecStart={NODEDAEMON_EXEC}" in unit
    assert "KillMode=control-group" in unit


# -- the internal join: unmodified flow, machine-consumed ------------------------


class Harness:
    """Fake runner + API for bootstrap_local_node: records everything and
    answers like a healthy serve + successful exchange."""

    def __init__(self, tmp_path, *, consume_token: bool = True, provision_rc: int = 0):
        self.settings = make_settings(tmp_path)
        self.settings.tls_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.tls_dir / "ca.pem").write_bytes(b"--fake ca pem--")
        self.calls: list[list[str]] = []
        self.fetches: list[tuple[str, str, dict | None]] = []
        self.logs: list[str] = []
        self.minted_addr = ""
        self.consume_token = consume_token
        self.provision_rc = provision_rc

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1:2] == ["provision"]:
            return SimpleNamespace(
                returncode=self.provision_rc, stdout="", stderr="fingerprint mismatch"
            )
        return _ok()

    def fetch(self, method, url, *, token, ca, body=None):
        assert token == self.settings.admin_token
        self.fetches.append((method, url, body))
        if url.endswith("/api/v1/healthz"):
            return 200, {"ok": True}
        if method == "POST" and url.endswith("/api/v1/join-tokens"):
            self.minted_addr = body["addr"]
            return 200, {"id": "tok-1", "join_string": "ozjoin1:MACHINE-ONLY"}
        if method == "GET" and url.endswith("/api/v1/join-tokens"):
            tokens = [] if self.consume_token else [{"id": "tok-1"}]
            return 200, {"tokens": tokens}
        if url.endswith("/api/v1/state"):
            return 200, {"nodes": [{"name": "localbox", "version": "0.3.0"}]}
        raise AssertionError(f"unexpected fetch: {method} {url}")

    def bootstrap(self, tmp_path):
        localnode.bootstrap_local_node(
            self.settings,
            node_name="localbox",
            nodedaemon_exec=NODEDAEMON_EXEC,
            runner=self.runner,
            fetch=self.fetch,
            sleep=lambda seconds: None,
            unit_path=tmp_path / "nodedaemon.service",
            state_dir=tmp_path / "state",
            log=self.logs.append,
        )


def test_bootstrap_runs_the_standard_provision_grammar(tmp_path, monkeypatch):
    """M8 acceptance 1: the internal flow starts the real service, mints
    the join token through the standard endpoint with a loopback addr, and
    invokes the standard provision implementation — the installed CLI's
    exact grammar, no reimplementation. The join string is never shown."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)
    harness.bootstrap(tmp_path)

    # Early serve start via the systemd unit init installed (ADR-0037).
    start_index = harness.calls.index(["systemctl", "start", "theozolith-control.service"])
    provision_call = next(c for c in harness.calls if c[1:2] == ["provision"])
    assert provision_call == [
        NODEDAEMON_EXEC,
        "provision",
        "ozjoin1:MACHINE-ONLY",
        "--node",
        "localbox",
    ]
    assert harness.calls.index(provision_call) > start_index

    # The join-string addr is the temporary loopback listener.
    host, _, port = harness.minted_addr.partition(":")
    assert host == "127.0.0.1" and int(port) > 0

    # Minted and consumed was verified through the standard endpoints.
    methods = [(m, u.rsplit("/", 1)[-1].split("?")[0]) for m, u, _ in harness.fetches]
    assert ("POST", "join-tokens") in methods
    assert ("GET", "join-tokens") in methods

    # The human never sees the join string (acceptance 1).
    assert all("ozjoin1:" not in line for line in harness.logs)
    assert any("registered and heartbeating" in line for line in harness.logs)


def test_bootstrap_fails_loud_when_the_token_survives(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, consume_token=False)
    with pytest.raises(SystemExit, match="not consumed"):
        harness.bootstrap(tmp_path)


def test_bootstrap_surfaces_a_provision_failure(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, provision_rc=1)
    with pytest.raises(SystemExit, match="fingerprint mismatch"):
        harness.bootstrap(tmp_path)


def test_bootstrap_times_out_when_serve_never_answers(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)

    def dead_fetch(method, url, *, token, ca, body=None):
        raise OSError("connection refused")

    ticks = iter([0.0, 0.0, 1000.0, 2000.0, 3000.0])
    with pytest.raises(SystemExit, match="systemctl status"):
        localnode.bootstrap_local_node(
            harness.settings,
            node_name="localbox",
            nodedaemon_exec=NODEDAEMON_EXEC,
            runner=harness.runner,
            fetch=dead_fetch,
            sleep=lambda seconds: None,
            clock=lambda: next(ticks),
            unit_path=tmp_path / "nodedaemon.service",
            state_dir=tmp_path / "state",
            log=harness.logs.append,
        )


# -- the scaffold: complete, commented, stopped (acceptance 5) -------------------


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
    ).stdout


def test_scaffold_is_complete_staged_and_committed(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    written = localnode.write_scaffold(repo, "localbox", log=lambda _: None)
    assert sorted(written) == ["README.md", "images/claude-dev.toml", "stacks/worker.toml"]

    # The scaffold parses under the real validator, staged stopped.
    config = load_config(repo)
    worker = next(s for s in config.stacks if s.name == "worker")
    assert worker.kind == "process" and worker.node == "localbox"
    assert worker.state == "stopped"
    assert worker.run_image == "claude-dev"
    assert set(worker.secrets.values()) == {"github-worker", "anthropic-api-key"}
    assert "claude-dev" in config.images  # placeholder digest passes validation

    # Stage-don't-deploy (ADR-0037): nothing rides desired state to build.
    assert config.desired_state_for("localbox")["images"] == []

    # Committed with the machine identity, like every machine write.
    assert _git(repo, "log", "--format=%an").strip() == "theozolith"
    assert "scaffold" in _git(repo, "log", "--format=%s")
    assert _git(repo, "status", "--porcelain").strip() == ""

    # The README names the finish line: digest pin, secrets, the flip.
    readme = (repo / "README.md").read_text()
    assert "docker inspect" in readme
    assert "theozolith secret set" in readme
    assert 'state = "running"' in readme
    assert "theozolith status" in readme


def test_scaffold_never_overwrites_operator_edits(tmp_path):
    repo = tmp_path / "configs"
    (repo / "stacks").mkdir(parents=True)
    edited = 'kind = "process"\nnode = "elsewhere"\ncommand = "theozolith-worker"\n'
    (repo / "stacks" / "worker.toml").write_text(edited)
    written = localnode.write_scaffold(repo, "localbox", log=lambda _: None)
    assert "stacks/worker.toml" not in written
    assert (repo / "stacks" / "worker.toml").read_text() == edited
