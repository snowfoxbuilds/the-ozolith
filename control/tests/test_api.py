"""The control-plane API: auth, heartbeats/commands, events, claim dispatch.

Includes the write-through dispatch contract (ADR-0017), the ADR-0016
dispatch gates (failed label, quarantine, pending lifecycle commands), and
the telemetry ingestion caps.
"""

from __future__ import annotations

from controlrig import ADMIN_TOKEN, ControlRig, make_rig, run_event

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
    # A per-node token is not the admin token.
    assert control.admin("GET", "/api/v1/state", token=control.node_token()).status_code == 401


def test_per_node_tokens_bind_identity(control: ControlRig):
    """ADR-0023: no node can speak as another — a declared node name that is
    not the token's node is refused on every node-channel shape."""
    control.provision_node("box2")
    imposter = control.heartbeat(node="box2", token=control.node_token("box1"))
    assert imposter.status_code == 403
    event = control.node_post(
        "/api/v1/events", run_event(5, "claimed", node="box2"), token=control.node_token("box1")
    )
    assert event.status_code == 403


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


def test_provisioning_is_registration_no_register_endpoint(control: ControlRig):
    """ADR-0023 supersedes register-on-first-heartbeat: the endpoint is gone,
    and an unknown token never creates a node record — it surfaces as an
    unregistered sighting instead."""
    gone = control.node_post("/api/v1/nodes/register", {"node": "box9", "version": "0.3.0"})
    assert gone.status_code == 404
    rejected = control.heartbeat(node="ghost", token="not-a-token")
    assert rejected.status_code == 401
    state = control.admin("GET", "/api/v1/state").json()
    assert "ghost" not in [n["name"] for n in state["nodes"]]
    assert [u["name"] for u in state["unregistered_nodes"]] == ["ghost"]


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
    assert first == [{"id": command_id, "verb": "drain", "target": "worker", "force": False}]
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


# -- claim dispatch (ADR-0017: write-through, single serialized writer) ------------


def test_dispatch_writes_the_claim_before_answering(control: ControlRig):
    """Acceptance 9: the issue is assigned and labeled in_progress by the
    Control Node itself; the grant response carries the issue."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    answer = control.dispatch().json()
    assert answer["issue"]["number"] == 7
    issue = control.github.get_issue(7)
    assert issue.assignees == ["ozolith-worker-a"]
    assert "in_progress" in issue.labels and "plan_ready" not in issue.labels
    # Write-through ordering: assign, label, then unqueue.
    assert [w[0] for w in control.github.writes] == [
        "add_assignees",
        "add_labels",
        "remove_label",
    ]


def test_dispatch_grants_each_issue_exactly_once(control: ControlRig):
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    assert control.dispatch(worker="worker-a").json()["issue"]["number"] == 7
    assert control.dispatch(worker="worker-b", node="box2").json()["issue"] is None


def test_dispatch_refuses_failed_plus_plan_ready_and_flags_it(control: ControlRig):
    """ADR-0016: failed overrides plan_ready — refused at dispatch, surfaced
    as a malformed state, never granted and never auto-stripped."""
    control.github.add_issue(7, labels={"plan_ready", "failed"}, assignees=[])
    answer = control.dispatch().json()
    assert answer["issue"] is None
    assert control.github.writes == []  # never laundered
    flags = control.admin("GET", "/api/v1/flags").json()
    assert flags["malformed_states"][0]["issue"] == 7
    # The human fixed the labels: the flag clears on the next pass.
    control.github.issues[7]["labels"] = {"plan_ready"}
    assert control.dispatch().json()["issue"]["number"] == 7
    assert control.admin("GET", "/api/v1/flags").json()["malformed_states"] == []


def test_dispatch_skips_issues_already_spoken_for_on_github(control: ControlRig):
    control.github.add_issue(7, labels={"plan_ready", "in_progress"}, assignees=[])
    control.github.add_issue(8, labels={"plan_ready"}, assignees=["someone"])
    assert control.dispatch().json()["issue"] is None


def test_quarantined_node_gets_no_work(control: ControlRig):
    """Acceptance 11: two consecutive failed Runs close the dispatch gate
    until a human releases it."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    for run_id in ("r1", "r2"):
        control.node_post("/api/v1/events", run_event(5, "failed", run_id=run_id))
    answer = control.dispatch(node="box1").json()
    assert answer["issue"] is None and "quarantined" in answer["reason"]
    # Another node still gets the work.
    assert control.dispatch(worker="worker-b", node="box2").json()["issue"]["number"] == 7
    # Human release reopens the gate (recycle is one of the two releases).
    control.github.issues[7]["labels"] = {"plan_ready"}
    control.github.issues[7]["assignees"] = []
    control.store.release_grant(7)
    control.admin("POST", "/api/v1/nodes/box1/quarantine/release")
    assert control.dispatch(node="box1").json()["issue"]["number"] == 7


