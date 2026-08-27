"""The ``theozolith`` CLI: every human command on the Control Node (ADR-0032).

One surface, two halves. The service-admin half operates on this box's local
state (ADR-0023) — ``init``, ``tls-init``, ``serve``, ``recover`` — plus
local maintenance (``set-password``, ``rotate-key``, ``janitor --once``) and
HTTP-driven operator subcommands. The fleet-operator half (``update``,
``build``, ``test``, ``join-token``) is registered from ``product`` — that
module stays stdlib-only at import for the build.py bootstrap (ADR-0030),
so the merged parser lives here.
``theozolith-control`` is a deprecated alias for the same entry point.

``init`` is the unified first-run command (root-mediated on bare metal,
ADR-0034; machine surface only since ADR-0036): master key → admin bearer
token → control address → CA/TLS (IP SANs: the box's IP + loopback) →
systemd unit → operator handoff. No browser credential is taken:
``origin-init`` is the opt-in browser step, prompting for the browser
origin and the admin password together and re-minting the server
certificate with the origin host in the SAN. Run as root, all state lands
under ``/var/lib/theozolith-control/`` (the ADR-0024 partition at a system
path) and the admin subcommands run under ``sudo``; unprivileged and
containerized runs keep ``~/.theozolith/``. ``recover`` validates a
restored copy loudly and re-mints the server certificate from the restored
CA — a same-IP restore reconnects the fleet untouched (nodes dial the
persisted control IP directly; ADR-0023 as amended 2026-07-28).

Secret entry happens here and through the dashboard's web form — both write
through the same PUT /api/v1/secrets/{name} API to the same encrypted store
(NODE-SUBSTRATE.md). The HTTP subcommands take the admin token from
THEOZOLITH_ADMIN_TOKEN or the init-written ``secrets/admin-token`` file, and
the server URL from CONTROL_NODE_URL or the persisted control address.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import secrets as _secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from theozolith_worker.config import ConfigError, env_value

from theozolith_control import (
    bearerhttp,
    bootstrap,
    configrepo,
    controltoml,
    ingest,
    janitor,
    localnode,
    migrate,
    origin,
    passwords,
    product,
    statuscli,
    tls,
)
from theozolith_control.crypto import CryptoError, SecretBox, ensure_key_file, generate_key
from theozolith_control.origin import OriginError
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings, load_settings
from theozolith_control.store import Store
from theozolith_control.tls import CA_FILE, CA_KEY_FILE, CERT_FILE, KEY_FILE, provision


def _log(message: str) -> None:
    print(message, flush=True)


# -- HTTP plumbing for the operator subcommands --------------------------------


def _call(
    url: str,
    path: str,
    *,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    ca: str | None = None,
) -> Any:
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "theozolith-cli",
        },
    )
    try:
        _status, raw = bearerhttp.open_bearer(request, ca=ca, timeout=30)
        return json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"error: HTTP {exc.code} from {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}") from exc
    except bearerhttp.BearerTransportError as exc:
        raise SystemExit(f"error: {exc}") from exc


def _admin_env(args) -> tuple[str, str, str | None]:
    """(url, admin token, ca) for the HTTP subcommands. Everything falls
    back to init's artifacts on this box — on the Control Node itself the
    commands work with no environment at all (`.env` is gone). One
    implementation: statuscli.resolve_target (ADR-0039); this wrapper only
    converts the refusal into the CLI error contract."""
    try:
        return statuscli.resolve_target(args.url, args.ca)
    except statuscli.TargetError as exc:
        raise SystemExit(f"error: {exc}") from exc


# -- serve ---------------------------------------------------------------------


EVICTION_EVERY_SECONDS = 3600.0  # scanning progress payloads is not a per-minute job


def _sweep_pass(settings: ControlSettings, store: Store, client, *, evict: bool = True) -> None:
    """One janitor pass: zombie escalation, never-activated grant release,
    the base-drift lane (ADR-0053), and (on its slower cadence)
    progress-telemetry eviction (ADR-0016/0017)."""
    janitor.sweep(store, client, grace_seconds=settings.zombie_grace_seconds, log=_log)
    janitor.release_never_activated(
        store, client, window_seconds=settings.activation_window_seconds, log=_log
    )
    janitor.sweep_base_drift(store, client, log=_log)
    if evict:
        evicted = store.evict_progress(settings.tail_budget_bytes)
        if evicted:
            _log(f"evicted {evicted} progress event(s) past the tail budget (cache, not archive)")


def _sweep_loop(settings: ControlSettings, store: Store, stop: threading.Event) -> None:
    """The janitor on its cadence, in one thread (ADR-0015)."""
    import time as _time

    from theozolith_worker.githubapi import GitHubClient

    client = GitHubClient(settings.repo or "", settings.github_token or "", settings.api_url)
    next_eviction = 0.0
    while not stop.wait(settings.janitor_sweep_seconds):
        evict = _time.monotonic() >= next_eviction
        if evict:
            next_eviction = _time.monotonic() + EVICTION_EVERY_SECONDS
        try:
            _sweep_pass(settings, store, client, evict=evict)
        except Exception as exc:
            _log(f"janitor sweep failed: {exc}")


def _serve(args) -> int:
    import dataclasses

    import uvicorn

    from theozolith_control.app import create_app

    settings = load_settings()
    cert = settings.tls_dir / CERT_FILE
    key = settings.tls_dir / KEY_FILE
    tls = cert.is_file() and key.is_file()
    if not tls and not args.insecure_dev:
        raise SystemExit(
            f"error: no TLS material at {settings.tls_dir} — run 'theozolith tls-init"
            " --host <ip>' first (TLS is mandatory; --insecure-dev for local dev only)"
        )
    control_address = None
    if not args.insecure_dev or settings.control_ip:
        # Production requires the persisted control address (ADR-0034): it
        # is the machine channel every node and mint surface dials. It is
        # independent of --host/--port (the bind address); a
        # persisted-but-invalid address fails closed in dev too.
        try:
            control_address = origin.derive_origin(settings.control_ip, settings.control_port)
        except OriginError as exc:
            raise SystemExit(
                f"error: {exc} — production startup requires the persisted control"
                " address (run 'theozolith init'; --insecure-dev for local dev only)"
            ) from exc
    # The browser surface is lazy (ADR-0036): no admin password is required
    # to serve. A persisted browser origin arms the guard in mount_web; a
    # persisted-but-malformed one fails closed here.
    browser_origin = None
    if settings.browser_origin:
        try:
            browser_origin = origin.parse_browser_origin(settings.browser_origin)
        except OriginError as exc:
            raise SystemExit(
                f"error: persisted browser origin is invalid: {exc} — re-run"
                " 'theozolith origin-init --force'"
            ) from exc
    settings = dataclasses.replace(settings, secrets_channel_ok=True, serve_tls=tls)
    # A running fleet always has a recorded version (ADR-0015, 2026-07-22):
    # a fresh install with no product.toml pin resolves the latest release
    # and writes the pin. Best-effort at startup — an unreachable release
    # index must never keep the Control Node down.
    try:
        pinned = product.ensure_pin(settings.config_repo, log=_log)
        _log(f"product version pin: {pinned}")
    except Exception as exc:
        _log(f"product pin not resolved yet ({exc}); run 'theozolith update' to pin")
    store = Store(settings.cache_db_path)
    secret_store = SecretStore(settings.store_db_path)
    box = SecretBox(
        env_value(os.environ, "THEOZOLITH_MASTER_KEY") or ensure_key_file(settings.key_path)
    )
    app = create_app(settings, store, secret_store, box)

    # The plaintext bootstrap listener (ADR-0023): CA cert, browser origin,
    # and the IP-based control URL, on its own port — never on the HTTPS
    # app. It exists to serve provisioning, so it runs exactly when a CA
    # does. /control-url must agree with the join exchange's answer; since
    # ADR-0034 /origin carries the same IP-based URL (browsers and nodes
    # dial one address — the route stays for join-string compatibility).
    bootstrap_server = None
    ca_path = settings.tls_dir / CA_FILE
    if ca_path.is_file():
        bootstrap_server = bootstrap.BootstrapServer(
            ca_pem=ca_path.read_bytes(),
            origin=browser_origin.origin if browser_origin else "",
            control_url=_node_control_url(settings),
            port=settings.bootstrap_port,
        )
        bootstrap_server.start()
        _log(f"bootstrap listener on port {bootstrap_server.port} (CA cert, origin, control URL)")

    stop = threading.Event()
    if settings.coordination_jobs_enabled:
        threading.Thread(
            target=_sweep_loop, args=(settings, store, stop), daemon=True, name="sweeps"
        ).start()
        _log(f"claim dispatch + janitor active on {settings.repo}")
    else:
        _log(
            "claim dispatch + janitor DISABLED — the pipeline pauses"
            " (set THEOZOLITH_REPO and CONTROL_GITHUB_TOKEN; ADR-0017)"
        )

    _log(f"control node bound to {args.host}:{args.port} (TLS {'on' if tls else 'OFF — dev mode'})")
    if browser_origin is not None:
        _log(f"browser origin (exact Host/Origin): {browser_origin.origin}")
    elif tls:
        _log("browser surface disabled — enable it with 'theozolith origin-init' (ADR-0036)")
    # The mismatch fails loud (ADR-0034): outside a container nothing can
    # be mapping the external port onto this bind, so a bind that differs
    # from the persisted external port means browsers and nodes dial a
    # port nobody answers — the exact silent failure that prompted the
    # ADR. In the compose flow the 443:8443 mapping is the bridge, and a
    # container bind is never the external truth.
    if (
        control_address is not None
        and not _running_in_container()
        and args.port != settings.control_port
    ):
        _log(
            f"WARNING: bound to port {args.port}, but the persisted external"
            f" port is {settings.control_port} — every browser and node dials"
            f" {control_address.origin}. Bare metal: use the systemd unit"
            f" (binds --port {settings.control_port} directly), or re-run"
            " 'theozolith init --force --port <port>' to re-point the fleet."
        )
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            ssl_certfile=str(cert) if tls else None,
            ssl_keyfile=str(key) if tls else None,
        )
    finally:
        stop.set()
        if bootstrap_server is not None:
            bootstrap_server.stop()
    return 0


# -- local maintenance ----------------------------------------------------------


def _node_control_url(settings: ControlSettings) -> str:
    """The IP-based URL nodes dial (ADR-0023 § node channel addressing):
    the persisted control IP + external https port. Empty until init has
    persisted the IP. One implementation, in statuscli (ADR-0039)."""
    return statuscli.control_url(settings)


def _running_in_container() -> bool:
    """True inside docker/podman — where auto-detected addresses are the
    container bridge IP, unreachable from the LAN (ADR-0031)."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


