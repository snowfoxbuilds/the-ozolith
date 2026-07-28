"""The daemon's client for the Control Node channel.

HTTPS with bearer-token auth at a configured URL; TLS via a pinned CA bundle
(self-signed or install-provisioned — NODE-SUBSTRATE.md). Unreachability is
an expected state, not an error: ``heartbeat`` answers None and the caller
falls back to cached desired state (ADR-0006). The one hard rule is the
channel invariant's TLS clause: secret values never transit plain HTTP
unless the operator explicitly opted into insecure dev mode.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
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


def _urllib_transport(ca: str | None) -> Transport:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        context = None
        if url.startswith("https"):
            context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15, context=context) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControlUnreachable(str(exc)) from exc

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
        self._url = url.rstrip("/")
        self._token = token
        self._insecure_dev = insecure_dev
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

    def pull_secrets(self, node: str, names: list[str]) -> dict[str, str]:
        """Node-scoped secret pull — values transit TLS only (ADR-0006)."""
        if not names:
            return {}
        if not self._url.startswith("https") and not self._insecure_dev:
            raise ControlError(
                "refusing to pull secrets over plain HTTP (TLS is mandatory;"
                " THEOZOLITH_INSECURE_DEV=1 for local dev only)"
            )
        answer = self._post("/api/v1/secrets/pull", {"node": node, "names": names})
        secrets = answer.get("secrets")
        if not isinstance(secrets, dict):
            raise ControlError("secrets-pull answer carried no 'secrets' object")
        return {str(k): str(v) for k, v in secrets.items()}
