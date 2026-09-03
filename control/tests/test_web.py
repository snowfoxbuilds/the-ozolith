"""The M4 web surface: admin auth, the read-only dashboard fragments, the
secret entry form, and the web terminal (PTY bridge + audit log), plus the
M5 hardening contracts (ADR-0019): browser-origin isolation, server-derived
terminal targets, and terminal resource caps."""

from __future__ import annotations

import json

import pytest
from controlrig import (
    ADMIN_PASSWORD,
    ADMIN_TOKEN,
    CONTROL_IP,
    CONTROL_ORIGIN,
    ControlRig,
    make_rig,
    run_event,
)
from starlette.websockets import WebSocketDisconnect
from theozolith_control.web.auth import SESSION_COOKIE

ADMIN_BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

# Process Stacks expose no terminal (ADR-0019: run containers are headless
# and never attach targets).
# A neutral process Stack (worker semantics are irrelevant to the dashboard
# rendering here); a built-in-driver command would be rejected (ADR-0044).
WORKER_STACK_TOML = """\
kind = "process"
node = "box1"
command = "sleep 30"

[secrets]
IMPLEMENTER_GITHUB_TOKEN = "github-implementer"
"""

MUTE_STACK_TOML = """\
kind = "process"
node = "box1"
command = "acme-runner"
"""

# The Flight Deck (ADR-0019): the web terminal's primary target — a
# container-kind Stack with its own machine identity and an attach argv
# (placeholders only as complete arguments; the sh trampoline receives the
# validated container name as $1).
FLIGHTDECK_STACK_TOML = """\
kind = "container"
node = "box1"
image = "ghcr.io/example/flightdeck:1"
command = "tmux new-session -d -s flightdeck claude"
attach = ["sh", "-c", "printf 'hello-%s' \\"$1\\"; cat", "attach-sh", "{container}"]

[secrets]
GITHUB_TOKEN = "flightdeck-github-token"
"""

# A container Stack with no attach command: exposes no terminal.
MUTE_DECK_TOML = """\
kind = "container"
node = "box1"
image = "ghcr.io/example/mutedeck:1"
"""

# The canonical browser origin is the rig's control IP (ADR-0034). Default
# HTTPS: no port in the origin, the Host header, or anywhere else — the
# Uvicorn bind port is invisible to browsers (M5). NEUTRAL_BASE builds a
# rig whose client sends neither the canonical Host nor any Origin, so
# each request opts in explicitly.
CANONICAL_HOST = CONTROL_IP
CANONICAL_ORIGIN = CONTROL_ORIGIN
CANONICAL_HEADERS = {"Host": CANONICAL_HOST, "Origin": CANONICAL_ORIGIN}
NEUTRAL_BASE = "https://testserver"


