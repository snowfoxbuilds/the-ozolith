"""Agent Policy validator tests (ADR-0055): the policy-validation matrix.

The ADR's Implementation obligations bind this matrix: the motivating
attribution drop-in validates; every identity/steering key, executable-
reference key, the env block, and any unclassified key refuses naming the
file and the offending key path; the recursively closed schema refuses
unknown nested members, wrong types, and extra depth; the strict tree
shape refuses everything that is not a top-level regular *.json file; and
no error message ever echoes a document value.
"""

from __future__ import annotations

import json

import pytest
from theozolith_worker import adapters, identity, policy


def _refusal(document: object, label: str = "policy/t/x.json") -> str:
    with pytest.raises(policy.PolicyError) as err:
        policy.validate_policy_document(label, document)
    return str(err.value)


def test_attribution_drop_in_validates():
    for value in (False, True):
        policy.validate_policy_document(
            "policy/claude-defaults/attribution.json", {"attribution": {"sessionUrl": value}}
        )


def test_empty_object_validates():
    policy.validate_policy_document("policy/t/empty.json", {})


@pytest.mark.parametrize("key", identity.IDENTITY_SETTING_KEYS)
def test_identity_steering_keys_refuse(key):
    message = _refusal({key: "anything"})
    assert f"'{key}'" in message
    assert "policy/t/x.json" in message
    assert "identity/steering" in message


@pytest.mark.parametrize("key", policy.EXECUTABLE_REFERENCE_KEYS)
def test_executable_reference_keys_refuse(key):
    message = _refusal({key: "run-me"})
    assert f"'{key}'" in message
    assert "executable-reference" in message


def test_env_block_refuses():
    message = _refusal({"env": {"HARMLESS": "1"}})
    assert "'env'" in message
    assert "no admitted env classes" in message


@pytest.mark.parametrize("key", ["cleanupPeriodDays", "mcpServers", "enabledPlugins", "extraKey"])
def test_unclassified_keys_refuse(key):
    message = _refusal({key: True})
    assert f"'{key}'" in message
    assert "allowlist" in message


def test_non_string_key_refuses():
    # json.loads can never produce one, but the validator takes any parsed
    # object — a non-string key is unclassified by construction.
    assert "allowlist" in _refusal({1: True})


def test_values_are_redacted():
    sentinel = "SENTINEL-9f3a-value-must-not-leak"
    for document in (
        {"model": sentinel},
        {"hooks": sentinel},
        {"unclassified": sentinel},
        {"attribution": {"unknownMember": sentinel}},
        {"attribution": {"sessionUrl": sentinel}},
        sentinel,
    ):
        assert sentinel not in _refusal(document)


# -- the recursively closed schema (ADR-0055: the interior closes too) --------


def test_second_member_beside_session_url_refuses():
    message = _refusal({"attribution": {"sessionUrl": False, "other": True}})
    assert "attribution.other" in message


def test_session_url_string_refuses():
    message = _refusal({"attribution": {"sessionUrl": "false"}})
    assert "attribution.sessionUrl" in message
    assert "boolean" in message


def test_session_url_integer_refuses():
    # bool is checked by identity: JSON 1 is not a boolean even though
    # Python's bool subclasses int.
    message = _refusal({"attribution": {"sessionUrl": 1}})
    assert "attribution.sessionUrl" in message


def test_extra_depth_under_session_url_refuses():
    message = _refusal({"attribution": {"sessionUrl": {"deeper": False}}})
    assert "attribution.sessionUrl" in message


def test_attribution_scalar_refuses():
    message = _refusal({"attribution": True})
    assert "'attribution'" in message
    assert "object" in message


def test_non_object_documents_refuse():
    for document in (["attribution"], "attribution", 7, None, True):
        assert "JSON object" in _refusal(document)


# -- the strict tree shape ----------------------------------------------------


def _write(tree, name, document):
    (tree / name).write_text(json.dumps(document), encoding="utf-8")


def test_valid_tree_passes(tmp_path):
    tree = tmp_path / "claude-defaults"
    tree.mkdir()
    _write(tree, "attribution.json", {"attribution": {"sessionUrl": False}})
    policy.validate_policy_tree(tree, label="policy/claude-defaults")


def test_empty_tree_passes_shape(tmp_path):
    tree = tmp_path / "empty"
    tree.mkdir()
    policy.validate_policy_tree(tree, label="policy/empty")


def _tree_refusal(tree, label="policy/t"):
    with pytest.raises(policy.PolicyError) as err:
        policy.validate_policy_tree(tree, label=label)
    return str(err.value)


def test_missing_root_refuses(tmp_path):
    assert "not a directory" in _tree_refusal(tmp_path / "absent")


def test_file_root_refuses(tmp_path):
    root = tmp_path / "afile"
    root.write_text("{}", encoding="utf-8")
    assert "not a directory" in _tree_refusal(root)


def test_symlinked_root_refuses(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert "symlink" in _tree_refusal(link)


def test_subdirectory_refuses(tmp_path):
    tree = tmp_path / "t"
    (tree / "nested").mkdir(parents=True)
    message = _tree_refusal(tree)
    assert "policy/t/nested" in message
    assert "subdirectories" in message


def test_non_json_name_refuses(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "notes.txt").write_text("{}", encoding="utf-8")
    message = _tree_refusal(tree)
    assert "policy/t/notes.txt" in message
    assert "*.json" in message


def test_dot_prefixed_name_refuses(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / ".hidden.json").write_text("{}", encoding="utf-8")
    message = _tree_refusal(tree)
    assert "policy/t/.hidden.json" in message


def test_symlinked_drop_in_refuses(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    real = tmp_path / "outside.json"
    real.write_text("{}", encoding="utf-8")
    (tree / "alias.json").symlink_to(real)
    message = _tree_refusal(tree)
    assert "policy/t/alias.json" in message
    assert "symlink" in message


def test_malformed_json_refuses_without_content(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "bad.json").write_text('{"attribution": SENTINEL-broken', encoding="utf-8")
    message = _tree_refusal(tree)
    assert "policy/t/bad.json" in message
    assert "SENTINEL" not in message


def test_document_refusal_names_tree_relative_file(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    _write(tree, "steer.json", {"model": "claude-opus-5"})
    message = _tree_refusal(tree)
    assert "policy/t/steer.json" in message
    assert "'model'" in message


# -- ownership ----------------------------------------------------------------


def test_claude_adapter_owns_the_policy_module():
    # Structural ownership (ADR-0055 §2): the allowlist is adapter-owned and
    # advances only with the classification review that moves the adapter's
    # validated-CLI floor.
    assert adapters.ClaudeAdapter.POLICY_MODULE is policy
