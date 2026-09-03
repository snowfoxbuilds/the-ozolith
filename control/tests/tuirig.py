"""TUI test rig: document builders shared by the model/app tests, and a
fake ControlClient recording every call — the app's one I/O seam, so the
tests prove each datum crossed the (faked) HTTP boundary and each write hit
exactly one endpoint. (Named tuirig, controlrig's precedent.)"""

from __future__ import annotations

import copy
from typing import Any

NOW = 1_000_000.0

ATTACH_ARGV = ["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "tmux", "attach"]


def state_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "now": NOW,
        "nodes": [
            {"name": "box1", "version": "0.3.0", "registered_at": NOW - 900, "last_seen": NOW - 10}
        ],
        "stacks": [
            {
                "node": "box1",
                "name": "deck",
                "kind": "container",
                "state": "running",
                "detail": "",
                "updated_at": NOW - 10,
            }
        ],
        "desired_stacks": [
            {
                "node": "box1",
                "name": "deck",
                "kind": "container",
                "state": "running",
                "env": {},
                "attach": list(ATTACH_ARGV),
            }
        ],
        "node_health": [],
        "product_pin": "0.3.0",
        "run_containers": [],
        "stack_containers": [
            {
                "node": "box1",
                "name": "flight-deck-1",
                "stack": "deck",
                "state": "running",
                "status": "Up 2 hours",
                "updated_at": NOW - 30,
            }
        ],
        "images": [],
        "commands": [],
        "provisioned_nodes": [],
        "unregistered_nodes": [],
        "repos": ["acme/sandbox"],
        "dispatch_pauses": [],
        "control_toml": {
            "control_ip": "203.0.113.5",
            "control_port": 443,
            "browser_origin": None,
            "settings": {"heartbeat_seconds": 60.0},
        },
    }
    doc.update(over)
    return doc


def page(events: list[dict[str, Any]], *, evicted: bool = False, next_cursor=None) -> dict:
    return {
        "events": events,
        "next_cursor": next_cursor,
        "evicted": evicted,
        "any_evicted": evicted,
    }


def run_event(
    event_id: int,
    issue: int,
    phase: str,
    *,
    run_id: str = "r1",
    attempt: int | None = 1,
    pr: int | None = None,
    failure_class: str | None = None,
    at: float = NOW - 60,
    repo: str | None = "acme/sandbox",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "theozolith.run",
        "worker": "worker-a",
        "node": "box1",
        "stack": "worker",
        "issue": issue,
        "run_id": run_id,
        "phase": phase,
    }
    # A repo-less event models a pre-ADR-0056 legacy row (skipped by run_rows).
    if repo is not None:
        payload["repo"] = repo
    if attempt is not None:
        payload["attempt"] = attempt
    if pr is not None:
        payload["pr"] = pr
    if failure_class is not None:
        payload["failure_class"] = failure_class
    return {
        "id": event_id,
        "type": "theozolith.run",
        "received_at": at,
        "node": "box1",
        "component": None,
        "payload": payload,
    }


def progress_event(event_id: int, run_id: str, **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "theozolith.run.progress",
        "worker": "worker-a",
        "node": "box1",
        "stack": "worker",
        "issue": 7,
        "run_id": run_id,
        "attempt": 1,
        "phase": "agent",
        "elapsed_seconds": 120.0,
        "tool_calls": 14,
        "tokens": 5000,
        "transcript_bytes": 65536,
        "transcript_tail": "...tail...",
    }
    payload.update(over)
    return {
        "id": event_id,
        "type": "theozolith.run.progress",
        "received_at": NOW - 30,
        "node": "box1",
        "component": None,
        "payload": payload,
    }


class FakeClient:
    """The app's I/O seam, faked: canned documents in, every call recorded.
    ``events_pages`` maps a filter ``type`` (or "" for none) to either one
    page (answered for every cursor) or a cursor-keyed dict of pages
    (``None`` is the head fetch) so tests can exercise real cursor walks;
    ``fail_with`` makes every call raise (the degraded path)."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.url = "https://127.0.0.1:9443"
        self.state_doc = state if state is not None else state_doc()
        self.events_pages: dict[str, dict[str, Any]] = {}
        self.events_calls: list[dict[str, Any]] = []
        self.state_calls = 0
        self.commands: list[tuple] = []
        self.released: list[str] = []
        self.secrets: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    def state(self) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        self.state_calls += 1
        return copy.deepcopy(self.state_doc)

    def events(self, **params: Any) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        # Record what would go on the wire: the real client omits Nones.
        self.events_calls.append({k: v for k, v in params.items() if v is not None})
        answer = self.events_pages.get(params.get("type") or "", page([]))
        if "events" not in answer:  # cursor-keyed pages: pick by cursor
            answer = answer.get(params.get("cursor"), page([]))
        return copy.deepcopy(answer)

    def queue_command(self, node: str, verb: str, target=None, *, force: bool = False):
        if self.fail_with:
            raise self.fail_with
        self.commands.append((node, verb, target, force))
        return {"id": len(self.commands)}

    def release_quarantine(self, node: str):
        if self.fail_with:
            raise self.fail_with
        self.released.append(node)
        for row in self.state_doc.get("node_health") or []:
            if row.get("node") == node:
                row["quarantined"] = 0
        return {"released": True}

    def put_secret(self, name: str, value: str):
        if self.fail_with:
            raise self.fail_with
        self.secrets.append((name, value))
        return {"ok": True}
