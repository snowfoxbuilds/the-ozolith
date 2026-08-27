"""Dashboard read models: the store + Config Repo, shaped for templates.

Strictly read-only over the fleet (V1 scope): nothing here writes to GitHub
or to nodes. Everything agent-authored (transcript tails, event payloads)
is treated as untrusted text — the templates escape it, these functions
never mark anything safe.
"""

from __future__ import annotations

import json
import time
from typing import Any

from theozolith_control.app import ERROR_CONTEXT_LIMIT, ERROR_MESSAGE_LIMIT
from theozolith_control.configrepo import DeployConfig
from theozolith_control.store import EVENT_ERROR, EVENT_PROGRESS, EVENT_REVIEW, EVENT_RUN, Store

# A node is stale once it has missed ~2 heartbeats (60s cadence).
STALE_AFTER_SECONDS = 150.0

RUN_PHASE_STATUS = {
    "claimed": "info",
    "gate": "info",
    "pr-open": "good",
    "failed": "serious",
    "escalated": "critical",
}


def elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def ago(seconds: float) -> str:
    return f"{elapsed(seconds)} ago"


def image_skew(images: list[dict[str, Any]]) -> set[str]:
    """Image names whose build metadata differs across nodes (surfaced,
    not prevented — NODE-SUBSTRATE image builds)."""
    builds: dict[str, set[tuple[str, str]]] = {}
    for image in images:
        builds.setdefault(image["name"], set()).add(
            (image["base_digest"], image["instruction_hash"])
        )
    return {name for name, variants in builds.items() if len(variants) > 1}


