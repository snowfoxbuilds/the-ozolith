"""Admin session mechanics (ADR-0023) and browser-origin isolation (ADR-0022).

Two credentials, two audiences (ADR-0023, superseding ADR-0018's session
section): the admin *password* is the human/browser credential — the login
form checks it against the stored scrypt hash and mints a server-side
session; the admin *bearer token* remains the machine credential for the
CLI and API, exempt from browser-origin checks. Neither derives from the
other.

Sessions are stateful and revocable: a row in cache.db (deleting that file
costs a re-login, nothing more — ADR-0024), absolute expiry (default 30
days, ``session_days`` in control.toml), logout deletes the row, a password
change truncates the table. The cookie carries only a random 128-bit
session id — nothing decodable client-side.

Over TLS (production) the cookie is host-only ``__Host-ozolith_session`` —
Secure, HttpOnly, SameSite=Strict, Path=/ (the ``__Host-`` prefix makes
browsers refuse it from any other origin or path). ``--insecure-dev`` serves
plain HTTP, where browsers reject a Secure/``__Host-`` cookie from any
non-localhost origin; there the session uses the unprefixed
``ozolith_session`` name without Secure. This is a per-deployment choice,
not per-request.

Login hardening (ADR-0023): the hash comparison is constant-time
(passwords.verify_password) and the form is rate-limited — a small global
failure budget per window; within the single-operator trust model a lockout
window is a tolerable cost for a hard brute-force bound. The 128-bit origin
slug is defense in depth; the password check stands alone.

``BrowserGuard`` enforces the origin contract: with a public origin
configured (mandatory in production, see origin.py), cookie-authenticated
state-changing requests and websockets must carry exactly the ``Host`` and
``Origin`` derived from the parsed public-origin URL — a browser on any
other origin (DNS rebinding, a lured click) fails closed before any
handler runs.
"""

from __future__ import annotations

import hmac
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request, WebSocket

from theozolith_control.origin import PublicOrigin
from theozolith_control.passwords import verify_password
from theozolith_control.store import Store

SESSION_COOKIE = "__Host-ozolith_session"  # over TLS
DEV_SESSION_COOKIE = "ozolith_session"  # --insecure-dev over plain HTTP

# The login rate limit: at most this many failed attempts per window,
# globally (single admin credential, single operator — per-source buckets
# would only help an attacker spread the same guesses).
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60.0


def session_cookie_name(secure: bool) -> str:
    return SESSION_COOKIE if secure else DEV_SESSION_COOKIE


class AdminSessions:
    def __init__(
        self,
        store: Store,
        *,
        password_reader: Callable[[], str],
        admin_token: str,
        session_days: float = 30.0,
        clock: Callable[[], float] = time.time,
        secure_cookies: bool = True,
    ):
        self._store = store
        # Read per attempt, not cached: `theozolith set-password`
        # (server running or not) takes effect on the next login.
        self._password_reader = password_reader
        self._admin_token = admin_token
        self._clock = clock
        self._ttl = session_days * 86400.0
        # A deployment is uniformly TLS (production) or not (--insecure-dev);
        # that decides the session cookie's name and Secure flag once, not
        # per request. The __Host- name requires Secure, which requires TLS.
        self._secure = secure_cookies
        self._cookie_name = session_cookie_name(secure_cookies)
        self._failures: deque[float] = deque()

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def secure(self) -> bool:
        return self._secure

    @property
    def configured(self) -> bool:
        """False until init has stored an admin password hash."""
        return bool(self._password_reader())

    # -- login (password -> stateful session) -------------------------------

    def throttled_for(self) -> float:
        """Seconds until another login attempt is allowed (0 = go)."""
        now = self._clock()
        while self._failures and now - self._failures[0] >= LOGIN_WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) < LOGIN_MAX_FAILURES:
            return 0.0
        return LOGIN_WINDOW_SECONDS - (now - self._failures[0])

    def login(self, password: str) -> str | None:
        """A session cookie value for the right password, else None (the
        failure is counted toward the rate limit)."""
        record = self._password_reader()
        if record and verify_password(record, password):
            return self._store.create_session(self._ttl)
        self._failures.append(self._clock())
        return None

    def logout(self, cookie_value: str) -> None:
        if cookie_value:
            self._store.delete_session(cookie_value)

    # -- request authorization ----------------------------------------------

    def _bearer_valid(self, header: str) -> bool:
        scheme, _, token = header.partition(" ")
        return (
            scheme.lower() == "bearer"
            and bool(self._admin_token)
            and hmac.compare_digest(token.strip(), self._admin_token)
        )

    def cookie_value(self, request: Request | WebSocket) -> str:
        return request.cookies.get(self._cookie_name) or ""

    def auth_mode(self, request: Request | WebSocket) -> str | None:
        """How this request is authorized: ``"bearer"`` (non-browser client,
        exempt from origin checks), ``"cookie"`` (browser), or None.
        Bearer wins so a scripted client with a stray cookie jar never
        trips the browser-only origin contract. The session cookie is read
        under the name its own scheme sets — ``__Host-`` over TLS, the
        unprefixed dev name over plain HTTP — never both."""
        if self._bearer_valid(request.headers.get("authorization", "")):
            return "bearer"
        if self._store.session_active(self.cookie_value(request)):
            return "cookie"
        return None

    def authorized(self, request: Request | WebSocket) -> bool:
        return self.auth_mode(request) is not None


class BrowserGuard:
    """Exact-match Host and Origin enforcement for browser-shaped requests.

    Armed only when a public origin is configured (production always is;
    ``--insecure-dev`` may run bare). The expected values derive from the
    parsed public-origin URL alone — never from the Uvicorn bind host or
    port, so changing ``serve --port`` cannot silently change what is
    accepted. Browsers omit the default https port (443) and include any
    other, so exactly one Host and one Origin spelling is correct for a
    given deployment."""

    def __init__(self, public_origin: PublicOrigin | None):
        self._armed = public_origin is not None
        self._host = public_origin.host_header if public_origin else ""
        self._origin = public_origin.origin if public_origin else ""

    @property
    def expected_host(self) -> str:
        return self._host

    @property
    def expected_origin(self) -> str:
        return self._origin

    def ok(self, request: Request | WebSocket) -> bool:
        """True when the request carries exactly the expected Host and
        Origin (or the guard is unarmed). Missing headers fail."""
        if not self._armed:
            return True
        return (
            request.headers.get("host", "") == self._host
            and request.headers.get("origin", "") == self._origin
        )
