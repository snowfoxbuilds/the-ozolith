"""The ``theozolith-control`` CLI: serve, TLS init, secrets, commands, state.

Secret entry happens here and through the dashboard's web form — both write
through the same PUT /api/v1/secrets/{name} API to the same encrypted store
(NODE-SUBSTRATE.md). Everything except ``serve``, ``tls-init``, and
``rotate-key`` talks HTTP to a running Control Node; the admin token comes
from THEOZOLITH_ADMIN_TOKEN and the server URL from CONTROL_NODE_URL.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

from theozolith_worker.config import ConfigError, env_value

from theozolith_control import janitor, origin
from theozolith_control.crypto import SecretBox, ensure_key_file, generate_key
from theozolith_control.origin import OriginError
from theozolith_control.settings import ControlSettings, load_settings
from theozolith_control.store import Store
from theozolith_control.tls import CERT_FILE, KEY_FILE, provision


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
    url = args.url or env_value(os.environ, "CONTROL_NODE_URL")
    if not url:
        raise SystemExit("error: set CONTROL_NODE_URL or pass --url")
    token = env_value(os.environ, "THEOZOLITH_ADMIN_TOKEN")
    if not token:
        raise SystemExit("error: set THEOZOLITH_ADMIN_TOKEN (or its _FILE form)")
    return url, token, args.ca or env_value(os.environ, "THEOZOLITH_TLS_CA")


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
    if not args.insecure_dev:
        # Production requires the persistent randomized canonical origin
        # (ADR-0019): it arms exact Host/Origin enforcement and is the one
        # name browsers may reach this deployment by.
        try:
            origin.validate_canonical_host(settings.canonical_host)
        except OriginError as exc:
            raise SystemExit(
                f"error: {exc} — production startup requires the installer-generated"
                " canonical origin (run 'theozolith-control origin-init';"
                " --insecure-dev for local dev only)"
            ) from exc
    settings = dataclasses.replace(settings, secrets_channel_ok=True, public_port=args.port)
    store = Store(settings.db_path)
    box = SecretBox(
        env_value(os.environ, "THEOZOLITH_MASTER_KEY") or ensure_key_file(settings.key_path)
    )
    app = create_app(settings, store, box)

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

    _log(f"control node on {args.host}:{args.port} (TLS {'on' if tls else 'OFF — dev mode'})")
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
    return 0


# -- local maintenance ----------------------------------------------------------


def _origin_init(args) -> int:
    """Provision the canonical origin (ADR-0019): one randomized hostname
    with 128 bits of slug entropy, persisted in the data dir."""
    settings = load_settings()
    existing = origin.read_canonical_host(settings.data_dir)
    if existing and not args.force:
        raise SystemExit(
            f"error: canonical host already provisioned ({existing}) — the origin is"
            " persistent by design; pass --force to mint a new one (DNS, TLS, and"
            " every CONTROL_NODE_URL must then be repointed)"
        )
    host = origin.compose_host(origin.generate_slug(), args.base_domain)
    path = origin.write_canonical_host(settings.data_dir, host)
    _log(f"wrote {path}")
    _log(f"canonical host: {host}")
    _log(
        "next: create a trusted-network-only DNS record (or hosts entries) for it and run"
        " 'theozolith-control tls-init' — the Control Node must have no public ingress path"
    )
    return 0


def _tls_init(args) -> int:
    settings = load_settings()
    hosts = list(args.host or [])
    # The canonical origin (when provisioned) belongs in the certificate;
    # extra --host entries (an IP, a LAN alias) are additive.
    canonical = settings.canonical_host
    if canonical and canonical not in hosts:
        hosts.insert(0, canonical)
    if not hosts:
        raise SystemExit(
            "error: pass --host, or provision the canonical origin first"
            " ('theozolith-control origin-init')"
        )
    ca, cert, key = provision(settings.tls_dir, hosts)
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
    store = Store(settings.db_path)
    names = store.secret_names()
    store.replace_secret_tokens(
        {name: new.encrypt(old.decrypt(store.get_secret_token(name) or "")) for name in names}
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
    store = Store(settings.db_path)
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
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument(
        "--insecure-dev",
        action="store_true",
        help="Serve plain HTTP and allow secret traffic anyway. Local development ONLY.",
    )
    serve.set_defaults(func=_serve)

    origin_init = sub.add_parser(
        "origin-init",
        help="Provision the deployment's canonical origin: a randomized hostname with"
        " 128 bits of entropy under --base-domain, resolved by trusted-network DNS only.",
    )
    origin_init.add_argument(
        "--base-domain",
        default=origin.DEFAULT_BASE_DOMAIN,
        help=f"Base domain for the canonical hostname (default: {origin.DEFAULT_BASE_DOMAIN},"
        " inside the ICANN-reserved private namespace).",
    )
    origin_init.add_argument(
        "--force", action="store_true", help="Replace an already-provisioned canonical host."
    )
    origin_init.set_defaults(func=_origin_init)

    tls_init = sub.add_parser("tls-init", help="Mint a self-signed CA + server certificate.")
    tls_init.add_argument(
        "--host",
        action="append",
        help="DNS name or IP (repeatable; the canonical host is always included)."
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
    command.add_argument("verb", choices=["drain", "recycle", "update", "rebuild"])
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