def login(control: ControlRig) -> None:
    response = control.client.post(
        "/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303


def heartbeat_worker_node(control: ControlRig, state: str = "running") -> None:
    control.heartbeat(
        node="box1",
        stacks=[{"name": "worker", "kind": "process", "state": state, "detail": "pid 7"}],
        run_containers=[
            {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up"}
        ],
    )


FLIGHTDECK_CONTAINER = "ozolith-stack-flightdeck"


def heartbeat_flightdeck_node(control: ControlRig) -> None:
    control.heartbeat(
        node="box1",
        stacks=[{"name": "flightdeck", "kind": "container", "state": "running", "detail": "Up"}],
        stack_containers=[
            {
                "name": FLIGHTDECK_CONTAINER,
                "stack": "flightdeck",
                "state": "running",
                "status": "Up",
            }
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
    assert control.secret_store.secret_names() == []  # nothing was stored


def test_login_flow_and_wrong_credential(control: ControlRig):
    assert control.client.post("/login", data={"password": "wrong"}).status_code == 401
    login(control)
    assert control.client.get("/", follow_redirects=False).status_code == 200
    # The admin bearer token works too (same single credential).
    fresh = make_rig(control.settings.data_dir.parent)
    assert fresh.client.get("/fragments/fleet", headers=ADMIN_BEARER).status_code == 200


def test_logout_ends_the_session(control: ControlRig):
    login(control)
    control.client.post("/logout", follow_redirects=False)
    assert control.client.get("/", follow_redirects=False).status_code == 303


# -- OZ-07: baseline browser security headers ------------------------------------


def test_security_headers_present_and_static_stays_cacheable(control: ControlRig):
    resp = control.client.get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp  # no 'unsafe-inline' for scripts
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert resp.headers["Cache-Control"] == "no-store"

    # Static assets carry the blanket headers but stay cacheable (no no-store).
    asset = control.client.get("/static/htmx.min.js")
    assert asset.status_code == 200
    assert asset.headers.get("Cache-Control", "") != "no-store"


def test_csp_nonce_is_per_response(control: ControlRig):
    def _nonce() -> str:
        csp = control.client.get("/login").headers["Content-Security-Policy"]
        return csp.split("'nonce-")[1].split("'")[0]

    assert _nonce() != _nonce()


def test_join_token_page_is_never_cached(control: ControlRig):
    """The join page renders a live join string — a browser or proxy must not
    retain it (OZ-07)."""
    login(control)
    resp = control.client.get("/join")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"


def test_terminal_inline_script_carries_the_csp_nonce(control: ControlRig):
    """The terminal's inline bootstrap must carry the per-response CSP nonce,
    or a production browser blocks it under our nonce-only script-src (OZ-07).
    Render the REAL ok-branch (a live attach-capable container) and assert the
    inline <script>'s nonce equals the response's CSP nonce — the wiring that
    happy-path header tests never exercise."""
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    login(control)
    control.heartbeat(
        node="box1",
        stacks=[{"name": "flightdeck", "kind": "container", "state": "running", "detail": ""}],
        stack_containers=[
            {
                "name": FLIGHTDECK_CONTAINER,
                "stack": "flightdeck",
                "state": "running",
                "status": "Up",
            }
        ],
    )
    resp = control.client.get(
        "/terminal", params={"node": "box1", "container": FLIGHTDECK_CONTAINER}
    )
    assert resp.status_code == 200
    assert 'id="terminal"' in resp.text  # the ok-branch actually rendered
    nonce = resp.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
    assert nonce and f'<script nonce="{nonce}">' in resp.text


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


def test_product_version_skew_is_surfaced(control: ControlRig):
    """ADR-0015 (2026-07-22): every heartbeat reports the running product
    version; the dashboard surfaces nodes off the recorded pin."""
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    login(control)
    control.heartbeat(node="box1", version="0.4.0")
    control.heartbeat(node="box2", version="0.3.0+gabc123def456")

    page = control.client.get("/fragments/fleet").text
    assert "product version skew" in page and "box2" in page
    assert "0.3.0+gabc123def456" in page  # the odd version is visible

    control.heartbeat(node="box2", version="0.4.0")  # converged
    assert "product version skew" not in control.client.get("/fragments/fleet").text


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
    control.store.flag_zombie("acme/sandbox", 5, "r1", "worker-a", "box1")
    control.store.record_malformed("acme/sandbox", 9, "carries failed + plan_ready")
    control.store.record_chained_dependent("acme/sandbox", 12, 3, 7, "closed unmerged", "a" * 40)
    for run_id in ("r2", "r3"):
        control.node_post("/api/v1/events", run_event(6, "failed", run_id=run_id))
    page = control.client.get("/fragments/runs").text
    assert "zombie" in page and "awaiting swept evidence" in page
    assert "malformed" in page and "failed + plan_ready" in page
    assert "chained dependent" in page and "closed unmerged" in page
    assert "quarantined" in page and "consecutive failed Runs" in page


def test_dispatch_pause_is_visible_with_repo_reason_and_age(control: ControlRig):
    """A per-repo dispatch pause raises the Needs-attention section on its own
    (ADR-0056): the repo, the reason, and both ages render — escaped, since the
    reason is failure text — with no other flag present."""
    login(control)
    control.store.record_dispatch_pause("acme/sandbox", "GitHub 503 <listing failed>")
    control.clock.advance(120)  # the pause ages on the store clock
    page = control.client.get("/fragments/runs").text
    assert "Needs attention" in page  # a pause alone makes the section visible
    assert "dispatch paused" in page
    assert "acme/sandbox" in page
    assert "GitHub 503 &lt;listing failed&gt;" in page  # escaped, not interpreted
    assert "first seen" in page and "latest" in page  # both ages rendered


def test_janitor_ledger_renders_repo_keyed_and_node_scoped_rows(control: ControlRig):
    """The ledger fragment shows owner/name#N where a repo is present and
    only the reason (which names the node) otherwise (ADR-0056): a NULL-repo
    node act never renders a repo reference, and same-numbered issues in two
    repos stay distinguishable."""
    login(control)
    control.store.record_janitor_action("acme/sandbox", 5, "r1", "worker-a", "zombie escalated")
    control.store.record_janitor_action(
        None, 0, "", "", "node box1: quarantine released by operator"
    )
    page = control.client.get("/fragments/runs").text
    assert "acme/sandbox#5: zombie escalated" in page
    assert "node box1: quarantine released by operator" in page
    assert "#0" not in page  # the node act carries no repo reference


def test_two_bound_workspaces_render_distinct_linked_rows(control: ControlRig):
    """Two Bound Workspaces sharing an issue number render two Runs rows, each
    with its own owner/name#N issue link, per-row PR link, and per-row
    evidence link on the run id (ADR-0056)."""
    login(control)
    control.node_post(
        "/api/v1/events", run_event(7, "pr-open", run_id="rA", pr=11, repo="acme/app")
    )
    control.node_post("/api/v1/events", run_event(7, "claimed", run_id="rB", repo="acme/infra"))
    page = control.client.get("/fragments/runs").text
    assert "acme/app#7" in page and "acme/infra#7" in page
    assert "github.com/acme/app/issues/7" in page
    assert "github.com/acme/infra/issues/7" in page
    assert "github.com/acme/app/pull/11" in page  # per-row PR link
    # The run id links to that row's own repo evidence directory.
    assert "github.com/acme/app/tree/theozolith/evidence/runs/issue-7" in page


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


# -- the errors panel (2026-07-21 grilling) ---------------------------------------


def _error_event(node: str, component: str, message: str) -> dict:
    return {
        "type": "theozolith.error",
        "node": node,
        "component": component,
        "error_class": "RuntimeError",
        "message": message,
        "context": "stack trace tail",
    }


def test_errors_panel_lists_and_filters_by_node_and_component(control: ControlRig):
    login(control)
    control.node_post("/api/v1/events", _error_event("box1", "node-daemon", "image build failed"))
    control.node_post(
        "/api/v1/events",
        _error_event("box2", "implementer-driver", "evidence push failed"),
        node="box2",
    )

    page = control.client.get("/fragments/errors").text
    assert "image build failed" in page and "evidence push failed" in page
    assert "node-daemon@box1" in page

    filtered = control.client.get("/fragments/errors?node=box1").text
    assert "image build failed" in filtered
    assert "evidence push failed" not in filtered

    filtered = control.client.get("/fragments/errors?component=implementer-driver").text
    assert "evidence push failed" in filtered
    assert "image build failed" not in filtered


def test_errors_panel_escapes_untrusted_message_text(control: ControlRig):
    login(control)
    control.node_post(
        "/api/v1/events", _error_event("box1", "node-daemon", "<script>alert(1)</script>")
    )
    page = control.client.get("/fragments/errors").text
    assert "<script>alert(1)</script>" not in page  # escaped, not interpreted
    assert "alert(1)" in page


# -- the secret form (acceptance 5) ----------------------------------------------


def test_web_secret_reaches_a_referencing_node_like_the_cli(control: ControlRig):
    """Acceptance 5: the form writes through the same store as the API; a
    referencing node pulls the exact value; the UI never echoes it."""
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    login(control)
    response = control.client.post(
        "/secrets",
        data={"name": "github-implementer", "value": "ghp_secret_value"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    pulled = control.node_post(
        "/api/v1/secrets/pull", {"node": "box1", "names": ["github-implementer"]}
    )
    assert pulled.json() == {"secrets": {"github-implementer": "ghp_secret_value"}}

    page = control.client.get("/secrets").text
    assert "github-implementer" in page  # the name is listed…
    assert "ghp_secret_value" not in page  # …the value is never echoed


def test_secrets_form_recommends_the_implementer_secret_name(control: ControlRig):
    """The form's example copy teaches the shipped convention (ADR-0020 sweep):
    asserted with NO secrets entered, so the listing can't mask stale copy."""
    login(control)
    page = control.client.get("/secrets").text
    assert "github-implementer" in page
    assert "github-worker" not in page


def test_secret_form_refuses_without_the_tls_channel(tmp_path):
    rig = make_rig(tmp_path, secrets_channel_ok=False)
    rig.client.post("/login", data={"password": ADMIN_PASSWORD})
    response = rig.client.post("/secrets", data={"name": "n", "value": "v"})
    assert response.status_code == 403
    assert rig.secret_store.secret_names() == []


# -- the web terminal (acceptance 6) ---------------------------------------------


def test_attach_affordance_requires_config_and_live_container(control: ControlRig):
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    control.write_config("stacks/mutedeck.toml", MUTE_DECK_TOML)
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    login(control)
    control.heartbeat(
        node="box1",
        stacks=[
            {"name": "flightdeck", "kind": "container", "state": "running", "detail": ""},
            {"name": "mutedeck", "kind": "container", "state": "running", "detail": ""},
            {"name": "worker", "kind": "process", "state": "running", "detail": ""},
        ],
        run_containers=[
            {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up"},
        ],
        stack_containers=[
            {
                "name": FLIGHTDECK_CONTAINER,
                "stack": "flightdeck",
                "state": "running",
                "status": "Up",
            },
            {
                "name": "ozolith-stack-mutedeck",
                "stack": "mutedeck",
                "state": "running",
                "status": "Up",
            },
        ],
    )
    page = control.client.get("/fragments/fleet").text
    # The attach affordance exists exactly for the Stack with an attach
    # command; run containers never get one (ADR-0019).
    assert f"/terminal?node=box1&amp;container={FLIGHTDECK_CONTAINER}" in page
    assert "container=ozolith-stack-mutedeck" not in page
    assert "container=ozolith-run-r1" not in page

    # The no-attach Stack (derived from the container's stack — the URL
    # names no Stack at all) and a dead container both refuse the page.
    no_attach = control.client.get(
        "/terminal", params={"node": "box1", "container": "ozolith-stack-mutedeck"}
    )
    assert "exposes no terminal" in no_attach.text
    dead = control.client.get(
        "/terminal", params={"node": "box1", "container": "ozolith-stack-gone"}
    )
    assert "not live" in dead.text


def test_run_containers_are_never_attach_targets(control: ControlRig):
    """ADR-0019 acceptance: a live run container is refused categorically,
    and a process Stack cannot even declare an attach command."""
    control.write_config("stacks/worker.toml", WORKER_STACK_TOML)
    login(control)
    heartbeat_worker_node(control)
    page = control.client.get("/terminal", params={"node": "box1", "container": "ozolith-run-r1"})
    assert "never attach targets (ADR-0019)" in page.text
    assert _refused_ws(control, "/terminal/ws?node=box1&container=ozolith-run-r1") == 4404


def test_terminal_bridge_relays_io_and_audit_logs_the_session(control: ControlRig):
    """Acceptance 6: attach works (the Flight Deck is the target), the audit
    log records actor, timestamp, target (with the server-derived Stack),
    and the detach reason."""
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(control)

    with control.client.websocket_connect(
        f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}",
        headers=ADMIN_BEARER,
    ) as socket:
        received = b""
        while f"hello-{FLIGHTDECK_CONTAINER}".encode() not in received:
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
    assert attach["node"] == "box1" and attach["container"] == FLIGHTDECK_CONTAINER
    assert attach["stack"] == "flightdeck"  # derived from the live record
    assert attach["command"][0] == "sh" and "{container}" not in attach["command"]
    detach = next(line for line in lines if line["event"] == "detach")
    assert detach["container"] == FLIGHTDECK_CONTAINER
    assert detach["reason"] == "client-closed"


def test_terminal_websocket_rejects_unauthenticated_and_unconfigured(control: ControlRig):
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(control)
    # No credential at all.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        control.client.websocket_connect(
            f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}"
        ),
    ):
        pass
    assert refused.value.code == 4401
    # Authenticated, but the target container is not live.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        control.client.websocket_connect(
            "/terminal/ws?node=box1&container=ozolith-stack-gone",
            headers=ADMIN_BEARER,
        ),
    ):
        pass
    assert refused.value.code == 4404


def _refused_ws(rig: ControlRig, url: str, headers: dict | None = None) -> int:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        rig.client.websocket_connect(url, headers=headers or ADMIN_BEARER),
    ):
        pass
    return refused.value.code


