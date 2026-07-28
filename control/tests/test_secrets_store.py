"""The secret store (acceptance 7, control half): entered once, encrypted at
rest, pull-only, node-scoped, and refused off-TLS."""

from __future__ import annotations

from controlrig import ControlRig, make_rig
from theozolith_control.crypto import SecretBox, generate_key

SENTINEL = "ghp_SUPERSECRETVALUE12345"

WORKER_STACK = (
    'kind = "process"\nnode = "box1"\ncommand = "theozolith-worker"\n'
    '[secrets]\nWORKER_GITHUB_TOKEN = "github-worker"\n'
)


def enter_secret(control: ControlRig, name: str = "github-worker", value: str = SENTINEL):
    return control.admin("PUT", f"/api/v1/secrets/{name}", {"value": value})


def test_value_entered_once_pulls_on_the_referencing_node(control):
    control.write_config("stacks/worker.toml", WORKER_STACK)
    assert enter_secret(control).status_code == 200

    answer = control.node_post("/api/v1/secrets/pull", {"node": "box1", "names": ["github-worker"]})
    assert answer.json() == {"secrets": {"github-worker": SENTINEL}}


def test_non_referencing_node_is_denied(control):
    control.write_config("stacks/worker.toml", WORKER_STACK)
    enter_secret(control)

    denied = control.node_post(
        "/api/v1/secrets/pull", {"node": "box2", "names": ["github-worker"]}, node="box2"
    )
    assert denied.status_code == 403
    # A node with no Stacks at all may pull nothing (the brief's exact case).
    assert "no Stack referencing" in denied.json()["detail"]
    # And box2's token cannot pull AS box1 (per-node identity, ADR-0023).
    imposter = control.node_post(
        "/api/v1/secrets/pull",
        {"node": "box1", "names": ["github-worker"]},
        token=control.node_token("box2"),
    )
    assert imposter.status_code == 403


def test_secret_entry_requires_the_admin_token(control):
    refused = control.client.put(
        "/api/v1/secrets/github-worker",
        json={"value": SENTINEL},
        headers={"Authorization": "Bearer node-token"},
    )
    assert refused.status_code == 401


def test_no_api_returns_values_to_admins(control):
    control.write_config("stacks/worker.toml", WORKER_STACK)
    enter_secret(control)
    listing = control.admin("GET", "/api/v1/secrets").json()
    assert listing == {"names": ["github-worker"]}
    assert SENTINEL not in listing.get("names", [])


def test_at_rest_storage_is_encrypted(control):
    """No plaintext recoverable from the DB file (acceptance 7)."""
    control.write_config("stacks/worker.toml", WORKER_STACK)
    enter_secret(control)

    raw = control.settings.store_db_path.read_bytes()
    assert SENTINEL.encode() not in raw
    # The name is metadata, the value is not: re-reading through the store
    # without the box also yields only ciphertext.
    token = control.secret_store.get_secret_token("github-worker")
    assert token is not None and SENTINEL not in token


def test_reentering_a_secret_replaces_the_value(control):
    control.write_config("stacks/worker.toml", WORKER_STACK)
    enter_secret(control, value="old-value")
    enter_secret(control, value="new-value")
    answer = control.node_post("/api/v1/secrets/pull", {"node": "box1", "names": ["github-worker"]})
    assert answer.json()["secrets"]["github-worker"] == "new-value"


def test_secret_endpoints_refuse_a_non_tls_channel(tmp_path):
    """TLS mandatory: with the channel not TLS (and no --insecure-dev), both
    entry and pull are refused — values never transit plaintext."""
    rig = make_rig(tmp_path, secrets_channel_ok=False)
    entry = rig.admin("PUT", "/api/v1/secrets/x", {"value": "v"})
    pull = rig.node_post("/api/v1/secrets/pull", {"node": "box1", "names": []})
    assert entry.status_code == 403 and "TLS" in entry.json()["detail"]
    assert pull.status_code == 403
    # The rest of the channel (desired state, events) still serves.
    assert rig.heartbeat().status_code == 200


def test_key_rotation_reencrypts_everything(control):
    control.write_config("stacks/worker.toml", WORKER_STACK)
    enter_secret(control)
    old_token = control.secret_store.get_secret_token("github-worker")

    new_box = SecretBox(generate_key())
    control.secret_store.replace_secret_tokens(
        {
            name: new_box.encrypt(
                control.box.decrypt(control.secret_store.get_secret_token(name) or "")
            )
            for name in control.secret_store.secret_names()
        }
    )
    new_token = control.secret_store.get_secret_token("github-worker")
    assert new_token != old_token
    assert new_box.decrypt(new_token or "") == SENTINEL
