"""The control-plane API: auth, heartbeats/commands, events, claim dispatch.

Includes the write-through dispatch contract (ADR-0017), the ADR-0016
dispatch gates (failed label, quarantine, pending lifecycle commands), and
the telemetry ingestion caps.
"""

from __future__ import annotations

import threading

from controlrig import ADMIN_TOKEN, ControlRig, make_rig, run_event

# A driver worker type + the thin Stack that names it (ADR-0044). The
# resolved Stack is an ordinary process Stack: command/env/secrets come from
# the worker type, and its derived-image recipe rides in the wire `images`.
WORKER_TYPE_TOML = """\
driver = "builtin:implementer"
adapter = "claude"
model = "claude-sonnet-5"
workspace = "acme/sandbox"
base = "ghcr.io/x/run:1.0@sha256:{digest}"
setup = ["pip install uv"]

[secrets]
IMPLEMENTER_GITHUB_TOKEN = "github-implementer"
""".format(digest="0" * 64)

STACK_TOML = """\
worker_type = "claude-dev"
node = "box1"
"""


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
    control.write_config("worker-types/claude-dev.toml", WORKER_TYPE_TOML)
    control.write_config("stacks/worker.toml", STACK_TOML)
    control.write_config("stacks/elsewhere.toml", STACK_TOML.replace("box1", "box2"))

    config = control.heartbeat(node="box1").json()["config"]
    assert [s["name"] for s in config["stacks"]] == ["worker"]
    assert config["stacks"][0]["secrets"] == {"IMPLEMENTER_GITHUB_TOKEN": "github-implementer"}
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
    assert stored[0]["phase"] == "claimed" and stored[0]["driver"] == "worker-a"


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


# -- dependency-aware dispatch (ADR-0053) ------------------------------------------


def test_dispatch_grants_the_blocker_and_holds_the_dependent_with_a_visible_wait(
    control: ControlRig,
):
    """The earlier-created blocker is granted; the dependent behind its
    (still unreviewed) claim waits, visibly, and is never granted."""
    control.github.add_issue(1, labels={"plan_ready"}, assignees=[])
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)

    assert control.dispatch(worker="worker-a").json()["issue"]["number"] == 1
    answer = control.dispatch(worker="worker-b", node="box2").json()
    assert answer["issue"] is None

    waits = control.admin("GET", "/api/v1/flags").json()["dispatch_waits"]
    assert [w["issue"] for w in waits] == [2]
    assert "#1" in waits[0]["reason"]


def test_dispatch_grants_a_dependent_whose_blocker_closed_completed(control: ControlRig):
    control.github.add_issue(
        1, labels=set(), assignees=[], state="closed", state_reason="completed"
    )
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)
    assert control.dispatch().json()["issue"]["number"] == 2


def test_dispatch_flags_a_not_planned_blocker_as_malformed_until_a_human_fixes_it(
    control: ControlRig,
):
    """Only a completed close satisfies an edge: not_planned surfaces as
    malformed (the failed+plan_ready precedent), never granted, and clears
    on the pass after the graph is fixed."""
    control.github.add_issue(
        1, labels=set(), assignees=[], state="closed", state_reason="not_planned"
    )
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)

    assert control.dispatch().json()["issue"] is None
    assert control.github.writes == []  # never laundered
    malformed = control.admin("GET", "/api/v1/flags").json()["malformed_states"]
    assert [m["issue"] for m in malformed] == [2]
    assert "#1" in malformed[0]["detail"] and "not_planned" in malformed[0]["detail"]

    # The human re-closed the blocker as completed: granted, flag cleared.
    control.github.issues[1]["state_reason"] = "completed"
    assert control.dispatch().json()["issue"]["number"] == 2
    assert control.admin("GET", "/api/v1/flags").json()["malformed_states"] == []


def test_dispatch_flags_a_dependency_cycle_and_never_grants_it(control: ControlRig):
    control.github.add_issue(1, labels={"plan_ready"}, assignees=[])
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(1, 2)
    control.github.add_blocked_by(2, 1)

    assert control.dispatch().json()["issue"] is None
    assert control.github.writes == []
    malformed = control.admin("GET", "/api/v1/flags").json()["malformed_states"]
    assert [m["issue"] for m in malformed] == [1, 2]
    assert all("cycle" in m["detail"] for m in malformed)


def test_dispatch_flags_a_cross_repo_edge_as_malformed(control: ControlRig):
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 77, repo="acme/elsewhere")
    assert control.dispatch().json()["issue"] is None
    malformed = control.admin("GET", "/api/v1/flags").json()["malformed_states"]
    assert [m["issue"] for m in malformed] == [2]
    assert "acme/elsewhere" in malformed[0]["detail"]


def test_dispatch_grants_a_chained_dependent_with_ordinary_claim_writes(control: ControlRig):
    """The Chained Base go-ahead (ADR-0053): blocker claimed and approved
    (pr_ready + needs_human, no blocked) — the dependent is granted, and
    the claim write sequence is byte-identical to an ordinary grant."""
    control.github.add_issue(1, labels={"in_progress"}, assignees=["ozolith-worker-z"])
    control.github.add_pr(11, head_ref="ozolith/issue-1", labels={"pr_ready", "needs_human"})
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)

    answer = control.dispatch().json()
    assert answer["issue"]["number"] == 2
    assert control.github.writes == [
        ("add_assignees", 2, "ozolith-worker-a"),
        ("add_labels", 2, "in_progress"),
        ("remove_label", 2, "plan_ready"),
    ]
    # The go-ahead cleared the dependent's wait row (if any pass recorded one).
    assert control.admin("GET", "/api/v1/flags").json()["dispatch_waits"] == []


def test_dispatch_waits_on_a_blocked_blocker_pr(control: ControlRig):
    control.github.add_issue(1, labels={"in_progress"}, assignees=["ozolith-worker-z"])
    control.github.add_pr(
        11, head_ref="ozolith/issue-1", labels={"pr_ready", "needs_human", "blocked"}
    )
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)
    assert control.dispatch().json()["issue"] is None
    waits = control.admin("GET", "/api/v1/flags").json()["dispatch_waits"]
    assert [w["issue"] for w in waits] == [2]