# -- M5 target authorization (ADR-0019 acceptances 3-5) --------------------------


def test_terminal_stack_is_derived_from_the_live_owner(control: ControlRig):
    """Acceptance 4: the URL carries no Stack authority — an attach-enabled
    Stack cannot be used to reach a container owned by a Stack without
    attach configuration."""
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    control.write_config("stacks/mutedeck.toml", MUTE_DECK_TOML)
    control.heartbeat(
        node="box1",
        stack_containers=[
            {
                "name": "ozolith-stack-mutedeck",
                "stack": "mutedeck",
                "state": "running",
                "status": "",
            },
        ],
    )
    # A forged stack=flightdeck query param changes nothing: the owner is
    # mutedeck.
    code = _refused_ws(
        control, "/terminal/ws?node=box1&stack=flightdeck&container=ozolith-stack-mutedeck"
    )
    assert code == 4404


def test_terminal_refuses_stale_heartbeat_evidence(control: ControlRig):
    """Acceptance 5: attach demands fresh heartbeat evidence."""
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(control)
    control.clock.advance(151)  # past the ~2.5-missed-beats bound
    assert _refused_ws(control, f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}") == 4404
    page = control.client.get(
        "/terminal",
        params={"node": "box1", "container": FLIGHTDECK_CONTAINER},
        headers=ADMIN_BEARER,
    )
    assert "stale" in page.text

    heartbeat_flightdeck_node(control)  # fresh evidence again: attach works
    with control.client.websocket_connect(
        f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}", headers=ADMIN_BEARER
    ) as socket:
        assert b"hello" in socket.receive_bytes()