# -- the unified first run (ADR-0023, root-mediated per ADR-0034) ---------------


def _prompt_password(args) -> str:
    """The admin password, confirmed. A piped stdin (scripts, tests) is
    read once; a terminal prompts twice, never echoing."""
    if not sys.stdin.isatty():
        password = sys.stdin.readline().strip()
        if not password:
            raise SystemExit("error: empty admin password on stdin")
        return password
    while True:
        password = getpass.getpass("admin password (browser login): ")
        if not password:
            print("empty password refused; try again", flush=True)
            continue
        if getpass.getpass("repeat: ") == password:
            return password
        print("passwords do not match; try again", flush=True)


def _write_private(path: Path, text: str) -> None:
    """One 0600 credential file, written atomically: the record either
    keeps its previous content or carries the complete new one — a crash
    can never leave a partial credential (ADR-0036). Ownership follows the
    parent directory's owner, so a sudo-run subcommand (origin-init,
    set-password) hands the file straight to the service user whose
    partition it lands in (ADR-0034) instead of stranding a root-owned
    record the service cannot read after a restart."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.stat()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0 and parent.st_uid != 0:
                os.fchown(fd, parent.st_uid, parent.st_gid)
            handle.write(text + "\n")
            handle.flush()
            os.fsync(fd)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _git_init(config_repo: Path) -> None:
    if not (config_repo / ".git").exists():
        import subprocess

        subprocess.run(
            ["git", "init", "--quiet", str(config_repo)], capture_output=True, check=False
        )


CONTROL_SERVICE_USER = "ozolith-control"
CONTROL_SERVICE_NAME = "theozolith-control.service"
CONTROL_UNIT_PATH = Path("/etc/systemd/system") / CONTROL_SERVICE_NAME

# Unconditionally in the server-cert SAN (ADR-0036/0037): the local node of
# a Single-Node Deployment persists a loopback dial address, and the
# Operator TUI is a loopback API consumer — both verify against this cert.
LOOPBACK_IP = "127.0.0.1"


def _render_unit(exec_path: str, data_dir: Path, port: int) -> str:
    """The capability-granting unit (ADR-0034): serve binds the external
    port directly as an unprivileged dedicated user — no root serve, no
    setcap on a shared interpreter, nothing to keep alive in a terminal."""
    return f"""[Unit]
Description=TheOzolith Control Node
After=network-online.target
Wants=network-online.target

[Service]
User={CONTROL_SERVICE_USER}
Group={CONTROL_SERVICE_USER}
Environment=THEOZOLITH_DATA_DIR={data_dir}
ExecStart={exec_path} serve --port {port}
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
# OS backstop for the unauthenticated bootstrap listener (OZ-04): the
# in-process pool already caps handler threads, but a thread/fd ceiling
# keeps any flood from starving the box even if that cap regresses.
TasksMax=256
LimitNOFILE=4096
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _systemd_present() -> bool:
    return Path("/run/systemd/system").is_dir()


# The charset an ExecStart path may use without any systemd quoting or
# specifier escaping: unit directives here are deliberately unquoted, so
# spaces, quotes, backslashes, '%' (specifiers), and control characters are
# refused at setup instead of being escaped (rejection is simpler to get
# right than systemd's quoting rules, and real install paths are plain).
_UNIT_SAFE_EXEC = re.compile(r"^[A-Za-z0-9/._+-]+$")


def _exec_unreachable_reason(path: Path) -> str | None:
    """Why the INSTALLATION POLICY refuses ``path``, or None if accepted.

    This is a conservative policy, not a precise access computation as the
    service user: the file must be world-readable+executable and every
    ancestor world-traversable. Group-accessible or execute-only layouts
    that would in fact work are rejected by policy — a plain world-readable
    system install is the supported shape. The point is timing: a sudo
    invocation happily resolves an executable inside /root or a 0700 home
    venv, and persisting that into the unit would fail only at first boot;
    this check moves the failure to setup time."""
    if not path.is_file():
        return "no such file"
    if path.stat().st_mode & 0o005 != 0o005:
        return "the file is not readable+executable by others"
    for parent in path.parents:
        if parent.stat().st_mode & 0o001 == 0:
            return f"directory {parent} is not traversable by others"
    return None


def _service_executable() -> str:
    """The ExecStart path for the unit, checked against the installation
    policy (unit-syntax-safe characters, world-reachable) — setup fails
    with remediation rather than generating a unit that is malformed or
    dies at first start (ADR-0034)."""
    import shutil

    path = Path(shutil.which("theozolith") or Path(sys.argv[0])).resolve()
    if not _UNIT_SAFE_EXEC.match(str(path)):
        raise SystemExit(
            f"error: theozolith at {path} contains characters unsafe for an"
            " unquoted systemd ExecStart (spaces, quotes, '%', '\\', or control"
            " characters) — install TheOzolith at a plain system path (e.g. a"
            " venv under /opt/theozolith) and re-run, or use the docker compose"
            " flow"
        )
    reason = _exec_unreachable_reason(path)
    if reason:
        raise SystemExit(
            f"error: theozolith at {path} is not reachable by the"
            f" {CONTROL_SERVICE_USER} service user ({reason}) — install TheOzolith"
            " at a system path (e.g. a venv under /opt/theozolith with its bin on"
            " PATH, or pipx --global) and re-run, or use the docker compose flow"
        )
    return str(path)


