"""``init --with-local-node``: the Single-Node Deployment bootstrap (ADR-0037).

After standard init, this module installs the Node Daemon on the same box
and executes the UNMODIFIED join flow end to end internally: mint a join
token through the standard endpoint (explicit loopback addr) → machine-
composed provision line → the freshly started control service answers the
exchange → per-node token minted, join token consumed. The human never
sees the join string, and no second provisioning code path exists: the
provision step is a child process of the installed
``theozolith-nodedaemon`` CLI — byte-for-byte the grammar a human paste
runs.

Loopback falls out of the existing machinery: provision takes its exchange
host from the join-string addr and the port from the listener's
``/control-url``, and the exchange echoes the dialed Host back as the
canonical control URL — so a temporary loopback ``BootstrapServer`` (the
same class serve runs, ADR-0026) makes the local daemon persist
``https://127.0.0.1[:port]``. LAN renumbering never touches the local
node; the loopback IP SAN minted by init (ADR-0036) makes it verify.

The node-side install mirrors ``deploy/install-nodedaemon.sh`` minus the
venv install that already happened (the bare-metal build installs all four
distributions into one venv); a test pins the two unit bodies against
drift.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from theozolith_control.bootstrap import BootstrapServer
from theozolith_control.controltoml import COMMIT_AUTHOR_EMAIL, COMMIT_AUTHOR_NAME
from theozolith_control.settings import ControlSettings
from theozolith_control.tls import CA_FILE

NODE_SERVICE_NAME = "theozolith-nodedaemon.service"
NODE_UNIT_PATH = Path("/etc/systemd/system") / NODE_SERVICE_NAME
NODE_SERVICE_USER = "ozolith"
NODE_STATE_DIR = Path("/var/lib/theozolith")

SERVE_READY_TIMEOUT = 60.0
HEARTBEAT_TIMEOUT = 90.0

SCAFFOLD_FILES = ("stacks/worker.toml", "images/claude-dev.toml", "README.md")

# The base ref of the example worker type. The digest is a deliberate
# placeholder: fetching a real one at init would need a registry round-trip
# on the setup path and would silently age — pinning it is the README's
# explicit first step, an operator act (ADR-0006/0037). Stage-don't-deploy
# guarantees the placeholder can never reach a build while stopped.
SCAFFOLD_BASE_IMAGE = "ghcr.io/snowfoxbuilds/theozolith-run-claude"
PLACEHOLDER_DIGEST = "0" * 64


def render_node_unit(exec_path: str) -> str:
    """The Node Daemon unit — the same body ``install-nodedaemon.sh``
    embeds (drift-tested), with ExecStart parameterized to wherever this
    box's install put the daemon."""
    return f"""[Unit]
Description=TheOzolith Node Daemon (Container-Host: Stacks, drivers, builds, heartbeats)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# Not root: the daemon only needs docker access and its own state dirs.
User={NODE_SERVICE_USER}
Group={NODE_SERVICE_USER}
SupplementaryGroups=docker
ExecStart={exec_path}
KillMode=control-group
Restart=always
RestartSec=10
# /var/lib/theozolith: provisioned identity (control-url, node-token,
# ca.pem, node-name), config cache, materialized compose files (disk).
StateDirectory=theozolith
Environment=THEOZOLITH_STATE_DIR={NODE_STATE_DIR}
# /run/theozolith: secrets tmpfs — values never touch node disk and vanish
# with the daemon (NODE-SUBSTRATE.md).
RuntimeDirectory=theozolith
RuntimeDirectoryMode=0700
Environment=THEOZOLITH_RUNTIME_DIR=/run/theozolith

[Install]
WantedBy=multi-user.target
"""


def ensure_preconditions(*, which=None) -> str:
    """The --with-local-node pre-flight (before any state is written):
    docker and the installed ``theozolith-nodedaemon`` CLI must resolve.
    Returns the daemon executable path; refuses with remediation — a root
    setup path must not start pip-installing on its own (ADR-0037)."""
    import shutil

    which = shutil.which if which is None else which
    if not which("docker"):
        raise SystemExit(
            "error: --with-local-node needs docker on this box (the local node is"
            " a Container-Host) — install docker and re-run"
        )
    exec_path = which("theozolith-nodedaemon")
    if not exec_path:
        raise SystemExit(
            "error: --with-local-node needs the theozolith-nodedaemon CLI in this"
            " environment. The bare-metal build installs all four distributions"
            " into one venv (sudo python3 build.py from a source checkout,"
            " ADR-0041, or pip install theozolith-nodedaemon into the same venv)"
            " — install it and re-run (ADR-0037: a root setup path never"
            " pip-installs on its own)."
        )
    validate = _exec_policy()
    return validate(exec_path)


