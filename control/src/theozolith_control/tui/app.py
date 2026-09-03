"""``theozolith top``: the full-screen Textual application.

Rendering and interaction only — every fact on screen comes from
``tui.model`` derivations over the two read documents, and every mutation
goes through ``tui.client``'s three write calls. The app never touches a
database, a socket beyond the injected client, a PTY, or a subprocess; the
attach action ends in a PRINTED command inside a modal.

Panels (number keys): 1 Fleet (nodes, live containers, command queue),
2 Stacks & Runs (desired vs actual; run detail with the advisory transcript
tail), 3 Events (filters + follow mode), 4 Errors (``theozolith.error``),
5 Settings (control.toml, read-only). Writes: ``c`` queue an infrastructure
command (destructive verbs demand the target name typed back), ``x``
release a quarantine, ``s`` enter a secret (masked, never re-rendered),
``a`` print the attach command for the selected container.

Agent-authored text (transcript tails, event payloads, error messages) is
untrusted everywhere it renders: it is wrapped in ``rich.text.Text`` so no
markup interpretation ever applies to it.

Degraded mode (ADR-0040): a failed refresh keeps the last documents on
screen under a prominent banner naming the dial target and error class;
polling continues on the cadence and the banner clears on the next success.
Attach assistance fails CLOSED while degraded — a retained snapshot's
server clock is frozen, so heartbeat evidence cannot be proven fresh; the
refusal stands until a state refresh succeeds (ADR-0040 amendment).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from theozolith_control.tui import model
from theozolith_control.tui.client import ControlClient, ControlUnreachable

REFRESH_SECONDS = 5.0  # the polling cadence (state + events per tick)
EVENTS_PAGE_LIMIT = 100
RUNS_PAGE_LIMIT = 500  # max-size pages for the run-index cursor walks
EVENTS_KEEP = 1000  # rows the events panel retains client-side

# Terminal run detail (ADR-0040 amendment): failed/escalated events carry the
# worker's canonical failure class; a successful Run has none to carry, and a
# legacy event predating the field renders the gap explicitly — never as a
# channel defect and never as a blank.
FAILURE_CLASS_NOT_APPLICABLE = "not applicable (the Run completed and opened a PR)"
FAILURE_CLASS_LEGACY = (
    "(legacy event — emitted before failure_class rode the channel; recorded in"
    " the evidence bundle's run.json; ADR-0040)"
)


def _untrusted(value: Any) -> Text:
    """Agent-adjacent text: rendered verbatim, never as markup."""
    return Text(str(value))


class _Dialog(ModalScreen):
    """Shared modal chrome: Escape cancels, errors render in-place."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def show_error(self, message: str) -> None:
        self.query_one(".dialog-error", Static).update(_untrusted(message))


