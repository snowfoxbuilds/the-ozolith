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

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ControlUnreachable(RuntimeError):
    """A read or write failed — connection, TLS, or a non-2xx answer."""

    def __init__(self, dial_target: str, error_class: str, detail: str):
        super().__init__(detail)
        self.dial_target = dial_target
        self.error_class = error_class
        self.detail = detail


class ControlClient:
    """Bearer-auth JSON calls against one Control Node URL."""

    def __init__(self, url: str, token: str, ca: str | None, *, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self._token = token
        self._ca = ca
        self._timeout = timeout
        self._context: ssl.SSLContext | None = None
        if self.url.startswith("https"):
            self._context = (
                ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
            )

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
            with urllib.request.urlopen(
                request, timeout=self._timeout, context=self._context
            ) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ControlUnreachable(self.url, f"HTTP {exc.code}", detail) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            error_class = (
                type(reason).__name__ if isinstance(reason, Exception) else type(exc).__name__
            )
            raise ControlUnreachable(self.url, error_class, str(reason)) from exc
        except (OSError, ValueError) as exc:
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