def fleet_view(store: Store, config: DeployConfig, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    state = store.fleet_state()
    quarantined = {q["node"]: q for q in store.quarantines()}
    skewed = image_skew(state["images"])
    # Attach affordances exist only for container-kind Stacks (ADR-0019):
    # run containers are headless and never attach targets.
    attach_by_stack = {
        stack.name: bool(stack.attach) for stack in config.stacks if stack.kind == "container"
    }

    nodes = []
    for node in state["nodes"]:
        name = node["name"]
        desired = {stack.name: stack for stack in config.stacks_for(name)}
        actual = {s["name"]: s for s in state["stacks"] if s["node"] == name}
        stacks = []
        for stack_name in sorted(set(desired) | set(actual)):
            want = desired[stack_name].state if stack_name in desired else "(unplaced)"
            have = actual[stack_name]["state"] if stack_name in actual else "(unreported)"
            stacks.append(
                {
                    "name": stack_name,
                    "kind": (
                        actual.get(stack_name, {}).get("kind")
                        or (desired[stack_name].kind if stack_name in desired else "")
                    ),
                    "desired": want,
                    "actual": have,
                    "detail": actual.get(stack_name, {}).get("detail", ""),
                    "converged": want == have,
                }
            )
        containers = [c for c in state["run_containers"] if c["node"] == name]
        stack_containers = [
            {
                **c,
                "attachable": attach_by_stack.get(c["stack"], False),
            }
            for c in state["stack_containers"]
            if c["node"] == name
        ]
        images = [
            {**i, "skewed": i["name"] in skewed} for i in state["images"] if i["node"] == name
        ]
        nodes.append(
            {
                "name": name,
                "version": node["version"],
                # Version skew (ADR-0015, 2026-07-22): every heartbeat
                # reports the running product version; a node off the
                # recorded pin is surfaced, never silently tolerated.
                "version_skew": bool(config.product_version)
                and bool(node["version"])
                and node["version"] != config.product_version,
                # Off-hash (ADR-0042): the node REPORTED a config distribution
                # (the presence bit, never truthiness — an explicit '' from a
                # current daemon with no verified tree counts) that differs from
                # the recorded one. A convergence state, dispatch-blocking,
                # warning-grade (the exact analog of version skew). A heartbeat
                # that omitted the field is fail-open, never off-hash.
                "drivers_off_hash": bool(config.drivers_hash)
                and bool(node["drivers_hash_reported"])
                and node["drivers_hash"] != config.drivers_hash,
                "last_seen": ago(now - node["last_seen"]),
                "stale": now - node["last_seen"] > STALE_AFTER_SECONDS,
                "quarantine": quarantined.get(name),
                "stacks": stacks,
                "containers": containers,
                "stack_containers": stack_containers,
                "images": images,
            }
        )
    pending = [
        {**c, "state": "deferred" if c["deferred_reason"] else "pending"}
        for c in state["commands"]
        if c["completed_at"] is None
    ]
    drivers = [{**d, "last_dispatch": ago(now - d["last_dispatch_at"])} for d in store.drivers()]
    # Stamp skew (ADR-0042): a node's applied distribution was built against a
    # product version other than the one it runs — advisory ONLY, never a
    # health downgrade (real breakage crashes at driver start into supervision).
    drivers_stamp_skew = [
        {
            "node": node["name"],
            "built_against": node["drivers_built_against"],
            "product_version": node["version"],
        }
        for node in state["nodes"]
        if node["drivers_built_against"]
        and node["version"]
        and node["drivers_built_against"] != node["version"]
    ]
    return {
        "nodes": nodes,
        "skewed_images": sorted(skewed),
        "product_version": config.product_version,
        "version_skew": sorted(n["name"] for n in nodes if n["version_skew"]),
        # Config distribution (ADR-0042): the recorded hash, the off-hash
        # convergence worklist (dispatch-blocking), and advisory stamp skew.
        "config_drivers_hash": config.drivers_hash,
        "drivers_off_hash": sorted(n["name"] for n in nodes if n["drivers_off_hash"]),
        "drivers_stamp_skew": drivers_stamp_skew,
        "pending_commands": pending,
        "drivers": drivers,
        # Advisory display from unauthenticated input (ADR-0023): heartbeats
        # whose tokens were rejected — never node records, never
        # dispatch-eligible; the re-provision worklist after a stale-backup
        # recovery. Self-declared names render escaped like all agent text.
        "unregistered": [
            {**row, "last_seen": ago(now - row["last_seen"])} for row in store.unregistered_nodes()
        ],
    }


def runs_view(store: Store, repo: str | None, *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    base = f"https://github.com/{repo}" if repo else None
    rows = []
    for run in store.run_states():
        progress = run.get("progress") or {}
        rows.append(
            {
                **run,
                "status": RUN_PHASE_STATUS.get(run["phase"], "info"),
                "last_event": ago(now - run["last_event_at"]),
                "issue_url": f"{base}/issues/{run['issue']}" if base else None,
                "pr_url": f"{base}/pull/{run['pr']}" if base and run["pr"] else None,
                "progress": progress,
                "progress_elapsed": (
                    elapsed(progress["elapsed_seconds"])
                    if isinstance(progress.get("elapsed_seconds"), (int, float))
                    else None
                ),
            }
        )
    return rows


KNOWN_EVENT_TYPES = (EVENT_RUN, EVENT_REVIEW, EVENT_PROGRESS, EVENT_ERROR)


def _summarize(event: dict[str, Any]) -> str:
    payload = event["payload"]
    if event["type"] == EVENT_RUN:
        return (
            f"issue #{payload.get('issue')} · {payload.get('phase')}"
            f" · run {payload.get('run_id')} · {payload.get('driver')}@{payload.get('node')}"
        )
    if event["type"] == EVENT_REVIEW:
        return (
            f"PR #{payload.get('pr')} (issue #{payload.get('issue')})"
            f" · round {payload.get('round')}: {payload.get('verdict')}"
        )
    if event["type"] == EVENT_PROGRESS:
        return (
            f"issue #{payload.get('issue')} · {payload.get('phase')}"
            f" · {payload.get('tool_calls', 0)} tool call(s)"
            f" · {payload.get('transcript_bytes', 0)} transcript bytes"
        )
    if event["type"] == EVENT_ERROR:
        return (
            f"{payload.get('component')}@{payload.get('node')}"
            f" · {payload.get('error_class')}: {str(payload.get('message', ''))[:160]}"
        )
    return ""


def activity_view(
    store: Store, *, limit: int = 50, now: float | None = None
) -> list[dict[str, Any]]:
    """Recent typed events, newest first: known types render richly,
    unknown namespaced types render generically (the extension point)."""
    now = time.time() if now is None else now
    rows = []
    for event in store.recent_events(limit):
        known = event["type"] in KNOWN_EVENT_TYPES
        rows.append(
            {
                "type": event["type"],
                "known": known,
                "when": ago(now - event["received_at"]),
                "summary": _summarize(event) if known else "",
                "payload": (
                    "" if known else json.dumps(event["payload"], indent=2, sort_keys=True)[:2000]
                ),
            }
        )
    return rows


def errors_view(
    store: Store,
    *,
    node: str = "",
    component: str = "",
    limit: int = 50,
    now: float | None = None,
) -> dict[str, Any]:
    """The theozolith.error panel (2026-07-21 grilling): newest-first
    summaries with node/component filters. Message and context are
    agent-adjacent text: rendered escaped, never trusted."""
    now = time.time() if now is None else now
    rows = []
    for event in store.error_events(node=node or None, component=component or None, limit=limit):
        payload = event["payload"]
        rows.append(
            {
                "when": ago(now - event["received_at"]),
                "node": event["node"],
                "component": event["component"],
                "error_class": str(payload.get("error_class", "")),
                # Display caps == ingestion caps: the expander shows the FULL
                # stored context — depth was already bounded at ingestion.
                "message": str(payload.get("message", ""))[:ERROR_MESSAGE_LIMIT],
                "context": str(payload.get("context", ""))[:ERROR_CONTEXT_LIMIT],
            }
        )
    nodes, components = store.error_filters()
    return {
        "rows": rows,
        "nodes": nodes,
        "components": components,
        "selected_node": node,
        "selected_component": component,
    }


def flags_view(store: Store, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {
        "zombies": [
            {**flag, "flagged": ago(now - flag["flagged_at"])} for flag in store.zombie_flags()
        ],
        "malformed": [
            {**row, "last_seen": ago(now - row["last_seen"])} for row in store.malformed_states()
        ],
        "waits": [
            {**row, "last_seen": ago(now - row["last_seen"])} for row in store.dispatch_waits()
        ],
        "quarantines": [
            {**row, "since": ago(now - row["since"]) if row["since"] else ""}
            for row in store.quarantines()
        ],
        "janitor_actions": [
            {**row, "acted": ago(now - row["acted_at"])}
            for row in reversed(store.janitor_actions()[-10:])
        ],
    }