def test_dispatch_waits_on_fan_in_blocker_lines(control: ControlRig):
    for blocker in (1, 3):
        control.github.add_issue(blocker, labels={"in_progress"}, assignees=["ozolith-worker-z"])
        control.github.add_pr(
            10 + blocker,
            head_ref=f"ozolith/issue-{blocker}",
            labels={"pr_ready", "needs_human"},
        )
        control.github.add_blocked_by(2, blocker)
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    assert control.dispatch().json()["issue"] is None
    waits = control.admin("GET", "/api/v1/flags").json()["dispatch_waits"]
    assert "parallel open blocker lines" in waits[0]["reason"]


def test_squash_enabled_settings_turn_chaining_off_with_a_visible_reason(control: ControlRig):
    from theozolith_worker.githubapi import RepoMergeSettings

    control.github.merge_settings = RepoMergeSettings(
        merge_commit_allowed=True,
        squash_allowed=True,
        rebase_allowed=False,
        delete_branch_on_merge=True,
        complete=True,
    )
    control.github.add_issue(1, labels={"in_progress"}, assignees=["ozolith-worker-z"])
    control.github.add_pr(11, head_ref="ozolith/issue-1", labels={"pr_ready", "needs_human"})
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)

    assert control.dispatch().json()["issue"] is None
    waits = control.admin("GET", "/api/v1/flags").json()["dispatch_waits"]
    assert [w["issue"] for w in waits] == [2]
    assert "chaining off" in waits[0]["reason"]
    assert "squash merge enabled" in waits[0]["reason"]

    # The operator fixed the repo settings: granted, wait row cleared.
    control.github.merge_settings = RepoMergeSettings(
        merge_commit_allowed=True,
        squash_allowed=False,
        rebase_allowed=False,
        delete_branch_on_merge=True,
        complete=True,
    )
    assert control.dispatch().json()["issue"]["number"] == 2
    assert control.admin("GET", "/api/v1/flags").json()["dispatch_waits"] == []


def test_wait_and_malformed_lanes_reconcile_each_other(control: ControlRig):
    """An issue is waiting OR malformed, never both: a waiting dependent
    whose blocker gets closed not_planned moves lanes cleanly, and moves
    back when the human repairs the graph."""
    control.github.add_issue(1, labels=set(), assignees=[])
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)

    control.dispatch()  # open blocker, no PR -> wait
    flags = control.admin("GET", "/api/v1/flags").json()
    assert [w["issue"] for w in flags["dispatch_waits"]] == [2]

    control.github.issues[1].update(state="closed", state_reason="not_planned")
    control.dispatch()  # -> malformed; the stale wait row goes with it
    flags = control.admin("GET", "/api/v1/flags").json()
    assert flags["dispatch_waits"] == []
    assert [m["issue"] for m in flags["malformed_states"]] == [2]

    control.github.issues[1].update(state="open", state_reason="")
    control.dispatch()  # reopened blocker -> back to a healthy wait
    flags = control.admin("GET", "/api/v1/flags").json()
    assert [w["issue"] for w in flags["dispatch_waits"]] == [2]
    assert flags["malformed_states"] == []


def test_advisory_rows_clear_when_the_issue_leaves_the_pool(control: ControlRig):
    """Wait/malformed rows describe the plan_ready pool: an issue that
    departs it (closed, label stripped, claimed) takes its advisory rows
    with it on the next pass — a departed issue must not show as
    'waiting' on the dashboard forever."""
    control.github.add_issue(1, labels=set(), assignees=[])
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.github.add_blocked_by(2, 1)
    control.dispatch()
    flags = control.admin("GET", "/api/v1/flags").json()
    assert [w["issue"] for w in flags["dispatch_waits"]] == [2]

    # The human strips plan_ready (or closes the issue): the row departs.
    control.github.issues[2]["labels"] = set()
    control.dispatch()
    assert control.admin("GET", "/api/v1/flags").json()["dispatch_waits"] == []


