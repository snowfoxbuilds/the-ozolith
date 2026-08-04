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
ADR-0034): master key → control address → CA/TLS (with the box's IP in the
SAN) → admin password → systemd unit → operator handoff. Run as root, all
state lands under ``/var/lib/theozolith-control/`` (the ADR-0024 partition
at a system path) and the admin subcommands run under ``sudo``; unprivileged
and containerized runs keep ``~/.theozolith/``. ``recover`` validates a
restored copy loudly and re-mints the server certificate from the restored
CA — a same-IP restore reconnects the fleet untouched (nodes dial the
persisted control IP directly; ADR-0023 as amended 2026-07-28). Browsers
dial that same IP (ADR-0034 — the slug origin is retired).

Secret entry happens here and through the dashboard's web form — both write
through the same PUT /api/v1/secrets/{name} API to the same encrypted store
(NODE-SUBSTRATE.md). The HTTP subcommands take the admin token from
THEOZOLITH_ADMIN_TOKEN or the init-written ``secrets/admin-token`` file, and
the server URL from CONTROL_NODE_URL or the persisted control address.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as _secrets
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from theozolith_worker.config import ConfigError, env_value

from theozolith_control import bootstrap, controltoml, janitor, origin, passwords, product, tls
from theozolith_control.crypto import CryptoError, SecretBox, ensure_key_file, generate_key
from theozolith_control.origin import OriginError
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings, load_settings
from theozolith_control.store import EVENT_ERROR, Store
from theozolith_control.tls import (
    CA_FILE,
    CA_KEY_FILE,
    CERT_FILE,
    KEY_FILE,
    leaf_expiry_warning,
    pair_matches,
    provision,
)


def _log(message: str) -> None:
    print(message, flush=True)


# -- HTTP plumbing for the operator subcommands --------------------------------


def _ssl_context(ca: str | None) -> ssl.SSLContext:
    if ca:
        return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


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
    context = _ssl_context(ca) if url.startswith("https") else None
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(f"error: HTTP {exc.code} from {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}") from exc


def _admin_env(args) -> tuple[str, str, str | None]:
    """(url, admin token, ca) for the HTTP subcommands. Everything falls
    back to init's artifacts on this box — on the Control Node itself the
    commands work with no environment at all (`.env` is gone)."""
    settings = load_settings()
    # The one persisted address (ADR-0031/0034): IP-based, zero DNS
    # dependency — the server cert carries the IP SAN, so verification
    # passes.
    url = args.url or env_value(os.environ, "CONTROL_NODE_URL") or _node_control_url(settings)
    if not url:
        raise SystemExit(
            "error: no Control Node URL — set CONTROL_NODE_URL, pass --url, or run"
            " 'theozolith init' on this box first"
        )
    token = settings.admin_token
    if not token:
        raise SystemExit(
            "error: no admin token — on the Control Node run this under sudo"
            " (a root-mediated install keeps it in /var/lib/theozolith-control;"
            " ADR-0034); elsewhere set THEOZOLITH_ADMIN_TOKEN (or its _FILE"
            " form), or run 'theozolith init' first"
        )
    ca = args.ca or env_value(os.environ, "THEOZOLITH_TLS_CA")
    if not ca and (settings.tls_dir / CA_FILE).is_file():
        ca = str(settings.tls_dir / CA_FILE)
    return url, token, ca


# -- serve ---------------------------------------------------------------------


EVICTION_EVERY_SECONDS = 3600.0  # scanning progress payloads is not a per-minute job


def _sweep_pass(settings: ControlSettings, store: Store, client, *, evict: bool = True) -> None:
    """One janitor pass: zombie escalation, never-activated grant release,
    and (on its slower cadence) progress-telemetry eviction (ADR-0016/0017)."""
    janitor.sweep(store, client, grace_seconds=settings.zombie_grace_seconds, log=_log)
    janitor.release_never_activated(
        store, client, window_seconds=settings.activation_window_seconds, log=_log
    )
    if evict:
        evicted = store.evict_progress(settings.tail_budget_bytes)
        if evicted:
            _log(f"evicted {evicted} progress event(s) past the tail budget (cache, not archive)")