def _exec_policy():
    """The unit-syntax-safe, world-reachable ExecStart policy — the same
    one the control unit applies (ADR-0034), deferred import to avoid a
    module cycle with cli."""
    from theozolith_control.cli import _UNIT_SAFE_EXEC, _exec_unreachable_reason

    def validate(exec_path: str) -> str:
        path = Path(exec_path).resolve()
        if not _UNIT_SAFE_EXEC.match(str(path)):
            raise SystemExit(
                f"error: theozolith-nodedaemon at {path} contains characters unsafe"
                " for an unquoted systemd ExecStart — install at a plain system"
                " path (e.g. a venv under /opt/theozolith) and re-run"
            )
        reason = _exec_unreachable_reason(path)
        if reason:
            raise SystemExit(
                f"error: theozolith-nodedaemon at {path} is not reachable by the"
                f" {NODE_SERVICE_USER} service user ({reason}) — install at a"
                " system path (e.g. a venv under /opt/theozolith) and re-run"
            )
        return str(path)

    return validate


def _run_step(runner, argv: list[str], what: str) -> None:
    proc = runner(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise SystemExit(f"error: {what} failed ({' '.join(argv[:2])}): {detail}")


def install_node_daemon(
    exec_path: str,
    *,
    runner=subprocess.run,
    unit_path: Path = NODE_UNIT_PATH,
    state_dir: Path = NODE_STATE_DIR,
    log=print,
) -> None:
    """The node-side install, mirroring install-nodedaemon.sh minus the
    venv step: service user in the docker group, state dir, unit,
    daemon-reload. Idempotent — a --force re-run repairs in place."""
    import pwd

    try:
        pwd.getpwnam(NODE_SERVICE_USER)
    except KeyError:
        _run_step(
            runner,
            [
                "useradd",
                "--system",
                "--create-home",
                "--shell",
                "/usr/sbin/nologin",
                NODE_SERVICE_USER,
            ],
            "creating the node service user",
        )
    _run_step(
        runner,
        ["usermod", "-aG", "docker", NODE_SERVICE_USER],
        "adding the service user to the docker group",
    )
    # The daemon state dir, owned by the service user before provision
    # writes into it (provision chowns its files to the dir owner).
    _run_step(
        runner,
        [
            "install",
            "-d",
            "-m",
            "0750",
            "-o",
            NODE_SERVICE_USER,
            "-g",
            NODE_SERVICE_USER,
            str(state_dir),
        ],
        "creating the daemon state dir",
    )
    unit_path.write_text(render_node_unit(exec_path), encoding="utf-8")
    _run_step(runner, ["systemctl", "daemon-reload"], "systemd daemon-reload")
    log(f"installed {NODE_SERVICE_NAME} (user {NODE_SERVICE_USER}, state {state_dir})")


# -- the internal join (the unmodified flow, machine-consumed) -------------------


def _loopback_api(settings: ControlSettings) -> str:
    port = settings.control_port
    return "https://127.0.0.1" if port == 443 else f"https://127.0.0.1:{port}"


def _http(method: str, url: str, *, token: str, ca: str, body: dict | None = None):
    """(status, parsed JSON). The default fetch — loopback TLS verified
    against the freshly minted CA (the loopback IP SAN covers it)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "theozolith-init-local-node",
        },
    )
    context = ssl.create_default_context(cafile=ca)
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode(errors="replace")[:300]}


# The safe-retry contract (ADR-0037): every phase is idempotent or
# reconciled, so an interrupted bootstrap resumes with the same command —
# no --force, no CA rotation, no join string ever shown.
RESUME_HINT = (
    "re-run 'sudo theozolith init --with-local-node' to resume — it reconciles"
    " in place (no --force, no CA rotation, nothing was deleted)"
)

# Liveness evidence must be CURRENT: a node row's `version` is historical
# (the last heartbeat ever), so reconciliation classifies the row by the
# state document's own server clock against the fleet staleness threshold
# (ADR-0022/0039 — the same 150s the dashboard and status use).
FRESH_HEARTBEAT_SECONDS = 150.0

# The provision child's failure diagnostics are ALLOWLISTED classes from
# the ADR-0025 error catalogue: once a join string exists, raw
# stdout/stderr is never reprinted (a diagnostic that quotes argv would
# disclose machine-only join material). Classification beats redaction —
# every message below is a constant, disclosure-free by construction.
PROVISION_FAILURE_CLASSES = (
    ("malformed join string", "malformed join string"),
    ("unsupported join-string version", "unsupported join-string version"),
    ("CA fingerprint mismatch", "CA fingerprint mismatch (possible MITM, or a stale CA)"),
    ("join token rejected", "join token rejected (expired, consumed, or revoked)"),
)


def _provision_failure_class(output: str) -> str:
    for needle, label in PROVISION_FAILURE_CLASSES:
        if needle in output:
            return label
    return "unrecognized failure (the child's output was withheld: it could contain join material)"


# A join-token id rides the revocation URL as a path segment: only an
# allowlisted shape is ever captured (no '/', '.', '%', or anything else a
# hostile response could use to steer the DELETE elsewhere).
_TOKEN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _read_state_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _dial_address_problem(control_url: str, expected_port: int) -> str | None:
    """Why the persisted control URL is not EXACTLY the loopback origin
    serve answers, or None. Structural parsing, never a prefix check: a
    prefix accepts deceptive shapes (`https://127.0.0.1.example.com`,
    `https://127.0.0.1@remote.example`) whose actual host is not loopback
    at all."""
    parts = urlsplit(control_url)
    if parts.scheme != "https":
        return f"persisted dial address {control_url!r} is not https"
    if parts.username or parts.password:
        return f"persisted dial address {control_url!r} carries userinfo"
    try:
        port = parts.port
    except ValueError:
        return f"persisted dial address {control_url!r} has a malformed port"
    if parts.hostname != "127.0.0.1":
        return f"persisted dial address {control_url!r} host is not exactly 127.0.0.1"
    if (port if port is not None else 443) != expected_port:
        return (
            f"persisted dial address {control_url!r} port is not the persisted"
            f" control port {expected_port}"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return f"persisted dial address {control_url!r} carries a path, query, or fragment"
    return None


def _artifact_problem(path: Path, service_uid: int, *, confidential: bool) -> str | None:
    """Why the service user cannot rely on this identity artifact, or
    None: a regular, non-symlink file, owned and owner-readable per the
    installer's contract (and 0600-private for the per-node token)."""
    import stat as stat_module

    if path.is_symlink():
        return f"{path} is a symlink — identity artifacts must be regular files"
    try:
        info = path.stat()
    except OSError:
        return f"missing or unreadable {path}"
    if not stat_module.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_uid != service_uid:
        return (
            f"{path} is not owned by {NODE_SERVICE_USER} — the daemon cannot be"
            " relied on to read it"
        )
    if not info.st_mode & 0o400:
        return f"{path} is not readable by its owner"
    if confidential and info.st_mode & 0o077:
        return f"{path} is group/world-accessible — the per-node token must be private (0600)"
    return None


def local_identity_problem(
    settings: ControlSettings, state_dir: Path, node_name: str
) -> str | None:
    """Why the on-disk daemon identity cannot back the registered local
    node, or None when THE DAEMON CAN USE IT (the provisioned layout,
    ADR-0023). The bar is daemon-usability, not file presence: the control
    URL must parse structurally to exactly the loopback origin serve
    answers on the persisted control port, and every artifact must be a
    regular non-symlink file the ``ozolith`` service user owns and can
    read, in a state dir it can traverse — the row alone proves an
    exchange once happened, not that this box can still be that node."""
    import pwd

    try:
        service = pwd.getpwnam(NODE_SERVICE_USER)
    except KeyError:
        return f"the {NODE_SERVICE_USER!r} service user does not exist on this box"

    control_url = _read_state_file(state_dir / "control-url")
    if not control_url:
        return f"missing or unreadable {state_dir / 'control-url'}"
    dial_problem = _dial_address_problem(control_url, settings.control_port)
    if dial_problem:
        return dial_problem

    # The daemon must be able to reach the state dir at all: the dir is
    # its own (owner-traversable) and every ancestor lets it through.
    if state_dir.is_symlink():
        return f"{state_dir} is a symlink"
    try:
        dir_info = state_dir.stat()
    except OSError:
        return f"missing daemon state dir {state_dir}"
    if dir_info.st_uid != service.pw_uid or not dir_info.st_mode & 0o500:
        return f"{state_dir} is not owned and traversable by {NODE_SERVICE_USER}"
    for parent in state_dir.parents:
        try:
            parent_info = parent.stat()
        except OSError:
            return f"directory {parent} is not statable"
        traversable = bool(parent_info.st_mode & 0o001) or (
            parent_info.st_uid == service.pw_uid and parent_info.st_mode & 0o100
        )
        if not traversable:
            return f"directory {parent} is not traversable by {NODE_SERVICE_USER}"

    for path, confidential in (
        (state_dir / "control-url", False),
        (state_dir / "node-name", False),
        (state_dir / "node-token", True),
        (state_dir / "ca.pem", False),
    ):
        problem = _artifact_problem(path, service.pw_uid, confidential=confidential)
        if problem:
            return problem

    name = _read_state_file(state_dir / "node-name")
    if name != node_name:
        return f"persisted node name {name!r} is not {node_name!r}"
    if not _read_state_file(state_dir / "node-token"):
        return f"missing or empty {state_dir / 'node-token'}"
    if (state_dir / "ca.pem").read_bytes() != (settings.tls_dir / CA_FILE).read_bytes():
        return "the pinned ca.pem is not this deployment's CA (rotated since provisioning?)"
    return None


def _revoke_join_token(fetch, api: str, token: str, ca: str, token_id: str, log) -> None:
    """Best-effort revocation of an unconsumed machine-only token on a
    failed or interrupted join: the token was never shown to anyone, so
    nothing should remain outstanding. Failures are swallowed — the token
    also dies on its own one-hour TTL."""
    try:
        status, _ = fetch("DELETE", f"{api}/api/v1/join-tokens/{token_id}", token=token, ca=ca)
        if status == 200:
            log("local node [join]: revoked the unconsumed join token")
    except Exception:  # best-effort by design
        pass


def bootstrap_local_node(
    settings: ControlSettings,
    *,
    node_name: str,
    nodedaemon_exec: str,
    runner=subprocess.run,
    fetch=_http,
    sleep=time.sleep,
    clock=time.monotonic,
    unit_path: Path = NODE_UNIT_PATH,
    state_dir: Path = NODE_STATE_DIR,
    log=print,
) -> None:
    """The local bootstrap, in explicit phases (ADR-0037) — install,
    start-control, reconcile, join (temporary listener → mint → provision →
    consumption check), heartbeat. Every phase is idempotent or reconciled,
    so a failure at any boundary is retried with the same command; the
    reconcile phase makes an already-provisioned node a no-op and never
    deletes provisioned state. The join string is never logged or
    displayed."""
    from theozolith_control.cli import CONTROL_SERVICE_NAME

    api = _loopback_api(settings)
    ca = str(settings.tls_dir / CA_FILE)
    token = settings.admin_token

    # Phase: install — service user, state dir, unit; idempotent.
    install_node_daemon(
        nodedaemon_exec, runner=runner, unit_path=unit_path, state_dir=state_dir, log=log
    )

    # Phase: start-control — the systemd unit installed by init IS the
    # serve lifecycle (no throwaway in-process server; ADR-0035/0037), and
    # `systemctl start` is idempotent on a running service.
    _run_step(runner, ["systemctl", "start", CONTROL_SERVICE_NAME], "starting the control service")
    log(f"local node [start-control]: {CONTROL_SERVICE_NAME} started; waiting for {api} ...")
    deadline = clock() + SERVE_READY_TIMEOUT
    while True:
        try:
            status, _ = fetch("GET", f"{api}/api/v1/healthz", token=token, ca=ca)
            if status == 200:
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if clock() >= deadline:
            raise SystemExit(
                f"error: the control service did not answer {api} within"
                f" {SERVE_READY_TIMEOUT:.0f}s — check 'systemctl status"
                f" {CONTROL_SERVICE_NAME}' and 'journalctl -u {CONTROL_SERVICE_NAME}';"
                f" then {RESUME_HINT}"
            )
        sleep(0.5)

    # Phase: reconcile — a retry must not re-provision what already
    # exists, and must not TRUST what it cannot prove. Provisioning is
    # registration (ADR-0023): a node row means an exchange once completed
    # — it is trusted only on top of a valid on-disk identity, and
    # "heartbeating" is claimed only from CURRENT evidence (the server's
    # own clock against the fleet staleness threshold), never from the
    # historical `version` column.
    status, state = fetch("GET", f"{api}/api/v1/state", token=token, ca=ca)
    row, server_now = None, 0.0
    if status == 200:
        server_now = float(state.get("now") or 0.0)
        row = next((n for n in state.get("nodes", []) if n.get("name") == node_name), None)
    if row is not None:
        problem = local_identity_problem(settings, state_dir, node_name)
        if problem:
            raise SystemExit(
                f"error: node {node_name!r} is registered on the control plane, but this"
                f" box's daemon identity cannot back it: {problem}.\n"
                f"Restore {state_dir} from this box's own backup, or revoke the stale"
                f" registration (POST /api/v1/nodes/{node_name}/revoke with the admin"
                " token) and re-run 'sudo theozolith init --with-local-node' to"
                " re-provision. Nothing is deleted automatically."
            )
        last_seen = float(row.get("last_seen") or 0.0)
        fresh = (
            bool(row.get("version"))
            and server_now > 0.0
            and (server_now - last_seen) <= FRESH_HEARTBEAT_SECONDS
        )
        if fresh:
            log(
                f"local node [reconcile]: {node_name!r} heartbeat"
                f" {server_now - last_seen:.0f}s ago (fresh) — nothing to do"
            )
            return
        # Registered but not demonstrably alive (never heartbeat, or the
        # last heartbeat is stale). Restart the daemon — never
        # re-provision, never delete the node — and demand fresh proof.
        log(
            f"local node [reconcile]: {node_name!r} is registered but not demonstrably"
            " heartbeating — restarting the daemon"
        )
        _run_step(runner, ["systemctl", "enable", NODE_SERVICE_NAME], "enabling the node daemon")
        _run_step(runner, ["systemctl", "restart", NODE_SERVICE_NAME], "restarting the node daemon")
        _await_heartbeat(
            fetch,
            api,
            token=token,
            ca=ca,
            node_name=node_name,
            baseline=last_seen,
            clock=clock,
            sleep=sleep,
        )
        log(f"local node [heartbeat]: {node_name!r} is registered and heartbeating")
        return

    # Phase: join — the temporary loopback listener (ADR-0037): the same
    # class serve runs, second instance, loopback-only, ephemeral port —
    # it exists so the local daemon fetches the CA and a LOOPBACK control
    # URL, then dies. The production listener keeps answering the LAN
    # unchanged; it is stopped on every exit path.
    listener = BootstrapServer(
        ca_pem=(settings.tls_dir / CA_FILE).read_bytes(),
        origin="",
        control_url=api,
        port=0,
        host="127.0.0.1",
    )
    listener.start()
    minted_id = ""
    try:
        # Mint through the standard endpoint with an explicit loopback
        # addr — the same mint a human's CLI or the dashboard uses. The
        # response body is NEVER interpolated into a diagnostic (it
        # carries the join string); a mint is accepted only when the
        # status, the token id, and the join string are all present, and
        # the id is held separately so cleanup can revoke without the
        # string.
        status, minted = fetch(
            "POST",
            f"{api}/api/v1/join-tokens",
            token=token,
            ca=ca,
            body={"addr": f"127.0.0.1:{listener.port}"},
        )
        # The token id is captured INDEPENDENTLY of HTTP success — any
        # response that returned an identifiable token gets best-effort
        # revocation from the failure handler — and only in the
        # allowlisted shape (it rides the revocation URL).
        candidate = minted.get("id")
        if isinstance(candidate, str) and _TOKEN_ID_RE.fullmatch(candidate):
            minted_id = candidate
        join_candidate = minted.get("join_string")
        join_string = join_candidate if status == 200 and isinstance(join_candidate, str) else ""
        if not (status == 200 and minted_id and join_string):
            raise SystemExit(
                f"error: could not mint a usable local join token (HTTP {status};"
                f" token id {'present' if minted_id else 'MISSING'}, join string"
                f" {'present' if join_string else 'MISSING or unusable'}) — nothing"
                " was provisioned. An identifiable token is revoked on the way out;"
                " for anything unidentifiable the one-hour TTL is the backstop."
                " Check 'journalctl -u theozolith-control.service';"
                f" then {RESUME_HINT}"
            )
        log("local node [join]: token minted (single-use; the join string is machine-consumed)")

        # The provision line, machine-composed and machine-run: the exact
        # grammar a human paste executes, through the installed CLI — the
        # one provisioning implementation. From here on, raw child output
        # is never reprinted: failures surface as allowlisted classes.
        proc = runner(
            [nodedaemon_exec, "provision", join_string, "--node", node_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            failure = _provision_failure_class(f"{proc.stderr or ''}\n{proc.stdout or ''}")
            raise SystemExit(f"error: local node provisioning failed: {failure}\n{RESUME_HINT}")
        log(f"local node [join]: {node_name!r} provisioned (loopback dial address persisted)")

        # The join token must be consumed (single-use, redeemed on
        # exchange).
        status, outstanding = fetch("GET", f"{api}/api/v1/join-tokens", token=token, ca=ca)
        ids = {r.get("id") for r in outstanding.get("tokens", [])}
        if minted_id in ids:
            raise SystemExit(
                "error: the local join token was not consumed — the exchange did"
                f" not complete; check the daemon journal, then {RESUME_HINT}"
            )
    except BaseException:
        # Failed or interrupted anywhere after the mint answered: nothing
        # machine-only may stay outstanding when an id exists to revoke.
        if minted_id:
            _revoke_join_token(fetch, api, token, ca, minted_id, log)
        raise
    finally:
        listener.stop()

    # Phase: heartbeat — demand fresh proof of the daemon's first real
    # heartbeat. A timeout deletes NOTHING: the node stays provisioned and
    # registered, and the retry's reconcile phase restarts the daemon
    # instead of re-joining.
    _await_heartbeat(
        fetch,
        api,
        token=token,
        ca=ca,
        node_name=node_name,
        baseline=0.0,
        clock=clock,
        sleep=sleep,
    )
    log(f"local node [heartbeat]: {node_name!r} is registered and heartbeating")


def _await_heartbeat(fetch, api, *, token, ca, node_name, baseline, clock, sleep) -> None:
    """Success requires PROOF of liveness, never a stale echo: a heartbeat
    (the `version` column is heartbeat-written) whose `last_seen` is at or
    after the reconciliation baseline AND fresh against the fleet
    staleness threshold on the server's own clock. A timeout deletes
    nothing and names the resume command."""
    deadline = clock() + HEARTBEAT_TIMEOUT
    while True:
        status, state = fetch("GET", f"{api}/api/v1/state", token=token, ca=ca)
        if status == 200:
            server_now = float(state.get("now") or 0.0)
            row = next((n for n in state.get("nodes", []) if n.get("name") == node_name), None)
            if row is not None and row.get("version"):
                last_seen = float(row.get("last_seen") or 0.0)
                if (
                    last_seen >= baseline
                    and server_now > 0.0
                    and (server_now - last_seen) <= FRESH_HEARTBEAT_SECONDS
                ):
                    return
        if clock() >= deadline:
            raise SystemExit(
                f"error: local node {node_name!r} is provisioned and registered, but no"
                f" fresh heartbeat arrived within {HEARTBEAT_TIMEOUT:.0f}s — nothing was"
                f" deleted. Check 'journalctl -fu {NODE_SERVICE_NAME}'; then"
                f" {RESUME_HINT}"
            )
        sleep(2.0)


# -- the stage-don't-deploy scaffold (ADR-0037) ----------------------------------


def _product_version() -> str:
    import importlib.metadata

    try:
        # The distribution name (importlib normalizes the underscore).
        return importlib.metadata.version("theozolith_control")
    except importlib.metadata.PackageNotFoundError:
        return "0.3.0"


def _scaffold_stack(node_name: str) -> str:
    return f"""# The staged Implementer worker Stack (ADR-0037 scaffold): complete,
# commented, desired state STOPPED — nothing deploys or builds on first
# boot. The finish line is three steps; see README.md in this repo.

kind = "process"             # a worker driver runs as a supervised daemon child
node = "{node_name}"
state = "stopped"            # step 3: flip to "running" and commit
command = "theozolith-worker"
run_image = "claude-dev"     # images/claude-dev.toml — the derived run image

[env]
# Step 2 (while entering secrets): the GitHub repository (owner/name) the
# Implementer works.
THEOZOLITH_REPO = "you/your-repo"
WORKER_MODEL = "claude-sonnet-5"
WORKER_ID = "worker-{node_name}"

[secrets]
# Secret NAMES only — values live in the encrypted store, never this repo
# (ADR-0024): enter each once with 'sudo theozolith secret set <name>'.
WORKER_GITHUB_TOKEN = "github-worker"
ANTHROPIC_API_KEY = "anthropic-api-key"
"""


def _scaffold_image() -> str:
    version = _product_version().split("+")[0]
    return f"""# The example worker type (ADR-0037): the product's Claude run image
# plus your setup, built into a derived image by the local daemon when the
# Stack flips to running. The base MUST be pinned by digest (ADR-0006) —
# the zeros below are a placeholder; README.md step 1 replaces them.

base = "{SCAFFOLD_BASE_IMAGE}:{version}@sha256:{PLACEHOLDER_DIGEST}"

# Optional: one shell line per image build step.
# setup = [
#     "apt-get update && apt-get install -y --no-install-recommends ripgrep",
# ]

# Optional Knowledge Source: a git repo of skills/subagents/workflows the
# knowledge machinery bakes in at image build time (never at container
# start).
# knowledge_source = "https://github.com/you/your-knowledge.git"
# knowledge_pin = "<commit sha>"
"""


def _scaffold_readme(node_name: str) -> str:
    version = _product_version().split("+")[0]
    ref = f"{SCAFFOLD_BASE_IMAGE}:{version}"
    return f"""# Your Config Repo — staged, not deployed

`theozolith init --with-local-node` seeded this git-backed Config Repo
(ADR-0037) with a complete, commented Implementer worker Stack
(`stacks/worker.toml`) and its worker-type image definition
(`images/claude-dev.toml`) at desired state **stopped**. Nothing runs and
nothing builds on first boot: `theozolith status` shows node `{node_name}`
healthy and the Stack stopped-by-desire.

The finish line, three steps:

1. **Pin the base image digest** in `images/claude-dev.toml` — replace the
   placeholder zeros with the real digest:

       docker pull {ref}
       docker inspect --format '{{{{index .RepoDigests 0}}}}' {ref}

2. **Enter the secrets** the Stack references (values go to the encrypted
   store on the Control Node, never into this repo):

       sudo theozolith secret set github-worker
       sudo theozolith secret set anthropic-api-key

   While you are here, set `THEOZOLITH_REPO` in `stacks/worker.toml` to
   the repository the Implementer works.

3. **Flip desired state and commit**: set `state = "running"` in
   `stacks/worker.toml` and commit. On the next heartbeat the local daemon
   builds the derived image and brings the worker up — `theozolith status`
   exits 0 with the Stack running.

Everything here is ordinary git: edit, commit, done. Secrets never live in
this repo (ADR-0024), and a Stack named `control` is rejected — the
substrate never supervises its own control plane (ADR-0035).
"""


def write_scaffold(
    config_repo: Path, node_name: str, *, runner=subprocess.run, log=print
) -> list[str]:
    """Seed the Config Repo (committed with the machine identity). Existing
    files are never overwritten — a --force re-init keeps operator edits.
    Returns the relative paths written."""
    contents = {
        "stacks/worker.toml": _scaffold_stack(node_name),
        "images/claude-dev.toml": _scaffold_image(),
        "README.md": _scaffold_readme(node_name),
    }
    written: list[str] = []
    for relpath, text in contents.items():
        target = config_repo / relpath
        if target.exists():
            log(f"scaffold: {relpath} exists, keeping it")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(relpath)
    if written and (config_repo / ".git").exists():
        for argv in (
            ["git", "add", *written],
            [
                "git",
                "-c",
                f"user.name={COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                "theozolith: local-node scaffold (staged, stopped; ADR-0037)",
            ],
        ):
            proc = runner(argv, cwd=str(config_repo), capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[:300]
                raise SystemExit(f"error: could not commit the scaffold: {detail}")
        log(f"scaffolded {', '.join(written)} (desired state stopped — see configs/README.md)")
    return written
