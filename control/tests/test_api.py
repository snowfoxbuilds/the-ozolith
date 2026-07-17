"""The control-plane API: auth, heartbeats/commands, events, claim intents.

Includes the control-side halves of the pre-filter test (acceptance 4) and
the build-skew test (acceptance 6).
"""

from __future__ import annotations

from controlrig import ControlRig, run_event

STACK_TOML = """\
kind = "process"
node = "box1"
command = "theozolith-worker"
run_image = "claude-dev"

[env]
THEOZOLITH_REPO = "acme/sandbox"

[secrets]
WORKER_GITHUB_TOKEN = "github-worker"
"""

IMAGE_TOML = """\
base = "ghcr.io/x/run:1.0@sha256:{digest}"
setup = ["pip install uv"]
""".format(digest="0" * 64)


# -- auth --------------------------------------------------------------------------


def test_node_endpoints_reject_bad_tokens(control: ControlRig):
    assert control.heartbeat().status_code == 200
    assert control.node_post("/api/v1/heartbeats", {"node": "x"}, token="wrong").status_code == 401
    assert control.client.post("/api/v1/heartbeats", json={"node": "x"}).status_code == 401
    # The node token is not the admin token.
    assert control.admin("GET", "/api/v1/state", token="node-token").status_code == 401


# -- heartbeats: status in, commands + desired state out -----------------------------


def test_heartbeat_records_status_and_registers_the_node(control: ControlRig):
    response = control.heartbeat(
        stacks=[{"name": "worker", "kind": "process", "state": "running", "detail": "pid 7"}],
        run_containers=[
            {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up"}
        ],
        images=[
            {
                "name": "claude-dev",
                "tag": "t",
                "base_digest": "d",
                "instruction_hash": "h",
                "built_at": "now",
            }
        ],
    )
    assert response.status_code == 200
    state = control.admin("GET", "/api/v1/state").json()
    assert [n["name"] for n in state["nodes"]] == ["box1"]
    assert state["stacks"][0]["state"] == "running"
    assert state["run_containers"][0]["run_id"] == "r1"
    assert state["images"][0]["instruction_hash"] == "h"


def test_register_endpoint_is_idempotent(control: ControlRig):
    for _ in range(2):
        assert (
            control.node_post(
                "/api/v1/nodes/register", {"node": "box1", "version": "0.3.0"}
            ).status_code
            == 200
        )


def test_heartbeat_distributes_only_this_nodes_desired_state(control: ControlRig):
    control.write_config("stacks/worker.toml", STACK_TOML)
    control.write_config("stacks/elsewhere.toml", STACK_TOML.replace("box1", "box2"))
    control.write_config("images/claude-dev.toml", IMAGE_TOML)

    config = control.heartbeat(node="box1").json()["config"]
    assert [s["name"] for s in config["stacks"]] == ["worker"]
    assert config["stacks"][0]["secrets"] == {"WORKER_GITHUB_TOKEN": "github-worker"}
    # Only images referenced by this node's Stacks travel with it.
    assert [i["name"] for i in config["images"]] == ["claude-dev"]
    assert config["images"][0]["tag"].startswith("theozolith/claude-dev:1.0-")
    assert config["commit"]

    other = control.heartbeat(node="box3").json()["config"]
    assert other["stacks"] == [] and other["images"] == []


def test_commands_deliver_until_acknowledged(control: ControlRig):
    queued = control.admin(
        "POST", "/api/v1/commands", {"node": "box1", "verb": "drain", "target": "worker"}
    )
    command_id = queued.json()["id"]

    first = control.heartbeat().json()["commands"]
    assert first == [{"id": command_id, "verb": "drain", "target": "worker"}]
    # Unacknowledged: re-delivered.
    assert control.heartbeat().json()["commands"] == first
    # Acknowledged: gone.
    assert control.heartbeat(completed_commands=[command_id]).json()["commands"] == []
    state = control.admin("GET", "/api/v1/state").json()
    assert state["commands"][0]["completed_at"] is not None


def test_command_verbs_are_validated(control: ControlRig):
    bad = control.admin("POST", "/api/v1/commands", {"node": "box1", "verb": "explode"})
    assert bad.status_code == 400
    for verb in ("drain", "recycle", "update", "rebuild"):
        ok = control.admin("POST", "/api/v1/commands", {"node": "box1", "verb": verb})
        assert ok.status_code == 200


def test_build_skew_between_nodes_is_visible_in_state(control: ControlRig):
    """Acceptance 6: two nodes, same image name, different instruction
    hashes — the skew is a string comparison away in Control Node state."""
    for node, digest in (("box1", "hash-aaa"), ("box2", "hash-bbb")):
        control.heartbeat(
            node=node,
            images=[
                {
                    "name": "claude-dev",
                    "tag": f"t-{digest}",
                    "base_digest": "d",
                    "instruction_hash": digest,
                    "built_at": "now",
                }
            ],
        )
    images = control.admin("GET", "/api/v1/state").json()["images"]
    by_node = {i["node"]: i["instruction_hash"] for i in images if i["name"] == "claude-dev"}
    assert by_node == {"box1": "hash-aaa", "box2": "hash-bbb"}


# -- typed events (extension point) ----------------------------------------------------


def test_run_and_review_events_are_stored_typed(control: ControlRig):
    assert control.node_post("/api/v1/events", run_event(5, "claimed")).status_code == 200
    stored = control.store.events(type="theozolith.run", issue=5)
    assert stored[0]["phase"] == "claimed" and stored[0]["worker"] == "worker-a"


def test_unknown_event_types_are_accepted_and_stored(control: ControlRig):
    event = {"type": "acme.backup", "volume": "media", "ok": True}
    assert control.node_post("/api/v1/events", event).status_code == 200
    stored = control.store.events(type="acme.backup")
    assert stored[0]["volume"] == "media" and stored[0]["ok"] is True


def test_events_require_a_type(control: ControlRig):
    assert control.node_post("/api/v1/events", {"phase": "claimed"}).status_code == 400


# -- claim intents (acceptance 4, control half) -------------------------------------------


def test_claim_intents_serialize_two_workers(control: ControlRig):
    first = control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-a"}).json()
    second = control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-b"}).json()
    assert first == {"allow": True, "holder": "worker-a"}
    assert second == {"allow": False, "holder": "worker-a"}
    # The holder may re-ask (its own Claim Protocol retries).
    again = control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-a"}).json()
    assert again["allow"] is True


def test_claim_intents_expire_after_the_ttl(control: ControlRig):
    control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-a"})
    control.clock.advance(121)  # past THEOZOLITH_CLAIM_TTL_SECONDS
    late = control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-b"}).json()
    assert late["allow"] is True


def test_different_issues_do_not_contend(control: ControlRig):
    control.node_post("/api/v1/claim-intents", {"issue": 7, "worker": "worker-a"})
    other = control.node_post("/api/v1/claim-intents", {"issue": 8, "worker": "worker-b"}).json()
    assert other["allow"] is True


# -- deletion test (acceptance 8, control half) ---------------------------------------------


def test_boots_and_serves_with_no_config_repo_at_all(control: ControlRig):
    """No private Config Repo: every node's desired state is empty, nothing
    errors — the product boots from package + .env (ADR-0004)."""
    answer = control.heartbeat().json()
    assert answer["config"] == {"commit": "", "product_version": "", "stacks": [], "images": []}