def _validated_root_data_dir(data_dir: Path) -> Path:
    """The ONE directory a root-mediated install may ``chown -R``: exactly
    the dedicated system leaf, symlink-free. THEOZOLITH_DATA_DIR is honored
    everywhere else, but feeding an environment-controlled path to a root
    recursive chown is how a typo ('/', '/var', a symlink into the host)
    transfers the machine to the service user — so the root installer
    refuses everything but its own constant, and the constant doubles as a
    unit-syntax-safe Environment= value by construction."""
    from theozolith_control.settings import DEFAULT_ROOT_DATA_DIR

    expected = Path(DEFAULT_ROOT_DATA_DIR)
    if data_dir != expected or data_dir.is_symlink() or data_dir.resolve() != expected.resolve():
        raise SystemExit(
            f"error: root-mediated setup only manages {expected} — the data dir"
            f" resolved to {data_dir}"
            f"{' (a symlink)' if data_dir.is_symlink() else ''}. Unset"
            " THEOZOLITH_DATA_DIR (or point it at the default), or run"
            " unprivileged / via docker compose instead."
        )
    return data_dir


def _install_systemd_unit(settings: ControlSettings, port: int) -> bool:
    """Root-mediated bare-metal setup (ADR-0034), idempotent — shared by
    init and recover: ensure the dedicated service user, hand the partition
    to it, (re)write the unit, daemon-reload, enable. Skipped (False) when
    not root, inside a container, or without systemd — the compose flow and
    hand-run dev serve are unchanged. Everything mutating is gated behind
    the data-dir and executable validations."""
    if os.geteuid() != 0 or _running_in_container():
        return False
    if not _systemd_present():
        _log(f"systemd not detected — start serving yourself: theozolith serve --port {port}")
        return False
    import pwd
    import subprocess

    _validated_root_data_dir(settings.data_dir)
    exec_path = _service_executable()
    try:
        pwd.getpwnam(CONTROL_SERVICE_USER)
    except KeyError:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--shell",
                "/usr/sbin/nologin",
                "--home-dir",
                str(settings.data_dir),
                "--no-create-home",
                CONTROL_SERVICE_USER,
            ],
            check=True,
        )
    # The service user owns the partition; root (sudo) reads through it.
    # Re-running repairs ownership after a restore (recover's case).
    subprocess.run(
        ["chown", "-R", f"{CONTROL_SERVICE_USER}:{CONTROL_SERVICE_USER}", str(settings.data_dir)],
        check=True,
    )
    CONTROL_UNIT_PATH.write_text(_render_unit(exec_path, settings.data_dir, port), encoding="utf-8")
    for argv in (["systemctl", "daemon-reload"], ["systemctl", "enable", CONTROL_SERVICE_NAME]):
        subprocess.run(argv, check=True, capture_output=True)
    _log(
        f"installed and enabled {CONTROL_SERVICE_NAME} (user {CONTROL_SERVICE_USER},"
        f" binds {port} via CAP_NET_BIND_SERVICE)"
    )
    return True


def _print_trust_upgrade(settings: ControlSettings, ip: str) -> None:
    """The optional CA-trust step (ADR-0034): per-OS instructions to trust the
    per-deployment CA on a browser device — the step exists only once a
    browser is wanted. The CA is fetched over the PLAINTEXT bootstrap
    listener, so the operator must verify its SHA-256 fingerprint out of band
    before installing it as a system root; an unverified install trusts
    whatever a LAN man-in-the-middle substitutes (OZ-01). The node path
    already pins this exact fingerprint from the join string — the browser
    path makes the human do the same compare."""
    ca_url = f"http://{ip}:{settings.bootstrap_port}/ca.pem"
    try:
        digest = tls.ca_fingerprint_sha256((settings.tls_dir / CA_FILE).read_bytes())
        colon_fp = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()
    except OSError:
        colon_fp = None
    _log("OPTIONAL — trust the per-deployment CA on a browser device (removes the cert")
    _log("warning). The CA is served over PLAINTEXT http, so an unverified install would")
    _log("trust whatever a LAN attacker substitutes — verify the fingerprint FIRST:")
    if colon_fp is not None:
        _log(f"  expected CA SHA-256: {colon_fp}")
    _log(f"  1) download {ca_url} (served while serve runs)")
    _log("  2) verify out of band — this must print the fingerprint above:")
    _log("       openssl x509 -in ca.pem -noout -fingerprint -sha256")
    _log("  3) only on a match, install it:")
    _log(
        "  macOS:   sudo security add-trusted-cert -d -k /Library/Keychains/System.keychain ca.pem"
    )
    _log(
        "  Linux:   sudo cp ca.pem /usr/local/share/ca-certificates/theozolith.crt"
        " && sudo update-ca-certificates"
    )
    _log("  Firefox: uses its own store — Settings > Privacy & Security > Certificates > Import")
    _log("  iOS:     send ca.pem to the device, install the profile, then enable it under")
    _log("           Settings > General > About > Certificate Trust Settings")


def _print_handoff(settings: ControlSettings, ip: str, port: int, unit_installed: bool) -> None:
    """The operator handoff (ADR-0036): the machine surface only — no
    password was taken and no browser step exists. The dashboard is one
    optional command away and the handoff says exactly which."""
    control_url = origin.derive_origin(ip, port).origin
    _log("")
    _log("== Control Node initialized ==")
    _log(f"control address: {control_url} (nodes and the CLI dial this — no DNS")
    _log("anywhere; give this box a static IP or DHCP reservation)")
    _log("")
    if unit_installed:
        _log(f"1) start serving:      sudo systemctl start {CONTROL_SERVICE_NAME}")
    else:
        _log("1) start serving:      theozolith serve   (compose flow: docker compose")
        _log("   -f deploy/compose/control.yml up -d — the port mapping bridges the")
        _log(f"   external port {port} onto the bind)")
    _log("")
    _log("2) provision nodes:    sudo theozolith join-token create   (one paste per box)")
    _log("")
    _log("3) check the fleet:    sudo theozolith status")
    _log("")
    _log("optional — the browser dashboard stays off until you run")
    _log("'sudo theozolith origin-init': it asks for the browser origin and an")
    _log("admin password together (the two browser-only credentials; ADR-0036).")
    _log("Until then every browser route refuses and the bearer API serves")
    _log("everything.")
    _log("")
    _log(f"backup: copy {settings.data_dir}/ minus cache/ to another device after")
    _log("enrolling nodes or adding secrets — GitHub is never a full backup (ADR-0024)")


