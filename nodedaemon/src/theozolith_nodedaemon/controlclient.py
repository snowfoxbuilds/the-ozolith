"""The daemon's client for the Control Node channel.

HTTPS with bearer-token auth at a configured URL; TLS via a pinned CA bundle
(self-signed or install-provisioned — NODE-SUBSTRATE.md). Unreachability is
an expected state, not an error: ``heartbeat`` answers None and the caller
falls back to cached desired state (ADR-0006). The one hard rule is the
channel invariant's TLS clause: secret values — the per-node bearer token
included, and it rides EVERY request — never transit plain HTTP unless the
operator explicitly opted into insecure dev mode. Construction therefore
refuses anything but an exactly-parsed https URL (plain http only under the
dev flag), and the transport refuses every redirect that would carry the
token off that origin: a 3xx is followed only when its target parses to the
SAME scheme://host:port — an HTTPS→HTTP downgrade or cross-origin hop is a
``ControlError`` raised BEFORE the redirected request is issued, so the
Authorization header never leaves the configured origin.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# One HTTP exchange: (method, url, headers, body) -> (status, response body).
# Injectable so tests can drive the daemon against an in-process app.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class ControlError(RuntimeError):
    """The Control Node answered, but refused or garbled the request."""


class ControlUnreachable(RuntimeError):
    """No answer at all — degraded mode territory, never fatal."""


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) of an exactly-parsed http(s) URL —
    None for anything else. Exact parsing, never a prefix check: a scheme
    like 'httpsevil' or a URL with no usable hostname must not pass for
    https."""
    split = urllib.parse.urlsplit(url)
    if split.scheme not in ("https", "http") or not split.hostname:
        return None
    try:
        port = split.port
    except ValueError:
        return None
    return split.scheme, split.hostname, port or (443 if split.scheme == "https" else 80)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx to the transport instead of following it: urllib's
    default handler re-sends the request — Authorization header included —
    to whatever host Location names, cross-origin and plain-HTTP alike."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REDIRECT_LIMIT = 5


def _urllib_transport(ca: str | None) -> Transport:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=context)
        )
        origin = _origin(url)
        target = url
        for _ in range(_REDIRECT_LIMIT):
            request = urllib.request.Request(target, data=body, method=method, headers=headers)
            try:
                with opener.open(request, timeout=15) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in _REDIRECT_STATUSES:
                    return exc.code, exc.read()
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise ControlError(
                        f"{method} {target}: HTTP {exc.code} redirect without a Location"
                    ) from None
                # The policy gate, applied BEFORE the redirected request is
                # issued: only a hop that stays on the configured origin may
                # carry the bearer token (same method and body re-sent — the
                # control channel never wants a 303-style method switch).
                target = urllib.parse.urljoin(target, location)
                if origin is None or _origin(target) != origin:
                    raise ControlError(
                        f"refusing redirect to {target!r}: it leaves the configured"
                        " control origin (a cross-origin hop or HTTPS→HTTP downgrade"
                        " would hand the bearer token to someone else)"
                    ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ControlUnreachable(str(exc)) from exc
        raise ControlError(f"{method} {url}: too many redirects")

    return transport


class ControlClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        ca: str | None = None,
        insecure_dev: bool = False,
        transport: Transport | None = None,
    ):
        origin = _origin(url)
        if origin is None or (origin[0] != "https" and not insecure_dev):
            raise ControlError(
                f"refusing control URL {url!r}: the per-node bearer token rides"
                " every request, so the URL must parse as exactly https with a"
                " hostname (TLS is mandatory; THEOZOLITH_INSECURE_DEV=1 allows"
                " plain http for local dev only)"
            )
        self._url = url.rstrip("/")
        self._token = token
        self._transport = transport or _urllib_transport(ca)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        status, payload = self._transport(
            "POST",
            f"{self._url}{path}",
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "theozolith-nodedaemon",
            },
            json.dumps(body).encode(),
        )
        if status >= 400:
            detail = payload.decode(errors="replace")[:200]
            raise ControlError(f"POST {path}: HTTP {status} {detail}")
        try:
            answer = json.loads(payload or b"{}")
        except json.JSONDecodeError as exc:
            raise ControlError(f"POST {path}: non-JSON answer") from exc
        return answer if isinstance(answer, dict) else {}

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One heartbeat; the answer carries commands + desired state."""
        return self._post("/api/v1/heartbeats", payload)

    def emit_event(self, event: dict[str, Any]) -> None:
        self._post("/api/v1/events", event)

    def fetch_artifact(self, version: str, filename: str) -> bytes:
        """One built wheel from the Control Node's artifact store (ADR-0015
        amendment 2026-07-22): nodes never pull source and never build."""
        status, payload = self._transport(
            "GET",
            f"{self._url}/api/v1/product/artifacts/{version}/{filename}",
            {
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "theozolith-nodedaemon",
            },
            None,
        )
        if status >= 400:
            detail = payload.decode(errors="replace")[:200]
            raise ControlError(f"artifact {filename} for {version}: HTTP {status} {detail}")
        return payload

    def fetch_config_artifact(self, drivers_hash: str) -> bytes:
        """The config-distribution zip for a drivers-hash (ADR-0042), twin of
        ``fetch_artifact``. A 409 (the working repo moved past this hash) is a
        ``ControlError`` like any 4xx — logged and retried next pass, when the
        heartbeat carries the fresh hash."""
        status, payload = self._transport(
            "GET",
            f"{self._url}/api/v1/config/artifacts/{drivers_hash}",
            {
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "theozolith-nodedaemon",
            },
            None,
        )
        if status >= 400:
            detail = payload.decode(errors="replace")[:200]
            raise ControlError(f"config artifact {drivers_hash[:12]}: HTTP {status} {detail}")
        return payload

    def pull_secrets(self, node: str, names: list[str]) -> dict[str, str]:
        """Node-scoped secret pull — values transit TLS only (ADR-0006; the
        constructor already refused a plain-HTTP channel without the
        explicit dev opt-in)."""
        if not names:
            return {}
        answer = self._post("/api/v1/secrets/pull", {"node": node, "names": names})
        secrets = answer.get("secrets")
        if not isinstance(secrets, dict):
            raise ControlError("secrets-pull answer carried no 'secrets' object")
        return {str(k): str(v) for k, v in secrets.items()}