def test_terminal_refuses_wrong_node_and_unknown_owner(control: ControlRig):
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(control)
    # The container is live on box1, not box2.
    assert _refused_ws(control, f"/terminal/ws?node=box2&container={FLIGHTDECK_CONTAINER}") == 4404
    # A container whose stack is no configured Stack on the node.
    control.heartbeat(
        node="box1",
        stack_containers=[
            {"name": FLIGHTDECK_CONTAINER, "stack": "flightdeck", "state": "running", "status": ""},
            {"name": "ozolith-stack-x1", "stack": "ghost", "state": "running", "status": ""},
        ],
    )
    assert _refused_ws(control, "/terminal/ws?node=box1&container=ozolith-stack-x1") == 4404


def test_forged_heartbeat_identifiers_never_reach_the_command(control: ControlRig):
    """Acceptance 2-3: hostile container names from a forged heartbeat die
    in validation, before any process launch."""
    control.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    hostile = [
        "run;rm -rf /",
        "run$(reboot)",
        "run `x`",
        "-oProxyCommand=evil",
        "--privileged",
        "run x",
        "run\ttab",
    ]
    control.heartbeat(
        node="box1",
        stack_containers=[
            {"name": name, "stack": "flightdeck", "state": "running", "status": ""}
            for name in hostile
        ],
    )
    for name in hostile:
        params = {"node": "box1", "container": name}
        page = control.client.get("/terminal", params=params, headers=ADMIN_BEARER)
        assert "invalid container name" in page.text
    # Nothing was ever attached (no audit records, no processes).
    assert not control.settings.terminal_audit_path.exists()


