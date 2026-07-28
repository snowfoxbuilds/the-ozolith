"""Join tokens and per-node tokens (ADR-0023): the exchange is the sole way
a node comes to exist; tokens are single-use, revocable, and per-node;
rejected heartbeats surface as capped, deduplicated sightings (ADR-0028)."""

from __future__ import annotations

import re

import pytest
from controlrig import ADMIN_PASSWORD, ControlRig, make_rig
from theozolith_control.store import UNREGISTERED_CAP
from theozolith_control.tls import provision


def _mint(control: ControlRig, **overrides) -> dict:
    body = {"addr": "192.0.2.10:6965", **overrides}
    answer = control.admin("POST", "/api/v1/join-tokens", body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _with_ca(control: ControlRig) -> ControlRig:
    provision(control.settings.tls_dir, ["127.0.0.1"])
    return control


def _exchange(control: ControlRig, token_hex: str, node: str):
    return control.client.post("/api/v1/join/exchange", json={"node": node, "token": token_hex})


def _payload_of(join_string: str) -> bytes:
    import base64

    encoded = join_string.partition(":")[2]
    blob = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return blob[:-4]  # strip the checksum


def _token_hex_from(join_string: str) -> str:
    """Lift the raw token out of a composed join string (the node-side
    parser proper is exercised in nodedaemon/tests)."""
    return _payload_of(join_string)[4 + 32 : 4 + 32 + 16].hex()


def _addr_of(join_string: str) -> str:
    return _payload_of(join_string)[4 + 32 + 16 :].decode()


def test_join_token_create_answers_the_complete_paste(control: ControlRig):
    minted = _mint(_with_ca(control))
    assert minted["join_string"].startswith("ozjoin1:")
    assert len(minted["join_string"]) < 140  # copy-paste-atomic (~120 chars)
    assert minted["provision_command"].startswith("sudo theozolith-nodedaemon provision 'ozjoin1:")
    # Fresh box: installer over a pre-trusted channel, never the listener.
    assert minted["install_command"].startswith("curl -fsSL https://github.com/")
    assert "| sudo bash -s -- 'ozjoin1:" in minted["install_command"]


def test_join_token_create_requires_a_ca(control: ControlRig):
    refused = control.admin("POST", "/api/v1/join-tokens", {})
    assert refused.status_code == 409 and "init" in refused.json()["detail"]


def test_every_mint_surface_embeds_the_persisted_ip_never_detection(
    control: ControlRig, monkeypatch
):
    """Acceptance 3 (ADR-0031): the API and the dashboard join page (the
    CLI rides the API) default to the init-persisted control IP; no mint
    path calls detect_host_ip() — proven by making detection explode."""
    _with_ca(control)

    def _boom() -> str:
        raise AssertionError("detect_host_ip() must never run at mint time (ADR-0031)")

    monkeypatch.setattr("theozolith_control.bootstrap.detect_host_ip", _boom)

    # API mint, no addr: the persisted IP + bootstrap port.
    minted = control.admin("POST", "/api/v1/join-tokens", {}).json()
    assert _addr_of(minted["join_string"]) == "203.0.113.5:6965"

    # Dashboard mint: same address, same non-detection.
    login = control.client.post("/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    assert login.status_code == 303
    page = control.client.post("/join", data={"ttl_seconds": "3600", "uses": "1"}).text
    match = re.search(r"ozjoin1:[A-Za-z0-9_-]+", page)
    assert match is not None
    assert _addr_of(match.group(0)) == "203.0.113.5:6965"


def test_mints_refuse_without_a_persisted_ip(tmp_path, monkeypatch):
    """A pre-init deployment cannot mint a guessed address: 409 with
    instructions, never a silent detect_host_ip() fallback."""
    rig = make_rig(tmp_path, control_ip="")
    provision(rig.settings.tls_dir, ["127.0.0.1"])
    monkeypatch.setattr(
        "theozolith_control.bootstrap.detect_host_ip",
        lambda: pytest.fail("detection at mint time"),
    )
    refused = rig.admin("POST", "/api/v1/join-tokens", {})
    assert refused.status_code == 409
    assert "no persisted control IP" in refused.json()["detail"]
    # An explicit addr override still works (the expert path).
    explicit = rig.admin("POST", "/api/v1/join-tokens", {"addr": "192.0.2.1:6965"})
    assert explicit.status_code == 200


def test_exchange_mints_distinct_per_node_tokens_and_consumes_the_token(control: ControlRig):
    """Acceptance 9 + 11 (control half): one exchange per use; two nodes
    hold distinct tokens; the second use of a single-use token is rejected
    with nothing persisted."""
    control = _with_ca(control)
    first = _exchange(control, _token_hex_from(_mint(control)["join_string"]), "node-a").json()
    second = _exchange(control, _token_hex_from(_mint(control)["join_string"]), "node-b").json()
    assert first["node_token"] != second["node_token"]
    assert control.secret_store.node_for_token(first["node_token"]) == "node-a"
    assert control.secret_store.node_for_token(second["node_token"]) == "node-b"

    replay = _exchange(control, _token_hex_from(_mint(control)["join_string"]), "node-c")
    assert replay.status_code == 200
    spent = _exchange(control, _token_hex_from(minted := _mint(control)["join_string"]), "node-d")
    assert spent.status_code == 200
    again = _exchange(control, _token_hex_from(minted), "node-e")
    assert again.status_code == 401
    assert "expired, consumed, or revoked" in again.json()["detail"]
    assert control.secret_store.node_for_token("") is None
    assert "node-e" not in [n["node"] for n in control.secret_store.provisioned_nodes()]


def test_multi_use_ttl_and_revocation(control: ControlRig):
    control = _with_ca(control)
    minted = _mint(control, uses=2, ttl_seconds=100)
    token_hex = _token_hex_from(minted["join_string"])
    assert _exchange(control, token_hex, "n1").status_code == 200
    assert _exchange(control, token_hex, "n2").status_code == 200
    assert _exchange(control, token_hex, "n3").status_code == 401  # uses spent

    expiring = _mint(control, ttl_seconds=50)
    control.clock.advance(60)
    assert _exchange(control, _token_hex_from(expiring["join_string"]), "late").status_code == 401

    revoked = _mint(control)
    assert control.admin("DELETE", f"/api/v1/join-tokens/{revoked['id']}").json()["revoked"]
    assert _exchange(control, _token_hex_from(revoked["join_string"]), "nope").status_code == 401

    # Listing shows outstanding ids and windows only — never token material.
    outstanding = _mint(control)
    listing = control.admin("GET", "/api/v1/join-tokens").json()["tokens"]
    assert [t["id"] for t in listing] == [outstanding["id"]]
    assert all(set(t) == {"id", "created_at", "expires_at", "uses_left"} for t in listing)


def test_revoking_a_node_401s_only_that_node_which_surfaces_unregistered(control: ControlRig):
    """Acceptance 11: revocation is per-node; the revoked node keeps
    heartbeating, surfaces in the unregistered view with name/source/last
    seen, and never becomes dispatch-eligible."""
    control.provision_node("box2")
    assert control.heartbeat(node="box1").status_code == 200
    assert control.heartbeat(node="box2").status_code == 200

    assert control.admin("POST", "/api/v1/nodes/box2/revoke").json()["revoked"] is True
    assert control.heartbeat(node="box1").status_code == 200  # only box2 dies
    rejected = control.heartbeat(node="box2", token=control.node_token("box2"))
    assert rejected.status_code == 401

    state = control.admin("GET", "/api/v1/state").json()
    sighting = state["unregistered_nodes"][0]
    assert sighting["name"] == "box2" and sighting["source"] and sighting["last_seen"]
    # Never dispatch-eligible: its token is dead on the dispatch path too.
    dispatch = control.node_post(
        "/api/v1/dispatch",
        {"role": "worker", "worker": "w", "node": "box2", "login": "x"},
        token=control.node_token("box2"),
    )
    assert dispatch.status_code == 401


def test_unregistered_view_is_deduplicated_and_size_capped(control: ControlRig):
    """ADR-0028: dedupe on (name, source); cap with oldest-last_seen
    eviction — unauthenticated input cannot grow the cache unboundedly."""
    for _ in range(3):
        control.heartbeat(node="dupe", token="bad-token")
    assert [u["beats"] for u in control.store.unregistered_nodes()] == [3]

    for index in range(UNREGISTERED_CAP + 10):
        control.clock.advance(1)
        control.heartbeat(node=f"ghost-{index:03d}", token="bad-token")
    sightings = control.store.unregistered_nodes()
    assert len(sightings) == UNREGISTERED_CAP
    # The oldest sightings (dupe, ghost-000…) were evicted, newest kept.
    assert sightings[0]["name"] == f"ghost-{UNREGISTERED_CAP + 9:03d}"
    assert all(u["name"] != "dupe" for u in sightings)

    # A successful (re-)provision clears that name from the worklist.
    control.provision_node(sightings[0]["name"])
    control.heartbeat(node=sightings[0]["name"])
    assert all(u["name"] != sightings[0]["name"] for u in control.store.unregistered_nodes())