def _init(args) -> int:
    """The unified first run (ADR-0023, amended by ADR-0034/0036): master
    key -> admin bearer token -> control address -> CA/TLS with IP SANs
    (the box's IP + loopback) -> systemd unit (root, bare metal) ->
    operator handoff. No browser credential is taken (origin-init owns
    both). Re-run requires --force (which mints a new CA — invalidating
    every outstanding join string by construction — but never touches the
    master key: rotate-key owns that, with re-encryption)."""
    settings = load_settings()
    existing_ip = controltoml.read_control_ip(settings.config_repo)
    initialized = bool(existing_ip) or settings.key_path.is_file()
    # A repeated --with-local-node on an initialized box is a RESUME, not a
    # re-init (ADR-0037): every local-node phase is idempotent or
    # reconciled, and a failed bootstrap must be retryable without --force
    # or a CA rotation.
    resume_local = initialized and not args.force and getattr(args, "with_local_node", False)
    if initialized and not args.force and not resume_local:
        raise SystemExit(
            f"error: {settings.data_dir} is already initialized"
            f" ({existing_ip or 'master key present'}) — pass --force to re-run."
            " A new CA invalidates the pinned ca.pem on EVERY provisioned node:"
            " the whole fleet fails TLS until each box gets one join-string"
            " re-paste. Device trust and every outstanding join string are"
            " invalidated too. (A partial single-node bootstrap resumes with"
            " 'init --with-local-node' alone — no --force needed.)"
        )

    # Root-mediated init fails BEFORE any state is written: a bad
    # THEOZOLITH_DATA_DIR (or an unreachable executable) must not leave a
    # half-laid partition behind the eventual installer refusal. SAN input
    # is validated in the same zone: hostnames are refused here, not after
    # the address landed (ADR-0036: IP SANs only from init).
    extra_hosts = _ip_literals_only(list(args.host or []), "--host")
    if os.geteuid() == 0 and not _running_in_container() and _systemd_present():
        _validated_root_data_dir(settings.data_dir)
        _service_executable()

    # --with-local-node pre-flight (ADR-0037), same posture: everything the
    # internal join needs must resolve before any state lands.
    nodedaemon_exec = ""
    if getattr(args, "with_local_node", False):
        if os.geteuid() != 0 or _running_in_container() or not _systemd_present():
            raise SystemExit(
                "error: --with-local-node is a bare-metal root setup (it installs"
                " the Node Daemon and its systemd unit; ADR-0037) — run"
                " 'sudo theozolith init --with-local-node' on the box itself,"
                " outside any container"
            )
        nodedaemon_exec = localnode.ensure_preconditions()

    if resume_local:
        return _resume_local_node(settings, nodedaemon_exec)

    # The control IP (ADR-0031): confirmed once, persisted, and the ONLY
    # address any mint surface will ever embed. Inside a container the
    # auto-detected address is the bridge IP — wrong for the LAN by
    # construction — so detection is refused there, never silently used.
    ip = args.ip
    if not ip:
        if _running_in_container():
            raise SystemExit(
                "error: running inside a container — an auto-detected address would"
                " be the container bridge IP, unreachable from your LAN. Pass this"
                " box's LAN address explicitly:\n"
                "  docker compose -f deploy/compose/control.yml run --rm control"
                " init --ip <LAN-IP>"
            )
        ip = bootstrap.detect_host_ip()
        _log(f"control IP: {ip} (auto-detected; wrong for your LAN? re-run with --ip)")

    # The partition (ADR-0024, amended by ADR-0048): durability class legible
    # from the path. configs/ is the machine-owned pinned build; config-src/
    # is the scaffolded human Config Repo the operator edits and ingests.
    settings.config_repo.mkdir(parents=True, exist_ok=True)
    settings.config_source.mkdir(parents=True, exist_ok=True)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    settings.secrets_dir.chmod(0o700)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    _git_init(settings.config_repo)
    _git_init(settings.config_source)

    # 1. Master key (unchanged from ADR-0015 first-start behavior).
    ensure_key_file(settings.key_path)
    # The machine credential (ADR-0015, generated here per ADR-0023); kept
    # across --force so scripted CLI callers survive a re-init.
    if not settings.admin_token_path.is_file():
        _write_private(settings.admin_token_path, _secrets.token_urlsafe(32))

    # 2. The control address: one IP + external port for nodes AND browsers
    # (ADR-0031/0034), read-only control.toml fields.
    port = args.port or controltoml.DEFAULT_CONTROL_PORT
    try:
        controltoml.write_control_address(settings.config_repo, ip, port=port, log=_log)
    except controltoml.ControlTomlError as exc:
        raise SystemExit(f"error: {exc}") from exc

    # 3. Per-deployment CA + server cert with IP SANs only (ADR-0036): the
    # persisted IP so the join exchange, every node dial, and CA-trusting
    # browsers verify cleanly (ADR-0023/0034), plus loopback (ADR-0037: the
    # local node and the Operator TUI dial 127.0.0.1). A browser origin
    # persisted by an earlier origin-init survives --force: its host stays
    # in the SAN so the enabled surface outlives a re-init.
    hosts = [ip, LOOPBACK_IP]
    persisted_origin = controltoml.read_browser_origin(settings.config_repo)
    if persisted_origin:
        # A malformed persisted origin is surfaced by recover and
        # origin-init --force; init must not die on it.
        with contextlib.suppress(OriginError):
            hosts.append(origin.san_host(origin.parse_browser_origin(persisted_origin)))
    hosts = list(dict.fromkeys(hosts + extra_hosts))
    try:
        provision(settings.tls_dir, hosts, trust_root=settings.data_dir)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    # 3b. The stage-don't-deploy scaffold (ADR-0037/0048): seeded into the
    # HUMAN Config Repo, then materialized into the pinned build by the
    # initial ingest — before the unit install so the partition chown hands
    # everything to the service user whole.
    node_name = socket.gethostname()
    if nodedaemon_exec:
        localnode.write_scaffold(settings.config_source, node_name, log=_log)
    if initialized:
        # A --force re-init never ingests: the deployment's Config Repo may
        # live elsewhere, and materializing the default config-src over a
        # populated pinned build would discard real config (ADR-0048).
        _log(
            "existing deployment: skipping the initial ingest — run"
            " 'theozolith config ingest [SOURCE]' yourself"
        )
    else:
        try:
            ingest.ingest(
                str(settings.config_source),
                settings.config_repo,
                registry_credentials=_registry_credentials(settings),
                log=_log,
            )
        except ingest.IngestError as exc:
            raise SystemExit(f"error: initial ingest failed: {exc}") from exc

    # 4. The systemd unit (root-mediated bare metal only; ADR-0034) — after
    # every artifact exists, so the chown hands the complete partition over.
    unit_installed = _install_systemd_unit(settings, port)

    # 5. The local node (ADR-0037): early serve start via the unit just
    # installed, then the unmodified join flow, machine-consumed.
    if nodedaemon_exec:
        localnode.bootstrap_local_node(
            settings, node_name=node_name, nodedaemon_exec=nodedaemon_exec, log=_log
        )
        _print_local_handoff(settings, ip, port, node_name)
        return 0

    # 6. The handoff.
    _print_handoff(settings, ip, port, unit_installed)
    return 0


def _resume_local_node(settings: ControlSettings, nodedaemon_exec: str) -> int:
    """The retry path (ADR-0037): re-running ``init --with-local-node`` on
    an initialized box resumes the local bootstrap in place — no --force,
    no CA rotation, operator edits preserved. The standard-init artifacts
    must already exist: a damaged PARTITION is recover's territory, not
    resume's."""
    problems = [
        f"missing {name} at {path}"
        for name, path in (
            ("master key", settings.key_path),
            ("CA certificate", settings.tls_dir / CA_FILE),
            ("CA private key", settings.tls_dir / CA_KEY_FILE),
            ("server certificate", settings.tls_dir / CERT_FILE),
        )
        if not path.is_file()
    ]
    if not settings.control_ip:
        problems.append("no persisted control IP in control.toml")
    if problems:
        raise SystemExit(
            "error: cannot resume --with-local-node — the standard init state is"
            " incomplete:\n  - "
            + "\n  - ".join(problems)
            + "\nrestore it ('theozolith recover' from a backup) or re-init with --force."
        )
    _log("already initialized — resuming the local node in place (CA untouched;")
    _log("completed phases are skipped or reconciled; ADR-0037)")
    node_name = socket.gethostname()
    settings.config_source.mkdir(parents=True, exist_ok=True)
    _git_init(settings.config_source)
    localnode.write_scaffold(settings.config_source, node_name, log=_log)
    try:
        ingest.ingest(
            str(settings.config_source),
            settings.config_repo,
            registry_credentials=_registry_credentials(settings),
            log=_log,
        )
    except ingest.IngestError as exc:
        raise SystemExit(f"error: initial ingest failed: {exc}") from exc
    _install_systemd_unit(settings, settings.control_port)
    localnode.bootstrap_local_node(
        settings, node_name=node_name, nodedaemon_exec=nodedaemon_exec, log=_log
    )
    _print_local_handoff(settings, settings.control_ip, settings.control_port, node_name)
    return 0