def test_a_completed_run_resets_the_failure_counter(control: ControlRig):
    control.node_post("/api/v1/events", run_event(5, "failed", run_id="r1"))
    control.node_post("/api/v1/events", run_event(6, "pr-open", run_id="r2", pr=11))
    control.node_post("/api/v1/events", run_event(7, "failed", run_id="r3"))
    assert control.admin("GET", "/api/v1/flags").json()["quarantines"] == []


def test_recycle_command_releases_the_quarantine(control: ControlRig):
    for run_id in ("r1", "r2"):
        control.node_post("/api/v1/events", run_event(5, "failed", run_id=run_id))
    assert control.admin("GET", "/api/v1/flags").json()["quarantines"] != []
    control.admin("POST", "/api/v1/commands", {"node": "box1", "verb": "recycle"})
    assert control.admin("GET", "/api/v1/flags").json()["quarantines"] == []


def test_pending_lifecycle_command_pauses_grants_to_that_node(control: ControlRig):
    """Queue-behind: a node about to recycle gets no new work, which bounds
    the deferral by the current Run (NODE-SUBSTRATE)."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    queued = control.admin("POST", "/api/v1/commands", {"node": "box1", "verb": "recycle"})
    answer = control.dispatch(node="box1").json()
    assert answer["issue"] is None and "recycle" in answer["reason"]
    # Acknowledged: grants resume.
    control.heartbeat(completed_commands=[queued.json()["id"]])
    assert control.dispatch(node="box1").json()["issue"]["number"] == 7


def test_pin_bump_pauses_dispatch_fleet_wide_until_nodes_converge(control: ControlRig):
    """ADR-0015 revision acceptance 2: dispatch grants only to nodes whose
    REPORTED version equals the pin — issuing an update pauses new dispatch
    fleet-wide; capacity returns node by node as versions converge."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    for node in ("box1", "box2"):
        control.heartbeat(node=node, version="0.3.0")

    refused = control.dispatch(worker="worker-a", node="box1").json()
    assert refused["issue"] is None and "pin is 0.4.0" in refused["reason"]
    assert control.dispatch(worker="worker-b", node="box2").json()["issue"] is None
    # Reviewer discovery pauses on the same gate.
    review = control.node_post(
        "/api/v1/dispatch",
        {"role": "reviewer", "worker": "rev-1", "node": "box1", "login": "ozolith-rev"},
    ).json()
    assert review["prs"] == [] and "pin is 0.4.0" in review["reason"]

    # box1 converges: capacity returns for it alone.
    control.heartbeat(node="box1", version="0.4.0")
    assert control.dispatch(worker="worker-a", node="box1").json()["issue"]["number"] == 7
    assert control.dispatch(worker="worker-b", node="box2").json()["issue"] is None


def test_unreported_node_versions_stay_dispatch_eligible(control: ControlRig):
    """The daemon-less dev shape heartbeats no version at all: the gate is
    keyed on the reported version, and no report means no block."""
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    control.github.add_issue(9, labels={"plan_ready"}, assignees=[])
    assert control.dispatch(worker="worker-a", node="ghost").json()["issue"]["number"] == 9