# -- M5 browser-origin isolation (ADR-0019 acceptances 6-7) ----------------------


def test_session_cookie_is_host_locked(control: ControlRig):
    response = control.client.post(
        "/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False
    )
    header = response.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert SESSION_COOKIE == "__Host-ozolith_session"
    lowered = header.lower()
    for attribute in ("secure", "httponly", "path=/", "samesite=strict"):
        assert attribute in lowered
    assert "domain=" not in lowered


def test_insecure_dev_login_works_over_plain_http(tmp_path):
    """Over plain HTTP (--insecure-dev) the session uses the unprefixed,
    non-Secure cookie so a browser actually stores it and the dashboard
    authenticates — a __Host-/Secure cookie would be dropped and loop."""
    rig = make_rig(tmp_path, base_url="http://control.dev:8443", serve_tls=False)
    response = rig.client.post("/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    header = response.headers["set-cookie"].lower()
    assert header.startswith("ozolith_session=") and "secure" not in header
    # The stored cookie authenticates a follow-up request over the same scheme.
    assert rig.client.get("/", follow_redirects=False).status_code == 200


def test_cookie_state_changes_require_exact_host_and_origin(tmp_path):
    """The default-HTTPS control address yields Host ``<ip>`` and the same
    Origin — no port anywhere, whatever port Uvicorn binds (ADR-0034)."""
    rig = make_rig(tmp_path, base_url=NEUTRAL_BASE)
    guard = rig.client.app.state.browser_guard
    assert guard.expected_host == CANONICAL_HOST  # no :8443
    assert guard.expected_origin == CANONICAL_ORIGIN
    # The login form is browser-only: enforced from the first POST.
    assert rig.client.post("/login", data={"password": ADMIN_PASSWORD}).status_code == 403
    ok = rig.client.post(
        "/login",
        data={"password": ADMIN_PASSWORD},
        headers=CANONICAL_HEADERS,
        follow_redirects=False,
    )
    assert ok.status_code == 303

    for headers in (
        {"Host": CANONICAL_HOST},  # missing Origin
        {"Host": CANONICAL_HOST, "Origin": "https://evil.example"},  # wrong Origin
        {"Host": "testserver", "Origin": CANONICAL_ORIGIN},  # wrong Host
        # The retired host:serve-port coupling must NOT be accepted.
        {"Host": f"{CANONICAL_HOST}:8443", "Origin": f"{CANONICAL_ORIGIN}:8443"},
    ):
        refused = rig.client.post("/secrets", data={"name": "n", "value": "v"}, headers=headers)
        assert refused.status_code == 403
    assert rig.secret_store.secret_names() == []

    stored = rig.client.post(
        "/secrets",
        data={"name": "n", "value": "v"},
        headers=CANONICAL_HEADERS,
        follow_redirects=False,
    )
    assert stored.status_code == 303
    assert rig.secret_store.secret_names() == ["n"]


def test_nonstandard_public_port_is_enforced_exactly(tmp_path):
    """An explicit external control_port appears in exactly one accepted
    Host and Origin spelling."""
    rig = make_rig(tmp_path, base_url=NEUTRAL_BASE, control_port=9443)
    guard = rig.client.app.state.browser_guard
    assert guard.expected_host == f"{CANONICAL_HOST}:9443"
    assert guard.expected_origin == f"https://{CANONICAL_HOST}:9443"
    with_port = {
        "Host": f"{CANONICAL_HOST}:9443",
        "Origin": f"https://{CANONICAL_HOST}:9443",
    }
    ok = rig.client.post(
        "/login", data={"password": ADMIN_PASSWORD}, headers=with_port, follow_redirects=False
    )
    assert ok.status_code == 303
    for headers in (CANONICAL_HEADERS, {**with_port, "Host": CANONICAL_HOST}):
        refused = rig.client.post("/login", data={"password": ADMIN_PASSWORD}, headers=headers)
        assert refused.status_code == 403


def test_bearer_clients_work_without_origin(tmp_path):
    """Acceptance 7: non-browser callers keep working with no Origin."""
    rig = make_rig(tmp_path, base_url=NEUTRAL_BASE)
    assert rig.admin("PUT", "/api/v1/secrets/gh", body={"value": "v"}).status_code == 200
    form = rig.client.post(
        "/secrets", data={"name": "a", "value": "b"}, headers=ADMIN_BEARER, follow_redirects=False
    )
    assert form.status_code == 303


def test_cookie_websocket_requires_exact_origin(tmp_path):
    rig = make_rig(tmp_path, base_url=NEUTRAL_BASE)
    rig.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(rig)
    cookie = rig.client.app.state.admin_sessions.login(ADMIN_PASSWORD)
    with_cookie = {"Cookie": f"{SESSION_COOKIE}={cookie}"}

    target = f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}"
    code = _refused_ws(rig, target, with_cookie)
    assert code == 4403  # no Origin at all
    code = _refused_ws(
        rig,
        target,
        {**with_cookie, "Host": CANONICAL_HOST, "Origin": "https://evil.example"},
    )
    assert code == 4403

    with rig.client.websocket_connect(
        target,
        headers={**with_cookie, **CANONICAL_HEADERS},
    ) as socket:
        assert b"hello" in socket.receive_bytes()

    # Bearer websockets never need an Origin (non-browser clients).
    with rig.client.websocket_connect(target, headers=ADMIN_BEARER) as socket:
        assert b"hello" in socket.receive_bytes()


