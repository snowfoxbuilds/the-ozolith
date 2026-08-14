"""One reviewed bearer-token HTTP transport for the control-side clients.

The admin (or per-node) bearer token rides the ``Authorization`` header of
every request the CLI, status CLI, TUI, and artifact upload make, so the
transport must refuse to put it on a plaintext wire and must never let
urllib's default redirect handler re-send it to another origin or downgrade
it to http. Python's default handler copies every request header — the bearer
token included — onto the redirected request for whatever host ``Location``
names, cross-origin and HTTPS→HTTP alike.

This mirrors, stdlib-only, the node daemon's ``ControlClient`` transport
(``nodedaemon/controlclient.py``); the two are kept in step by parallel
invariant tests, not a shared import — the components install independently
(ADR-0010). The one deliberate difference: these clients run on (or SSH into)
the Control Node itself, so plain http to a *loopback* address is allowed for
local dev — the token never reaches a wire there — while any non-loopback
target must be https.
"""

from __future__ import annotations

import ipaddress
import ssl
import urllib.error
import urllib.parse
import urllib.request

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REDIRECT_LIMIT = 5


class BearerTransportError(RuntimeError):
    """The target or a redirect would put the bearer token somewhere it must
    not go: a plaintext non-loopback wire, or a cross-origin/downgrade hop."""


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) of an exactly-parsed http(s) URL — None
    for anything else. Exact parsing, never a prefix check: ``httpsevil`` or a
    URL with no hostname must not pass for https, and a total parse means a
    urlsplit that raises ValueError (unmatched IPv6 brackets, NFKC-invalid
    netlocs) is None here, never a leaked exception. An explicit ``:0`` names
    no dialable origin."""
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


def _require_bearer_origin(url: str) -> tuple[str, str, int]:
    origin = _origin(url)
    if origin is None:
        raise BearerTransportError(f"refusing bearer request to {url!r}: not a usable http(s) URL")
    if origin[0] != "https" and not _is_loopback(origin[1]):
        raise BearerTransportError(
            f"refusing bearer request to {url!r}: the token must ride https"
            " (plain http is allowed only to a loopback address for local dev)"
        )
    return origin


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx as an HTTPError instead of following it: the manual
    loop below owns all Location parsing and applies the same-origin gate
    before any redirected request carries the bearer token."""

    def _decline(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_302 = http_error_303 = _decline
    http_error_307 = http_error_308 = _decline


def open_bearer(
    request: urllib.request.Request, *, ca: str | None, timeout: float
) -> tuple[int, bytes]:
    """Issue one bearer request under the https-or-loopback floor, following
    only same-origin redirects (bounded). Returns ``(status, body)``.

    Re-raises ``urllib.error.HTTPError`` for a non-redirect >=400 answer (the
    caller reads and maps it), lets ``urllib.error.URLError``/``OSError``
    propagate for unreachability, and raises ``BearerTransportError`` for any
    policy refusal (bad scheme, cross-origin or downgrade redirect)."""
    method = request.get_method()
    origin = _require_bearer_origin(request.full_url)
    context = None
    if origin[0] == "https":
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)

    body = request.data
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
            if not location:
                raise BearerTransportError(
                    f"{method} {target}: HTTP {exc.code} redirect without a Location"
                ) from None
            try:
                target = urllib.parse.urljoin(target, location)
            except ValueError:
                raise BearerTransportError(
                    f"refusing redirect: malformed Location {location!r}"
                    " (the bearer token only rides an exactly-parsed same-origin target)"
                ) from None
            if _origin(target) != origin:
                raise BearerTransportError(
                    f"refusing redirect to {target!r}: it leaves the bearer origin"
                    " (a cross-origin hop or HTTPS→HTTP downgrade would hand the"
                    " token to someone else)"
                ) from None
    raise BearerTransportError(f"{method} {request.full_url}: too many redirects")