# The renewal watch (PR #15 review): a leaf that now genuinely expires
# within a deployment's lifetime needs an operator-visible warning that
# works for a service running continuously for years — one daily in-process
# pass, not a startup-only log line.
CERT_EXPIRY_CHECK_SECONDS = 86_400.0


def _cert_expiry_pass(settings: ControlSettings, store: Store, *, now=None) -> str | None:
    """One renewal-watch pass: inside the policy window, log the warning
    and land it on the dashboard errors panel (the existing operator
    surface — persistent, filterable, already watched)."""
    message = leaf_expiry_warning(settings.tls_dir / CERT_FILE, now=now)
    if message is None:
        return None
    _log(message)
    store.record_event(
        {
            "type": EVENT_ERROR,
            "node": "",
            "component": "control-node",
            "error_class": "server-certificate-expiring",
            "message": message,
        }
    )
    return message


def _cert_expiry_loop(settings: ControlSettings, store: Store, stop: threading.Event) -> None:
    """The daily renewal watch: an immediate pass at startup, then one per
    day for as long as serve runs."""
    while True:
        try:
            _cert_expiry_pass(settings, store)
        except Exception as exc:
            _log(f"certificate expiry check failed: {exc}")
        if stop.wait(CERT_EXPIRY_CHECK_SECONDS):
            return


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
            " --host <name-or-ip>' first (TLS is mandatory; --insecure-dev for local dev only)"
        )
    # A renewal interrupted mid-promotion can leave a certificate and key
    # that do not belong together; refusing here (with the fix named) is
    # what guarantees startup never serves a mixed pair (tls.py protocol).
    if tls and not pair_matches(cert, key):
        raise SystemExit(
            "error: server.pem and server.key do not match — an interrupted"
            " renewal leaves a mixed pair; run 'theozolith recover' to re-mint"
            " a consistent pair (the CA is unaffected)"
        )
    browser_origin = None
    if not args.insecure_dev or settings.control_ip:
        # Production requires the persisted control address (ADR-0034 — the
        # slug origin is retired): it arms exact Host/Origin enforcement and
        # is the one origin browsers may reach this deployment by. It is
        # independent of --host/--port (the bind address); a
        # persisted-but-invalid address fails closed in dev too.
        try:
            browser_origin = origin.derive_origin(settings.control_ip, settings.control_port)
        except OriginError as exc:
            raise SystemExit(
                f"error: {exc} — production startup requires the persisted control"
                " address (run 'theozolith init'; --insecure-dev for local dev only)"
            ) from exc
    if not args.insecure_dev and not settings.admin_password_path.is_file():
        raise SystemExit(
            "error: no admin password is set — run 'theozolith init' first"
            " (--insecure-dev for local dev only)"
        )
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
    if tls:
        # The daily renewal watch (unconditional in production — unlike the
        # sweeps it needs no GitHub identity).
        threading.Thread(
            target=_cert_expiry_loop, args=(settings, store, stop), daemon=True, name="cert-expiry"
        ).start()
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
        # The mismatch fails loud (ADR-0034): outside a container nothing
        # can be mapping the external port onto this bind, so a bind that
        # differs from the persisted external port means browsers and nodes
        # dial a port nobody answers — the exact silent failure that
        # prompted the ADR. In the compose flow the 443:8443 mapping is the
        # bridge, and a container bind is never the external truth.
        if not _running_in_container() and args.port != settings.control_port:
            _log(
                f"WARNING: bound to port {args.port}, but the persisted external"
                f" port is {settings.control_port} — every browser and node dials"
                f" {browser_origin.origin}. Bare metal: use the systemd unit"
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
    the persisted control IP + external https port. Since ADR-0034 this is
    also the browser origin — one address for everything. Empty until init
    has persisted the IP."""
    if not settings.control_ip:
        return ""
    try:
        return origin.derive_origin(settings.control_ip, settings.control_port).origin
    except OriginError:
        return ""


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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _git_init(config_repo: Path) -> None:
    if not (config_repo / ".git").exists():
        import subprocess

        subprocess.run(
            ["git", "init", "--quiet", str(config_repo)], capture_output=True, check=False
        )


CONTROL_SERVICE_USER = "ozolith-control"
CONTROL_SERVICE_NAME = "theozolith-control.service"
CONTROL_UNIT_PATH = Path("/etc/systemd/system") / CONTROL_SERVICE_NAME


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


def _print_handoff(settings: ControlSettings, ip: str, port: int, unit_installed: bool) -> None:
    """The operator handoff (ADR-0034): no DNS step exists; the first visit
    clicks through the certificate interstitial (the TrueNAS model), and CA
    trust is the optional green-lock upgrade — exact instructions, not
    prose."""
    dashboard = origin.derive_origin(ip, port).origin
    ca_url = f"http://{ip}:{settings.bootstrap_port}/ca.pem"
    _log("")
    _log("== Control Node initialized ==")
    _log(f"dashboard: {dashboard} (browsers and nodes dial this same address —")
    _log("no DNS anywhere; give this box a static IP or DHCP reservation)")
    _log("")
    if unit_installed:
        _log(f"1) start serving:      sudo systemctl start {CONTROL_SERVICE_NAME}")
    else:
        _log("1) start serving:      theozolith serve   (compose flow: docker compose")
        _log("   -f deploy/compose/control.yml up -d — the port mapping bridges the")
        _log(f"   external port {port} onto the bind)")
    _log("")
    _log(f"2) open {dashboard} and log in with the admin password. Your browser")
    _log("   warns about the self-signed certificate on first visit — click through")
    _log("   (ADR-0034; nodes are unaffected: they pin the CA cryptographically).")
    _log("")
    _log("   OPTIONAL green-lock upgrade — trust the per-deployment CA on a device:")
    _log(f"     download {ca_url} (served while serve runs), then:")
    _log(
        "     macOS:   sudo security add-trusted-cert -d"
        " -k /Library/Keychains/System.keychain ca.pem"
    )
    _log(
        "     Linux:   sudo cp ca.pem /usr/local/share/ca-certificates/theozolith.crt"
        " && sudo update-ca-certificates"
    )
    _log("     Firefox: uses its own store — Settings > Privacy & Security > Certificates > Import")
    _log("     iOS:     send ca.pem to the device, install the profile, then enable it under")
    _log("              Settings > General > About > Certificate Trust Settings")
    _log("")
    _log("3) provision nodes:    sudo theozolith join-token create   (one paste per box)")
    _log("")
    _log(f"backup: copy {settings.data_dir}/ minus cache/ to another device after")
    _log("enrolling nodes or adding secrets — GitHub is never a full backup (ADR-0024)")


def _init(args) -> int:
    """The unified first run (ADR-0023, amended by ADR-0034): master key ->
    control address -> CA/TLS with the box's IP in the SAN -> admin password
    -> systemd unit (root, bare metal) -> operator handoff. Re-run requires
    --force (which mints a new CA — invalidating every outstanding join
    string by construction — but never touches the master key: rotate-key
    owns that, with re-encryption)."""
    settings = load_settings()
    existing_ip = controltoml.read_control_ip(settings.config_repo)
    initialized = bool(existing_ip) or settings.key_path.is_file()
    if initialized and not args.force:
        raise SystemExit(
            f"error: {settings.data_dir} is already initialized"
            f" ({existing_ip or 'master key present'}) — pass --force to re-run."
            " A new CA invalidates the pinned ca.pem on EVERY provisioned node:"
            " the whole fleet fails TLS until each box gets one join-string"
            " re-paste. Device trust and every outstanding join string are"
            " invalidated too."
        )

    # Root-mediated init fails BEFORE any state is written: a bad
    # THEOZOLITH_DATA_DIR (or an unreachable executable) must not leave a
    # half-laid partition behind the eventual installer refusal.
    if os.geteuid() == 0 and not _running_in_container() and _systemd_present():
        _validated_root_data_dir(settings.data_dir)
        _service_executable()

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

    # The partition (ADR-0024): durability class legible from the path.
    settings.config_repo.mkdir(parents=True, exist_ok=True)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    settings.secrets_dir.chmod(0o700)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    _git_init(settings.config_repo)

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

    # 3. Per-deployment CA + server cert; the persisted IP rides the SAN so
    # the join exchange, every node dial, and CA-trusting browsers verify
    # cleanly (ADR-0023/0034).
    hosts = [ip] + [h for h in (args.host or []) if h != ip]
    try:
        provision(settings.tls_dir, hosts)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    # 4. The admin password: only its scrypt hash is stored (ADR-0023).
    _write_private(settings.admin_password_path, passwords.hash_password(_prompt_password(args)))
    Store(settings.cache_db_path).truncate_sessions()

    # 5. The systemd unit (root-mediated bare metal only; ADR-0034) — after
    # every artifact exists, so the chown hands the complete partition over.
    unit_installed = _install_systemd_unit(settings, port)

    # 6. The handoff.
    _print_handoff(settings, ip, port, unit_installed)
    return 0


def _set_password(args) -> int:
    """Change the admin password: rewrite the hash and truncate the session
    table — every browser session dies now (ADR-0023)."""
    settings = load_settings()
    if not settings.secrets_dir.is_dir():
        raise SystemExit("error: not initialized — run 'theozolith init' first")
    _write_private(settings.admin_password_path, passwords.hash_password(_prompt_password(args)))
    Store(settings.cache_db_path).truncate_sessions()
    _log("admin password updated; every browser session was invalidated")
    return 0


def _recover(args) -> int:
    """Two jobs, one command: recovery from a restored data-dir copy
    (ADR-0024) AND routine server-certificate renewal (PR #15 — the leaf
    now lives ~27 months, so re-minting it in place, same CA, is a normal
    maintenance act, not only a disaster move). Validate
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
    if not settings.admin_password_path.is_file():
        problems.append(f"missing admin password hash at {settings.admin_password_path}")
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

    try:
        cert, key = tls.remint_server_cert(settings.tls_dir, [ip])
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    _log(f"re-minted {cert} and {key} from the restored CA (SAN: {ip})")
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


def _tls_init(args) -> int:
    settings = load_settings()
    hosts = list(args.host or [])
    # The persisted control IP belongs in the certificate SAN (ADR-0031/
    # 0034 — nodes and browsers both dial it); extra --host entries (a LAN
    # alias, a public name) are additive.
    if settings.control_ip and settings.control_ip not in hosts:
        hosts.insert(0, settings.control_ip)
    if not hosts:
        raise SystemExit("error: pass --host, or run 'theozolith init' first (ADR-0031)")
    try:
        ca, cert, key = provision(settings.tls_dir, hosts)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
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
    url, token, ca = _admin_env(args)
    print(json.dumps(_call(url, "/api/v1/state", token=token, ca=ca), indent=2, sort_keys=True))
    return 0


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
        help="The unified first run (ADR-0023/0034; run under sudo on bare metal):"
        " master key, control address, CA/TLS with this box's IP in the SAN, admin"
        " password, systemd unit, and the operator handoff. Re-run requires --force.",
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
        help="Extra DNS name or IP for the certificate SAN (repeatable).",
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
        help="Validate the data dir (loudly, completely) and re-mint the server"
        " certificate from the existing CA — BOTH the restore-from-backup move"
        " (ADR-0024) and the routine certificate renewal (never a new CA; nodes"
        " untouched). Run as root on bare metal it also repairs the systemd"
        " service (ADR-0034).",
    )
    recover.add_argument(
        "--ip",
        help="This box's IP for the new SAN, persisted for future mints (default: the"
        " restored control_ip). A CHANGED IP costs one join-string re-paste per node;"
        " affected nodes will not appear in the unregistered view.",
    )
    recover.set_defaults(func=_recover)

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
        help="DNS name or IP (repeatable; the persisted control IP is always included)."
        " Wildcards are refused — every deployment gets its own TLS identity.",
    )
    tls_init.set_defaults(func=_tls_init)

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

    sub.add_parser("status", help="Fleet state as JSON.").set_defaults(func=_status)
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