def test_persistently_offpin_node_gets_restart_then_error_and_stays_ineligible(
    control: ControlRig,
):
    """ADR-0015 revision acceptance 3: threshold consecutive off-pin beats
    queue one restart; as many again, one theozolith.error — and the node
    stays ineligible until it actually converges."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')

    for _ in range(2):
        assert control.heartbeat(node="box1", version="0.3.0").json()["commands"] == []
    beat = control.heartbeat(node="box1", version="0.3.0").json()  # third off-pin beat
    assert [c["verb"] for c in beat["commands"]] == ["restart"]
    restart_id = beat["commands"][0]["id"]

    # The restart is applied but the node STAYS off-pin: after as many
    # beats again, the escalation event lands — exactly once.
    for _ in range(3):
        control.heartbeat(node="box1", version="0.3.0", completed_commands=[restart_id])
    errors = control.store.error_events(component="control-node")
    assert len(errors) == 1
    assert errors[0]["payload"]["error_class"] == "update-not-converging"
    assert "box1" in errors[0]["payload"]["message"]
    # No second restart is ever queued; the node stays ineligible.
    state = control.admin("GET", "/api/v1/state").json()
    assert [c["verb"] for c in state["commands"] if c["verb"] == "restart"] == ["restart"]
    assert control.dispatch(node="box1").json()["issue"] is None

    # Actual convergence clears the tracking and reopens dispatch.
    control.heartbeat(node="box1", version="0.4.0")
    assert control.dispatch(node="box1").json()["issue"]["number"] == 7


def test_dispatch_reviewer_side_is_discovery_only(control: ControlRig):
    control.github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})
    control.github.add_pr(12, head_ref="ozolith/issue-6", labels={"pr_ready", "needs_human"})
    control.github.add_pr(13, head_ref="ozolith/issue-7", labels={"pr_ready", "blocked"})
    answer = control.dispatch(role="reviewer", worker="reviewer-1").json()
    assert answer == {"prs": [11]}
    assert control.github.writes == []  # no claim label exists on PRs


def test_dispatch_without_a_control_pat_answers_503(tmp_path):
    """ADR-0017: no second claim path — an unconfigured Control Node pauses
    the pipeline rather than falling back."""
    rig = make_rig(tmp_path, github_token=None)
    assert rig.dispatch().status_code == 503


def test_dispatch_requires_the_node_token(control: ControlRig):
    body = {"role": "worker", "worker": "w", "node": "n", "login": "l"}
    assert control.node_post("/api/v1/dispatch", body, token="wrong").status_code == 401


def test_dispatch_registers_the_driver(control: ControlRig):
    control.dispatch(worker="worker-a", node="box1")
    drivers = control.store.drivers()
    assert drivers[0]["worker"] == "worker-a"
    assert drivers[0]["login"] == "ozolith-worker-a"
    assert drivers[0]["role"] == "worker"


# -- grant activation (ADR-0017: never-activated grants are released) --------------


def test_claimed_event_activates_and_retires_the_grant(control: ControlRig):
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.dispatch()
    assert control.store.granted_issues() == {7}
    control.node_post("/api/v1/events", run_event(7, "claimed", run_id="r1"))
    assert control.store.granted_issues() == set()


# -- telemetry ingestion caps (ADR-0016) --------------------------------------------


def test_progress_transcript_tail_is_truncated_at_ingestion(control: ControlRig):
    event = {
        "type": "theozolith.run.progress",
        "worker": "worker-a",
        "issue": 5,
        "run_id": "r1",
        "phase": "agent",
        "transcript_tail": "x" * 50_000,
    }
    assert control.node_post("/api/v1/events", event).status_code == 200
    stored = control.store.events(type="theozolith.run.progress")
    assert len(stored[0]["transcript_tail"]) == 8_192


def test_oversized_event_payloads_are_refused(control: ControlRig):
    event = {"type": "acme.blob", "data": "y" * 50_000}
    assert control.node_post("/api/v1/events", event).status_code == 413
    assert control.store.events(type="acme.blob") == []


def test_error_event_context_is_truncated_at_ingestion_never_refused(control: ControlRig):
    """2026-07-21 grilling: an oversized theozolith.error is truncated, not
    dropped — an error summary must not vanish for being verbose."""
    event = {
        "type": "theozolith.error",
        "node": "box1",
        "component": "node-daemon",
        "error_class": "RuntimeError",
        "message": "m" * 10_000,
        "context": "TAIL-MATTERS-" + "c" * 50_000,
    }
    assert control.node_post("/api/v1/events", event).status_code == 200
    (stored,) = control.store.events(type="theozolith.error")
    assert len(stored["context"]) == 8_192
    assert stored["context"].endswith("c")  # tail kept (innermost frames)
    assert len(stored["message"]) == 2_048


def test_error_events_are_queryable_by_node_and_component(control: ControlRig):
    for node, component in (("box1", "node-daemon"), ("box2", "implementer-driver")):
        event = {
            "type": "theozolith.error",
            "node": node,
            "component": component,
            "error_class": "EngineError",
            "message": f"boom on {node}",
        }
        assert control.node_post("/api/v1/events", event, node=node).status_code == 200
    rows = control.store.error_events(node="box1")
    assert [r["component"] for r in rows] == ["node-daemon"]
    rows = control.store.error_events(component="implementer-driver")
    assert [r["node"] for r in rows] == ["box2"]
    assert control.store.error_filters() == (
        ["box1", "box2"],
        ["implementer-driver", "node-daemon"],
    )


def test_heartbeat_reports_command_deferrals(control: ControlRig):
    """Acceptance 12: the queue-behind deferral is visible in fleet state."""
    queued = control.admin("POST", "/api/v1/commands", {"node": "box1", "verb": "recycle"})
    command_id = queued.json()["id"]
    control.heartbeat(
        deferred_commands=[{"id": command_id, "reason": "behind run r1 (stack worker)"}]
    )
    commands = control.admin("GET", "/api/v1/state").json()["commands"]
    assert commands[0]["deferred_reason"] == "behind run r1 (stack worker)"
    # The Run ended and the command executed: the deferral clears.
    control.heartbeat(completed_commands=[command_id])
    commands = control.admin("GET", "/api/v1/state").json()["commands"]
    assert commands[0]["deferred_reason"] is None


# -- the two update paths, one machinery (ADR-0015, 2026-07-22) -----------------------


CONTROL_STACK_TOML = 'kind = "container"\nnode = "boxctl"\nimage = "theozolith-control:pinned"\n'


def test_product_update_pins_and_fans_out_control_host_last(control: ControlRig):
    control.write_config("stacks/control.toml", CONTROL_STACK_TOML)
    # Three registered nodes; boxctl hosts the control Stack.
    for node in ("boxctl", "box1", "box2"):
        control.heartbeat(node=node)

    answer = control.admin("POST", "/api/v1/product/update", {"version": "0.4.0"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["version"] == "0.4.0"
    # Fan-out to every node; the control host is queued LAST (it applies
    # its own update only after the fan-out is queued).
    assert body["queued"] == ["box1", "box2", "boxctl"]

    # The pin landed in the Config Repo…
    pin = (control.settings.config_repo / "product.toml").read_text()
    assert 'version = "0.4.0"' in pin
    # …and every node's next heartbeat delivers both the command and the pin.
    for node in ("box1", "box2", "boxctl"):
        beat = control.heartbeat(node=node).json()
        assert [c["verb"] for c in beat["commands"]] == ["update"]
        assert beat["config"]["product_version"] == "0.4.0"
        # A release pin with no served artifacts carries no artifact refs.
        assert "product_artifacts" not in beat["config"]

    # Rollback: re-running with a previous version re-pins and redeploys.
    assert control.admin("POST", "/api/v1/product/update", {"version": "0.3.0"}).status_code == 200
    assert 'version = "0.3.0"' in (control.settings.config_repo / "product.toml").read_text()


def test_product_update_requires_admin_and_a_version(control: ControlRig):
    refused = control.node_post("/api/v1/product/update", {"version": "0.4.0"})
    assert refused.status_code == 401
    empty = control.admin("POST", "/api/v1/product/update", {"version": ""})
    assert empty.status_code == 400


def test_artifact_upload_serve_roundtrip_and_heartbeat_references(control: ControlRig):
    version = "0.3.0+gabc123def456"
    wheel = "theozolith_worker-0.3.0+gabc123def456-py3-none-any.whl"
    upload = control.client.put(
        f"/api/v1/product/artifacts/{version}/{wheel}",
        content=b"wheel-bytes",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert upload.status_code == 200 and upload.json()["bytes"] == 11

    # Nodes pull with the node token; the payload is byte-exact.
    pulled = control.client.get(
        f"/api/v1/product/artifacts/{version}/{wheel}",
        headers={"Authorization": f"Bearer {control.node_token()}"},
    )
    assert pulled.status_code == 200 and pulled.content == b"wheel-bytes"

    # Pinning that version makes heartbeats carry the artifact REFERENCES
    # (filenames only — the channel invariant holds).
    assert control.admin("POST", "/api/v1/product/update", {"version": version}).status_code == 200
    beat = control.heartbeat(node="box1").json()
    assert beat["config"]["product_version"] == version
    assert beat["config"]["product_artifacts"] == [wheel]


def test_pin_changes_prune_the_artifact_store_to_pinned_plus_previous(control: ControlRig):
    """Cache, not archive: at most the pinned and the previous version's
    artifact sets survive a pin change (the previous is the rollback path)."""
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    for version in ("0.3.0+gaaa111", "0.3.0+gbbb222", "0.3.0+gccc333"):
        upload = control.client.put(
            f"/api/v1/product/artifacts/{version}/x.whl", content=b"w", headers=headers
        )
        assert upload.status_code == 200
        pinned = control.admin("POST", "/api/v1/product/update", {"version": version})
        assert pinned.status_code == 200

    kept = sorted(p.name for p in control.settings.artifacts_dir.iterdir())
    assert kept == ["0.3.0+gbbb222", "0.3.0+gccc333"]  # the oldest set is gone


def test_artifact_endpoints_refuse_unsafe_segments_and_unknown_files(control: ControlRig):
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    # A `..` segment dies in URL normalization (route miss) or in the
    # handler's segment check — refused either way, never written.
    for version, name in (("..", "a.whl"), ("0.3.0", "..whl"), ("0.3.0", "not-a-wheel.txt")):
        refused = control.client.put(
            f"/api/v1/product/artifacts/{version}/{name}", content=b"x", headers=headers
        )
        assert refused.status_code in (400, 404), (version, name)
    assert not control.settings.artifacts_dir.exists()  # nothing landed anywhere
    missing = control.client.get(
        "/api/v1/product/artifacts/0.9.9/ghost.whl",
        headers={"Authorization": f"Bearer {control.node_token()}"},
    )
    assert missing.status_code == 404


# -- deletion test (acceptance 8, control half) ---------------------------------------------


def test_boots_and_serves_with_no_config_repo_at_all(control: ControlRig):
    """No private Config Repo: every node's desired state is empty, nothing
    errors — the product boots from docker + package + init output with
    every tier-2 tunable at its shipped default (ADR-0004 as restated by
    ADR-0023; `.env` is no longer a surface). The shipped cadence defaults
    ride desired state."""
    answer = control.heartbeat().json()
    assert answer["config"] == {
        "commit": "",
        "product_version": "",
        "stacks": [],
        "images": [],
        "heartbeat_seconds": 60.0,
        "stop_grace_seconds": 30.0,
    }
