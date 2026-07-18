"""The M4 web surface: admin auth, the read-only dashboard fragments, the
secret entry form, and the web terminal (PTY bridge + audit log)."""

from __future__ import annotations

import json

from controlrig import ADMIN_TOKEN, ControlRig, make_rig, run_event

ADMIN_BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

WORKER_STACK_TOML = """\
kind = "process"
node = "box1"
command = "theozolith-worker"
attach = "sh -c 'printf hello-{container}; cat'"

[secrets]
WORKER_GITHUB_TOKEN = "github-worker"
"""

MUTE_STACK_TOML = """\
kind = "process"
node = "box1"
command = "acme-runner"
"""


def login(control: ControlRig) -> None:
    response = control.client.post("/login", data={"token": ADMIN_TOKEN}, follow_redirects=False)
    assert response.status_code == 303


def heartbeat_worker_node(control: ControlRig, state: str = "running") -> None:
    control.heartbeat(
        node="box1",
        stacks=[{"name": "worker", "kind": "process", "state": state, "detail": "pid 7"}],
        run_containers=[
            {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up"}
        ],
    )


# -- auth (acceptance 7: all routes reject unauthenticated access) ---------------


def test_every_web_route_rejects_unauthenticated_access(control: ControlRig):
    for path in ("/", "/secrets", "/terminal"):
        response = control.client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    for path in ("/fragments/fleet", "/fragments/runs", "/fragments/activity"):
        assert control.client.get(path).status_code == 401
    submit = control.client.post(
        "/secrets", data={"name": "x", "value": "y"}, follow_redirects=False
    )
    assert submit.status_code == 303 and submit.headers["location"] == "/login"
    assert control.store.secret_names() == []  # nothing was stored


def test_login_flow_and_wrong_credential(control: ControlRig):
    assert control.client.post("/login", data={"token": "wrong"}).status_code == 401
    login(control)
    assert control.client.get("/", follow_redirects=False).status_code == 200
    # The admin bearer token works too (same single credential).
    fresh = make_rig(control.settings.data_dir.parent)
    assert fresh.client.get("/fragments/fleet", headers=ADMIN_BEARER).status_code == 200


def test_logout_ends_the_session(control: ControlRig):
    login(control)
    control.client.post("/logout", follow_redirects=False)
    assert control.client.get("/", follow_redirects=False).status_code == 303


# -- the fleet fragment (acceptances 1 and 4) ------------------------------------


def test_node_state_change_is_reflected_on_the_next_poll(control: ControlRig):
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    login(control)
    heartbeat_worker_node(control, state="running")
    page = control.client.get("/fragments/fleet").text
    assert "box1" in page and "worker" in page

    heartbeat_worker_node(control, state="stopped")
    page = control.client.get("/fragments/fleet").text
    assert "stopped" in page  # desired running vs actual stopped is visible


def test_build_skew_between_nodes_is_flagged(control: ControlRig):
    """Acceptance 4: same image name on two nodes, different build metadata."""
    login(control)
    for node, digest in (("box1", "hash-aaa"), ("box2", "hash-bbb")):
        control.heartbeat(
            node=node,
            images=[
                {
                    "name": "claude-dev",
                    "tag": f"t-{digest}",
                    "base_digest": "sha256:" + "0" * 20,
                    "instruction_hash": digest,
                    "built_at": "now",
                }
            ],
        )
    page = control.client.get("/fragments/fleet").text
    assert "build skew across the fleet" in page and "claude-dev" in page

    # Converged fleets show no skew banner.
    for node in ("box1", "box2"):
        control.heartbeat(
            node=node,
            images=[
                {
                    "name": "claude-dev",
                    "tag": "t-same",
                    "base_digest": "sha256:" + "0" * 20,
                    "instruction_hash": "hash-same",
                    "built_at": "now",
                }
            ],
        )
    assert "build skew" not in control.client.get("/fragments/fleet").text


# -- the runs fragment (acceptance 2) --------------------------------------------


def test_run_phases_and_links_appear_as_they_happen(control: ControlRig):
    login(control)
    control.node_post("/api/v1/events", run_event(5, "claimed"))
    page = control.client.get("/fragments/runs").text
    assert "claimed" in page and "issues/5" in page

    control.node_post(
        "/api/v1/events",
        {
            "type": "theozolith.run.progress",
            "worker": "worker-a",
            "node": "box1",
            "issue": 5,
            "run_id": "r1",
            "attempt": 1,
            "phase": "agent",
            "elapsed_seconds": 42,
            "tool_calls": 7,
            "transcript_tail": "<script>alert('pwn')</script> tail text",
        },
    )
    page = control.client.get("/fragments/runs").text
    assert "7 tool call(s)" in page and "tail text" in page
    # Agent-authored text is untrusted: escaped, never rendered as markup.
    assert "<script>alert" not in page and "&lt;script&gt;" in page

    control.node_post("/api/v1/events", run_event(5, "pr-open", pr=11))
    page = control.client.get("/fragments/runs").text
    assert "pr-open" in page and "pull/11" in page


def test_zombie_malformed_and_quarantine_flags_are_visible(control: ControlRig):
    login(control)
    control.store.flag_zombie(5, "r1", "worker-a", "box1")
    control.store.record_malformed(9, "carries failed + plan_ready")
    for run_id in ("r2", "r3"):
        control.node_post("/api/v1/events", run_event(6, "failed", run_id=run_id))
    page = control.client.get("/fragments/runs").text
    assert "zombie" in page and "awaiting swept evidence" in page
    assert "malformed" in page and "failed + plan_ready" in page
    assert "quarantined" in page and "consecutive failed Runs" in page


# -- the activity fragment (acceptance 3) ----------------------------------------


def test_unknown_custom_event_renders_generically(control: ControlRig):
    """Acceptance 3: a custom namespaced type gets visibility with no
    product change — type shown, payload rendered as escaped JSON."""
    login(control)
    event = {"type": "acme.backup", "volume": "media", "note": "<b>bold?</b>"}
    assert control.node_post("/api/v1/events", event).status_code == 200
    page = control.client.get("/fragments/activity").text
    assert "acme.backup" in page
    assert "&#34;volume&#34;: &#34;media&#34;" in page or "media" in page
    assert "<b>bold?</b>" not in page  # escaped, not interpreted


# -- the secret form (acceptance 5) ----------------------------------------------


def test_web_secret_reaches_a_referencing_node_like_the_cli(control: ControlRig):
    """Acceptance 5: the form writes through the same store as the API; a
    referencing node pulls the exact value; the UI never echoes it."""
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    login(control)
    response = control.client.post(
        "/secrets",
        data={"name": "github-worker", "value": "ghp_secret_value"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    pulled = control.node_post("/api/v1/secrets/pull", {"node": "box1", "names": ["github-worker"]})
    assert pulled.json() == {"secrets": {"github-worker": "ghp_secret_value"}}

    page = control.client.get("/secrets").text
    assert "github-worker" in page  # the name is listed…
    assert "ghp_secret_value" not in page  # …the value is never echoed


def test_secret_form_refuses_without_the_tls_channel(tmp_path):
    rig = make_rig(tmp_path, secrets_channel_ok=False)
    rig.client.post("/login", data={"token": ADMIN_TOKEN})
    response = rig.client.post("/secrets", data={"name": "n", "value": "v"})
    assert response.status_code == 403
    assert rig.store.secret_names() == []


# -- the web terminal (acceptance 6) ---------------------------------------------


def test_attach_affordance_requires_config_and_live_container(control: ControlRig):
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    control.write_config("stacks/acme.toml", MUTE_STACK_TOML)
    login(control)
    control.heartbeat(
        node="box1",
        stacks=[
            {"name": "worker", "kind": "process", "state": "running", "detail": ""},
            {"name": "acme", "kind": "process", "state": "running", "detail": ""},
        ],
        run_containers=[
            {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up"},
            {"name": "ozolith-run-r9", "run_id": "r9", "owner": "acme", "status": "Up"},
        ],
    )
    page = control.client.get("/fragments/fleet").text
    # The attach affordance exists exactly for the Stack with an attach command.
    assert "/terminal?node=box1&amp;stack=worker&amp;container=ozolith-run-r1" in page
    assert "stack=acme" not in page

    # The no-attach Stack and a dead container both refuse the terminal page.
    no_attach = control.client.get(
        "/terminal", params={"node": "box1", "stack": "acme", "container": "ozolith-run-r9"}
    )
    assert "exposes no terminal" in no_attach.text
    dead = control.client.get(
        "/terminal", params={"node": "box1", "stack": "worker", "container": "ozolith-run-gone"}
    )
    assert "not live" in dead.text


def test_terminal_bridge_relays_io_and_audit_logs_the_session(control: ControlRig):
    """Acceptance 6: attach works, the audit log records actor, timestamp,
    and target."""
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    heartbeat_worker_node(control)

    with control.client.websocket_connect(
        "/terminal/ws?node=box1&stack=worker&container=ozolith-run-r1",
        headers=ADMIN_BEARER,
    ) as socket:
        received = b""
        while b"hello-ozolith-run-r1" not in received:
            received += socket.receive_bytes()
        socket.send_json({"resize": {"cols": 120, "rows": 40}})
        socket.send_bytes(b"echo-me\n")
        while b"echo-me" not in received:
            received += socket.receive_bytes()

    lines = [
        json.loads(line) for line in control.settings.terminal_audit_path.read_text().splitlines()
    ]
    attach = next(line for line in lines if line["event"] == "attach")
    assert attach["actor"] == "admin" and attach["at"]
    assert attach["node"] == "box1" and attach["container"] == "ozolith-run-r1"
    assert attach["stack"] == "worker"
    detach = next(line for line in lines if line["event"] == "detach")
    assert detach["container"] == "ozolith-run-r1"


def test_terminal_websocket_rejects_unauthenticated_and_unconfigured(control: ControlRig):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    heartbeat_worker_node(control)
    # No credential at all.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        control.client.websocket_connect(
            "/terminal/ws?node=box1&stack=worker&container=ozolith-run-r1"
        ),
    ):
        pass
    assert refused.value.code == 4401
    # Authenticated, but the target container is not live.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        control.client.websocket_connect(
            "/terminal/ws?node=box1&stack=worker&container=ozolith-run-gone",
            headers=ADMIN_BEARER,
        ),
    ):
        pass
    assert refused.value.code == 4404
