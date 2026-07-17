"""Admin session mechanics (ADR-0018).

One admin credential fronts the dashboard, the secret form, and the
terminal (NODE-SUBSTRATE trust model: dashboard access = cluster access,
single-operator V1). The login form takes the admin token itself — no
second credential exists — and mints a signed session cookie: an expiry
timestamp HMAC'd with a per-process random key, so sessions are stateless,
tamper-evident, and die with a Control Node restart (re-login is one paste).

The cookie is HttpOnly + SameSite=Strict (the CSRF story for the two POST
forms) and Secure whenever the request arrived over TLS. API-style callers
may present the admin bearer token instead — same credential, same rights.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable

from fastapi import Request, WebSocket

SESSION_COOKIE = "ozolith_session"
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

    def authorized(self, request: Request | WebSocket) -> bool:
        return self._cookie_valid(request.cookies.get(SESSION_COOKIE)) or self._bearer_valid(
            request.headers.get("authorization", "")
        )