# -- M5 terminal session cap (ADR-0019 acceptance 12) ----------------------------


def test_terminal_session_cap_refuses_excess_without_launching(tmp_path):
    rig = make_rig(tmp_path, terminal_session_cap=1)
    rig.write_config("stacks/flightdeck.toml", FLIGHTDECK_STACK_TOML)
    heartbeat_flightdeck_node(rig)
    target = f"/terminal/ws?node=box1&container={FLIGHTDECK_CONTAINER}"
    with rig.client.websocket_connect(target, headers=ADMIN_BEARER) as first:
        assert b"hello" in first.receive_bytes()
        code = _refused_ws(rig, target)
        assert code == 4429

    lines = [json.loads(line) for line in rig.settings.terminal_audit_path.read_text().splitlines()]
    # Exactly one session ever attached: the refused one launched nothing.
    assert len([line for line in lines if line["event"] == "attach"]) == 1


# -- config distribution (ADR-0042) ---------------------------------------------


def test_config_dist_offhash_banner_and_stamp_skew_render(control: ControlRig):
    from theozolith_control import configdist

    control.write_config("drivers/custom/impl.py", "def run():\n    return 1\n")
    digest = configdist.dist_hash(control.settings.config_repo)
    login(control)
    # box1 off-hash (blocking, warning); box2 converged but stamp-skewed (advisory).
    control.heartbeat(node="box1", version="0.3.0", drivers_hash="d" * 64)
    control.heartbeat(
        node="box2", version="0.4.0", drivers_hash=digest, drivers_built_against="0.3.0"
    )
    page = control.client.get("/fragments/fleet").text
    assert "config-distribution skew" in page and "box1" in page
    assert "config-dist skew" in page  # the per-node badge
    assert "stamp skew" in page and "advisory" in page  # muted info line
    # Convergence clears the blocking banner (stamp skew is a separate fact).
    control.heartbeat(node="box1", version="0.3.0", drivers_hash=digest)
    assert "config-distribution skew" not in control.client.get("/fragments/fleet").text


