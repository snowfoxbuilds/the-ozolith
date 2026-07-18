"""Admin session mechanics (ADR-0018) and browser-origin isolation (ADR-0019).

One admin credential fronts the dashboard, the secret form, and the
terminal (NODE-SUBSTRATE trust model: dashboard access = cluster access,
single-operator V1). The login form takes the admin token itself — no
second credential exists — and mints a signed session cookie: an expiry
timestamp HMAC'd with a per-process random key, so sessions are stateless,
tamper-evident, and die with a Control Node restart (re-login is one paste).

The cookie is host-only ``__Host-ozolith_session`` — Secure, HttpOnly,
SameSite=Strict, Path=/ (the ``__Host-`` prefix makes browsers refuse it
from any other origin or path). API-style callers may present the admin
bearer token instead — same credential, same rights, and no browser-origin
checks: only cookie-authenticated requests are browser-shaped.

``BrowserGuard`` enforces the M5 origin contract: with a canonical origin
configured (mandatory in production, see origin.py), cookie-authenticated
state-changing requests and websockets must carry exactly the configured
``Host`` and ``Origin`` — a browser on any other origin (DNS rebinding, a
lured click) fails closed before any handler runs.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable

from fastapi import Request, WebSocket

SESSION_COOKIE = "__Host-ozolith_session"
SESSION_TTL_SECONDS = 12 * 3600.0


class AdminSessions:
    def __init__(
        self,
        admin_token: str,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = SESSION_TTL_SECONDS,
    ):
        self._admin_token = admin_token
        self._clock = clock
        self._ttl = ttl_seconds
        # Per-process: sessions are deliberately not durable (ADR-0018).
        self._key = secrets.token_bytes(32)

    def _sign(self, expires: str) -> str:
        return hmac.new(self._key, expires.encode(), "sha256").hexdigest()

    def login(self, credential: str) -> str | None:
        """A session cookie value for the right credential, else None."""
        if not hmac.compare_digest(credential, self._admin_token):
            return None
        expires = str(int(self._clock() + self._ttl))
        return f"{expires}.{self._sign(expires)}"

    def _cookie_valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        expires, _, signature = cookie.partition(".")
        if not hmac.compare_digest(signature, self._sign(expires)):
            return False
        return expires.isdigit() and self._clock() < int(expires)

    def _bearer_valid(self, header: str) -> bool:
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), self._admin_token)

    def auth_mode(self, request: Request | WebSocket) -> str | None:
        """How this request is authorized: ``"bearer"`` (non-browser client,
        exempt from origin checks), ``"cookie"`` (browser), or None.
        Bearer wins so a scripted client with a stray cookie jar never
        trips the browser-only origin contract."""
        if self._bearer_valid(request.headers.get("authorization", "")):
            return "bearer"
        if self._cookie_valid(request.cookies.get(SESSION_COOKIE)):
            return "cookie"
        return None

    def authorized(self, request: Request | WebSocket) -> bool:
        return self.auth_mode(request) is not None


class BrowserGuard:
    """Exact-match Host and Origin enforcement for browser-shaped requests.

    Armed only when a canonical host is configured (production always is;
    ``--insecure-dev`` may run bare). The expected values are computed once
    from the canonical host and the public port — browsers omit the
    default port (443) and include any other, so exactly one Host and one
    Origin spelling is correct for a given deployment."""

    def __init__(self, canonical_host: str, public_port: int):
        self._armed = bool(canonical_host)
        if public_port == 443:
            self._host = canonical_host
            self._origin = f"https://{canonical_host}"
        else:
            self._host = f"{canonical_host}:{public_port}"
            self._origin = f"https://{canonical_host}:{public_port}"

    def ok(self, request: Request | WebSocket) -> bool:
        """True when the request carries exactly the canonical Host and
        Origin (or the guard is unarmed). Missing headers fail."""
        if not self._armed:
            return True
        return (
            request.headers.get("host", "") == self._host
            and request.headers.get("origin", "") == self._origin
        )