def test_review_targets_passes_the_creation_order_through(control: ControlRig):
    """Oldest-first Reviewer discovery is load-bearing for chains
    (ADR-0053): the client sorts created-asc and dispatch must not
    reorder."""
    for number in (11, 12, 13):
        control.github.add_pr(number, head_ref=f"ozolith/issue-{number}", labels={"pr_ready"})
    answer = control.dispatch(role="reviewer", worker="reviewer-1").json()
    assert answer == {"prs": [11, 12, 13]}


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
        {"role": "reviewer", "driver": "rev-1", "node": "box1", "login": "ozolith-rev"},
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
    stays ineligible until it actually converges. These heartbeats OMIT
    update_deferred, so this is also the legacy-daemon coverage (issue #8):
    field absence keeps exactly the pre-deferral ladder behavior."""
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


def test_deferred_update_behind_a_run_never_restarts_or_escalates(control: ControlRig):
    """Issue #8: a node off-pin solely because its update is queued behind
    an in-flight Run trips nothing — no restart command, no error event —
    for arbitrarily many beats; it converges right after the Run ends."""
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    # Far past 2x the threshold in deferral beats: the ladder never moves.
    for _ in range(8):
        beat = control.heartbeat(
            node="box1", version="0.3.0", update_deferred="behind run r1 (stack worker)"
        )
        assert beat.json()["commands"] == []
    assert control.store.error_events(component="control-node") == []
    # Off-pin still pauses dispatch to the node meanwhile — that gate is
    # keyed on the reported version, not on the ladder.
    assert control.dispatch(node="box1").json()["issue"] is None
    # The Run ended, the update applied: dispatch reopens, nothing queued.
    control.heartbeat(node="box1", version="0.4.0")
    assert control.dispatch(node="box1").json()["issue"]["number"] == 7
    state = control.admin("GET", "/api/v1/state").json()
    assert [c["verb"] for c in state["commands"] if c["verb"] == "restart"] == []


def test_deferral_resets_the_climb_and_counting_resumes_when_it_clears(control: ControlRig):
    """Issue #8 acceptance 3: a deferral beat restores full patience; once
    the deferral clears, a genuinely stuck node still climbs to restart and
    escalation — needing the full threshold again, not the old residue."""
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    for _ in range(2):
        control.heartbeat(node="box1", version="0.3.0")  # two undeferred beats
    beat = control.heartbeat(
        node="box1", version="0.3.0", update_deferred="behind run r1 (stack worker)"
    )
    assert beat.json()["commands"] == []  # the reset beat — no third-strike restart
    # The deferral cleared but the node stays off-pin (a stuck install):
    # the ladder climbs from zero to the restart rung...
    for _ in range(2):
        assert control.heartbeat(node="box1", version="0.3.0").json()["commands"] == []
    beat = control.heartbeat(node="box1", version="0.3.0").json()
    assert [c["verb"] for c in beat["commands"]] == ["restart"]
    restart_id = beat["commands"][0]["id"]
    # ...and on to its escalation, exactly as before.
    for _ in range(3):
        control.heartbeat(node="box1", version="0.3.0", completed_commands=[restart_id])
    errors = control.store.error_events(component="control-node")
    assert [e["payload"]["error_class"] for e in errors] == ["update-not-converging"]


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
    body = {"role": "implementer", "driver": "w", "node": "n", "login": "l"}
    assert control.node_post("/api/v1/dispatch", body, token="wrong").status_code == 401


def test_dispatch_registers_the_driver(control: ControlRig):
    control.dispatch(worker="worker-a", node="box1")
    drivers = control.store.drivers()
    assert drivers[0]["worker"] == "worker-a"
    assert drivers[0]["login"] == "ozolith-worker-a"
    assert drivers[0]["role"] == "implementer"


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
        "driver": "worker-a",
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


def test_product_update_pins_and_fans_out_to_every_node(control: ControlRig):
    # Three registered nodes; no fan-out ordering exists for control — it
    # is never a Stack on any node (ADR-0035) and updates itself last
    # through its own os.execv path.
    for node in ("boxctl", "box1", "box2"):
        control.heartbeat(node=node)

    answer = control.admin("POST", "/api/v1/product/update", {"version": "0.4.0"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["version"] == "0.4.0"
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
        "drivers_hash": "",
        "stacks": [],
        "images": [],
        "heartbeat_seconds": 60.0,
        "stop_grace_seconds": 30.0,
    }


# -- the M9 state read model (ADR-0040, extending ADR-0039's keys) ---------------

ATTACH_STACK_TOML = """\
kind = "container"
node = "box1"
image = "ghcr.io/x/deck:1.0@sha256:%s"
attach = ["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "tmux", "attach"]

[env]
DECK_MODE = "quiet"
"""


def test_state_carries_attach_env_repo_and_the_settings_view(control: ControlRig):
    """The M9 Operator TUI read model (ADR-0040): desired_stacks entries
    carry the Stack's attach argv and non-secret env declarations, and the
    document carries the coordination repo and the read-only control_toml
    view — a pure API consumer needs no Config Repo access for any panel.
    The node channel is untouched: attach never rides a heartbeat."""
    control.write_config("stacks/deck.toml", ATTACH_STACK_TOML % ("0" * 64))
    control.write_config("worker-types/claude-dev.toml", WORKER_TYPE_TOML)
    control.write_config("stacks/worker.toml", STACK_TOML)

    state = control.admin("GET", "/api/v1/state").json()
    by_name = {s["name"]: s for s in state["desired_stacks"]}
    assert by_name["deck"]["attach"] == [
        "ssh",
        "{host}",
        "-t",
        "docker",
        "exec",
        "-it",
        "{container}",
        "tmux",
        "attach",
    ]
    assert by_name["deck"]["env"] == {"DECK_MODE": "quiet"}
    assert by_name["worker"]["attach"] == []
    # The resolved worker-type env surfaces in the read model (ADR-0044): the
    # type injects THEOZOLITH_REPO/ADAPTER/RUN_IMAGE control-side.
    worker_env = by_name["worker"]["env"]
    assert worker_env["THEOZOLITH_REPO"] == "acme/sandbox"
    assert worker_env["THEOZOLITH_ADAPTER"] == "claude"
    assert worker_env["THEOZOLITH_RUN_IMAGE"].startswith("theozolith/claude-dev:")

    assert state["repo"] == "acme/sandbox"
    toml_view = state["control_toml"]
    assert toml_view["control_ip"] == "203.0.113.5"
    assert toml_view["control_port"] == 443
    assert toml_view["settings"]["heartbeat_seconds"] == 60.0
    assert toml_view["settings"]["tail_budget_bytes"] == 10 * 1024**3

    # The channel invariant survives (ADR-0015): the heartbeat's desired
    # state carries no attach argv — it is consumed control-side only.
    config = control.heartbeat(node="box1").json()["config"]
    assert all("attach" not in stack for stack in config["stacks"])


# -- config distribution (ADR-0042) ---------------------------------------------


def _write_driver(control, content: str = "def run():\n    return 1\n") -> str:
    from theozolith_control import configdist

    control.write_config("drivers/custom/impl.py", content)
    return configdist.dist_hash(control.settings.config_repo)


def test_heartbeat_carries_drivers_hash_both_directions(control: ControlRig):
    digest = _write_driver(control)
    beat = control.heartbeat(node="box1", drivers_hash="", drivers_built_against="").json()
    # Down: the recorded reference rides desired state.
    assert beat["config"]["drivers_hash"] == digest
    # Up: what the node reports is recorded for the gate.
    control.heartbeat(node="box1", drivers_hash=digest, drivers_built_against="0.3.0")
    assert control.store.node_drivers_hash("box1") == digest


def test_config_artifact_builds_on_demand_and_serves(control: ControlRig):
    digest = _write_driver(control)
    pulled = control.client.get(
        f"/api/v1/config/artifacts/{digest}",
        headers={"Authorization": f"Bearer {control.node_token()}"},
    )
    assert pulled.status_code == 200
    # The served zip verifies by recompute on the node side.
    from theozolith_nodedaemon import configdist as node_configdist

    dest = control.settings.data_dir / "unpacked"
    dest.mkdir(parents=True, exist_ok=True)
    node_configdist.extract_zip(pulled.content, dest)
    assert node_configdist.manifest_hash_of_tree(dest) == digest
    # Second pull is served from cache (still 200, byte-identical).
    again = control.client.get(
        f"/api/v1/config/artifacts/{digest}",
        headers={"Authorization": f"Bearer {control.node_token()}"},
    )
    assert again.status_code == 200 and again.content == pulled.content


def test_config_artifact_409_when_repo_moved_past_the_requested_hash(control: ControlRig):
    _write_driver(control, "def run():\n    return 1\n")
    stale = "a" * 64  # a well-formed hash the current repo will never build
    answer = control.client.get(
        f"/api/v1/config/artifacts/{stale}",
        headers={"Authorization": f"Bearer {control.node_token()}"},
    )
    assert answer.status_code == 409


def test_config_artifact_rejects_bad_hashes_and_requires_node_token(control: ControlRig):
    _write_driver(control)
    for bad in ("nothex", "ABCDEF" + "0" * 58, "0" * 63, "../escape"):
        answer = control.client.get(
            f"/api/v1/config/artifacts/{bad}",
            headers={"Authorization": f"Bearer {control.node_token()}"},
        )
        assert answer.status_code in (400, 404), bad
    digest = _write_driver(control)
    # The admin token is not a node token: node-authenticated endpoint.
    unauth = control.client.get(
        f"/api/v1/config/artifacts/{digest}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert unauth.status_code == 401


def test_offhash_node_is_dispatch_ineligible_with_a_reason(control: ControlRig):
    digest = _write_driver(control)
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})
    # Node reports a DIFFERENT hash → blocked, with an explanatory reason.
    control.heartbeat(node="box1", drivers_hash="b" * 64)
    refused = control.dispatch(worker="worker-a", node="box1").json()
    assert refused["issue"] is None and "config distribution" in refused["reason"]
    review = control.node_post(
        "/api/v1/dispatch",
        {"role": "reviewer", "driver": "rev-1", "node": "box1", "login": "ozolith-rev"},
    ).json()
    assert review["prs"] == [] and "config distribution" in review["reason"]
    # Converge → eligible again, no human action.
    control.heartbeat(node="box1", drivers_hash=digest)
    assert control.dispatch(worker="worker-a", node="box1").json()["issue"]["number"] == 7


def test_unreported_and_converged_and_no_recorded_hash_stay_eligible(control: ControlRig):
    control.github.add_issue(9, labels={"plan_ready"}, assignees=[])
    # No recorded distribution: always eligible.
    assert control.dispatch(worker="w", node="box1").json()["issue"]["number"] == 9
    _write_driver(control)
    control.github.add_issue(10, labels={"plan_ready"}, assignees=[])
    # Unreported hash (daemon-less shape heartbeats nothing) stays eligible.
    assert control.dispatch(worker="w", node="ghost").json()["issue"]


def test_persistently_offhash_node_gets_restart_then_error(control: ControlRig):
    """The config-dist ladder mirrors the pin ladder exactly (ADR-0042).
    These heartbeats OMIT drivers_deferred — the legacy-daemon fallback
    (issue #8): field absence keeps the pre-deferral ladder behavior."""
    digest = _write_driver(control)
    off = "c" * 64
    for _ in range(2):
        assert control.heartbeat(node="box1", drivers_hash=off).json()["commands"] == []
    beat = control.heartbeat(node="box1", drivers_hash=off).json()  # third off-hash beat
    assert [c["verb"] for c in beat["commands"]] == ["restart"]
    restart_id = beat["commands"][0]["id"]
    for _ in range(3):
        control.heartbeat(node="box1", drivers_hash=off, completed_commands=[restart_id])
    errors = control.store.error_events(component="control-node")
    classes = [e["payload"]["error_class"] for e in errors]
    assert "config-dist-not-converging" in classes
    assert sum(1 for c in classes if c == "config-dist-not-converging") == 1
    # Convergence clears it.
    control.heartbeat(node="box1", drivers_hash=digest)
    assert control.store.observe_drivers("box1", digest, digest, 3) is None


def test_deferred_drivers_swap_behind_a_run_never_restarts_or_escalates(control: ControlRig):
    """Issue #8, drivers ladder: a swap queued behind an in-flight Run is
    not divergence — the config-dist ladder pauses exactly like the pin's."""
    digest = _write_driver(control)
    off = "c" * 64
    for _ in range(8):
        beat = control.heartbeat(
            node="box1", drivers_hash=off, drivers_deferred="behind run r1 (stack worker)"
        )
        assert beat.json()["commands"] == []
    assert control.store.error_events(component="control-node") == []
    # The Run ended, the swap applied: nothing was ever queued.
    control.heartbeat(node="box1", drivers_hash=digest)
    state = control.admin("GET", "/api/v1/state").json()
    assert [c["verb"] for c in state["commands"] if c["verb"] == "restart"] == []


def test_simultaneous_offpin_and_offhash_queue_exactly_one_restart(control: ControlRig):
    """A node off BOTH the pin and the drivers-hash needs a single re-exec, not
    two: the two subsystems share the one restart lever but keep independent
    counters and each emits its own terminal error (ADR-0042)."""
    digest = _write_driver(control)
    control.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    off_hash = "c" * 64
    for _ in range(2):
        control.heartbeat(node="box1", version="0.3.0", drivers_hash=off_hash)
    beat = control.heartbeat(node="box1", version="0.3.0", drivers_hash=off_hash).json()
    # Exactly one restart despite BOTH subsystems reaching the restart rung.
    assert [c["verb"] for c in beat["commands"]] == ["restart"]
    restart_id = beat["commands"][0]["id"]
    state = control.admin("GET", "/api/v1/state").json()
    assert [c["verb"] for c in state["commands"] if c["verb"] == "restart"] == ["restart"]
    # Completing that restart reveals no second stale restart, and each
    # subsystem still emits its OWN distinct error class past 2x the threshold.
    for _ in range(3):
        control.heartbeat(
            node="box1", version="0.3.0", drivers_hash=off_hash, completed_commands=[restart_id]
        )
    state = control.admin("GET", "/api/v1/state").json()
    assert [c["verb"] for c in state["commands"] if c["verb"] == "restart"] == ["restart"]
    classes = sorted(
        e["payload"]["error_class"] for e in control.store.error_events(component="control-node")
    )
    assert classes == ["config-dist-not-converging", "update-not-converging"]
    # Converging BOTH clears BOTH trackers.
    control.heartbeat(node="box1", version="0.4.0", drivers_hash=digest)
    assert control.store.observe_version("box1", "0.4.0", "0.4.0", 3) is None
    assert control.store.observe_drivers("box1", digest, digest, 3) is None


def test_corrupted_cached_config_artifact_is_repaired_on_next_pull(control: ControlRig):
    """A corrupted <hash>.zip in the deletable cache must not be served forever:
    the next pull discards it and rebuilds from the working repo (ADR-0042)."""
    digest = _write_driver(control)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    first = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert first.status_code == 200
    cached = control.settings.config_artifacts_dir / f"{digest}.zip"
    assert cached.is_file()
    cached.write_bytes(b"corrupted, not a zip")  # poison the cache entry
    again = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert again.status_code == 200
    # The repaired artifact verifies by recompute on the node side.
    from theozolith_nodedaemon import configdist as node_configdist

    dest = control.settings.data_dir / "repaired"
    dest.mkdir(parents=True, exist_ok=True)
    node_configdist.extract_zip(again.content, dest)
    assert node_configdist.manifest_hash_of_tree(dest) == digest


def test_cached_artifact_with_bad_crc_is_repaired_not_500(control: ControlRig):
    """A cached <hash>.zip that OPENS as a zip but whose member fails its CRC is
    recovered on the next pull — rebuilt from the still-matching working repo,
    never served and never a 500 (ADR-0042)."""
    import io
    import zipfile

    from theozolith_control import configdist

    digest = _write_driver(control)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    first = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert first.status_code == 200
    cached = control.settings.config_artifacts_dir / f"{digest}.zip"
    # Rewrite the cache entry as a structurally-openable zip whose stored member
    # data is corrupt: it opens, but archive.read() fails its CRC.
    raw = cached.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        first_driver = next(n for n in src.namelist() if n.startswith("drivers/"))
        info = src.getinfo(first_driver)
    data_start = info.header_offset + 30 + len(first_driver.encode()) + len(info.extra or b"")
    poisoned = bytearray(raw)
    for offset in range(data_start, data_start + max(1, info.compress_size)):
        poisoned[offset] ^= 0xFF
    cached.write_bytes(bytes(poisoned))
    # The poisoned entry must not verify (proves the corruption is real).
    try:
        configdist.verify_artifact(cached)
        raise AssertionError("poisoned cache entry unexpectedly verified")
    except configdist.ConfigDistError:
        pass
    # Repeated pulls each return a valid, recomputable artifact — not another 500.
    for _ in range(2):
        again = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
        assert again.status_code == 200
        recomputed, _ = configdist.verify_artifact(cached)
        assert recomputed == digest


def test_cached_artifact_with_invalid_utf8_metadata_is_repaired_not_500(control: ControlRig):
    """A cached <hash>.zip whose drivers tree is intact but whose
    ``config-dist.json`` member decays into invalid UTF-8 is discarded and
    rebuilt on the next pull — a normalized ConfigDistError inside
    verification, never a leaked UnicodeDecodeError surfacing as a 500
    (ADR-0042 amendment)."""
    import io
    import zipfile

    import pytest
    from theozolith_control import configdist

    digest = _write_driver(control)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    first = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert first.status_code == 200
    cached = control.settings.config_artifacts_dir / f"{digest}.zip"
    # Rewrite the cache entry keeping every drivers member byte-identical but
    # replacing the metadata member with invalid UTF-8.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(cached.read_bytes())) as src, zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            if info.filename == configdist.ARTIFACT_METADATA:
                dst.writestr(info, b"\xff\xfe not utf-8 {")
            else:
                dst.writestr(info, src.read(info))
    cached.write_bytes(buf.getvalue())
    # The poison is real, and it normalizes (never UnicodeDecodeError).
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(cached)
    # Repeated pulls each recover: valid 200s, and the repaired cache verifies.
    for _ in range(2):
        again = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
        assert again.status_code == 200
        recomputed, _ = configdist.verify_artifact(cached)
        assert recomputed == digest


def test_concurrent_builds_and_prunes_settle_to_keep_two(control: ControlRig):
    """Concurrent pulls that build a THIRD hash race the keep-two prune (and
    each other's prunes): every response is a valid verified artifact, nothing
    500s because a candidate vanished mid-prune, and once the churn settles the
    cache holds exactly the two newest distributions (ADR-0042 amendment)."""
    import os

    from theozolith_control import configdist

    headers = {"Authorization": f"Bearer {control.node_token()}"}
    cache = control.settings.config_artifacts_dir
    digest_a = _write_driver(control, "v = 1\n")
    seeded = control.client.get(f"/api/v1/config/artifacts/{digest_a}", headers=headers)
    assert seeded.status_code == 200
    digest_b = _write_driver(control, "v = 2\n")
    seeded = control.client.get(f"/api/v1/config/artifacts/{digest_b}", headers=headers)
    assert seeded.status_code == 200
    now = cache.stat().st_mtime
    os.utime(cache / f"{digest_a}.zip", (now - 100, now - 100))
    os.utime(cache / f"{digest_b}.zip", (now - 50, now - 50))
    digest_c = _write_driver(control, "v = 3\n")
    results: list[tuple[int, bytes]] = []
    lock = threading.Lock()

    def pull() -> None:
        r = control.client.get(f"/api/v1/config/artifacts/{digest_c}", headers=headers)
        with lock:
            results.append((r.status_code, r.content))

    threads = [threading.Thread(target=pull) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results and all(status == 200 for status, _ in results)
    for _, content in results:
        recomputed, _ = configdist.verify_artifact_bytes(content)
        assert recomputed == digest_c
    # Churn settled: exactly the two newest survive the racing keep-two prunes.
    zips = sorted(p.name for p in cache.iterdir() if p.suffix == ".zip")
    assert zips == sorted(f"{d}.zip" for d in (digest_b, digest_c))


def test_concurrent_pulls_do_not_tear_the_cache(control: ControlRig):
    """Cache publication is atomic (tempfile + os.replace), so concurrent pulls
    for the same hash each get a COMPLETE, valid artifact that recomputes to the
    requested hash — a torn/partial <hash>.zip is impossible. (Bytes may differ
    only in the advisory built_at stamp; integrity is what matters.)"""
    from theozolith_control import configdist

    digest = _write_driver(control)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    results: list[tuple[int, bytes]] = []
    lock = threading.Lock()

    def pull() -> None:
        r = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
        with lock:
            results.append((r.status_code, r.content))

    threads = [threading.Thread(target=pull) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results and all(status == 200 for status, _ in results)
    # Every concurrent response is a complete artifact that recomputes to digest.
    scratch = control.settings.data_dir / "concurrent"
    scratch.mkdir(parents=True, exist_ok=True)
    for i, (_, content) in enumerate(results):
        probe = scratch / f"{i}.zip"
        probe.write_bytes(content)
        recomputed, _ = configdist.verify_artifact(probe)
        assert recomputed == digest
    cached = control.settings.config_artifacts_dir / f"{digest}.zip"
    recomputed, _ = configdist.verify_artifact(cached)
    assert recomputed == digest


def test_heartbeat_stays_responsive_during_a_slow_rebuild(control: ControlRig, monkeypatch):
    """Cache verify/build run off the event loop (asyncio.to_thread): a heartbeat
    completes while a slow rebuild is in flight, never queued behind it."""
    from theozolith_control import configdist

    digest = _write_driver(control)
    entered = threading.Event()
    release = threading.Event()
    real_build = configdist.build_artifact

    def slow_build(*args, **kwargs):
        entered.set()
        assert release.wait(5), "rebuild was never released"
        return real_build(*args, **kwargs)

    monkeypatch.setattr(configdist, "build_artifact", slow_build)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    pull_status: dict[str, int] = {}

    def pull() -> None:
        r = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
        pull_status["code"] = r.status_code

    puller = threading.Thread(target=pull)
    puller.start()
    try:
        assert entered.wait(5), "the rebuild never started"
        # The build is parked in a worker thread; the loop is free to heartbeat.
        beat = control.heartbeat(node="box1")
        assert beat.status_code == 200
    finally:
        release.set()
        puller.join()
    assert pull_status["code"] == 200


def test_inflight_artifact_response_survives_concurrent_pruning(control: ControlRig, monkeypatch):
    """An artifact response accepted after verification must remain complete
    even when another request builds a third hash and the keep-two prune
    unlinks the entry being served: the response owns an immutable byte
    snapshot, not a pathname (ADR-0042 amendment). Deterministic: the first
    response is paused after verification, the prune is forced while it is
    parked, then it resumes."""
    import os

    from theozolith_control import configdist

    headers = {"Authorization": f"Bearer {control.node_token()}"}
    cache = control.settings.config_artifacts_dir
    # Two retained artifacts, with explicit mtimes so the keep-two prune
    # deterministically takes the oldest.
    digest_a = _write_driver(control, "v = 1\n")
    first = control.client.get(f"/api/v1/config/artifacts/{digest_a}", headers=headers)
    assert first.status_code == 200
    digest_b = _write_driver(control, "v = 2\n")
    second = control.client.get(f"/api/v1/config/artifacts/{digest_b}", headers=headers)
    assert second.status_code == 200
    now = cache.stat().st_mtime
    os.utime(cache / f"{digest_a}.zip", (now - 100, now - 100))
    os.utime(cache / f"{digest_b}.zip", (now - 50, now - 50))

    entered = threading.Event()
    release = threading.Event()
    real_verify = configdist.verify_artifact_bytes

    def pausing(data):
        result = real_verify(data)
        if result[0] == digest_a and not entered.is_set():
            entered.set()
            assert release.wait(10), "the in-flight response was never released"
        return result

    monkeypatch.setattr(configdist, "verify_artifact_bytes", pausing)
    answer: dict = {}

    def pull_oldest() -> None:
        r = control.client.get(f"/api/v1/config/artifacts/{digest_a}", headers=headers)
        answer["status"], answer["content"] = r.status_code, r.content

    puller = threading.Thread(target=pull_oldest)
    puller.start()
    try:
        assert entered.wait(10), "the paused pull never reached verification"
        # While the oldest artifact's response is parked post-verification, a
        # THIRD hash builds and the keep-two prune takes the served entry.
        digest_c = _write_driver(control, "v = 3\n")
        built = control.client.get(f"/api/v1/config/artifacts/{digest_c}", headers=headers)
        assert built.status_code == 200
        assert not (cache / f"{digest_a}.zip").exists()  # pruned mid-flight
    finally:
        release.set()
        puller.join(timeout=10)
    assert not puller.is_alive()
    # The resumed response is a 200 with a COMPLETE artifact that recomputes
    # to the requested hash — the prune never touched what was verified.
    assert answer["status"] == 200
    recomputed, _ = real_verify(answer["content"])
    assert recomputed == digest_a
    # And after the in-flight use ends, the cache still converges to keep-two.
    zips = sorted(p.name for p in cache.iterdir() if p.suffix == ".zip")
    assert zips == sorted(f"{d}.zip" for d in (digest_b, digest_c))


def test_post_build_cache_corruption_is_never_served(control: ControlRig, monkeypatch):
    """The build branch must send the bytes it VERIFIED, not whatever the cache
    file happens to hold after publication: a corruption landing between
    build_artifact's publish and the endpoint's post-build read is caught by
    verifying that exact response snapshot against the requested hash, the
    suspect entry is dropped, and the bounded loop rebuilds a valid artifact
    (ADR-0042 amendment)."""
    from theozolith_control import configdist

    digest = _write_driver(control)
    garbage = b"corrupted after publication, before the response read"
    corrupted = {"done": False}
    real_build = configdist.build_artifact

    def build_then_corrupt(*args, **kwargs):
        built, path = real_build(*args, **kwargs)
        if path is not None and not corrupted["done"]:
            corrupted["done"] = True
            path.write_bytes(garbage)  # lands before the endpoint's post-build read
        return built, path

    monkeypatch.setattr(configdist, "build_artifact", build_then_corrupt)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    answer = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert corrupted["done"]  # the corruption really landed inside the window
    assert answer.status_code == 200
    assert answer.content != garbage  # the corrupted snapshot was never sent
    probe = control.settings.data_dir / "postbuild.zip"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(answer.content)
    recomputed, _ = configdist.verify_artifact(probe)
    assert recomputed == digest


def test_sustained_post_build_corruption_returns_the_retry_response(
    control: ControlRig, monkeypatch
):
    """If EVERY rebuild's published entry is corrupted inside the
    publish-to-read window, the endpoint exhausts its bounded loop with the
    documented 503 retry answer — never the corrupt bytes, never an unverified
    response (ADR-0042 amendment)."""
    from theozolith_control import configdist

    digest = _write_driver(control)
    garbage = b"corrupted after every publication"
    real_build = configdist.build_artifact

    def build_then_corrupt(*args, **kwargs):
        built, path = real_build(*args, **kwargs)
        if path is not None:
            path.write_bytes(garbage)
        return built, path

    monkeypatch.setattr(configdist, "build_artifact", build_then_corrupt)
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    answer = control.client.get(f"/api/v1/config/artifacts/{digest}", headers=headers)
    assert answer.status_code == 503
    assert garbage not in answer.content


def test_broken_config_dist_keeps_dispatch_fail_open(control: ControlRig):
    """A config-distribution validation failure (symlinked drivers root) must
    preserve dispatch's documented fail-open posture: the grant path proceeds
    as if no pin/hash were recorded, rather than 500-ing (ADR-0042)."""
    import os

    external = control.settings.data_dir / "external"
    external.mkdir(parents=True, exist_ok=True)
    (external / "x.py").write_text("escape\n", encoding="utf-8")
    control.settings.config_repo.mkdir(parents=True, exist_ok=True)
    os.symlink(external, control.settings.config_repo / "drivers")
    control.github.add_issue(3, labels={"plan_ready"}, assignees=[])
    # Heartbeat still answers (degraded desired state) rather than crashing.
    assert control.heartbeat(node="box1").status_code == 500
    # Dispatch stays fail-open on the broken repo — a grant still lands.
    granted = control.dispatch(worker="worker-a", node="box1").json()
    assert granted["issue"]["number"] == 3


def test_flags_lists_stamp_skew_and_state_carries_the_hash(control: ControlRig):
    digest = _write_driver(control)
    # A node whose applied dist was built against a different product version.
    control.heartbeat(
        node="box1", version="0.4.0", drivers_hash=digest, drivers_built_against="0.3.0"
    )
    flags = control.admin("GET", "/api/v1/flags").json()
    assert flags["drivers_skew"] == [
        {"node": "box1", "built_against": "0.3.0", "product_version": "0.4.0"}
    ]
    state = control.admin("GET", "/api/v1/state").json()
    assert state["config_drivers_hash"] == digest


# -- field-absent vs explicit-none: the two empty reports differ (ADR-0042) -------


def test_desired_nonempty_field_absent_stays_eligible(control: ControlRig):
    """A heartbeat that OMITS drivers_hash is the legacy/daemon-less shape:
    fail-open eligible even when a non-empty distribution is desired."""
    digest = _write_driver(control)
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    # The rig's default heartbeat carries NO drivers_hash key at all.
    control.heartbeat(node="box1")
    assert control.store.node_drivers_hash("box1") is None
    assert control.dispatch(worker="worker-a", node="box1").json()["issue"]["number"] == 7
    assert digest  # a real distribution is desired, yet the absent field is fail-open


def test_desired_nonempty_field_explicit_empty_is_blocked(control: ControlRig):
    """A current daemon that reports drivers_hash='' because it has no verified
    applied tree is off-hash and dispatch-blocked — never conflated with the
    legacy omission — on BOTH the implementer and reviewer paths."""
    digest = _write_driver(control)
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})
    control.heartbeat(node="box1", drivers_hash="")  # explicit none applied
    assert control.store.node_drivers_hash("box1") == ""
    refused = control.dispatch(worker="worker-a", node="box1").json()
    assert refused["issue"] is None and "config distribution" in refused["reason"]
    assert "none applied" in refused["reason"]
    review = control.node_post(
        "/api/v1/dispatch",
        {"role": "reviewer", "driver": "rev-1", "node": "box1", "login": "ozolith-rev"},
    ).json()
    assert review["prs"] == [] and "config distribution" in review["reason"]
    # A successful repair restores eligibility with no human action.
    control.heartbeat(node="box1", drivers_hash=digest)
    assert control.dispatch(worker="worker-a", node="box1").json()["issue"]["number"] == 7


def test_missing_applied_tree_with_repeated_failed_fetch_stays_blocked(control: ControlRig):
    """A node whose applied tree is missing/mutated reports '' every beat (its
    own recompute fails) and repeated failed artifact fetches never flip the
    gate: it stays blocked until a real converged hash arrives (ADR-0042)."""
    digest = _write_driver(control)
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    headers = {"Authorization": f"Bearer {control.node_token()}"}
    # Two off-hash beats (below the restart threshold, so the block stays the
    # drivers gate, not a queued lifecycle command).
    for _ in range(2):
        control.heartbeat(node="box1", drivers_hash="")
        # The node retries the artifact and keeps failing on a stale hash.
        stale = control.client.get(f"/api/v1/config/artifacts/{'d' * 64}", headers=headers)
        assert stale.status_code == 409
        assert control.dispatch(worker="worker-a", node="box1").json()["issue"] is None
    # The repair (a real fetch + a converged report) restores eligibility.
    control.heartbeat(node="box1", drivers_hash=digest)
    assert control.dispatch(worker="worker-a", node="box1").json()["issue"]["number"] == 7


def test_explicit_empty_participates_in_restart_then_error_escalation(control: ControlRig):
    """An explicit '' report is a real off-hash beat: it climbs the SAME
    restart-then-error ladder as a mismatching hash (ADR-0042)."""
    _write_driver(control)
    for _ in range(2):
        assert control.heartbeat(node="box1", drivers_hash="").json()["commands"] == []
    beat = control.heartbeat(node="box1", drivers_hash="").json()  # third off-hash beat
    assert [c["verb"] for c in beat["commands"]] == ["restart"]
    restart_id = beat["commands"][0]["id"]
    for _ in range(3):
        control.heartbeat(node="box1", drivers_hash="", completed_commands=[restart_id])
    classes = [
        e["payload"]["error_class"] for e in control.store.error_events(component="control-node")
    ]
    assert classes.count("config-dist-not-converging") == 1


def test_field_absent_does_not_escalate(control: ControlRig):
    """The legacy omission never climbs the ladder — no report is a reset, so
    no restart and no error are ever queued for it (ADR-0042)."""
    _write_driver(control)
    for _ in range(6):
        beat = control.heartbeat(node="box1").json()  # no drivers_hash key
        assert beat["commands"] == []
    assert control.store.error_events(component="control-node") == []


def test_no_desired_distribution_stays_eligible_for_every_report(control: ControlRig):
    """With no drivers/ desired, every report shape — absent, explicit empty,
    or a stale non-empty hash — is eligible and never off-hash (ADR-0042)."""
    control.github.add_issue(1, labels={"plan_ready"}, assignees=[])
    control.github.add_issue(2, labels={"plan_ready"}, assignees=[])
    control.heartbeat(node="box1", drivers_hash="")
    assert control.dispatch(worker="w", node="box1").json()["issue"]
    control.heartbeat(node="box1", drivers_hash="a" * 64)  # stale non-empty, no desired
    assert control.dispatch(worker="w", node="box1").json()["issue"]
    state = control.admin("GET", "/api/v1/state").json()
    assert state["config_drivers_hash"] is None


# -- managed registry pull credentials (ADR-0049) ------------------------------

import json  # noqa: E402

REGISTRY_SECRET = "registry:ghcr.io"  # WORKER_TYPE_TOML's base is ghcr.io/x/run


def _store_secret(control: ControlRig, name: str, value: str) -> None:
    control.secret_store.put_secret(name, control.box.encrypt(value))


def _running_worker_on_box1(control: ControlRig) -> None:
    control.write_config("worker-types/claude-dev.toml", WORKER_TYPE_TOML)
    control.write_config("stacks/worker.toml", STACK_TOML)  # defaults to running


def test_heartbeat_carries_registry_secret_names_only_when_stored(control: ControlRig):
    """The heartbeat references a stored `registry:<host>` credential by NAME
    only (the value rides the node-scoped pull), and only when it is stored."""
    _running_worker_on_box1(control)
    assert "registry_secrets" not in control.heartbeat(node="box1").json()["config"]

    _store_secret(control, REGISTRY_SECRET, "octocat:ghp_token")
    config = control.heartbeat(node="box1").json()["config"]
    assert config["registry_secrets"] == {"ghcr.io": REGISTRY_SECRET}
    # Channel invariant: the value never rides the heartbeat.
    assert "octocat" not in json.dumps(config)


def test_registry_secret_for_a_different_host_never_rides(control: ControlRig):
    """A stored credential for a host this node builds no base from is not
    referenced — box1's base is ghcr.io, a Hub credential stays off the wire."""
    _running_worker_on_box1(control)
    _store_secret(control, "registry:registry-1.docker.io", "u:t")
    assert "registry_secrets" not in control.heartbeat(node="box1").json()["config"]


def test_stopped_worker_stack_carries_no_registry_secret(control: ControlRig):
    """A stopped Stack builds no image, so no base is pulled and no credential
    is referenced (mirrors the images scoping)."""
    control.write_config("worker-types/claude-dev.toml", WORKER_TYPE_TOML)
    control.write_config("stacks/worker.toml", STACK_TOML + 'state = "stopped"\n')
    _store_secret(control, REGISTRY_SECRET, "octocat:ghp_token")
    assert "registry_secrets" not in control.heartbeat(node="box1").json()["config"]


def test_registry_secret_pull_allowed_when_scoped_and_stored(control: ControlRig):
    _running_worker_on_box1(control)
    _store_secret(control, REGISTRY_SECRET, "octocat:ghp_token")
    resp = control.node_post(
        "/api/v1/secrets/pull", {"node": "box1", "names": [REGISTRY_SECRET]}, node="box1"
    )
    assert resp.status_code == 200
    assert resp.json()["secrets"] == {REGISTRY_SECRET: "octocat:ghp_token"}


def test_registry_secret_pull_forbidden_off_the_running_node(control: ControlRig):
    """A node with nothing placed on it may not pull the credential (403) — the
    same node-scoping the workload secrets get."""
    _running_worker_on_box1(control)
    _store_secret(control, REGISTRY_SECRET, "octocat:ghp_token")
    resp = control.node_post(
        "/api/v1/secrets/pull", {"node": "box2", "names": [REGISTRY_SECRET]}, node="box2"
    )
    assert resp.status_code == 403


def test_registry_secret_pull_404_when_scoped_but_unstored(control: ControlRig):
    """Scoped (running type's base host) but never stored -> the ordinary 404,
    not a scoping 403."""
    _running_worker_on_box1(control)
    resp = control.node_post(
        "/api/v1/secrets/pull", {"node": "box1", "names": [REGISTRY_SECRET]}, node="box1"
    )
    assert resp.status_code == 404


def test_put_registry_secret_enforces_the_shape_guard(control: ControlRig):
    """The write surface rejects a malformed registry credential with an
    actionable 400; a well-formed one and any normal secret store fine."""
    bad_value = control.admin("PUT", "/api/v1/secrets/registry:ghcr.io", body={"value": "no-colon"})
    assert bad_value.status_code == 400
    bad_name = control.admin("PUT", "/api/v1/secrets/registry:", body={"value": "u:t"})
    assert bad_name.status_code == 400
    good = control.admin("PUT", "/api/v1/secrets/registry:ghcr.io", body={"value": "octo:tok"})
    assert good.status_code == 200
    normal = control.admin(
        "PUT", "/api/v1/secrets/github-implementer", body={"value": "opaque-value"}
    )
    assert normal.status_code == 200
