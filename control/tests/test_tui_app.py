"""tui.app (M9): the Textual application driven headless — panels render
from the two documents, the three write flows hit their endpoints (and only
after their confirmations), attach prints without spawning anything, and a
dead Control Node degrades to a banner over stale data, never a blank
screen."""

from __future__ import annotations

import pty
import subprocess

import pytest
from textual.widgets import DataTable, Input, Select, Static
from theozolith_control.tui.app import (
    AttachScreen,
    CommandScreen,
    QuarantineScreen,
    SecretScreen,
    TopApp,
)
from theozolith_control.tui.client import ControlUnreachable
from tuirig import NOW, FakeClient, page, progress_event, run_event, state_doc

LOCAL_NOW = 42_000.0


def make_app(fake: FakeClient) -> TopApp:
    return TopApp(fake, refresh_seconds=None, clock=lambda: LOCAL_NOW)


def rows(app: TopApp, table_id: str) -> list[list[str]]:
    table = app.query_one(table_id, DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


def all_rendered_text(app: TopApp) -> str:
    chunks: list[str] = []
    for screen in app.screen_stack:
        for static in screen.query(Static):
            chunks.append(str(static.content))
        for table in screen.query(DataTable):
            for i in range(table.row_count):
                chunks.append(" ".join(str(cell) for cell in table.get_row_at(i)))
    return "\n".join(chunks)


# -- panels render from the two documents (acceptance 1's data path) ------------


@pytest.mark.asyncio
async def test_panels_render_from_the_api_documents():
    fake = FakeClient()
    fake.state_doc["commands"] = [
        {
            "id": 4,
            "node": "box1",
            "verb": "recycle",
            "target": "deck",
            "completed_at": None,
            "deferred_reason": "queued behind run r7",
        }
    ]
    fake.events_pages["theozolith.run"] = page([run_event(30, 7, "gate", run_id="r2", attempt=2)])
    fake.events_pages["theozolith.run.progress"] = page(
        [progress_event(31, "r2", elapsed_seconds=480.0, tool_calls=44)]
    )
    fake.events_pages[""] = page([run_event(30, 7, "gate", run_id="r2")])
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        assert fake.state_calls >= 1
        node_rows = rows(app, "#nodes")
        assert node_rows[0][:3] == ["box1", "ok", "0.3.0"]
        assert rows(app, "#containers")[0][1] == "flight-deck-1"
        # Queue-behind visibility (acceptance 3): the deferral state the
        # daemon reported over heartbeats surfaces on the command queue.
        command_rows = rows(app, "#commands")
        assert command_rows[0][4] == "deferred"
        assert "queued behind run r7" in command_rows[0][5]
        assert rows(app, "#stacks")[0][:5] == ["box1", "deck", "container", "running", "running"]
        run_rows = rows(app, "#runs-table")
        assert run_rows[0][0] == "#7" and run_rows[0][1] == "gate"
        settings = dict((r[0], r[1]) for r in rows(app, "#settings-table"))
        assert settings["control_ip"] == "203.0.113.5"
        assert settings["heartbeat_seconds"] == "60.0"


@pytest.mark.asyncio
async def test_run_detail_labels_the_advisory_tail_with_its_byte_count():
    fake = FakeClient()
    fake.events_pages["theozolith.run"] = page([run_event(30, 7, "gate", run_id="r2", attempt=2)])
    fake.events_pages["theozolith.run.progress"] = page(
        [
            progress_event(
                31,
                "r2",
                elapsed_seconds=480.0,
                tool_calls=44,
                transcript_tail="agent said [bold]things[/bold]",
                transcript_bytes=99999,
            )
        ]
    )
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        detail = str(app.query_one("#run-detail", Static).content)
        assert "phase gate · attempt 2" in detail
        assert "elapsed 8m / budget 60m" in detail  # worker default: 3600s
        assert "44 tool call(s)" in detail
        assert "advisory transcript tail" in detail
        shown = len(b"agent said [bold]things[/bold]")
        assert f"last {shown} bytes of a 99999-byte transcript" in detail
        # Untrusted text renders verbatim — the [bold] stays literal.
        assert "[bold]things[/bold]" in detail


@pytest.mark.asyncio
async def test_run_detail_timeout_budget_honors_the_stack_env_override():
    fake = FakeClient()
    fake.state_doc["desired_stacks"].append(
        {
            "node": "box1",
            "name": "worker",
            "kind": "process",
            "state": "running",
            "env": {"THEOZOLITH_AGENT_TIMEOUT_SECONDS": "7200"},
            "attach": [],
        }
    )
    fake.events_pages["theozolith.run"] = page([run_event(30, 7, "claimed", run_id="r2")])
    fake.events_pages["theozolith.run.progress"] = page(
        [progress_event(31, "r2", elapsed_seconds=600.0)]
    )
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        detail = str(app.query_one("#run-detail", Static).content)
        assert "elapsed 10m / budget 2.0h" in detail


@pytest.mark.asyncio
async def test_terminal_run_detail_shows_outcome_pr_and_evidence():
    fake = FakeClient()
    fake.events_pages["theozolith.run"] = page([run_event(30, 5, "failed", run_id="r9", attempt=2)])
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        detail = str(app.query_one("#run-detail", Static).content)
        assert "outcome: failed" in detail
        # The channel gap, rendered honestly (ADR-0040) — never a blank.
        assert "failure class:" in detail and "not on the channel" in detail
        assert "theozolith/evidence: runs/issue-5/r9" in detail
        assert "github.com/acme/sandbox/tree/theozolith/evidence/runs/issue-5/r9" in detail


# -- the destructive-command flow (acceptance 3) --------------------------------


@pytest.mark.asyncio
async def test_recycle_demands_the_stack_name_typed_back():
    fake = FakeClient()
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        await pilot.press("c")
        screen = app.screen
        assert isinstance(screen, CommandScreen)
        screen.query_one("#cmd-verb", Select).value = "recycle"
        screen.query_one("#cmd-node", Input).value = "box1"
        screen.query_one("#cmd-target", Input).value = "deck"
        screen.query_one("#cmd-confirm", Input).value = "dcek"  # wrong name
        screen.query_one("#cmd-queue").press()
        await pilot.pause()
        assert fake.commands == []  # refused before any HTTP write
        assert isinstance(app.screen, CommandScreen)  # still open, error shown
        assert "type the target name" in str(screen.query_one(".dialog-error", Static).content)
        screen.query_one("#cmd-confirm", Input).value = "deck"
        screen.query_one("#cmd-queue").press()
        await pilot.pause()
        assert fake.commands == [("box1", "recycle", "deck", False)]


@pytest.mark.asyncio
async def test_drain_needs_no_typed_confirmation():
    fake = FakeClient()
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        await pilot.press("c")
        screen = app.screen
        screen.query_one("#cmd-verb", Select).value = "drain"
        screen.query_one("#cmd-node", Input).value = "box1"
        screen.query_one("#cmd-target", Input).value = "deck"
        screen.query_one("#cmd-queue").press()
        await pilot.pause()
        assert fake.commands == [("box1", "drain", "deck", False)]


# -- quarantine release (acceptance 4) ------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_release_is_one_confirmed_action():
    fake = FakeClient(
        state_doc(
            node_health=[
                {
                    "node": "box1",
                    "consecutive_failures": 2,
                    "quarantined": 1,
                    "reason": "2 consecutive failed Runs",
                    "since": NOW - 60,
                }
            ]
        )
    )
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        assert rows(app, "#nodes")[0][1] == "quarantined"
        await pilot.press("x")
        screen = app.screen
        assert isinstance(screen, QuarantineScreen)
        screen.query_one("#q-node", Select).value = "box1"
        screen.query_one("#q-release").press()
        await pilot.pause()
        assert fake.released == ["box1"]
        # The fake's release cleared node_health; the follow-up refresh
        # rendered the node dispatch-eligible again.
        assert rows(app, "#nodes")[0][1] == "ok"


# -- secret entry (acceptance 5) ------------------------------------------------


@pytest.mark.asyncio
async def test_secret_entry_is_masked_and_never_rendered():
    fake = FakeClient()
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        await pilot.press("s")
        screen = app.screen
        assert isinstance(screen, SecretScreen)
        value_input = screen.query_one("#secret-value", Input)
        assert value_input.password is True  # masked at the input
        screen.query_one("#secret-name", Input).value = "anthropic-api-key"
        value_input.value = "sk-super-secret-value"
        screen.query_one("#secret-store").press()
        await pilot.pause()
        assert fake.secrets == [("anthropic-api-key", "sk-super-secret-value")]
        # The never-display contract: the value appears nowhere in any
        # rendered surface, and no notification carries it.
        assert "sk-super-secret-value" not in all_rendered_text(app)


# -- attach assistance (acceptance 6) -------------------------------------------


@pytest.mark.asyncio
async def test_attach_prints_the_exact_command_and_spawns_nothing(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the TUI must never spawn a process or PTY (M9 ruling)")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(pty, "openpty", explode)
    monkeypatch.setattr(pty, "fork", explode)
    monkeypatch.setattr("os.system", explode)

    fake = FakeClient()
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        await pilot.press("a")  # containers cursor sits on the only row
        screen = app.screen
        assert isinstance(screen, AttachScreen)
        printed = str(screen.query_one("#attach-command", Static).content)
        assert printed == "ssh box1 -t docker exec -it flight-deck-1 tmux attach"
        assert "nothing was executed" in all_rendered_text(app)


@pytest.mark.asyncio
async def test_attach_refuses_on_stale_heartbeat_evidence():
    fake = FakeClient()
    fake.state_doc["stack_containers"][0]["updated_at"] = NOW - 400
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        await pilot.press("a")
        screen = app.screen
        assert isinstance(screen, AttachScreen)
        reason = str(screen.query_one("#attach-refusal", Static).content)
        assert "stale" in reason and "server clock" in reason and "150" in reason


# -- events: follow + eviction honesty (acceptance 8) ---------------------------


@pytest.mark.asyncio
async def test_events_follow_advances_by_cursor_without_refetching_history():
    fake = FakeClient()
    fake.events_pages[""] = page([run_event(10, 1, "claimed")])
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        assert [e["id"] for e in app.events_feed] == [10]
        # New rows land; the head fetch overlaps id 10 and stops there.
        fake.events_pages[""] = page(
            [run_event(12, 2, "claimed"), run_event(11, 2, "claimed"), run_event(10, 1, "claimed")],
            next_cursor="10",
        )
        fake.events_calls.clear()
        await app.refresh_now()
        assert [e["id"] for e in app.events_feed] == [12, 11, 10]
        unfiltered = [c for c in fake.events_calls if "type" not in c]
        assert len(unfiltered) == 1 and "cursor" not in unfiltered[0]


@pytest.mark.asyncio
async def test_eviction_notice_is_per_panel_and_query_relative():
    fake = FakeClient()
    # The errors panel's own query lost evidence; the unfiltered events
    # panel did not (any_evicted alone must not flag it — ADR-0038/0039).
    fake.events_pages["theozolith.error"] = page([], evicted=True)
    unaffected = page([])
    unaffected["any_evicted"] = True
    fake.events_pages[""] = unaffected
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        errors_notice = app.query_one("#errors-notice", Static)
        events_notice = app.query_one("#events-notice", Static)
        assert errors_notice.display is True
        assert "may be evicted" in str(errors_notice.content)
        assert events_notice.display is False


@pytest.mark.asyncio
async def test_changing_a_filter_resets_the_feed_and_requeries():
    fake = FakeClient()
    fake.events_pages[""] = page([run_event(10, 1, "claimed")])
    app = make_app(fake)
    async with app.run_test(size=(120, 50)) as pilot:
        await app.refresh_now()
        assert app.events_feed
        flt = app.query_one("#flt-type", Input)
        flt.value = "acme.custom"
        flt.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.events_filters["type"] == "acme.custom"
        typed = [c for c in fake.events_calls if c.get("type") == "acme.custom"]
        assert typed  # the new conjunction was queried


# -- degraded mode (ADR-0040) ---------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_control_degrades_to_a_banner_over_stale_data():
    fake = FakeClient()
    app = make_app(fake)
    async with app.run_test(size=(120, 50)):
        await app.refresh_now()
        assert rows(app, "#nodes")  # data on screen
        fake.fail_with = ControlUnreachable(
            "https://127.0.0.1:9443", "ConnectionRefusedError", "refused"
        )
        await app.refresh_now()
        banner = app.query_one("#banner", Static)
        assert banner.display is True
        text = str(banner.content)
        assert "CONTROL UNREACHABLE" in text and "ConnectionRefusedError" in text
        assert rows(app, "#nodes")  # the last documents remain rendered
        fake.fail_with = None
        await app.refresh_now()
        assert app.query_one("#banner", Static).display is False
