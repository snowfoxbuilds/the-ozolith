"""The ``theozolith-control`` CLI: the service-admin surface for the machine
it runs on (ADR-0023) — ``init``, ``origin-init``, ``tls-init``, ``serve``,
``recover`` — plus local maintenance (``set-password``, ``rotate-key``,
``janitor --once``) and HTTP-driven operator subcommands.

``init`` is the unified first-run command: master key → origin → CA/TLS
(with the box's IP in the SAN) → admin password → operator handoff. All
state lands under the ``~/.theozolith/`` partition (ADR-0024); ``recover``
validates a restored copy loudly and re-mints the server certificate from
the restored CA so nodes reconnect untouched.

Secret entry happens here and through the dashboard's web form — both write
through the same PUT /api/v1/secrets/{name} API to the same encrypted store
(NODE-SUBSTRATE.md). The HTTP subcommands take the admin token from
THEOZOLITH_ADMIN_TOKEN or the init-written ``secrets/admin-token`` file, and
the server URL from CONTROL_NODE_URL or the persisted public origin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
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
from theozolith_control.store import Store
from theozolith_control.tls import CA_FILE, CA_KEY_FILE, CERT_FILE, KEY_FILE, provision


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
            "User-Agent": "theozolith-control-cli",
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
    url = args.url or env_value(os.environ, "CONTROL_NODE_URL") or settings.public_origin
    if not url:
        raise SystemExit(
            "error: no Control Node URL — set CONTROL_NODE_URL, pass --url, or run"
            " 'theozolith-control init' on this box first"
        )
    token = settings.admin_token
    if not token:
        raise SystemExit(
            "error: no admin token — run 'theozolith-control init' or set"
            " THEOZOLITH_ADMIN_TOKEN (or its _FILE form)"
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
            f"error: no TLS material at {settings.tls_dir} — run 'theozolith-control tls-init"
            " --host <name-or-ip>' first (TLS is mandatory; --insecure-dev for local dev only)"
        )
    if not args.insecure_dev or settings.public_origin:
        # Production requires the persistent randomized public origin
        # (ADR-0019) — from the origin-init artifact or the
        # THEOZOLITH_PUBLIC_ORIGIN override: it arms exact Host/Origin
        # enforcement and is the one origin browsers may reach this
        # deployment by. It is independent of --host/--port (the bind
        # address); a configured-but-invalid origin fails closed in dev too.
        try:
            origin.parse_public_origin(settings.public_origin)
        except OriginError as exc:
            raise SystemExit(
                f"error: {exc} — production startup requires the generated public"
                " origin (run 'theozolith-control origin-init' or set"
                " THEOZOLITH_PUBLIC_ORIGIN; --insecure-dev for local dev only)"
            ) from exc
    if not args.insecure_dev and not settings.admin_password_path.is_file():
        raise SystemExit(
            "error: no admin password is set — run 'theozolith-control init' first"
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

    # The plaintext bootstrap listener (ADR-0023): CA cert, public origin,
    # canonical control URL, on its own port — never on the HTTPS app. It
    # exists to serve provisioning, so it runs exactly when a CA does.
    bootstrap_server = None
    ca_path = settings.tls_dir / CA_FILE
    if ca_path.is_file():
        bootstrap_server = bootstrap.BootstrapServer(
            ca_pem=ca_path.read_bytes(),
            origin=settings.public_origin,
            control_url=settings.public_origin,
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
    if settings.public_origin:
        _log(f"public origin (exact browser Host/Origin): {settings.public_origin}")
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


def _mint_origin(settings: ControlSettings, base_domain: str, port: int | None) -> str:
    """Mint and persist a fresh public origin as the read-only control.toml
    field (ADR-0022 as amended by ADR-0024)."""
    try:
        text = origin.compose_origin(origin.generate_slug(), base_domain, port=port)
    except OriginError as exc:
        raise SystemExit(f"error: {exc}") from exc
    controltoml.write_public_origin(settings.config_repo, text, log=_log)
    return text


def _origin_init(args) -> int:
    """Provision the public origin (ADR-0019): one https origin with a
    128-bit-entropy hostname slug, persisted as the read-only [control]
    field of control.toml in the Config Repo (ADR-0024). Default HTTPS
    omits the port; --port includes a nonstandard external port
    explicitly. Independent of the serve bind host/port."""
    settings = load_settings()
    existing = controltoml.read_public_origin(settings.config_repo)
    if existing and not args.force:
        raise SystemExit(
            f"error: public origin already provisioned ({existing}) — the origin is"
            " persistent by design; pass --force to mint a new one (DNS, TLS, and"
            " every CONTROL_NODE_URL must then be repointed)"
        )
    text = _mint_origin(settings, args.base_domain, args.port)
    _log(f"public origin: {text}")
    _log(
        "next: create a trusted-network-only DNS record (or hosts entries) for its hostname"
        " and run 'theozolith-control tls-init' — the Control Node must have no public"
        " ingress path"
    )
    return 0


# -- the unified first run (ADR-0023) --------------------------------------------


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


def _print_handoff(settings: ControlSettings, origin_text: str, ip: str) -> None:
    """The operator handoff (ADR-0023): generation is fully automated; the
    two irreducibly manual actions — DNS record, CA trust per device — get
    exact copy-pasteable instructions, not prose."""
    try:
        hostname = origin.parse_public_origin(origin_text).hostname
    except OriginError:
        hostname = ""
    ca_url = f"http://{ip}:{settings.bootstrap_port}/ca.pem"
    _log("")
    _log("== Control Node initialized ==")
    _log(f"dashboard: {origin_text}")
    _log("")
    _log("1) DNS (trusted network only) — hosts entry on every operator device,")
    _log("   or one router/private-DNS record:")
    _log(f"     {ip} {hostname}")
    _log("")
    _log("2) Trust the CA on operator devices (nodes pin it automatically when")
    _log(f"   provisioned). Download: {ca_url}")
    _log("     macOS:   sudo security add-trusted-cert -d -k /Library/Keychains/System.keychain ca.pem")
    _log("     Linux:   sudo cp ca.pem /usr/local/share/ca-certificates/theozolith.crt && sudo update-ca-certificates")
    _log("     Firefox: uses its own store — Settings > Privacy & Security > Certificates > Import")
    _log("     iOS:     send ca.pem to the device, install the profile, then enable it under")
    _log("              Settings > General > About > Certificate Trust Settings")
    _log("")
    _log("3) start serving:      theozolith-control serve")
    _log("4) provision nodes:    theozolith join-token create   (one paste per box)")
    _log("")
    _log(f"backup: copy {settings.data_dir}/ minus cache/ to another device after")
    _log("enrolling nodes or adding secrets — GitHub is never a full backup (ADR-0024)")


def _init(args) -> int:
    """The unified first run (ADR-0023): master key -> origin -> CA/TLS with
    the box's IP in the SAN -> admin password -> operator handoff. Re-run
    requires --force (which mints a new origin and CA — invalidating every
    outstanding join string by construction — but never touches the master
    key: rotate-key owns that, with re-encryption)."""
    settings = load_settings()
    existing_origin = controltoml.read_public_origin(settings.config_repo)
    initialized = bool(existing_origin) or settings.key_path.is_file()
    if initialized and not args.force:
        raise SystemExit(
            f"error: {settings.data_dir} is already initialized"
            f" ({existing_origin or 'master key present'}) — pass --force to re-run"
            " (a new origin and CA invalidate DNS entries, device trust, and every"
            " outstanding join string)"
        )

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

    # 2. The public origin, a read-only control.toml field.
    origin_text = _mint_origin(settings, args.base_domain, args.port)

    # 3. Per-deployment CA + server cert; the box's IP rides the SAN so
    # nodes and browsers reaching it by IP verify cleanly (ADR-0023).
    ip = args.ip or bootstrap.detect_host_ip()
    hostname = origin.parse_public_origin(origin_text).hostname
    hosts = [hostname, ip] + [h for h in (args.host or []) if h not in (hostname, ip)]
    try:
        provision(settings.tls_dir, hosts)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    # 4. The admin password: only its scrypt hash is stored (ADR-0023).
    _write_private(settings.admin_password_path, passwords.hash_password(_prompt_password(args)))
    Store(settings.cache_db_path).truncate_sessions()

    # 5. The handoff.
    _print_handoff(settings, origin_text, ip)
    return 0


def _set_password(args) -> int:
    """Change the admin password: rewrite the hash and truncate the session
    table — every browser session dies now (ADR-0023)."""
    settings = load_settings()
    if not settings.secrets_dir.is_dir():
        raise SystemExit("error: not initialized — run 'theozolith-control init' first")
    _write_private(settings.admin_password_path, passwords.hash_password(_prompt_password(args)))
    Store(settings.cache_db_path).truncate_sessions()
    _log("admin password updated; every browser session was invalidated")
    return 0


def _recover(args) -> int:
    """Recovery from a restored ~/.theozolith/ copy (ADR-0024): validate
    loudly and COMPLETELY — every missing or invalid artifact enumerated in
    one pass, exit nonzero — then re-mint the server certificate from the
    restored CA with this box's IP in the SAN. Nodes pin the CA and hold
    non-expiring tokens restored with store.db: they reconnect untouched
    once DNS points here."""
    settings = load_settings()
    problems: list[str] = []

    origin_text = controltoml.read_public_origin(settings.config_repo)
    if not settings.config_repo.is_dir():
        problems.append(f"missing Config Repo at {settings.config_repo}")
    elif not origin_text:
        problems.append(f"{settings.config_repo / controltoml.CONTROL_TOML}: no public origin")
    else:
        try:
            origin.parse_public_origin(origin_text)
        except OriginError as exc:
            problems.append(f"public origin invalid: {exc}")
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

    ip = args.ip or bootstrap.detect_host_ip()
    hostname = origin.parse_public_origin(origin_text).hostname
    try:
        cert, key = tls.remint_server_cert(settings.tls_dir, [hostname, ip])
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    _log(f"re-minted {cert} and {key} from the restored CA (SAN: {hostname}, {ip})")
    _log("recovery validated. next:")
    _log(f"  1) update the private-side DNS/hosts record: {ip} {hostname}")
    _log("  2) start serving: theozolith-control serve")
    _log("nodes pin the CA and reconnect on their own backoff — no node-side action.")
    _log("sessions and cached state died with cache/ (by design): re-log-in; one")
    _log("heartbeat round re-warms the fleet. nodes provisioned after the backup")
    _log("surface as unregistered — the re-provision worklist.")
    return 0


def _tls_init(args) -> int:
    settings = load_settings()
    hosts = list(args.host or [])
    # The public origin's hostname (when provisioned) belongs in the
    # certificate SAN — the hostname alone, never a port; extra --host
    # entries (an IP, a LAN alias) are additive.
    if settings.public_origin:
        try:
            hostname = origin.parse_public_origin(settings.public_origin).hostname
        except OriginError as exc:
            raise SystemExit(f"error: {exc}") from exc
        if hostname not in hosts:
            hosts.insert(0, hostname)
    if not hosts:
        raise SystemExit(
            "error: pass --host, or provision the public origin first"
            " ('theozolith-control origin-init')"
        )
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
        {name: new.encrypt(old.decrypt(secret_store.get_secret_token(name) or "")) for name in names}
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
        prog="theozolith-control",
        description="TheOzolith Control Node: serve the control plane and operate it.",
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
        help="Uvicorn bind port only — the public origin (browser Host/Origin) is"
        " independent of it (origin-init / THEOZOLITH_PUBLIC_ORIGIN).",
    )
    serve.add_argument(
        "--insecure-dev",
        action="store_true",
        help="Serve plain HTTP and allow secret traffic anyway. Local development ONLY.",
    )
    serve.set_defaults(func=_serve)

    init = sub.add_parser(
        "init",
        help="The unified first run (ADR-0023): master key, public origin, CA/TLS with"
        " this box's IP in the SAN, admin password, and the operator handoff."
        " Re-run requires --force.",
    )
    init.add_argument(
        "--base-domain",
        default=origin.DEFAULT_BASE_DOMAIN,
        help=f"Base domain for the origin's hostname (default: {origin.DEFAULT_BASE_DOMAIN}).",
    )
    init.add_argument(
        "--port",
        type=int,
        default=None,
        help="Nonstandard EXTERNAL https port browsers dial (default: none).",
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
        help="Re-initialize: mints a NEW origin and CA (outstanding join strings become"
        " invalid by construction; DNS and device trust must be redone). The master"
        " key and stored secrets are never touched.",
    )
    init.set_defaults(func=_init)

    recover = sub.add_parser(
        "recover",
        help="Validate a restored ~/.theozolith/ copy (loudly, completely) and re-mint"
        " the server certificate from the restored CA — nodes reconnect untouched"
        " (ADR-0024).",
    )
    recover.add_argument("--ip", help="This box's IP for the new SAN (default: auto-detected).")
    recover.set_defaults(func=_recover)

    set_password = sub.add_parser(
        "set-password",
        help="Change the admin password (stores only the scrypt hash) and invalidate"
        " every browser session.",
    )
    set_password.set_defaults(func=_set_password)

    origin_init = sub.add_parser(
        "origin-init",
        help="Provision the deployment's public origin: https:// plus a randomized hostname"
        " with 128 bits of entropy under --base-domain, resolved by trusted-network DNS"
        " only. Independent of the serve bind host/port.",
    )
    origin_init.add_argument(
        "--base-domain",
        default=origin.DEFAULT_BASE_DOMAIN,
        help=f"Base domain for the origin's hostname (default: {origin.DEFAULT_BASE_DOMAIN},"
        " inside the ICANN-reserved private namespace).",
    )
    origin_init.add_argument(
        "--port",
        type=int,
        default=None,
        help="Nonstandard EXTERNAL port browsers dial, included explicitly in the origin"
        " (default: none — https omits :443). Not the Uvicorn bind port.",
    )
    origin_init.add_argument(
        "--force", action="store_true", help="Replace an already-provisioned public origin."
    )
    origin_init.set_defaults(func=_origin_init)

    tls_init = sub.add_parser("tls-init", help="Mint a self-signed CA + server certificate.")
    tls_init.add_argument(
        "--host",
        action="append",
        help="DNS name or IP (repeatable; the public origin's hostname is always included)."
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

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
