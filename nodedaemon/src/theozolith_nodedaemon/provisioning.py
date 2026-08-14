"""``theozolith-nodedaemon provision``: one paste turns a box into a node.

The join string (ADR-0023) carries the bootstrap address, the CA
fingerprint, and a short-lived join token::

    ozjoin1:<base64url, unpadded, of payload || checksum>
    payload  = exp:u32be || ca_sha256:32 || token:16 || addr:utf-8
    checksum = first 4 bytes of SHA-256(b"ozjoin1" || payload)

(the byte-for-byte mirror of control's joinstring.py — stdlib-only, no
shared import; a cross-component test pins the two together).

Flow, failing closed and loud at every step:

1. parse + checksum — a truncated paste is "malformed join string", never a
   confusing network error;
2. fetch the CA certificate and canonical control URL from the plaintext
   bootstrap listener at ``addr`` (three public values; code never rides
   this channel);
3. require exactly ONE certificate in the answer and hash it against the
   pinned fingerprint — on a bundle or a mismatch, abort with ZERO bytes
   transmitted to the control channel: possible MITM, or a stale join
   string after a CA rotation (re-inits are not attacks — the error says
   which to suspect). The PEM that goes forward is re-encoded from the
   verified DER, so the trust store can never hold a byte the fingerprint
   did not cover. A nonempty control URL from the same unauthenticated
   listener must parse as exactly https with a hostname — anything else
   aborts HERE, before the exchange, so a hostile bootstrap value can
   never consume the join token or register a node;
4. exchange the join token for this node's own non-expiring bearer token
   over TLS verified against the fetched-and-verified CA (the server cert
   carries the Control Node's IP in its SAN, so dialing by IP verifies);
5. persist CA, control URL (https only — the daemon attaches its bearer
   token to every request on it), node name, and node token under the
   daemon state dir; enable the systemd unit; the daemon's first heartbeat
   completes enrollment (provisioning IS registration).

The node channel is IP-only (ADR-0023 as amended 2026-07-28): the persisted
control URL is the IP-based address this flow just verified — nodes never
resolve the deployment's slug hostname (browser-only) and carry zero DNS
dependency. A Control Node IP change is recovered by re-pasting one fresh
join string here, which rotates this node's token and replaces the
persisted state in place.

There is no fingerprint-less manual path: verification is the machine's
job, and trust flows from where the join string came from (an authenticated
dashboard session or SSH on the Control Node).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from theozolith_nodedaemon.config import DEFAULT_STATE_DIR

PREFIX = "ozjoin1"
TOKEN_BYTES = 16
# The bootstrap listener's fixed default port (ADR-0026); a portless addr
# means "the default", never http-80 — mirrored constant, no shared import.
DEFAULT_BOOTSTRAP_PORT = 6965
_FIXED = 4 + 32 + TOKEN_BYTES  # exp + fingerprint + token, before addr

MALFORMED = "malformed join string"
MISMATCH = (
    "CA fingerprint mismatch: possible MITM, or a stale join string after a CA"
    " rotation (a re-run of 'theozolith init --force' invalidates every"
    " outstanding join string by construction — mint a fresh one). Nothing was"
    " transmitted to the control channel."
)
REJECTED = (
    "join token rejected (expired, consumed, or revoked) — nothing was persisted"
    " on this node; mint a fresh join string with 'theozolith join-token create'"
)
BUNDLE = (
    "the bootstrap listener served more than one certificate where exactly one"
    " CA belongs — refusing the bundle (possible MITM): fingerprinting only the"
    " first block while trusting the rest would let an appended CA into the"
    " trust store. Nothing was transmitted to the control channel."
)
INSECURE_BOOTSTRAP = (
    "the bootstrap listener served a non-HTTPS or malformed control URL —"
    " refusing before the join exchange: nothing was transmitted to the"
    " control channel, the join token was not consumed, and nothing was"
    " persisted"
)
INSECURE_URL = (
    "refusing to persist the non-HTTPS control URL the exchange answered —"
    " the daemon attaches this node's bearer token to every request on it."
    " Nothing was persisted on this node, but the exchange itself already ran:"
    " the join token is consumed and the node may be registered — mint a fresh"
    " join string to retry"
)

# What provisioning persists under the state dir; the daemon's config reads
# these as its fallback for the corresponding environment variables.
CA_FILE = "ca.pem"
CONTROL_URL_FILE = "control-url"
NODE_TOKEN_FILE = "node-token"
NODE_NAME_FILE = "node-name"

SYSTEMD_UNIT = "theozolith-nodedaemon"


class ProvisionError(RuntimeError):
    """The provisioning flow stopped; the message says exactly why."""


@dataclass(frozen=True)
class JoinPayload:
    addr: str  # bootstrap listener: host or host:port
    ca_sha256: str  # pinned CA fingerprint, hex
    token: bytes  # the join token (disposable; never the node token)
    expires_at: int

    @property
    def host(self) -> str:
        return self.addr.rsplit(":", 1)[0] if ":" in self.addr else self.addr

    @property
    def bootstrap_port(self) -> int:
        if ":" in self.addr:
            return int(self.addr.rsplit(":", 1)[1])
        return DEFAULT_BOOTSTRAP_PORT


def parse_join_string(text: str) -> JoinPayload:
    text = text.strip()
    version, sep, encoded = text.partition(":")
    if not sep or not encoded:
        raise ProvisionError(f"{MALFORMED}: expected '{PREFIX}:<payload>'")
    if version != PREFIX:
        if version.startswith("ozjoin"):
            raise ProvisionError(
                f"unsupported join-string version {version!r} — this node distribution"
                f" speaks {PREFIX}; update it"
            )
        raise ProvisionError(f"{MALFORMED}: expected '{PREFIX}:<payload>'")
    try:
        blob = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ProvisionError(f"{MALFORMED}: {exc}") from None
    if len(blob) < _FIXED + 1 + 4:
        raise ProvisionError(f"{MALFORMED}: truncated paste")
    payload, checksum = blob[:-4], blob[-4:]
    expected = hashlib.sha256(PREFIX.encode("ascii") + payload).digest()[:4]
    if checksum != expected:
        raise ProvisionError(f"{MALFORMED}: checksum mismatch (truncated or altered paste)")
    try:
        addr = payload[_FIXED:].decode("utf-8")
    except UnicodeDecodeError:
        raise ProvisionError(f"{MALFORMED}: address is not UTF-8") from None
    parsed = JoinPayload(
        addr=addr,
        ca_sha256=payload[4 : 4 + 32].hex(),
        token=payload[4 + 32 : _FIXED],
        expires_at=int.from_bytes(payload[:4], "big"),
    )
    if ":" in parsed.addr:
        try:
            port = int(parsed.addr.rsplit(":", 1)[1])
        except ValueError:
            raise ProvisionError(f"{MALFORMED}: bad bootstrap port in {parsed.addr!r}") from None
        if not 0 < port < 65536:
            raise ProvisionError(f"{MALFORMED}: bad bootstrap port in {parsed.addr!r}")
    return parsed


def inspect_text(payload: JoinPayload, *, now: float | None = None) -> str:
    """--inspect: pretty-print without acting (the debuggability the opaque
    blob would otherwise cost)."""
    now = time.time() if now is None else now
    remaining = payload.expires_at - now
    state = f"expires in {remaining / 60:.0f}m" if remaining > 0 else "EXPIRED (server enforces)"
    return "\n".join(
        [
            f"version:         {PREFIX}",
            f"bootstrap addr:  {payload.host}:{payload.bootstrap_port}",
            f"ca fingerprint:  sha256:{payload.ca_sha256}",
            f"join token:      {payload.token.hex()[:8]}… ({TOKEN_BYTES} bytes; disposable)",
            f"expiry:          {payload.expires_at} ({state})",
        ]
    )


# -- the two channels (both injectable for tests) --------------------------------


def _http_get(url: str) -> bytes:
    """Plaintext GET against the bootstrap listener — public values only."""
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProvisionError(f"cannot reach the bootstrap listener at {url}: {exc}") from exc


def _https_post_json(url: str, body: dict, ca_pem: bytes) -> tuple[int, bytes]:
    """POST over TLS verified against exactly the fetched-and-verified CA."""
    context = ssl.create_default_context(cadata=ca_pem.decode("ascii", errors="strict"))
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "theozolith-provision"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProvisionError(f"cannot reach the Control Node at {url}: {exc}") from exc


def pem_to_der(pem: bytes) -> bytes:
    """The single CERTIFICATE block, base64-decoded — the DER bytes the
    fingerprint is computed over (matches control's cryptography-based
    fingerprint; this side stays stdlib-only). Exactly one block, refused
    BEFORE fingerprinting: everything in the answer becomes the exchange's
    trust store, so a block the fingerprint does not cover must never
    exist in the first place."""
    text = pem.decode("ascii", errors="replace")
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    if text.count(begin) == 0 or text.count(end) == 0:
        raise ProvisionError("the bootstrap listener answered no PEM certificate")
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ProvisionError(BUNDLE)
    start = text.find(begin)
    stop = text.find(end)
    if stop < start:
        raise ProvisionError("the bootstrap listener answered no PEM certificate")
    body = "".join(text[start + len(begin) : stop].split())
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProvisionError(f"the served CA certificate does not decode: {exc}") from None


def der_to_pem(der: bytes) -> bytes:
    """One canonical PEM block re-encoded from the verified DER — what the
    exchange trusts and provisioning persists. Rebuilding from the checked
    bytes guarantees the trust store holds exactly the certificate the
    fingerprint covered, nothing around it (64-char lines: byte-identical
    to control's cryptography PEM output)."""
    encoded = base64.b64encode(der).decode("ascii")
    lines = "\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode("ascii")


def _https_parts(url: str) -> tuple[str, int] | None:
    """(hostname, effective port) of an exactly-parsed https URL — None for
    anything else (non-https scheme, missing hostname, garbled port). Exact
    parsing, never a prefix check: a scheme like 'httpsneak' must not pass
    for https. Total over arbitrary input: urlsplit and the hostname
    property raise ValueError themselves (unmatched IPv6 brackets,
    NFKC-invalid netlocs) before .port is ever reached, so the whole parse
    sits inside one boundary — every malformed URL is None here, never a
    leaked ValueError that would bypass the ProvisionError the flow
    documents. An explicit :0 is refused: 443 fills in only when the port
    is omitted, and 0 names no dialable origin."""
    try:
        split = urllib.parse.urlsplit(url)
        hostname = split.hostname
        port = split.port
    except ValueError:
        return None
    if split.scheme != "https" or not hostname or port == 0:
        return None
    return hostname, 443 if port is None else port


def provision(
    join: str,
    *,
    state_dir: Path,
    node_name: str,
    http_get=_http_get,
    https_post=_https_post_json,
    runner=subprocess.run,
    enable_systemd: bool = True,
    log=print,
) -> None:
    payload = parse_join_string(join)
    bootstrap = f"http://{payload.host}:{payload.bootstrap_port}"

    # Fetch, then VERIFY BEFORE ANY TRANSMISSION to the control channel:
    # nothing — not the token, not the node name, not a TCP SYN — goes to
    # the HTTPS endpoint until the fetched CA hashes to the pinned value.
    ca_der = pem_to_der(http_get(f"{bootstrap}/ca.pem"))
    fingerprint = hashlib.sha256(ca_der).hexdigest()
    if fingerprint != payload.ca_sha256:
        raise ProvisionError(MISMATCH)
    # Trust EXACTLY what was verified: the PEM the exchange dials with and
    # provisioning persists is re-encoded from the checked DER, never the
    # fetched bytes.
    ca_pem = der_to_pem(ca_der)
    control_url = http_get(f"{bootstrap}/control-url").decode("utf-8", errors="replace").strip()
    exchange_port = 443
    if control_url:
        # The same unauthenticated listener an attacker would own: a nonempty
        # value must parse as exactly https with a hostname BEFORE the
        # exchange runs — the join token is single-use and the exchange
        # mutates Control Node state, so a hostile value has to die while
        # both are still intact. (Empty stays allowed: a dev Control Node
        # with no public origin; the exchange answer or its fallback fills
        # the canonical URL below.)
        parts = _https_parts(control_url)
        if parts is None:
            raise ProvisionError(f"{INSECURE_BOOTSTRAP} (got {control_url!r})")
        exchange_port = parts[1]

    # The exchange dials the join-string host (DNS for the canonical name
    # may not exist yet); the server cert's IP SAN makes verification pass.
    status, body = https_post(
        f"https://{payload.host}:{exchange_port}/api/v1/join/exchange",
        {"node": node_name, "token": payload.token.hex()},
        ca_pem,
    )
    if status == 401:
        raise ProvisionError(REJECTED)
    if status >= 400:
        raise ProvisionError(
            f"the Control Node refused the exchange (HTTP {status}):"
            f" {body.decode(errors='replace')[:200]}"
        )
    try:
        answer = json.loads(body)
        node_token = str(answer["node_token"])
        canonical = str(answer.get("control_url") or control_url)
    except (json.JSONDecodeError, KeyError) as exc:
        raise ProvisionError(f"the exchange answered no node token: {exc}") from None
    if not canonical:
        # A dev Control Node with no public origin: heartbeat where the
        # exchange happened.
        canonical = f"https://{payload.host}:{exchange_port}"
    if _https_parts(canonical) is None:
        # The exchange answer rides verified TLS, but a garbled or hostile
        # server answer must still fail closed before the URL the daemon
        # dials with its bearer token is persisted (the unauthenticated
        # bootstrap value was already gated before the exchange).
        raise ProvisionError(f"{INSECURE_URL} (got {canonical!r})")

    _persist(
        state_dir,
        {
            CA_FILE: ca_pem.decode("ascii"),
            CONTROL_URL_FILE: canonical,
            NODE_NAME_FILE: node_name,
            NODE_TOKEN_FILE: node_token,
        },
    )
    log(f"provisioned node {node_name!r}: per-node token persisted under {state_dir}")
    log(f"control URL: {canonical} (CA pinned; IP-based — this node needs no DNS)")

    if enable_systemd and shutil.which("systemctl"):
        for argv in (
            ["systemctl", "enable", SYSTEMD_UNIT],
            ["systemctl", "restart", SYSTEMD_UNIT],
        ):
            proc = runner(argv, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                log(
                    f"warning: {' '.join(argv)} failed:"
                    f" {(proc.stderr or proc.stdout or '').strip()[:200]}"
                )
                break
        else:
            log(f"systemd unit {SYSTEMD_UNIT} enabled and (re)started — first heartbeat follows")
    else:
        log("no systemd here: start 'theozolith-nodedaemon' yourself for the first heartbeat")


def _persist(state_dir: Path, files: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    # The installer runs provision as root before the unit ever starts: hand
    # each file to whoever owns the state dir (the service user). The chown
    # rides the temp fd (fchown), never a path, and its result is surfaced —
    # a file the service user cannot read is a provisioning failure, not a
    # silence to swallow.
    stat = state_dir.stat()
    owner = (stat.st_uid, stat.st_gid) if os.geteuid() == 0 and stat.st_uid != 0 else None
    for name, content in files.items():
        _persist_one(state_dir / name, content, private=name == NODE_TOKEN_FILE, owner=owner)


def _persist_one(
    path: Path, content: str, *, private: bool, owner: tuple[int, int] | None
) -> None:
    """Write one state file atomically and symlink-safely.

    mkstemp creates a fresh regular file (no destination symlink to follow),
    ``fchmod``/``fchown`` set mode and owner on the fd, then ``os.replace``
    swaps it in — a symlink pre-planted at ``path`` is *replaced*, never
    written through, so a compromised service account that owns the state dir
    cannot steer a root-run reprovision onto an arbitrary file (OZ-02). A
    non-regular destination is refused loudly as a tamper signal."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProvisionError(f"refusing to write {path}: destination is not a regular file")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(fd, 0o600 if private else 0o644)
            if owner is not None:
                os.fchown(fd, owner[0], owner[1])
            handle.write(content.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(fd)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-nodedaemon provision",
        description=(
            "Join this box to a TheOzolith deployment with one pasted join string"
            " (mint it with 'theozolith join-token create' on the Control Node)."
        ),
    )
    parser.add_argument("join_string", help="the ozjoin1:… paste (requires one; no manual path)")
    parser.add_argument(
        "--inspect", action="store_true", help="pretty-print the payload without acting"
    )
    parser.add_argument(
        "--node", default=socket.gethostname(), help="node name (default: hostname)"
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("THEOZOLITH_STATE_DIR") or DEFAULT_STATE_DIR,
        help=f"daemon state dir (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--no-systemd", action="store_true", help="persist only; do not touch systemd"
    )
    args = parser.parse_args(argv)
    try:
        if args.inspect:
            print(inspect_text(parse_join_string(args.join_string)), flush=True)
            return 0
        provision(
            args.join_string,
            state_dir=Path(args.state_dir),
            node_name=args.node,
            enable_systemd=not args.no_systemd,
        )
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