def test_explicit_empty_report_renders_off_hash_not_healthy(control: ControlRig):
    """A current daemon that reports drivers_hash='' (no verified tree) is
    off-hash in the fleet worklist, never healthy; a heartbeat that OMITS the
    field is fail-open and never listed (ADR-0042)."""
    from theozolith_control.configrepo import load_config
    from theozolith_control.web.views import fleet_view

    control.write_config("drivers/custom/impl.py", "def run():\n    return 1\n")
    login(control)
    control.heartbeat(node="box1", version="0.3.0", drivers_hash="")  # explicit none
    control.heartbeat(node="box2", version="0.3.0")  # legacy omission
    view = fleet_view(control.store, load_config(control.settings.config_repo))
    # box1 is off-hash (explicit ''), box2 is fail-open (field absent).
    assert view["drivers_off_hash"] == ["box1"]
    banner = control.client.get("/fragments/fleet").text
    assert "config-distribution skew" in banner and "box1" in banner


def test_settings_form_write_path_is_retired(control: ControlRig):
    """ADR-0048: the pinned build has no second writer — the settings form is
    display-only, and ANY authorized POST (a legitimate key, a drivers-shaped
    key, anything) is refused with the ingest pointer and writes nothing."""
    login(control)
    for key in ("heartbeat_seconds", "drivers/custom/impl.py"):
        answer = control.client.post(
            "/settings",
            data={"key": key, "value": "30"},
            headers={"Origin": CONTROL_ORIGIN},
        )
        assert answer.status_code == 403
        assert "config ingest" in answer.text
    assert not (control.settings.config_repo / "drivers").exists()
    assert not (control.settings.config_repo / "control.toml").exists()


