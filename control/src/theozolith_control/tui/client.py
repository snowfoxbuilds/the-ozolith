"""The TUI's HTTP client: bearer JSON over urllib, nothing else.

One class, five calls — the two reads every panel consumes and the exactly
three writes the M9 ruling allows (infrastructure commands, quarantine
release, secret entry). Stdlib-only by design and by test: the TUI is a
pure API consumer, and this module is the only place it does I/O.

Failures fold into ``ControlUnreachable`` carrying the dial target and the
error class (the statuscli.Unreachable shape, ADR-0039): the app renders a
degraded banner from it and keeps the last-known documents on screen.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REDIRECT_LIMIT = 5


class ControlUnreachable(RuntimeError):
    """A read or write failed — connection, TLS, or a non-2xx answer."""

    def __init__(self, dial_target: str, error_class: str, detail: str):
        super().__init__(detail)
        self.dial_target = dial_target
        self.error_class = error_class
        self.detail = detail


class _BearerRefused(RuntimeError):
    """The target or a redirect would put the admin token on a plaintext
    non-loopback wire or across a cross-origin/downgrade hop.

    The TUI may import only its own subpackage (M9 acceptance 1), so this
    mirrors ``theozolith_control.bearerhttp`` in-tree rather than importing
    it — the same principled copy the module already makes of the
    statuscli.Unreachable shape. Parallel invariant tests keep them in step."""


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) of an exactly-parsed http(s) URL, or
    None — a total parse (a urlsplit ValueError becomes None, never a leak)
    and never a prefix check, so ``httpsevil`` cannot pass for https."""
    try:
        split = urllib.parse.urlsplit(url)
        hostname = split.hostname
        port = split.port
    except ValueError:
        return None
    if split.scheme not in ("https", "http") or not hostname or port == 0:
        return None
    if port is None:
        port = 443 if split.scheme == "https" else 80
    return split.scheme, hostname, port


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx as an HTTPError; the loop below owns the same-origin
    gate before any redirected request carries the admin token."""

    def _decline(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_302 = http_error_303 = _decline
    http_error_307 = http_error_308 = _decline


def _open_bearer(
    request: urllib.request.Request, *, ca: str | None, timeout: float
) -> tuple[int, bytes]:
    """Issue one bearer request under the https-or-loopback floor, following
    only same-origin redirects (bounded). Returns ``(status, body)`` and
    re-raises HTTPError/URLError like urlopen; a policy refusal is
    ``_BearerRefused``."""
    origin = _origin(request.full_url)
    if origin is None:
        raise _BearerRefused(f"refusing bearer request to {request.full_url!r}: not http(s)")
    if origin[0] != "https" and not _is_loopback(origin[1]):
        raise _BearerRefused(
            f"refusing bearer request to {request.full_url!r}: the admin token must ride"
            " https (plain http is allowed only to a loopback address)"
        )
    context = None
    if origin[0] == "https":
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)

    method, body = request.get_method(), request.data
    headers = dict(request.header_items())
    target = request.full_url
    for _ in range(_REDIRECT_LIMIT):
        req = urllib.request.Request(target, data=body, method=method, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in _REDIRECT_STATUSES:
                raise
            location = exc.headers.get("Location")
            exc.close()
            target = urllib.parse.urljoin(target, location) if location else ""
            if _origin(target) != origin:
                raise _BearerRefused(
                    f"refusing redirect to {target!r}: it leaves the admin-token origin"
                ) from None
    raise _BearerRefused(f"{method} {request.full_url}: too many redirects")


class ControlClient:
    """Bearer-auth JSON calls against one Control Node URL."""

    def __init__(self, url: str, token: str, ca: str | None, *, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self._token = token
        self._ca = ca
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "theozolith-top",
            },
        )
        try:
            _status, raw = _open_bearer(request, ca=self._ca, timeout=self._timeout)
            return json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ControlUnreachable(self.url, f"HTTP {exc.code}", detail) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            error_class = (
                type(reason).__name__ if isinstance(reason, Exception) else type(exc).__name__
            )
            raise ControlUnreachable(self.url, error_class, str(reason)) from exc
        except (_BearerRefused, OSError, ValueError) as exc:
            raise ControlUnreachable(self.url, type(exc).__name__, str(exc)) from exc

    # -- the two reads (ADR-0039/0038/0040) ---------------------------------

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/state")

    def events(
        self,
        *,
        type: str | None = None,
        node: str | None = None,
        component: str | None = None,
        since: float | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        for key, value in (
            ("type", type),
            ("node", node),
            ("component", component),
            ("since", since),
            ("cursor", cursor),
            ("limit", limit),
        ):
            if value is not None:
                params[key] = str(value)
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._request("GET", f"/api/v1/events{query}")

    # -- the exactly three writes (M9 ruling) -------------------------------

    def queue_command(
        self, node: str, verb: str, target: str | None = None, *, force: bool = False
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"node": node, "verb": verb}
        if target:
            body["target"] = target
        if force:
            body["force"] = True
        return self._request("POST", "/api/v1/commands", body)

    def release_quarantine(self, node: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(node, safe="")
        return self._request("POST", f"/api/v1/nodes/{quoted}/quarantine/release")

    def put_secret(self, name: str, value: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(name, safe="")
        return self._request("PUT", f"/api/v1/secrets/{quoted}", {"value": value})