def _print_local_handoff(settings: ControlSettings, ip: str, port: int, node_name: str) -> None:
    """The Single-Node Deployment handoff (ADR-0037): serve already runs
    under systemd and the local node heartbeats — no start step, no join
    string was ever shown. The finish line lives in the scaffold README."""
    control_url = origin.derive_origin(ip, port).origin
    _log("")
    _log("== Single-Node Deployment initialized ==")
    _log(f"control address: {control_url} (the service is running; the local node")
    _log(f"{node_name!r} is registered and heartbeating over loopback)")
    _log("")
    _log("a worker Stack is STAGED, not deployed — finish line (three steps) in")
    _log(f"{settings.config_source / 'README.md'}:")
    _log("  1) pin the base image digest in worker-types/claude-dev.toml")
    _log("     (or use a tag-only base — ingest resolves it)")
    _log("  2) enter secrets:      sudo theozolith secret set github-implementer")
    _log("                         sudo theozolith secret set anthropic-api-key")
    _log('  3) flip state = "running" in stacks/implementer.toml, commit, then')
    _log("     sudo theozolith config ingest")
    _log("")
    _log("check the fleet:       sudo theozolith status")
    _log("more nodes later:      sudo theozolith join-token create   (one paste per box)")
    _log("browser (optional):    sudo theozolith origin-init   (ADR-0036)")
    _log("")
    _log(f"backup: copy {settings.data_dir}/ minus cache/ to another device after")
    _log("enrolling nodes or adding secrets — GitHub is never a full backup (ADR-0024)")