def test_web_secret_form_enforces_the_registry_shape_guard(control: ControlRig):
    """Form parity with PUT /api/v1/secrets (ADR-0049): a malformed registry
    credential is a 400 and stores nothing; a well-formed one is accepted."""
    login(control)
    bad = control.client.post(
        "/secrets",
        data={"name": "registry:ghcr.io", "value": "no-colon"},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert control.secret_store.secret_names() == []

    good = control.client.post(
        "/secrets",
        data={"name": "registry:ghcr.io", "value": "octocat:ghp_token"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert "registry:ghcr.io" in control.secret_store.secret_names()


def test_secret_write_surfaces_reject_an_unsafe_stored_name(control: ControlRig):
    """Both admin write surfaces refuse a stored name unsafe to materialize as a
    tmpfs leaf (#114): PUT /api/v1/secrets/{name} and the web form each 400 and
    store nothing — the shared validator, before the value reaches the store."""
    login(control)
    # The API path param — a leading-dot name is reserved (dotfile / temp
    # namespace) and never routes into the store.
    api = control.admin("PUT", "/api/v1/secrets/.hidden", body={"value": "v"})
    assert api.status_code == 400
    # The web form — a traversing name is refused the same way.
    form = control.client.post(
        "/secrets", data={"name": "../evil", "value": "v"}, follow_redirects=False
    )
    assert form.status_code == 400
    assert control.secret_store.secret_names() == []