class CommandScreen(_Dialog):
    """Queue an infrastructure command (drain/recycle/update/rebuild).
    Destructive verbs refuse until the target's name is typed back exactly
    (model.command_refusal) — a wrong name never reaches the endpoint."""

    def __init__(self, nodes: list[str]):
        super().__init__()
        # Not `_nodes`: that name is DOMNode's child list.
        self._node_names = nodes

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("queue an infrastructure command")
            yield Select(
                [(verb, verb) for verb in model.COMMAND_VERBS],
                prompt="verb",
                id="cmd-verb",
            )
            yield Input(
                placeholder=f"node ({', '.join(self._node_names) or 'none known'})", id="cmd-node"
            )
            yield Input(placeholder="target Stack/image (empty = whole node)", id="cmd-target")
            yield Input(
                placeholder="destructive verbs: type the target name to confirm",
                id="cmd-confirm",
            )
            yield Static("", classes="dialog-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("queue", variant="primary", id="cmd-queue")
                yield Button("cancel", id="cmd-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cmd-cancel":
            self.dismiss(None)
            return
        verb = self.query_one("#cmd-verb", Select).value
        verb = verb if isinstance(verb, str) else ""
        node = self.query_one("#cmd-node", Input).value.strip()
        target = self.query_one("#cmd-target", Input).value.strip()
        typed = self.query_one("#cmd-confirm", Input).value.strip()
        refusal = model.command_refusal(verb, node, target, typed)
        if refusal:
            self.show_error(refusal)
            return
        self.dismiss({"node": node, "verb": verb, "target": target or None})


class QuarantineScreen(_Dialog):
    """Release one node's quarantine — one confirmed action (ADR-0016:
    human-only; the endpoint is the existing release route)."""

    def __init__(self, quarantined: list[str]):
        super().__init__()
        self._quarantined = quarantined

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("release a node's dispatch quarantine")
            if self._quarantined:
                yield Select(
                    [(node, node) for node in self._quarantined],
                    prompt="quarantined node",
                    id="q-node",
                )
            else:
                yield Static("no node is quarantined")
            yield Static("", classes="dialog-error")
            with Horizontal(classes="dialog-buttons"):
                if self._quarantined:
                    yield Button("release", variant="warning", id="q-release")
                yield Button("cancel", id="q-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "q-cancel":
            self.dismiss(None)
            return
        node = self.query_one("#q-node", Select).value
        if not isinstance(node, str) or not node:
            self.show_error("pick the quarantined node to release")
            return
        self.dismiss(node)


class SecretScreen(_Dialog):
    """Masked secret entry. The value is never rendered back anywhere: the
    input masks, the dialog result leaves this screen only toward the PUT,
    and the confirmation names the secret, never the value."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("enter a secret (stored encrypted; pull-only, node-scoped)")
            yield Input(placeholder="secret name (e.g. anthropic-api-key)", id="secret-name")
            yield Input(placeholder="value (masked)", password=True, id="secret-value")
            yield Static("", classes="dialog-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("store", variant="primary", id="secret-store")
                yield Button("cancel", id="secret-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "secret-cancel":
            self.dismiss(None)
            return
        name = self.query_one("#secret-name", Input).value.strip()
        value = self.query_one("#secret-value", Input).value
        if not name or not value:
            self.show_error("both a name and a non-empty value are required")
            return
        self.dismiss((name, value))


class AttachScreen(_Dialog):
    """The attach assistance result: a pastable command or the refusal
    reason. Print-only by ruling — nothing is executed, no PTY, no
    websocket; pasted SSH bypasses terminal-audit.log by accepted design."""

    def __init__(self, result: model.AttachResult, node: str, container: str):
        super().__init__()
        self._result = result
        self._node = node
        self._container = container

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            if self._result.ok:
                yield Label(f"attach to {self._container} on {self._node}")
                yield Static(_untrusted(self._result.command), id="attach-command")
                yield Static(
                    "paste into your own terminal — nothing was executed here"
                    " (no embedded terminal, by ruling)"
                )
            else:
                yield Label("attach refused")
                yield Static(_untrusted(self._result.reason), id="attach-refusal")
            with Horizontal(classes="dialog-buttons"):
                yield Button("close", id="attach-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class TopApp(App):
    """The Operator TUI. ``client`` is the one I/O seam (injectable);
    ``refresh_seconds=None`` disables the timer (tests drive refreshes)."""

    TITLE = "theozolith top"
    CSS = """
    #banner { background: $error; color: $text; padding: 0 1; }
    #run-detail { padding: 0 1; height: auto; }
    .notice { color: $warning; padding: 0 1; }
    .filters { height: 3; }
    .filters Input { width: 1fr; }
    CommandScreen, QuarantineScreen, SecretScreen, AttachScreen { align: center middle; }
    .dialog { width: 90; max-height: 80%; border: thick $accent; background: $surface;
              padding: 1 2; }
    .dialog-error { color: $error; }
    .dialog-buttons { height: 3; }
    DataTable { height: 1fr; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("1", "switch_tab('fleet')", "fleet"),
        Binding("2", "switch_tab('runs')", "stacks+runs"),
        Binding("3", "switch_tab('events')", "events"),
        Binding("4", "switch_tab('errors')", "errors"),
        Binding("5", "switch_tab('settings')", "settings"),
        Binding("c", "command", "command"),
        Binding("x", "release_quarantine", "unquarantine"),
        Binding("s", "secret", "secret"),
        Binding("a", "attach", "attach cmd"),
        Binding("f", "toggle_follow", "follow on/off"),
    ]

    def __init__(
        self,
        client: ControlClient,
        *,
        refresh_seconds: float | None = REFRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__()
        self.client = client
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._refreshing = False
        self.state_doc: dict[str, Any] = {}
        self.freshness = model.Freshness()
        self.runs_index = model.RunIndex()
        self.run_rows: list[model.RunRow] = []
        self._selected_run: str | None = None
        # The events panel: filters, follow mode, and the client-side feed.
        # ``_gap`` is the panel's client-side continuity fact — a follow
        # overflow skipped matching events — kept separate from the server's
        # query-relative ``evicted``; both stick until the conjunction
        # changes (model.continuity_notice).
        self.events_filters: dict[str, str] = {"type": "", "node": "", "component": ""}
        self.events_feed: list[dict[str, Any]] = []
        self.events_evicted = False
        self.events_gap = False
        self.follow = True
        # The errors panel: same machinery with the type pinned.
        self.errors_filters: dict[str, str] = {"node": "", "component": ""}
        self.errors_feed: list[dict[str, Any]] = []
        self.errors_evicted = False
        self.errors_gap = False

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        with TabbedContent(initial="fleet"):
            with TabPane("Fleet", id="fleet"):
                yield Static("", id="pin")
                yield DataTable(id="nodes")
                yield Label("live containers (attach: select a row, press a)")
                yield DataTable(id="containers")
                yield Label("command queue (queue-behind deferrals surface here)")
                yield DataTable(id="commands")
            with TabPane("Stacks & Runs", id="runs"):
                yield DataTable(id="stacks")
                yield Static("", id="pauses-notice", classes="notice")
                yield Static("", id="runs-notice", classes="notice")
                yield DataTable(id="runs-table")
                with VerticalScroll():
                    yield Static("", id="run-detail")
            with TabPane("Events", id="events"):
                with Horizontal(classes="filters"):
                    yield Input(placeholder="type filter (exact)", id="flt-type")
                    yield Input(placeholder="node filter (exact)", id="flt-node")
                    yield Input(placeholder="component filter (exact)", id="flt-component")
                yield Static("", id="events-notice", classes="notice")
                yield DataTable(id="events-table")
            with TabPane("Errors", id="errors"):
                with Horizontal(classes="filters"):
                    yield Input(placeholder="node filter (exact)", id="err-node")
                    yield Input(placeholder="component filter (exact)", id="err-component")
                yield Static("", id="errors-notice", classes="notice")
                yield DataTable(id="errors-table")
            with TabPane("Settings", id="settings"):
                yield Static(
                    "control.toml is read-only here — editing stays git-native"
                    " ($EDITOR + commit in the Config Repo)",
                    classes="notice",
                )
                yield DataTable(id="settings-table")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#nodes", DataTable).add_columns(
            "node", "health", "version", "last seen", "quarantine reason"
        )
        containers = self.query_one("#containers", DataTable)
        containers.cursor_type = "row"
        containers.add_columns("node", "container", "stack", "state", "status", "seen")
        self.query_one("#commands", DataTable).add_columns(
            "id", "node", "verb", "target", "state", "deferred reason"
        )
        self.query_one("#stacks", DataTable).add_columns(
            "node", "stack", "kind", "desired", "actual", "detail"
        )
        runs = self.query_one("#runs-table", DataTable)
        runs.cursor_type = "row"
        runs.add_columns("issue", "phase", "attempt", "driver · node", "run", "PR", "last event")
        self.query_one("#events-table", DataTable).add_columns(
            "when", "type", "node", "component", "payload"
        )
        self.query_one("#errors-table", DataTable).add_columns(
            "when", "node", "component", "class", "message"
        )
        self.query_one("#settings-table", DataTable).add_columns("key", "value")
        if self._refresh_seconds:
            self.set_interval(self._refresh_seconds, self.refresh_now)
        self.call_after_refresh(self.refresh_now)

    # -- the refresh cycle --------------------------------------------------

    async def refresh_now(self) -> None:
        """One polling tick: both reads plus the follow advances, off the
        event loop; a failure keeps the last documents and raises the
        banner (degraded rendering, never blocking)."""
        if self._refreshing:
            return
        self._refreshing = True
        events_filters = {k: v for k, v in self.events_filters.items() if v}
        errors_filters = {k: v for k, v in self.errors_filters.items() if v}
        events_max = model.max_event_id(self.events_feed)
        errors_max = model.max_event_id(self.errors_feed)
        follow = self.follow

        def fetch() -> dict[str, Any]:
            bundle: dict[str, Any] = {"state": self.client.state()}
            # The run index keeps latest-per-issue complete across page
            # boundaries (bootstrap walk once, head advances after); a
            # failure raises out before any partial rendering.
            self.runs_index.refresh(
                lambda cursor: self.client.events(
                    type="theozolith.run", cursor=cursor, limit=RUNS_PAGE_LIMIT
                ),
                lambda cursor: self.client.events(
                    type="theozolith.run.progress", cursor=cursor, limit=RUNS_PAGE_LIMIT
                ),
            )
            if follow or events_max is None:
                bundle["events"] = model.advance_events(
                    lambda cursor: self.client.events(
                        cursor=cursor, limit=EVENTS_PAGE_LIMIT, **events_filters
                    ),
                    events_max,
                )
            if follow or errors_max is None:
                bundle["errors"] = model.advance_events(
                    lambda cursor: self.client.events(
                        type="theozolith.error",
                        cursor=cursor,
                        limit=EVENTS_PAGE_LIMIT,
                        **errors_filters,
                    ),
                    errors_max,
                )
            return bundle

        try:
            bundle = await asyncio.to_thread(fetch)
        except ControlUnreachable as exc:
            self.freshness.fail(exc.dial_target, exc.error_class)
            self._render_banner()
            return
        finally:
            self._refreshing = False
        self.freshness.succeed(self._clock())
        self.state_doc = bundle["state"]
        self.run_rows = model.run_rows(self.runs_index)
        if "events" in bundle:
            fresh, evicted, gap = bundle["events"]
            if gap:
                # The unseen backlog outran the bounded walk: resync from
                # the newest rows and REMEMBER the skip — the intermediate
                # matching events were never fetched and never will be
                # (history is not re-fetched), so the panel stays marked
                # until its query changes (model.continuity_notice).
                self.events_feed = fresh
                self.events_gap = True
            else:
                self.events_feed = (fresh + self.events_feed)[:EVENTS_KEEP]
            self.events_evicted = self.events_evicted or evicted
        if "errors" in bundle:
            fresh, evicted, gap = bundle["errors"]
            if gap:
                self.errors_feed = fresh
                self.errors_gap = True
            else:
                self.errors_feed = (fresh + self.errors_feed)[:EVENTS_KEEP]
            self.errors_evicted = self.errors_evicted or evicted
        self._render_all()

    # -- rendering ----------------------------------------------------------

    def _render_banner(self) -> None:
        banner = self.freshness.banner(self._clock())
        widget = self.query_one("#banner", Static)
        widget.update(_untrusted(banner))
        widget.display = bool(banner)

    def _render_all(self) -> None:
        self._render_banner()
        state = self.state_doc
        now = float(state.get("now") or 0.0)

        pin = state.get("product_pin")
        self.query_one("#pin", Static).update(_untrusted(f"product pin: {pin or '(none)'}"))

        nodes = self.query_one("#nodes", DataTable)
        nodes.clear()
        for row in model.node_rows(state):
            nodes.add_row(
                row.name,
                row.health,
                row.version,
                row.last_seen,
                _untrusted(row.quarantine_reason),
            )

        containers = self.query_one("#containers", DataTable)
        containers.clear()
        for record in state.get("stack_containers") or []:
            age = now - float(record.get("updated_at") or 0.0)
            containers.add_row(
                _untrusted(record.get("node") or ""),
                _untrusted(record.get("name") or ""),
                _untrusted(record.get("stack") or ""),
                _untrusted(record.get("state") or ""),
                _untrusted(record.get("status") or ""),
                model.ago(age),
                key=json.dumps([record.get("node"), record.get("name")]),
            )

        commands = self.query_one("#commands", DataTable)
        commands.clear()
        for cmd in model.command_rows(state):
            commands.add_row(
                str(cmd.id),
                cmd.node,
                cmd.verb,
                cmd.target,
                cmd.state,
                _untrusted(cmd.deferred_reason),
            )

        stacks = self.query_one("#stacks", DataTable)
        stacks.clear()
        for stack in model.stack_rows(state):
            stacks.add_row(
                stack.node,
                stack.name,
                stack.kind,
                stack.desired,
                stack.actual if stack.converged else f"{stack.actual} ← off desired",
                _untrusted(stack.detail),
            )

        runs_notice = self.query_one("#runs-notice", Static)
        notice_text = model.runs_notice(self.runs_index.truncated)
        runs_notice.update(notice_text)
        runs_notice.display = bool(notice_text)

        pauses_notice = self.query_one("#pauses-notice", Static)
        pause_text = model.pause_notice(state)
        pauses_notice.update(_untrusted(pause_text))
        pauses_notice.display = bool(pause_text)

        runs = self.query_one("#runs-table", DataTable)
        runs.clear()
        for run in self.run_rows:
            runs.add_row(
                f"{run.repo}#{run.issue}",
                run.phase,
                str(run.attempt or "-"),
                f"{run.driver} · {run.node}",
                run.run_id,
                f"#{run.pr}" if run.pr else "-",
                model.ago(now - run.last_event_at),
                # Unique by construction (latest per (repo, issue), ADR-0056).
                key=f"{run.repo}#{run.issue}",
            )
        self._render_run_detail()

        self._render_feed(
            "#events-table",
            "#events-notice",
            self.events_feed,
            self.events_evicted,
            self.events_gap,
            now,
        )
        self._render_feed(
            "#errors-table",
            "#errors-notice",
            self.errors_feed,
            self.errors_evicted,
            self.errors_gap,
            now,
            errors=True,
        )

        settings_table = self.query_one("#settings-table", DataTable)
        settings_table.clear()
        for key, value in model.settings_rows(state):
            settings_table.add_row(key, _untrusted(value))

    def _render_feed(
        self,
        table_id: str,
        notice_id: str,
        feed: list[dict[str, Any]],
        evicted: bool,
        gap: bool,
        now: float,
        *,
        errors: bool = False,
    ) -> None:
        notice = self.query_one(notice_id, Static)
        text = model.continuity_notice(evicted, gap)
        notice.update(text)
        notice.display = bool(text)
        table = self.query_one(table_id, DataTable)
        table.clear()
        for event in feed:
            payload = event.get("payload") or {}
            when = model.ago(now - float(event.get("received_at") or 0.0))
            if errors:
                table.add_row(
                    when,
                    _untrusted(event.get("node") or ""),
                    _untrusted(event.get("component") or ""),
                    _untrusted(payload.get("error_class") or ""),
                    _untrusted(str(payload.get("message") or "")[:200]),
                )
            else:
                compact = json.dumps(payload, sort_keys=True)[:160]
                table.add_row(
                    when,
                    _untrusted(event.get("type") or ""),
                    _untrusted(event.get("node") or ""),
                    _untrusted(event.get("component") or ""),
                    _untrusted(compact),
                )

    def _render_run_detail(self) -> None:
        detail = self.query_one("#run-detail", Static)
        run = next(
            (r for r in self.run_rows if f"{r.repo}#{r.issue}" == self._selected_run),
            self.run_rows[0] if self.run_rows else None,
        )
        if run is None:
            detail.update("no Runs on record")
            return
        now = float(self.state_doc.get("now") or 0.0)
        budget = model.timeout_budget_seconds(self.state_doc, run.stack)
        lines = Text()
        lines.append(
            f"run {run.run_id or '?'} · {run.repo}#{run.issue} · {run.driver} · {run.node}"
            f" · stack {run.stack or '?'}\n"
        )
        lines.append(
            f"phase {run.phase} · attempt {run.attempt or '-'}"
            f" · last event {model.ago(now - run.last_event_at)}\n"
        )
        if run.terminal:
            lines.append(f"outcome: {run.phase}\n")
            lines.append("failure class: ", style="bold")
            if run.phase == "pr-open":
                lines.append(f"{FAILURE_CLASS_NOT_APPLICABLE}\n")
            else:
                lines.append(Text(run.failure_class or FAILURE_CLASS_LEGACY))
                lines.append("\n")
            lines.append(f"PR: {f'#{run.pr} · {run.pr_url}' if run.pr else '(none)'}\n")
            lines.append(
                f"evidence bundle: {run.evidence_ref or '(unknown run id)'}"
                + (f" · {run.evidence_url}" if run.evidence_url else "")
                + "\n"
            )
        elif run.elapsed_seconds is not None:
            lines.append(
                f"live: {run.progress_phase or '?'}"
                f" · elapsed {model.elapsed(run.elapsed_seconds)}"
                f" / budget {model.elapsed(budget)}"
                f" · {run.tool_calls or 0} tool call(s)\n"
            )
        else:
            lines.append(
                "telemetry unavailable (progress events are evictable advisory"
                " telemetry, ADR-0016 — their absence never removes the Run)\n"
            )
        if not run.terminal and run.transcript_tail:
            shown = len(run.transcript_tail.encode("utf-8", errors="replace"))
            total = run.transcript_bytes or 0
            lines.append(
                f"\nadvisory transcript tail — rolling cache, last {shown} bytes"
                f" of a {total}-byte transcript (not an archive; ADR-0016):\n",
                style="bold",
            )
            lines.append(Text(run.transcript_tail))  # untrusted: never markup
        detail.update(lines)

    # -- interactions -------------------------------------------------------

    def action_switch_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    async def action_refresh(self) -> None:
        await self.refresh_now()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.notify(f"follow mode {'on' if self.follow else 'paused'}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.control.id == "runs-table" and event.row_key is not None:
            self._selected_run = event.row_key.value
            self._render_run_detail()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        mapping = {
            "flt-type": (self.events_filters, "type"),
            "flt-node": (self.events_filters, "node"),
            "flt-component": (self.events_filters, "component"),
            "err-node": (self.errors_filters, "node"),
            "err-component": (self.errors_filters, "component"),
        }
        if event.input.id not in mapping:
            return
        filters, key = mapping[event.input.id]
        filters[key] = event.value.strip()
        # A changed conjunction is a NEW query: the feed, its follow point,
        # its eviction verdict, AND its client-side continuity state all
        # reset (ADR-0038: `evicted` is query-relative — the old panel's
        # verdicts do not carry over; a follow gap belongs to the old query).
        if filters is self.events_filters:
            self.events_feed, self.events_evicted, self.events_gap = [], False, False
        else:
            self.errors_feed, self.errors_evicted, self.errors_gap = [], False, False
        self.run_worker(self.refresh_now, exclusive=False)

    # -- the three writes ---------------------------------------------------

    def action_command(self) -> None:
        nodes = [str(n.get("name") or "") for n in self.state_doc.get("nodes") or []]
        self.push_screen(CommandScreen(nodes), self._queue_command)

    def _queue_command(self, result: dict[str, Any] | None) -> None:
        if not result:
            return

        async def submit() -> None:
            try:
                answer = await asyncio.to_thread(
                    self.client.queue_command,
                    result["node"],
                    result["verb"],
                    result["target"],
                )
            except ControlUnreachable as exc:
                self.notify(f"command failed: {exc.error_class}", severity="error")
                return
            self.notify(
                f"queued command {answer.get('id')} ({result['verb']} on {result['node']})"
                " — a mid-Run node defers it (queue-behind); watch the command queue"
            )
            await self.refresh_now()

        self.run_worker(submit, exclusive=False)

    def action_release_quarantine(self) -> None:
        quarantined = [
            str(r.get("node") or "")
            for r in self.state_doc.get("node_health") or []
            if r.get("quarantined")
        ]
        self.push_screen(QuarantineScreen(quarantined), self._release_quarantine)

    def _release_quarantine(self, node: str | None) -> None:
        if not node:
            return

        async def submit() -> None:
            try:
                answer = await asyncio.to_thread(self.client.release_quarantine, node)
            except ControlUnreachable as exc:
                self.notify(f"release failed: {exc.error_class}", severity="error")
                return
            released = answer.get("released")
            self.notify(
                f"node {node}: quarantine released"
                if released
                else f"node {node}: was not quarantined"
            )
            await self.refresh_now()

        self.run_worker(submit, exclusive=False)

    def action_secret(self) -> None:
        self.push_screen(SecretScreen(), self._store_secret)

    def _store_secret(self, result: tuple[str, str] | None) -> None:
        if not result:
            return
        name, value = result

        async def submit() -> None:
            try:
                await asyncio.to_thread(self.client.put_secret, name, value)
            except ControlUnreachable as exc:
                self.notify(f"secret store failed: {exc.error_class}", severity="error")
                return
            # The never-display contract: the confirmation names the secret
            # and nothing anywhere re-renders the value.
            self.notify(f"secret {name!r} stored (encrypted at rest; value never displayed)")

        self.run_worker(submit, exclusive=False)

    def action_attach(self) -> None:
        containers = self.query_one("#containers", DataTable)
        if not containers.row_count:
            self.notify("no live containers (per heartbeats)", severity="warning")
            return
        row_key = containers.coordinate_to_cell_key(containers.cursor_coordinate).row_key
        node, container = json.loads(row_key.value or "[]")
        # Fail closed while degraded (ADR-0040 amendment): a retained
        # snapshot's server clock is frozen, so heartbeat evidence can look
        # fresh forever — refuse until a refresh succeeds; the write flows
        # stay available (they attempt the server call and report failure).
        result = model.attach_command(
            self.state_doc, node, container, snapshot_fresh=not self.freshness.degraded
        )
        self.push_screen(AttachScreen(result, node, container))


def run_top(url: str, token: str, ca: str | None) -> int:
    """The ``theozolith top`` entry: one client, one app, block until quit."""
    TopApp(ControlClient(url, token, ca)).run()
    return 0