def _prompt_browser_origin(default_origin: str) -> str:
    """The browser origin, defaulting to the IP origin (ADR-0036). A piped
    stdin (scripts, tests) reads one line — blank keeps the default; a
    terminal prompts once."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip() or default_origin
    return input(f"browser origin [{default_origin}]: ").strip() or default_origin


def _repair_partition_ownership(settings: ControlSettings) -> None:
    """Hand a sudo-run subcommand's writes back to the service user
    (ADR-0036): origin-init, set-password, and tls-init mutate the
    partition AFTER init's chown handed it over, and a root-owned
    credential or server key is unreadable to theozolith-control.service
    on its next restart. Same blast radius as the installer (ADR-0034,
    PR #12): the ONE path a root recursive chown may touch is the constant
    system leaf, symlink-free — everything else is skipped (unprivileged
    and hand-run-dev partitions are already owned correctly, and a
    root-owned partition means no service user exists to hand over to)."""
    if os.geteuid() != 0:
        return
    from theozolith_control.settings import DEFAULT_ROOT_DATA_DIR

    expected = Path(DEFAULT_ROOT_DATA_DIR)
    data_dir = settings.data_dir
    if data_dir != expected or data_dir.is_symlink() or data_dir.resolve() != expected.resolve():
        return
    try:
        owner = data_dir.stat()
    except OSError:
        return
    if owner.st_uid == 0:
        return
    import subprocess

    subprocess.run(
        ["chown", "-R", f"{owner.st_uid}:{owner.st_gid}", str(data_dir)],
        check=True,
    )


def _origin_init(args) -> int:
    """The opt-in browser step (ADR-0036): take the two browser-only
    credentials together — the origin browsers may dial (default: the IP
    origin; an operator running their own DNS may enter a hostname) and the
    admin password — re-mint the server certificate via the same machinery
    recover uses (same CA, never a new one), then persist the origin LAST:
    its presence is the enablement bit, so an earlier failure leaves the
    surface cleanly disabled."""
    settings = load_settings()
    if not settings.control_ip:
        raise SystemExit("error: no persisted control IP — run 'theozolith init' first")
    for name, path in (
        ("CA certificate", settings.tls_dir / CA_FILE),
        ("CA private key", settings.tls_dir / CA_KEY_FILE),
    ):
        if not path.is_file():
            raise SystemExit(f"error: missing {name} at {path} — run 'theozolith init' first")
    if settings.browser_origin and not args.force:
        raise SystemExit(
            f"error: browser origin already persisted ({settings.browser_origin}) — pass"
            " --force to re-run origin-init. It re-mints the server certificate,"
            " replaces the admin password, and invalidates every browser session."
        )
    try:
        default_origin = origin.derive_origin(settings.control_ip, settings.control_port).origin
    except OriginError as exc:
        raise SystemExit(f"error: {exc}") from exc
    try:
        parsed = origin.parse_browser_origin(_prompt_browser_origin(default_origin))
    except OriginError as exc:
        raise SystemExit(f"error: {exc}") from exc
    password = _prompt_password(args)

    # Certificate first (recover's machinery: same CA), then the password
    # hash, then the origin — the enablement bit lands last. Whatever this
    # sudo-run writes, complete or interrupted, is handed back to the
    # service user in the finally: a root-owned server key or password
    # record must never survive to break the next service restart.
    hosts = list(dict.fromkeys([settings.control_ip, LOOPBACK_IP, origin.san_host(parsed)]))
    try:
        try:
            cert, _key = tls.remint_server_cert(
                settings.tls_dir, hosts, trust_root=settings.data_dir
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from exc
        _log(f"re-minted {cert.name} from the existing CA (SAN: {', '.join(hosts)}) — nodes")
        _log("pin the CA, not the server cert: no node is touched")
        _write_private(settings.admin_password_path, passwords.hash_password(password))
        # Invalidate sessions without root-creating the service's cache
        # database: no cache.db means no sessions exist to invalidate.
        if settings.cache_db_path.is_file():
            Store(settings.cache_db_path).truncate_sessions()
        try:
            controltoml.write_browser_origin(settings.config_repo, parsed.origin, log=_log)
        except controltoml.ControlTomlError as exc:
            raise SystemExit(f"error: {exc}") from exc
    finally:
        _repair_partition_ownership(settings)
    _log("")
    _log(f"browser surface enabled: {parsed.origin}")
    _log(f"restart the service to arm it: sudo systemctl restart {CONTROL_SERVICE_NAME}")
    _log("(compose flow: docker compose -f deploy/compose/control.yml restart)")
    _log("")
    _log("first visit shows the self-signed-certificate interstitial — click")
    _log("through and log in (ADR-0034; nodes are unaffected: they pin the CA).")
    _print_trust_upgrade(settings, settings.control_ip)
    return 0


def _set_password(args) -> int:
    """Change the admin password: rewrite the hash and truncate the session
    table — every browser session dies now (ADR-0023). Under sudo the
    record is handed to the service user (ADR-0036)."""
    settings = load_settings()
    if not settings.secrets_dir.is_dir():
        raise SystemExit("error: not initialized — run 'theozolith init' first")
    try:
        _write_private(
            settings.admin_password_path, passwords.hash_password(_prompt_password(args))
        )
        if settings.cache_db_path.is_file():
            Store(settings.cache_db_path).truncate_sessions()
    finally:
        _repair_partition_ownership(settings)
    _log("admin password updated; every browser session was invalidated")
    return 0


def _recover(args) -> int:
    """Recovery from a restored data-dir copy (ADR-0024): validate
    loudly and COMPLETELY — every missing or invalid artifact enumerated in
    one pass, exit nonzero — then re-mint the server certificate from the
    restored CA with this box's IP in the SAN. Nodes dial the persisted
    control IP directly (ADR-0023 as amended): a same-IP restore reconnects
    the fleet untouched; a NEW IP means one join-string re-paste per node —
    and those nodes will NOT appear in the unregistered view, because their
    heartbeats go to the dead address and never arrive here."""
    settings = load_settings()
    problems: list[str] = []

    persisted_ip = controltoml.read_control_ip(settings.config_repo)
    ip = args.ip or persisted_ip
    if not ip:
        problems.append(
            f"{settings.config_repo / controltoml.CONTROL_TOML}: no persisted control IP"
            " — pass --ip <this box's LAN IP> (pre-ADR-0031 backup)"
        )

    if not settings.config_repo.is_dir():
        problems.append(f"missing Config Repo at {settings.config_repo}")
    elif ip:
        try:
            origin.derive_origin(ip, controltoml.read_control_port(settings.config_repo))
        except (OriginError, controltoml.ControlTomlError) as exc:
            problems.append(f"control address invalid: {exc}")
    try:
        controltoml.read_values(settings.config_repo)
    except controltoml.ControlTomlError as exc:
        problems.append(str(exc))

    box: SecretBox | None = None
    if not settings.key_path.is_file():
        problems.append(f"missing {settings.key_path}")
    else:
        try:
            box = SecretBox(settings.key_path.read_text(encoding="utf-8").strip())
        except CryptoError as exc:
            problems.append(f"{settings.key_path}: {exc}")
    for name, path in (
        ("admin token", settings.admin_token_path),
        ("CA certificate", settings.tls_dir / CA_FILE),
        ("CA private key", settings.tls_dir / CA_KEY_FILE),
    ):
        if not path.is_file():
            problems.append(f"missing {name} at {path}")
    # Browser credentials are optional-but-consistent (ADR-0036): with no
    # persisted origin the surface is disabled and no password is owed;
    # with one, the password hash must exist and parse.
    if settings.browser_origin:
        try:
            origin.parse_browser_origin(settings.browser_origin)
        except OriginError as exc:
            problems.append(
                f"persisted browser origin invalid: {exc} — re-run"
                " 'theozolith origin-init --force' after recovery"
            )
        if not settings.admin_password_path.is_file():
            problems.append(
                "browser surface is enabled (browser origin persisted) but the admin"
                f" password hash is missing at {settings.admin_password_path} — restore"
                " it, or re-run 'theozolith origin-init --force' after recovery"
            )
        else:
            try:
                passwords.parse_record(settings.admin_password_path.read_text(encoding="utf-8"))
            except passwords.PasswordFormatError as exc:
                problems.append(f"{settings.admin_password_path}: {exc}")
    if not settings.store_db_path.is_file():
        problems.append(f"missing {settings.store_db_path}")
    elif box is not None:
        # Complete validation now beats discovery later: every stored secret
        # must decrypt under the restored master key.
        secret_store = SecretStore(settings.store_db_path)
        for name in secret_store.secret_names():
            try:
                box.decrypt(secret_store.get_secret_token(name) or "")
            except CryptoError:
                problems.append(f"secret {name!r} does not decrypt under the restored master key")

    if problems:
        _log(f"recovery validation FAILED — {len(problems)} problem(s):")
        for problem in problems:
            _log(f"  - {problem}")
        _log("restore a complete copy of the data dir (minus cache/) and re-run")
        return 1

    hosts = [ip, LOOPBACK_IP]
    if settings.browser_origin:
        # Validated above; problems abort before this line.
        hosts.append(origin.san_host(origin.parse_browser_origin(settings.browser_origin)))
    hosts = list(dict.fromkeys(hosts))
    try:
        cert, key = tls.remint_server_cert(settings.tls_dir, hosts, trust_root=settings.data_dir)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    _log(f"re-minted {cert} and {key} from the restored CA (SAN: {', '.join(hosts)})")
    if args.ip and args.ip != persisted_ip:
        controltoml.write_control_address(settings.config_repo, args.ip, log=_log)
    # Root-mediated recovery repairs the service too (ADR-0034): same
    # idempotent installer as init — service user, partition ownership,
    # unit, enable — never a new CA. The printed instruction is truthful:
    # the unit exists and is enabled before it is suggested.
    unit_installed = _install_systemd_unit(
        settings, controltoml.read_control_port(settings.config_repo)
    )
    _log("recovery validated. next:")
    if unit_installed:
        _log(f"  start serving: sudo systemctl start {CONTROL_SERVICE_NAME}")
    else:
        _log("  start serving: theozolith serve (or the compose flow)")
    if args.ip and persisted_ip and args.ip != persisted_ip:
        _log("")
        _log(f"control IP CHANGED ({persisted_ip} -> {args.ip}): every provisioned node")
        _log("still dials the old address — each needs ONE join-string re-paste")
        _log("('theozolith join-token create'). Those nodes will NOT appear in the")
        _log("unregistered view: their heartbeats go to the dead address and never")
        _log("arrive here. Re-provisioning rotates each node's token in place.")
    else:
        _log(f"nodes dial {ip} directly and reconnect on their own backoff — no")
        _log("node-side action. (If this box's IP is actually different, re-run")
        _log("'recover --ip <new-ip>': a changed IP costs one re-paste per node,")
        _log("and affected nodes will NOT show up as unregistered.)")
    _log("sessions and cached state died with cache/ (by design): re-log-in; one")
    _log("heartbeat round re-warms the fleet. nodes provisioned after the backup")
    _log("surface as unregistered — the re-provision worklist.")
    return 0


def _ip_literals_only(values: list[str], flag: str) -> list[str]:
    """SAN inputs on the machine-surface commands are IP literals only
    (ADR-0036): a hostname enters the certificate exactly one way — the
    persisted browser origin, via origin-init."""
    import ipaddress

    checked: list[str] = []
    for value in values:
        try:
            checked.append(str(ipaddress.ip_address(value)))
        except ValueError as exc:
            raise SystemExit(
                f"error: {flag} {value!r} is not an IP literal — init and tls-init"
                " mint IP SANs only; hostnames enter the certificate exactly one"
                " way: 'theozolith origin-init' (ADR-0036)"
            ) from exc
    return checked


def _tls_init(args) -> int:
    settings = load_settings()
    hosts = _ip_literals_only(list(args.host or []), "--host")
    # Every persisted dial identity belongs in the certificate SAN: the
    # control IP (ADR-0031/0034), loopback (ADR-0037), and the browser
    # origin's host when one is enabled (ADR-0036) — a manual re-mint must
    # not silently break the node channel or the dashboard. Extra --host
    # entries (extra LAN addresses) are additive and IP-only.
    if settings.control_ip:
        preserved = [settings.control_ip, LOOPBACK_IP]
        if settings.browser_origin:
            try:
                preserved.append(
                    origin.san_host(origin.parse_browser_origin(settings.browser_origin))
                )
            except OriginError as exc:
                raise SystemExit(
                    f"error: persisted browser origin is invalid: {exc} — re-run"
                    " 'theozolith origin-init --force'"
                ) from exc
        hosts = list(dict.fromkeys(preserved + hosts))
    if not hosts:
        raise SystemExit("error: pass --host, or run 'theozolith init' first (ADR-0031)")
    try:
        try:
            ca, cert, key = provision(settings.tls_dir, hosts, trust_root=settings.data_dir)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from exc
    finally:
        _repair_partition_ownership(settings)
    _log(f"wrote {ca}, {cert}, {key}")
    _log(f"distribute {ca.name} to every node (install script --ca, or THEOZOLITH_TLS_CA)")
    return 0


def _rotate_key(args) -> int:
    settings = load_settings()
    old = SecretBox(
        env_value(os.environ, "THEOZOLITH_MASTER_KEY") or ensure_key_file(settings.key_path)
    )
    new_key = generate_key()
    new = SecretBox(new_key)
    secret_store = SecretStore(settings.store_db_path)
    names = secret_store.secret_names()
    secret_store.replace_secret_tokens(
        {
            name: new.encrypt(old.decrypt(secret_store.get_secret_token(name) or ""))
            for name in names
        }
    )
    settings.key_path.write_text(new_key + "\n", encoding="utf-8")
    settings.key_path.chmod(0o600)
    _log(f"re-encrypted {len(names)} secret(s) under a fresh master key")
    return 0


def _janitor_once(args) -> int:
    from theozolith_worker.githubapi import GitHubClient

    settings = load_settings()
    if not settings.coordination_jobs_enabled:
        raise SystemExit("error: set THEOZOLITH_REPO and CONTROL_GITHUB_TOKEN")
    client = GitHubClient(settings.repo or "", settings.github_token or "", settings.api_url)
    store = Store(settings.cache_db_path)
    _sweep_pass(settings, store, client)
    _log("janitor: pass complete")
    return 0


# -- operator subcommands (HTTP) -------------------------------------------------


def _registry_credentials(settings) -> dict[str, str]:
    """Stored ``registry:<host>`` pull credentials, decrypted for ingest's
    authenticated base resolution (ADR-0049), keyed by host. Discovery is
    strictly READ-ONLY: no store, or a store with no ``registry:`` secrets,
    is ``{}`` without touching key material — reading must never create
    ``store.db`` or the master key as a side effect of an ingest (fresh init
    creates the key explicitly in ``_init``; a lost key must stay lost until
    the operator restores it, never be silently replaced). The key loads only
    when a registry credential actually exists — THEOZOLITH_MASTER_KEY(_FILE)
    when set, else the EXISTING key file. A stored credential that will not
    decrypt is FATAL: a corrupt credential must fail loud here, never silently
    degrade to anonymous resolution that then 403s on the private base with a
    misleading message."""
    if not settings.store_db_path.is_file():
        return {}
    store = SecretStore(settings.store_db_path)
    names = [n for n in store.secret_names() if n.startswith(configrepo.REGISTRY_SECRET_PREFIX)]
    if not names:
        return {}
    key = env_value(os.environ, "THEOZOLITH_MASTER_KEY")
    if not key:
        if not settings.key_path.is_file():
            raise SystemExit(
                "error: registry credentials are stored but the master key is missing"
                f" ({settings.key_path}) — restore the key file (it is never"
                " regenerated here) or set THEOZOLITH_MASTER_KEY(_FILE)"
            )
        key = settings.key_path.read_text(encoding="utf-8").strip()
    try:
        box = SecretBox(key)
    except CryptoError as exc:
        raise SystemExit(f"error: {exc} — restore the master key") from exc
    credentials: dict[str, str] = {}
    for name in names:
        host = name[len(configrepo.REGISTRY_SECRET_PREFIX) :]
        try:
            credentials[host] = box.decrypt(store.get_secret_token(name) or "")
        except CryptoError as exc:
            raise SystemExit(
                f"error: registry credential {name!r} does not decrypt under the"
                " master key — restore the key or re-store the credential"
            ) from exc
    return credentials


def _config_ingest(args) -> int:
    """`theozolith config ingest` (ADR-0048): the only write path into the
    pinned build. Runs locally against the data dir — no server round-trip;
    the running service observes the new commit on its next config read.
    `--dry-run` is the config linter: the identical pipeline through the lint
    step, then a preview of what ingest would change — nothing written."""
    settings = load_settings()
    source = args.source or str(settings.config_source)
    try:
        ingest.ingest(
            source,
            settings.config_repo,
            dry_run=args.dry_run,
            registry_credentials=_registry_credentials(settings),
            log=_log,
        )
    except ingest.IngestError as exc:
        raise SystemExit(f"error: {exc}") from exc
    if not args.dry_run:
        _repair_partition_ownership(settings)
    return 0


def _config_migrate(args) -> int:
    """`theozolith config migrate` (ADR-0048 amendment): pre-ingest configs ->
    human Config Repo. Reads the legacy tree (never modifies it), writes the
    migrated repo, and prints the follow-up steps — run BEFORE the upgraded
    service must load the now-retired knowledge_source/knowledge_pin fields."""
    settings = load_settings()
    legacy = Path(args.legacy) if args.legacy else settings.config_repo
    dest = Path(args.dest) if args.dest else settings.config_source
    try:
        migrate.migrate_legacy(legacy, dest, log=_log)
    except migrate.MigrateError as exc:
        raise SystemExit(f"error: {exc}") from exc
    _repair_partition_ownership(settings)
    return 0


def _secret_set(args) -> int:
    url, token, ca = _admin_env(args)
    value = args.value
    if value is None:
        value = (
            sys.stdin.read().rstrip("\n")
            if not sys.stdin.isatty()
            else getpass.getpass(f"value for {args.name}: ")
        )
    if not value:
        raise SystemExit("error: empty secret value")
    _call(
        url, f"/api/v1/secrets/{args.name}", token=token, method="PUT", body={"value": value}, ca=ca
    )
    _log(f"secret {args.name!r} stored (encrypted at rest; pull-only, node-scoped)")
    return 0


def _secret_list(args) -> int:
    url, token, ca = _admin_env(args)
    for name in _call(url, "/api/v1/secrets", token=token, ca=ca).get("names", []):
        _log(name)
    return 0


def _command(args) -> int:
    url, token, ca = _admin_env(args)
    body: dict[str, Any] = {"node": args.node, "verb": args.verb}
    if args.target:
        body["target"] = args.target
    if getattr(args, "force", False):
        body["force"] = True
    result = _call(url, "/api/v1/commands", token=token, method="POST", body=body, ca=ca)
    _log(f"queued command {result.get('id')} ({args.verb} on {args.node})")
    return 0


def _unquarantine(args) -> int:
    url, token, ca = _admin_env(args)
    result = _call(
        url, f"/api/v1/nodes/{args.node}/quarantine/release", token=token, method="POST", ca=ca
    )
    if result.get("released"):
        _log(f"node {args.node}: quarantine released")
    else:
        _log(f"node {args.node}: was not quarantined")
    return 0


def _status(args) -> int:
    return statuscli.run(args)


def _top(args) -> int:
    """The Operator TUI (M9, ADR-0040): resolve the target through the ONE
    admin resolution path (statuscli.resolve_target via _admin_env — flags
    beat env beats init's on-box artifacts, so `sudo theozolith top` needs
    no environment and an SSH-forwarded socket needs only CONTROL_NODE_URL
    + THEOZOLITH_ADMIN_TOKEN + THEOZOLITH_TLS_CA), then hand off to the
    Textual app. Textual imports lazily: every other subcommand stays free
    of the TUI dependency."""
    url, token, ca = _admin_env(args)
    from theozolith_control.tui.app import run_top

    return run_top(url, token, ca)


def _flags(args) -> int:
    url, token, ca = _admin_env(args)
    print(json.dumps(_call(url, "/api/v1/flags", token=token, ca=ca), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith",
        description="TheOzolith Control Node: serve the control plane, administer this"
        " box, and operate the fleet (ADR-0032 — one human CLI;"
        " `theozolith-control` is a deprecated alias).",
    )
    parser.add_argument("--url", help="Control Node URL (default: CONTROL_NODE_URL)")
    parser.add_argument("--ca", help="CA bundle for TLS verification (default: THEOZOLITH_TLS_CA)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the control-plane service.")
    serve.add_argument("--host", default="0.0.0.0", help="Uvicorn bind address only.")
    serve.add_argument(
        "--port",
        type=int,
        default=8443,
        help="Uvicorn bind port only — the external port browsers and nodes dial is"
        " the persisted control_port (ADR-0034; the systemd unit binds it directly,"
        " the compose flow maps it onto this bind).",
    )
    serve.add_argument(
        "--insecure-dev",
        action="store_true",
        help="Serve plain HTTP and allow secret traffic anyway. Local development ONLY.",
    )
    serve.set_defaults(func=_serve)

    init = sub.add_parser(
        "init",
        help="The unified first run (ADR-0023/0034/0036; run under sudo on bare"
        " metal): master key, admin bearer token, control address, CA/TLS with IP"
        " SANs (this box's IP + loopback), systemd unit, and the operator handoff."
        " No browser credential — that is 'origin-init'. Re-run requires --force.",
    )
    init.add_argument(
        "--port",
        type=int,
        default=None,
        help="Nonstandard EXTERNAL https port browsers and nodes dial (default: 443,"
        " which the systemd unit binds directly via CAP_NET_BIND_SERVICE).",
    )
    init.add_argument(
        "--ip",
        help="This box's IP for the certificate SAN, hosts line, and CA URL"
        " (default: auto-detected).",
    )
    init.add_argument(
        "--host",
        action="append",
        help="Extra IP literal for the certificate SAN (repeatable). Hostnames are"
        " refused: init mints IP SANs only, and a hostname enters the certificate"
        " exactly one way — 'theozolith origin-init' (ADR-0036).",
    )
    init.add_argument(
        "--with-local-node",
        action="store_true",
        help="Single-Node Deployment (ADR-0037): after standard init, install the"
        " Node Daemon on this box and run the UNMODIFIED join flow internally"
        " (loopback dial address; the join string is machine-consumed, never"
        " shown), then stage a stopped worker Stack scaffold in the Config Repo."
        " Requires bare-metal root with systemd, docker, and the"
        " theozolith-nodedaemon CLI installed.",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Re-initialize: mints a NEW CA. A new CA invalidates the pinned ca.pem"
        " on EVERY provisioned node — the entire fleet fails TLS until each box"
        " gets one join-string re-paste. Outstanding join strings and device"
        " trust are invalidated too. The master key and stored secrets are never"
        " touched.",
    )
    init.set_defaults(func=_init)

    recover = sub.add_parser(
        "recover",
        help="Validate a restored data-dir copy (loudly, completely), re-mint the"
        " server certificate from the restored CA — nodes reconnect untouched"
        " (ADR-0024) — and, run as root on bare metal, repair the systemd service"
        " (ADR-0034; never a new CA).",
    )
    recover.add_argument(
        "--ip",
        help="This box's IP for the new SAN, persisted for future mints (default: the"
        " restored control_ip). A CHANGED IP costs one join-string re-paste per node;"
        " affected nodes will not appear in the unregistered view.",
    )
    recover.set_defaults(func=_recover)

    origin_init = sub.add_parser(
        "origin-init",
        help="Enable the browser surface (ADR-0036): prompt for the browser origin"
        " (default: the IP origin; a hostname if you run your own DNS) and the"
        " admin password together, re-mint the server certificate from the"
        " existing CA with the origin host in the SAN, and persist the origin."
        " Re-run requires --force.",
    )
    origin_init.add_argument(
        "--force",
        action="store_true",
        help="Replace a persisted browser origin: re-mints the server certificate"
        " (same CA — no node is touched), replaces the admin password, and"
        " invalidates every browser session.",
    )
    origin_init.set_defaults(func=_origin_init)

    set_password = sub.add_parser(
        "set-password",
        help="Change the admin password (stores only the scrypt hash) and invalidate"
        " every browser session.",
    )
    set_password.set_defaults(func=_set_password)

    tls_init = sub.add_parser("tls-init", help="Mint a self-signed CA + server certificate.")
    tls_init.add_argument(
        "--host",
        action="append",
        help="Extra IP literal (repeatable; the persisted control IP, loopback, and"
        " any enabled browser-origin host are always included). Hostnames and"
        " wildcards are refused — hostnames enter only via 'theozolith"
        " origin-init' (ADR-0036).",
    )
    tls_init.set_defaults(func=_tls_init)

    config_cmd = sub.add_parser(
        "config",
        help="The Config Repo -> pinned build pipeline (ADR-0048).",
    )
    config_sub = config_cmd.add_subparsers(dest="config_cmd", required=True)
    config_ingest = config_sub.add_parser(
        "ingest",
        help="Harvest the Config Repo (path or git URL), lint it, resolve"
        " mechanical pins (knowledge tree hashes, base tag->digest), compile"
        " knowledge, and commit the pinned build — the ONLY write path into"
        " configs/. The running service picks the new tree up within one"
        " heartbeat; control.toml settings changes need a restart.",
    )
    config_ingest.add_argument(
        "source",
        nargs="?",
        default="",
        help="Config Repo location: a directory or a git URL (default: the"
        " init-scaffolded config-src/ beside the data dir).",
    )
    config_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="Lint and preview only: run the identical pipeline (harvest,"
        " compile, resolve pins, lint) and report what ingest would change —"
        " per-file diff, worker-type re-tags, settings movement — committing"
        " nothing. Uncommitted edits in a local source are previewed from the"
        " working tree.",
    )
    config_ingest.set_defaults(func=_config_ingest)
    config_migrate = config_sub.add_parser(
        "migrate",
        help="Migrate a pre-ADR-0048 configs tree (knowledge_source/"
        "knowledge_pin era) into a human Config Repo: config files copied,"
        " retired knowledge fields removed and noted, control.toml reduced to"
        " its [settings] surface. The legacy tree is never modified; run this"
        " before the upgraded service must load it, then review, ingest, and"
        " restart (deploy/README.md: upgrading a pre-ingest deployment).",
    )
    config_migrate.add_argument(
        "dest",
        nargs="?",
        default="",
        help="Destination directory for the migrated Config Repo (default: the"
        " config-src/ location beside the data dir); must be missing or empty.",
    )
    config_migrate.add_argument(
        "--legacy",
        default="",
        help="The pre-ADR-0048 configs tree to migrate from (default: the"
        " deployment's configs/ dir — the future pinned build).",
    )
    config_migrate.set_defaults(func=_config_migrate)

    secret = sub.add_parser("secret", help="Enter and list secrets (values never displayed).")
    secret_sub = secret.add_subparsers(dest="secret_cmd", required=True)
    secret_set = secret_sub.add_parser("set", help="Store one secret value (prompt or stdin).")
    secret_set.add_argument("name")
    secret_set.add_argument("--value", help="Value (prefer the prompt or stdin: argv leaks).")
    secret_set.set_defaults(func=_secret_set)
    secret_list = secret_sub.add_parser("list", help="List stored secret names.")
    secret_list.set_defaults(func=_secret_list)

    command = sub.add_parser("command", help="Queue an infrastructure command for a node.")
    command.add_argument("verb", choices=["drain", "recycle", "update", "rebuild", "restart"])
    command.add_argument("--node", required=True)
    command.add_argument("--target", help="Stack (drain/recycle) or image (rebuild) name.")
    command.add_argument(
        "--force",
        action="store_true",
        help="Apply immediately (kill-the-tree) instead of queueing behind an in-flight Run.",
    )
    command.set_defaults(func=_command)

    unquarantine = sub.add_parser(
        "unquarantine", help="Release a node's dispatch quarantine (human-only, ADR-0016)."
    )
    unquarantine.add_argument("--node", required=True)
    unquarantine.set_defaults(func=_unquarantine)

    status = sub.add_parser(
        "status",
        help="Fleet health (ADR-0039): human table on stdout; exit 0 healthy,"
        " 1 degraded (reason on the first line), 2 Control Node unreachable."
        " A pure API consumer — no local systemd/docker probing, ever.",
    )
    status.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit the raw read-model documents plus the computed verdict —"
        " the ONLY parsing contract (the table is for humans).",
    )
    status.set_defaults(func=_status)

    top = sub.add_parser(
        "top",
        help="The Operator TUI (M9): full-screen fleet surface over the bearer"
        " API — run 'sudo theozolith top' on the Control Node, or point it at"
        " an SSH-forwarded socket with CONTROL_NODE_URL. A pure API consumer:"
        " reads state+events, writes only commands / quarantine release /"
        " secrets. No embedded terminal — attach prints a pastable command.",
        description="The Operator TUI (`theozolith top`, M9/ADR-0040).",
        epilog=(
            "panels: 1 Fleet (nodes, live containers, command queue) · 2 Stacks &"
            " Runs (run detail with the advisory transcript tail) · 3 Events"
            " (filters + follow) · 4 Errors · 5 Settings (read-only control.toml).\n"
            "keys: c queue command (destructive verbs demand the target name typed"
            " back) · x release quarantine · s secret entry (masked) · a print the"
            " attach command for the selected container · f follow on/off ·"
            " r refresh · q quit.\n"
            "refresh: polls state+events every 5s; when control is unreachable the"
            " last documents stay on screen under a banner and polling continues —"
            " attach refuses while degraded (a frozen snapshot cannot prove heartbeat"
            " freshness) until a refresh succeeds.\n"
            "notices: per-panel 'history incomplete' lines disclose server-side"
            " eviction and client-side follow overflows; the Runs panel discloses if"
            " its bounded history walk ever truncates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    top.set_defaults(func=_top)

    sub.add_parser(
        "flags", help="Zombie flags, janitor actions, malformed states, quarantines."
    ).set_defaults(func=_flags)

    janitor_cmd = sub.add_parser("janitor", help="Zombie-claim sweep against the local store.")
    janitor_cmd.add_argument("--once", action="store_true", required=True)
    janitor_cmd.set_defaults(func=_janitor_once)

    sub.add_parser(
        "rotate-key", help="Re-encrypt all secrets under a fresh master key (server stopped)."
    ).set_defaults(func=_rotate_key)

    # The fleet-operator half (update/build/test/join-token) — ADR-0032.
    product.register(sub)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except product.ProductError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
