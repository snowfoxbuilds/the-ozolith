"""Bench-contract conformance (ADR-0054; BENCH-CONTRACT.md).

These test classes ARE contract surface (the spec's Conformance obligations
section): the golden identity vectors, the negative-verification tampering
matrix, build-lifecycle behavior, credential hygiene, source rejection, and
the version-key drift-locks. Each pins a behavior an external bench harness
may rely on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
from test_ingest import CODEX_ROLE, FULL_CODEX_ROLE, HOOKS_DOC, FakeRegistry
from theozolith_control import candidate, configrepo
from theozolith_control import ingest as ingest_mod
from theozolith_control.candidate import CandidateError
from theozolith_nodedaemon import builds
from theozolith_nodedaemon import configdist as node_configdist
from theozolith_nodedaemon.dockerctl import DockerError
from theozolith_worker import policy as worker_policy
from theozolith_worker.proposal import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_REF = "ghcr.io/acme/theozolith-run-claude:1.2.3"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NOW = "2026-08-29T00:00:00Z"


def resolve_a(ref: str) -> str:
    return DIGEST_A


POLICY_DOC = '{"attribution": {"sessionUrl": false}}\n'


def make_source(
    tmp_path,
    *,
    base: str = BASE_REF,
    driver: str = "builtin:implementer",
    adapter: str = "claude",
    model: str = "claude-sonnet-5",
    effort: str = "",
    knowledge: str = "knowledge/gold",
    policy: str = "",
    name: str = "goldtype",
    knowledge_files: dict[str, str] | None = None,
) -> Path:
    source = tmp_path / "config-src"
    (source / "worker-types").mkdir(parents=True, exist_ok=True)
    (source / "knowledge" / "gold").mkdir(parents=True, exist_ok=True)
    (source / "knowledge" / "gold" / "AGENTS.md").write_text(
        "# golden knowledge\n", encoding="utf-8"
    )
    for relpath, text in (knowledge_files or {}).items():
        target = source / "knowledge" / "gold" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if policy:
        tree = source / "policy" / policy.removeprefix("policy/")
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "attribution.json").write_text(POLICY_DOC, encoding="utf-8")
    lines = [f'base = "{base}"', 'setup = ["apt-get update && apt-get install -y ripgrep"]']
    for key, value in (
        ("driver", driver),
        ("adapter", adapter),
        ("model", model),
        ("effort", effort),
        ("knowledge", knowledge),
        ("policy", policy),
    ):
        if value:
            lines.append(f'{key} = "{value}"')
    lines += ['workspace = "acme/sandbox"', "[secrets]", 'GITHUB_TOKEN = "github-implementer"']
    (source / "worker-types" / f"{name}.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return source


def export_gold(tmp_path, out_name: str = "bundle", **source_kwargs):
    source = make_source(tmp_path, **source_kwargs)
    out = tmp_path / out_name
    summary = candidate.export_candidate(
        source,
        source_kwargs.get("name", "goldtype"),
        out,
        resolve_digest=resolve_a,
        now=lambda: NOW,
    )
    return out, summary


def rewrite_manifest(bundle: Path, **overrides) -> None:
    manifest = json.loads((bundle / candidate.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest.update(overrides)
    (bundle / candidate.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# -- version keys: stamped in code, drift-locked to the spec ---------------------


def test_contract_version_keys_are_stamped_and_drift_locked():
    """The three exactly-owned compatibility keys (BENCH-CONTRACT.md): the
    code constants, the spec's stated versions, and the published vectors
    file must all agree — a bump anywhere without the others is a failure."""
    assert candidate.BUNDLE_FORMAT_VERSION == 2  # ADR-0055: policy joins the bundle
    assert candidate.IDENTITY_SPEC_VERSION == 2  # ADR-0055: conditional policy keys
    assert SCHEMA_VERSION == 1
    spec = (REPO_ROOT / "docs" / "specs" / "BENCH-CONTRACT.md").read_text(encoding="utf-8")
    assert f"**`schema_version`** (currently {SCHEMA_VERSION}" in spec
    assert f"**`bundle_format_version`** (currently {candidate.BUNDLE_FORMAT_VERSION}" in spec
    assert f"**`identity_spec_version`** (currently {candidate.IDENTITY_SPEC_VERSION}" in spec
    vectors = json.loads(
        (REPO_ROOT / "docs" / "specs" / "bench-identity-vectors.json").read_text(encoding="utf-8")
    )
    assert vectors["identity_spec_version"] == candidate.IDENTITY_SPEC_VERSION


def test_bundle_knowledge_dir_matches_the_daemon_staging_name():
    """The bundle is a plain docker build context: its knowledge and policy
    dirs must be the exact names the daemon's shared staging writes and the
    shared codegen COPYs from."""
    assert candidate.KNOWLEDGE_SUBDIR == builds._CONTEXT_KNOWLEDGE
    assert candidate.POLICY_SUBDIR == builds._CONTEXT_POLICY


# -- golden identity vectors -----------------------------------------------------


def test_identity_vectors_recompute_through_the_production_formula():
    """Every published vector (docs/specs/bench-identity-vectors.json) must
    reproduce through the production identity machinery: materialized setup,
    canonical serialization, instruction hash, identity triple, and tag —
    with no adapter set hardcoded anywhere, the conditional knowledge_target
    key covered in both directions, and model/effort pairs that pass the
    production capability gate."""
    data = json.loads(
        (REPO_ROOT / "docs" / "specs" / "bench-identity-vectors.json").read_text(encoding="utf-8")
    )
    vectors = data["vectors"]
    assert len(vectors) >= 5
    assert len({vector["fields"]["adapter"] for vector in vectors}) >= 2
    seen_conditional_target = seen_default_target_omitted = seen_policy = False
    for vector in vectors:
        fields = vector["fields"]
        configrepo._validate_model_effort(
            f"vector {vector['name']}",
            fields["adapter"],
            fields["model"],
            fields["effort"],
            is_driver=bool(fields["driver"]),
        )
        wt = configrepo.WorkerTypeDef(
            name=fields["name"],
            base=fields["base"],
            setup=tuple(fields["setup"]),
            knowledge=fields["knowledge"],
            knowledge_pin=fields["knowledge_pin"],
            # The conditional policy keys (identity_spec_version 2, ADR-0055):
            # a vector that omits the fields is a policy-less identity.
            policy=fields.get("policy", ""),
            policy_pin=fields.get("policy_pin", ""),
            driver=fields["driver"],
            adapter=fields["adapter"],
            model=fields["model"],
            effort=fields["effort"],
        )
        expected = vector["expected"]
        assert list(wt.materialized_setup) == expected["materialized_setup"]
        assert wt.instruction_hash == expected["instruction_hash"]
        canonical = expected["canonical_identity"]
        assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == wt.instruction_hash
        parsed = json.loads(canonical)
        assert parsed["base"] == wt.base
        assert parsed["setup"] == list(wt.materialized_setup)
        assert parsed["knowledge"] == wt.baked_knowledge
        assert parsed["knowledge_pin"] == wt.baked_knowledge_pin
        if "knowledge_target" in parsed:
            assert parsed["knowledge_target"] == wt.knowledge_target
            assert wt.knowledge_target != configrepo._DEFAULT_KNOWLEDGE_TARGET
            seen_conditional_target = True
        elif wt.baked_knowledge:
            assert wt.knowledge_target == configrepo._DEFAULT_KNOWLEDGE_TARGET
            seen_default_target_omitted = True
        if "policy" in parsed:
            assert parsed["policy"] == wt.baked_policy != ""
            assert parsed["policy_pin"] == wt.baked_policy_pin != ""
            seen_policy = True
        else:
            assert wt.baked_policy == ""
        assert expected["identity_triple"] == {
            "base_digest": wt.base_digest,
            "instruction_hash": wt.instruction_hash,
            "adapter": wt.adapter,
        }
        assert wt.tag == expected["tag"]
    assert seen_conditional_target and seen_default_target_omitted and seen_policy


def test_identity_vectors_carry_the_promoted_goldens():
    """The vectors are PROMOTED from the pre-existing control-side goldens:
    the literals must stay byte-identical to test_configrepo.py's."""
    data = json.loads(
        (REPO_ROOT / "docs" / "specs" / "bench-identity-vectors.json").read_text(encoding="utf-8")
    )
    by_name = {vector["name"]: vector["expected"] for vector in data["vectors"]}
    assert by_name["claude-no-knowledge"]["instruction_hash"] == (
        "fbd115afa5ef8c384c27b2d32325de43dac5fc88c275da7d1f3bc810728eea4a"
    )
    assert by_name["claude-model-knowledge-default-target"]["instruction_hash"] == (
        "a3f5923671c0705065a7076b662ead480d86de61d1f7241aca7523a378014fcf"
    )


# -- export -> verify roundtrip --------------------------------------------------


def test_export_writes_a_verifiable_bundle(tmp_path):
    bundle, summary = export_gold(tmp_path)
    assert sorted(entry.name for entry in bundle.iterdir()) == [
        "Dockerfile",
        "candidate.json",
        "knowledge",
    ]
    verified = candidate.verify_bundle(bundle)
    assert verified == summary
    assert summary.base_digest == DIGEST_A
    assert summary.tag == f"theozolith/goldtype:1.2.3-{summary.instruction_hash[:12]}"
    dockerfile = (bundle / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {BASE_REF}@{DIGEST_A}" in dockerfile
    assert "COPY --chown=ozolith:ozolith knowledge/ /home/ozolith/.claude/" in dockerfile
    assert f'LABEL theozolith.built-at="{NOW}"' in dockerfile


def test_export_codex_type_bakes_the_codex_view(tmp_path):
    """Per-tool compile reuse (ADR-0052): a codex candidate bundles the codex
    compile of the tree (AGENTS.md verbatim, no claude marker; native agent
    roles under agents/ and hooks/ verbatim) and the non-default COPY target.
    verify_bundle recomputing the pin over the bundle proves both sections
    are pinned content."""
    bundle, summary = export_gold(
        tmp_path,
        base="ghcr.io/acme/theozolith-run-codex:1.2.3",
        driver="builtin:reviewer",
        adapter="codex",
        model="gpt-5.2-codex",
        name="codexreview",
        knowledge_files={
            "agents/codex/grunt.toml": CODEX_ROLE,
            "agents/codex/scout.toml": FULL_CODEX_ROLE,
            "hooks/hooks.json": HOOKS_DOC,
        },
    )
    manifest = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    assert manifest["knowledge_target"] == "/home/ozolith/.codex/"
    assert (bundle / "knowledge" / "AGENTS.md").is_file()
    assert not (bundle / "knowledge" / "CLAUDE.md").exists()
    agents = bundle / "knowledge" / "agents"
    assert (agents / "grunt.toml").read_text(encoding="utf-8") == CODEX_ROLE
    assert (agents / "scout.toml").read_text(encoding="utf-8") == FULL_CODEX_ROLE
    assert (bundle / "knowledge" / "hooks" / "hooks.json").read_text(encoding="utf-8") == HOOKS_DOC
    dockerfile = (bundle / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=ozolith:ozolith knowledge/ /home/ozolith/.codex/" in dockerfile
    assert candidate.verify_bundle(bundle) == summary


def test_export_of_a_driverless_type_declaring_tmpfs_omits_it_from_the_manifest(tmp_path):
    """tmpfs is node-side runtime state, never image identity (exactly parallel
    to command/volumes, which also do not travel): a driverless type declaring
    it exports fine — the shared parse accepts the field — and the bundle
    manifest carries no ``tmpfs`` key, so verify's unknown-field refusal is
    untouched and a verified build sees identical bytes (#109)."""
    source = tmp_path / "config-src"
    (source / "worker-types").mkdir(parents=True)
    (source / "worker-types" / "deck.toml").write_text(
        f'base = "{BASE_REF}"\nsetup = ["apt-get update"]\n'
        'adapter = "claude"\ncommand = "sleep 30"\n'
        'tmpfs = ["/tmp:size=8g", "/scratch"]\n'
        'workspace = "acme/sandbox"\n'
        '[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
        encoding="utf-8",
    )
    out = tmp_path / "bundle"
    summary = candidate.export_candidate(
        source, "deck", out, resolve_digest=resolve_a, now=lambda: NOW
    )
    manifest = json.loads((out / "candidate.json").read_text(encoding="utf-8"))
    assert manifest["driver"] == ""  # driverless
    assert "tmpfs" not in manifest  # runtime state, not image identity
    # No bundle byte carries the field either — it never reached the context.
    for path in out.rglob("*"):
        if path.is_file():
            assert b"tmpfs" not in path.read_bytes()
    assert candidate.verify_bundle(out) == summary  # unknown-field refusal intact


def test_export_without_knowledge_writes_no_tree(tmp_path):
    bundle, summary = export_gold(tmp_path, knowledge="")
    assert sorted(entry.name for entry in bundle.iterdir()) == ["Dockerfile", "candidate.json"]
    manifest = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    assert manifest["knowledge"] == "" and manifest["knowledge_pin"] == ""
    assert manifest["knowledge_target"] == ""
    assert "COPY" not in (bundle / "Dockerfile").read_text(encoding="utf-8")
    assert candidate.verify_bundle(bundle) == summary


def test_export_is_deterministic_for_a_fixed_timestamp(tmp_path):
    bundle_a, _ = export_gold(tmp_path, out_name="bundle-a")
    bundle_b, _ = export_gold(tmp_path, out_name="bundle-b")
    files_a = sorted(p.relative_to(bundle_a) for p in bundle_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(bundle_b) for p in bundle_b.rglob("*") if p.is_file())
    assert files_a == files_b
    for relpath in files_a:
        assert (bundle_a / relpath).read_bytes() == (bundle_b / relpath).read_bytes()


def test_export_ships_secret_slot_names_never_stored_names(tmp_path):
    """Slot names travel so a consumer knows what to bind; the deployment's
    stored secret NAMES (let alone values) reach no bundle byte."""
    bundle, _ = export_gold(tmp_path)
    manifest = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    assert manifest["secret_slots"] == ["GITHUB_TOKEN"]
    for path in bundle.rglob("*"):
        if path.is_file():
            assert b"github-implementer" not in path.read_bytes()


def test_export_refuses_a_nonempty_output_directory(tmp_path):
    source = make_source(tmp_path)
    out = tmp_path / "occupied"
    out.mkdir()
    (out / "leftover").write_text("x", encoding="utf-8")
    with pytest.raises(CandidateError, match="new or empty"):
        candidate.export_candidate(source, "goldtype", out, resolve_digest=resolve_a)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_a_failed_export_leaves_no_partial_destination_and_retries(tmp_path, monkeypatch, failure):
    """Atomic publication: the bundle assembles in hidden staging beside the
    destination and lands in one rename — a mid-assembly failure or
    interruption leaves a NEW destination absent, a PRE-EXISTING empty
    destination empty, no staging litter beside them, and the very same
    destination retries immediately."""
    source = make_source(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}

    def boom(recipe, built_at):
        raise failure()

    monkeypatch.setattr(candidate.builds, "dockerfile_for", boom)
    fresh = tmp_path / "fresh-out"
    with pytest.raises(failure):
        candidate.export_candidate(source, "goldtype", fresh, resolve_digest=resolve_a)
    assert not fresh.exists()

    existing = tmp_path / "existing-out"
    existing.mkdir()
    with pytest.raises(failure):
        candidate.export_candidate(source, "goldtype", existing, resolve_digest=resolve_a)
    assert existing.is_dir() and list(existing.iterdir()) == []
    assert {p.name for p in tmp_path.iterdir()} == before | {"existing-out"}

    monkeypatch.undo()
    for out in (fresh, existing):
        summary = candidate.export_candidate(
            source, "goldtype", out, resolve_digest=resolve_a, now=lambda: NOW
        )
        assert candidate.verify_bundle(out) == summary


def test_a_destination_filled_during_export_refuses_at_publication(tmp_path, monkeypatch):
    """The rename is the only write to the final path: a destination that
    gains content while the export runs fails the publication and receives
    nothing — there is no incremental copy to interleave with."""
    source = make_source(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    real = candidate.builds.dockerfile_for

    def fill_then_render(recipe, built_at):
        (out / "intruder").write_text("x", encoding="utf-8")
        return real(recipe, built_at)

    monkeypatch.setattr(candidate.builds, "dockerfile_for", fill_then_render)
    with pytest.raises(CandidateError, match="cannot publish"):
        candidate.export_candidate(source, "goldtype", out, resolve_digest=resolve_a)
    assert [p.name for p in out.iterdir()] == ["intruder"]


def test_reexport_after_a_moved_tag_yields_a_new_identity(tmp_path):
    """Build lifecycle (BENCH-CONTRACT.md): export re-resolves the base at
    export time, so a moved tag re-exports to a new digest, identity, and
    deterministic tag."""
    source = make_source(tmp_path)
    first = candidate.export_candidate(
        source, "goldtype", tmp_path / "b1", resolve_digest=resolve_a, now=lambda: NOW
    )
    second = candidate.export_candidate(
        source, "goldtype", tmp_path / "b2", resolve_digest=lambda ref: DIGEST_B, now=lambda: NOW
    )
    assert first.base_digest != second.base_digest
    assert first.instruction_hash != second.instruction_hash
    assert first.tag != second.tag


# -- source rejection ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://github.com/acme/config.git", "git@github.com:acme/config.git", "ssh://host/repo"],
)
def test_url_sources_are_rejected_with_the_clone_it_yourself_error(tmp_path, url):
    with pytest.raises(CandidateError, match="clone the repository yourself"):
        candidate.export_candidate(url, "goldtype", tmp_path / "out", resolve_digest=resolve_a)


def test_absent_and_non_directory_sources_are_rejected(tmp_path):
    with pytest.raises(CandidateError, match="does not exist"):
        candidate.export_candidate(
            tmp_path / "nope", "goldtype", tmp_path / "out", resolve_digest=resolve_a
        )
    plain = tmp_path / "file.toml"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(CandidateError, match="not a directory"):
        candidate.export_candidate(plain, "goldtype", tmp_path / "out", resolve_digest=resolve_a)


def test_worker_type_name_shape_is_enforced_before_any_path_join(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(CandidateError, match="must match"):
        candidate.export_candidate(source, "../evil", tmp_path / "out", resolve_digest=resolve_a)


def test_missing_worker_type_and_symlinked_definition_are_rejected(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(CandidateError, match=r"has no worker-types/ghost\.toml"):
        candidate.export_candidate(source, "ghost", tmp_path / "out", resolve_digest=resolve_a)
    (source / "worker-types" / "linked.toml").symlink_to(source / "worker-types" / "goldtype.toml")
    with pytest.raises(CandidateError, match="symlinks are refused"):
        candidate.export_candidate(source, "linked", tmp_path / "out2", resolve_digest=resolve_a)


def test_unsafe_knowledge_trees_are_rejected(tmp_path):
    """The ingest compile machinery's fail-closed source walk is the export
    gate too: a symlinked knowledge root refuses."""
    source = make_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "knowledge" / "evil").symlink_to(outside)
    with pytest.raises(CandidateError, match="knowledge/evil"):
        candidate.export_candidate(source, "goldtype", tmp_path / "out", resolve_digest=resolve_a)


# -- Agent Policy in the bundle (ADR-0055) ---------------------------------------


def test_export_bundles_and_verifies_a_baked_policy_tree(tmp_path):
    """A policy-declaring driver type exports the tree under policy/, the
    Dockerfile carries the managed-drop-in COPY, the manifest records the
    baked view, and verification recomputes pin and identity."""
    bundle, summary = export_gold(tmp_path, policy="policy/claude-defaults")
    assert sorted(entry.name for entry in bundle.iterdir()) == [
        "Dockerfile",
        "candidate.json",
        "knowledge",
        "policy",
    ]
    assert (bundle / "policy" / "attribution.json").read_text() == POLICY_DOC
    manifest = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    assert manifest["policy"] == "policy/claude-defaults"
    assert manifest["policy_pin"]
    dockerfile = (bundle / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY policy/ /etc/claude-code/managed-settings.d/" in dockerfile
    assert dockerfile.index("COPY policy/") < dockerfile.index("RUN apt-get")
    assert candidate.verify_bundle(bundle) == summary
    # The identity gained the conditional keys: a policy-less twin differs.
    _, plain = export_gold(tmp_path, out_name="bundle-plain")
    assert summary.instruction_hash != plain.instruction_hash


def test_export_refuses_an_unadmitted_policy_key(tmp_path):
    """The shared safe-key allowlist gates export exactly as it gates ingest
    and config load (ADR-0055)."""
    source = make_source(tmp_path, policy="policy/claude-defaults")
    (source / "policy" / "claude-defaults" / "steer.json").write_text(
        '{"model": "claude-opus-5"}\n', encoding="utf-8"
    )
    with pytest.raises(CandidateError, match=r"steer\.json.*'model'"):
        candidate.export_candidate(source, "goldtype", tmp_path / "out", resolve_digest=resolve_a)


def test_export_refuses_a_cli_declaring_type_and_the_manifest_never_gained_cli(tmp_path):
    """The CLI Pin is a Flight Deck live surface, never bundle identity
    (ADR-0055): export refuses the field early with an actionable message —
    never the confusing missing-pin parse error — and the manifest key set
    and bundle_format_version deliberately do not move."""
    source = make_source(tmp_path, driver="", model="", knowledge="", name="deck")
    path = source / "worker-types" / "deck.toml"
    # Prepended: an append would land inside the trailing [secrets] table.
    path.write_text('cli = "2.1.260"\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(CandidateError, match="live surface, never Candidate Bundle identity"):
        candidate.export_candidate(source, "deck", tmp_path / "out", resolve_digest=resolve_a)
    assert not any(key.startswith("cli") for key in candidate._MANIFEST_KEYS)
    assert candidate.BUNDLE_FORMAT_VERSION == 2  # unchanged by the CLI Pin


def test_tampering_with_policy_bytes_fails_the_pin_before_docker(tmp_path):
    """One flipped drop-in byte refuses at verification — before any Docker
    invocation (the verified build runs verify on its private snapshot
    first, so the refusal precedes the build there too)."""
    bundle, _ = export_gold(tmp_path, policy="policy/claude-defaults")
    target = bundle / "policy" / "attribution.json"
    original = target.read_bytes()
    mutated = bytearray(original)
    mutated[-2] ^= 0x01
    target.write_bytes(bytes(mutated))
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(bundle)
    target.write_bytes(original)
    candidate.verify_bundle(bundle)

    extra = bundle / "policy" / "smuggled.json"
    extra.write_text("{}", encoding="utf-8")
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(bundle)


def _export_coherent_malicious_bundle(tmp_path, dropin: str, document: str) -> Path:
    """Craft the bundle a malicious PRODUCER would ship: disable the shared
    validator during export only, so the production machinery itself
    recomputes the policy pin, the conditional instruction hash, every
    manifest field, and the Dockerfile over the forbidden drop-in. The result
    is fully self-consistent — pin matches bytes, identity matches pin — and
    only the verifier's own allowlist enforcement can refuse it."""
    source = make_source(tmp_path, policy="policy/claude-defaults")
    (source / "policy" / "claude-defaults" / dropin).write_text(document, encoding="utf-8")
    out = tmp_path / "malicious-bundle"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker_policy, "validate_policy_tree", lambda *args, **kwargs: None)
        candidate.export_candidate(
            source, "goldtype", out, resolve_digest=resolve_a, now=lambda: NOW
        )
    return out


@pytest.mark.parametrize(
    ("dropin", "document", "key", "value"),
    [
        (
            "hooks.json",
            '{"hooks": {"PostToolUse": [{"command": "curl-evil-payload"}]}}\n',
            "hooks",
            "curl-evil-payload",
        ),
        ("steer.json", '{"model": "smuggled-model-id"}\n', "model", "smuggled-model-id"),
        (
            "extra.json",
            '{"unclassifiedKnob": "smuggled-value"}\n',
            "unclassifiedKnob",
            "smuggled-value",
        ),
    ],
)
def test_a_self_consistent_malicious_policy_bundle_is_refused_before_docker(
    tmp_path, dropin, document, key, value
):
    """A matching pin proves byte consistency, not admissibility: a bundle
    whose pin, instruction hash, manifest, and Dockerfile are all coherently
    recomputed over a forbidden drop-in must still refuse — the verifier
    enforces the shared safe-key allowlist independently, naming the file and
    key with the value redacted, before Docker is ever invoked (ADR-0055)."""
    bundle = _export_coherent_malicious_bundle(tmp_path, dropin, document)
    # Sanity: the bundle really is internally consistent — the recorded pin
    # matches the tree bytes, so only the allowlist can catch it.
    manifest = json.loads((bundle / candidate.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert node_configdist.tree_hash(bundle / "policy") == manifest["policy_pin"]
    with pytest.raises(CandidateError, match=rf"{dropin}.*'{key}'") as refusal:
        candidate.verify_bundle(bundle)
    assert value not in str(refusal.value)
    # The verified build refuses on its private snapshot the same way, and
    # Docker is never invoked.
    fake = FakeDocker()
    with pytest.raises(CandidateError, match=rf"{dropin}.*'{key}'"):
        candidate.build_candidate(bundle, docker=fake)
    assert fake.builds == []


def test_policy_layout_entry_without_a_declared_policy_is_refused(tmp_path):
    bundle, _ = export_gold(tmp_path)
    (bundle / "policy").mkdir()
    (bundle / "policy" / "attribution.json").write_text(POLICY_DOC, encoding="utf-8")
    with pytest.raises(CandidateError, match="carries a policy/ entry"):
        candidate.verify_bundle(bundle)


def test_driverless_manifest_with_a_baked_policy_is_refused(tmp_path):
    """The manifest carries the BAKED view: a driverless candidate can never
    declare a baked policy (ADR-0055)."""
    bundle, _ = export_gold(tmp_path, knowledge="", policy="policy/claude-defaults")
    rewrite_manifest(bundle, driver="")
    with pytest.raises(CandidateError, match="driverless candidate bakes no policy"):
        candidate.verify_bundle(bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy", "policy/evil"),
        ("policy", ""),
        ("policy_pin", "f" * 64),
        ("policy_pin", ""),
    ],
)
def test_tampering_with_policy_manifest_fields_fails_verification(tmp_path, field, value):
    bundle, _ = export_gold(tmp_path, policy="policy/claude-defaults")
    rewrite_manifest(bundle, **{field: value})
    with pytest.raises(CandidateError):
        candidate.verify_bundle(bundle)


# -- negative verification: the tampering matrix ---------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base", f"ghcr.io/acme/theozolith-run-claude:1.2.3@{DIGEST_B}"),
        ("base_digest", DIGEST_B),
        ("setup", ["echo pwned"]),
        ("model", "claude-opus-5"),
        ("effort", "high"),
        ("adapter", "codex"),
        ("knowledge", "knowledge/evil"),
        ("knowledge_pin", "f" * 64),
        ("knowledge_target", "/home/ozolith/.codex/"),
        ("instruction_hash", "f" * 64),
        ("exported_at", "2027-01-01T00:00:00Z"),
        ("driver", ""),
        ("secret_slots", ["THEOZOLITH_MODEL"]),
        ("product_version", ""),
    ],
)
def test_tampering_with_a_manifest_field_fails_verification(tmp_path, field, value):
    """Independent tampering with every identity-bearing manifest field (and
    the guarded metadata fields) must refuse — identity is recomputed from
    bundle bytes, never trusted."""
    bundle, _ = export_gold(tmp_path)
    rewrite_manifest(bundle, **{field: value})
    with pytest.raises(CandidateError):
        candidate.verify_bundle(bundle)


def test_tampering_with_knowledge_bytes_or_layout_fails_the_pin(tmp_path):
    bundle, _ = export_gold(tmp_path)
    target = bundle / "knowledge" / "CLAUDE.md"
    original = target.read_bytes()

    target.write_bytes(original + b"x")
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(bundle)
    target.write_bytes(original)
    candidate.verify_bundle(bundle)

    extra = bundle / "knowledge" / "smuggled.md"
    extra.write_text("payload", encoding="utf-8")
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(bundle)
    extra.unlink()

    # The exec bit is hash-bearing (entry_mode): a chmod is a content change.
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(bundle)


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace("FROM ghcr.io", "FROM evil.example"),
        lambda text: text.replace("USER root", "USER root\nRUN echo pwned"),
        lambda text: text.replace("RUN chmod -R a+rwX /home/ozolith\n", ""),
        lambda text: text.replace("/home/ozolith/.claude/", "/home/ozolith/.evil/"),
        lambda text: text.replace(
            'LABEL theozolith.base-digest="sha256:a', 'LABEL theozolith.base-digest="sha256:b'
        ),
        lambda text: text.replace("USER ozolith", "USER root"),
        lambda text: text + "\n",
    ],
)
def test_tampering_with_the_dockerfile_fails_the_byte_match(tmp_path, transform):
    bundle, _ = export_gold(tmp_path)
    dockerfile = bundle / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    mutated = transform(original)
    assert mutated != original, "transform must change the Dockerfile"
    dockerfile.write_text(mutated, encoding="utf-8")
    with pytest.raises(CandidateError, match="byte-match"):
        candidate.verify_bundle(bundle)


@pytest.mark.parametrize("missing", [key for key in candidate._MANIFEST_KEYS])
def test_a_missing_manifest_field_is_refused(tmp_path, missing):
    bundle, _ = export_gold(tmp_path)
    manifest = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    del manifest[missing]
    (bundle / "candidate.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(CandidateError, match=r"candidate\.json"):
        candidate.verify_bundle(bundle)


def test_unknown_duplicate_and_malformed_manifest_fields_are_refused(tmp_path):
    bundle, _ = export_gold(tmp_path)
    manifest_path = bundle / "candidate.json"
    pristine = manifest_path.read_text(encoding="utf-8")

    rewrite_manifest(bundle, smuggled=1)
    with pytest.raises(CandidateError, match="unknown fields: smuggled"):
        candidate.verify_bundle(bundle)

    manifest_path.write_text(
        pristine.replace('{\n  "adapter"', '{\n  "adapter": "codex",\n  "adapter"', 1),
        encoding="utf-8",
    )
    with pytest.raises(CandidateError, match="duplicate field 'adapter'"):
        candidate.verify_bundle(bundle)

    manifest_path.write_text("not json", encoding="utf-8")
    with pytest.raises(CandidateError, match="does not parse as JSON"):
        candidate.verify_bundle(bundle)

    manifest_path.write_text('["a list"]', encoding="utf-8")
    with pytest.raises(CandidateError, match="JSON object"):
        candidate.verify_bundle(bundle)

    manifest_path.write_text(pristine, encoding="utf-8")
    for field, value in (
        ("setup", "not-a-list"),
        ("secret_slots", ["B", "A"]),
        ("secret_slots", ["A", "A"]),
        ("bundle_format_version", True),
        ("exported_at", "yesterday"),
        ("base", "ghcr.io/acme/run:1.2.3"),
        ("instruction_hash", "short"),
    ):
        rewrite_manifest(bundle, **{field: value})
        with pytest.raises(CandidateError, match=r"candidate\.json"):
            candidate.verify_bundle(bundle)
        manifest_path.write_text(pristine, encoding="utf-8")


@pytest.mark.parametrize("key", ["bundle_format_version", "identity_spec_version"])
@pytest.mark.parametrize("version", [1, 3])
def test_unsupported_versions_are_refused_outright(tmp_path, key, version):
    """Older AND newer stamps refuse with the re-export message — a
    v1-stamped bundle predates the ADR-0055 policy keys and is never
    reinterpreted (no compatibility windows, BENCH-CONTRACT.md)."""
    bundle, _ = export_gold(tmp_path)
    rewrite_manifest(bundle, **{key: version})
    with pytest.raises(CandidateError, match="unsupported"):
        candidate.verify_bundle(bundle)


def test_unexpected_entries_symlinks_and_special_files_are_refused(tmp_path):
    bundle, _ = export_gold(tmp_path)

    extra = bundle / "extra.txt"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(CandidateError, match=r"unexpected entries: extra\.txt"):
        candidate.verify_bundle(bundle)
    extra.unlink()

    # A symlinked Dockerfile whose TARGET byte-matches still refuses: the
    # layout allowlist authenticates shape, not just content.
    twin = tmp_path / "dockerfile-copy"
    shutil.copy2(bundle / "Dockerfile", twin)
    (bundle / "Dockerfile").unlink()
    (bundle / "Dockerfile").symlink_to(twin)
    with pytest.raises(CandidateError, match="regular file"):
        candidate.verify_bundle(bundle)
    (bundle / "Dockerfile").unlink()
    shutil.copy2(twin, bundle / "Dockerfile")
    candidate.verify_bundle(bundle)

    outside = tmp_path / "outside.md"
    outside.write_text("# golden knowledge\n", encoding="utf-8")
    link = bundle / "knowledge" / "linked.md"
    link.symlink_to(outside)
    with pytest.raises(CandidateError):
        candidate.verify_bundle(bundle)
    link.unlink()

    fifo = bundle / "knowledge" / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(CandidateError):
        candidate.verify_bundle(bundle)
    fifo.unlink()
    candidate.verify_bundle(bundle)


def test_an_empty_knowledge_ref_forbids_a_knowledge_tree(tmp_path):
    bundle, _ = export_gold(tmp_path, knowledge="")
    (bundle / "knowledge").mkdir()
    (bundle / "knowledge" / "CLAUDE.md").write_text("payload", encoding="utf-8")
    with pytest.raises(CandidateError, match="no knowledge tree may be present"):
        candidate.verify_bundle(bundle)


# -- build lifecycle -------------------------------------------------------------


class FakeDocker:
    def __init__(self, *, fail: bool = False, on_build=None):
        self.builds: list[dict] = []
        self.fail = fail
        self.on_build = on_build

    def build(self, context_dir, tag, *, no_cache=False, docker_config=None):
        if self.on_build is not None:
            self.on_build(Path(context_dir))
        self.builds.append(
            {
                "context": Path(context_dir),
                "tag": tag,
                "no_cache": no_cache,
                "docker_config": docker_config,
            }
        )
        if self.fail:
            raise DockerError("docker build failed: boom")


@pytest.fixture
def tracked_tmp(tmp_path, monkeypatch):
    """Route candidate build staging into a private observable tempdir so
    cleanup on every exit path is assertable."""
    private = tmp_path / "staging-root"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


def test_an_invalid_bundle_never_invokes_docker(tmp_path, tracked_tmp):
    bundle, _ = export_gold(tmp_path)
    rewrite_manifest(bundle, instruction_hash="f" * 64)
    fake = FakeDocker()
    with pytest.raises(CandidateError, match="recomputed"):
        candidate.build_candidate(bundle, docker=fake)
    assert fake.builds == []
    assert list(tracked_tmp.iterdir()) == []


def test_build_verifies_and_builds_the_private_snapshot(tmp_path, tracked_tmp):
    """The wrapper builds the SAME snapshot it verified: the caller's
    directory is never the build context, and mutating it after staging
    cannot affect the build (the verify-then-mutate race is closed)."""
    bundle, summary = export_gold(tmp_path)
    knowledge_file = bundle / "knowledge" / "CLAUDE.md"
    pristine_knowledge = knowledge_file.read_bytes()
    observed: dict = {}

    def mutate_caller_and_observe(context: Path) -> None:
        observed["context"] = context
        observed["dockerfile"] = (context / "Dockerfile").read_bytes()
        observed["knowledge"] = (context / "knowledge" / "CLAUDE.md").read_bytes()
        knowledge_file.write_bytes(b"tampered after staging")
        (bundle / "planted.txt").write_text("x", encoding="utf-8")

    fake = FakeDocker(on_build=mutate_caller_and_observe)
    built = candidate.build_candidate(bundle, docker=fake, docker_config=Path("/cfg"))
    assert built == summary
    assert fake.builds[0]["tag"] == summary.tag
    assert fake.builds[0]["docker_config"] == Path("/cfg")
    assert observed["context"] != bundle
    assert observed["knowledge"] == pristine_knowledge
    assert not (observed["context"] / "planted.txt").exists()
    assert observed["dockerfile"] == (bundle / "Dockerfile").read_bytes()
    assert list(tracked_tmp.iterdir()) == []  # snapshot cleaned up on success


def test_build_cleans_up_on_docker_failure_and_interruption(tmp_path, tracked_tmp):
    bundle, _ = export_gold(tmp_path)
    failing = FakeDocker(fail=True)
    with pytest.raises(CandidateError, match="docker build"):
        candidate.build_candidate(bundle, docker=failing)
    assert list(tracked_tmp.iterdir()) == []

    def interrupt(context: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        candidate.build_candidate(bundle, docker=FakeDocker(on_build=interrupt))
    assert list(tracked_tmp.iterdir()) == []


def test_a_symlink_swapped_in_during_staging_is_refused_and_never_copied(tmp_path, monkeypatch):
    """Deterministic replay of the copy race the descriptor walk closes: the
    knowledge file is swapped for a symlink to an outside secret AFTER the
    directory listing is taken but BEFORE the entry is opened. The
    O_NOFOLLOW open fails on the swapped-in link, so the secret's bytes
    never enter staging — and the raced bundle never reaches Docker."""
    bundle, _ = export_gold(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("outside-bundle-secret", encoding="utf-8")
    victim = bundle / "knowledge" / "CLAUDE.md"

    real_listdir = os.listdir
    state = {"swapped": False}

    def swapping_listdir(path="."):
        names = real_listdir(path)
        if not state["swapped"] and "CLAUDE.md" in names:
            victim.unlink()
            victim.symlink_to(secret)
            state["swapped"] = True
        return names

    monkeypatch.setattr(os, "listdir", swapping_listdir)
    dest = tmp_path / "snapshot"
    with pytest.raises(CandidateError, match="is a symlink — refused before staging"):
        candidate._snapshot_bundle(bundle, dest)
    monkeypatch.undo()
    assert state["swapped"]
    for path in dest.rglob("*"):
        if path.is_file():
            assert b"outside-bundle-secret" not in path.read_bytes()
    assert not (dest / "knowledge" / "CLAUDE.md").exists()

    fake = FakeDocker()
    with pytest.raises(CandidateError, match="symlink"):
        candidate.build_candidate(bundle, docker=fake)
    assert fake.builds == []


def test_a_symlinked_bundle_root_is_refused(tmp_path, tracked_tmp):
    bundle, _ = export_gold(tmp_path)
    link = tmp_path / "linked-bundle"
    link.symlink_to(bundle)
    fake = FakeDocker()
    with pytest.raises(CandidateError, match="is a symlink"):
        candidate.build_candidate(link, docker=fake)
    assert fake.builds == []
    assert list(tracked_tmp.iterdir()) == []


def test_snapshot_copy_preserves_the_exec_bit_the_pin_covers(tmp_path):
    """The one mode bit tree_hash classifies survives the descriptor copy,
    normalized to exactly the 755/644 classes the pin distinguishes."""
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    script = source / "sub" / "tool.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o750)
    (source / "plain.txt").write_text("x", encoding="utf-8")
    dest = tmp_path / "snap"
    candidate._snapshot_bundle(source, dest)
    assert (dest / "sub" / "tool.sh").stat().st_mode & 0o777 == 0o755
    assert (dest / "plain.txt").stat().st_mode & 0o777 == 0o644
    assert (dest / "sub" / "tool.sh").read_bytes() == b"#!/bin/sh\n"


def test_a_docker_timeout_normalizes_to_a_candidate_error(tmp_path, tracked_tmp):
    """The spec's cleanup-after-timeout obligation: TimeoutExpired from the
    docker subprocess surfaces as a CandidateError naming the tag and the
    budget — the CLI's normal `error:` result, never a traceback — and the
    staging directory is gone."""
    bundle, summary = export_gold(tmp_path)

    def hang(context):
        raise subprocess.TimeoutExpired(cmd=["docker", "build"], timeout=3600)

    with pytest.raises(CandidateError, match="timed out after 3600 seconds") as excinfo:
        candidate.build_candidate(bundle, docker=FakeDocker(on_build=hang))
    assert summary.tag in str(excinfo.value)
    assert list(tracked_tmp.iterdir()) == []


def test_a_cleanup_failure_after_success_is_surfaced_not_swallowed(
    tmp_path, tracked_tmp, monkeypatch
):
    bundle, summary = export_gold(tmp_path)
    fake = FakeDocker()

    def refuse(path, *args, **kwargs):
        raise OSError("busy")

    monkeypatch.setattr(candidate.shutil, "rmtree", refuse)
    with pytest.raises(CandidateError, match="could not be removed") as excinfo:
        candidate.build_candidate(bundle, docker=fake)
    assert summary.tag in str(excinfo.value)  # the message states the build DID succeed
    assert len(fake.builds) == 1


def test_a_cleanup_failure_never_masks_the_primary_failure(tmp_path, tracked_tmp, monkeypatch):
    bundle, _ = export_gold(tmp_path)

    def refuse(path, *args, **kwargs):
        raise OSError("busy")

    monkeypatch.setattr(candidate.shutil, "rmtree", refuse)
    with pytest.raises(CandidateError, match=r"docker build of .* failed") as excinfo:
        candidate.build_candidate(bundle, docker=FakeDocker(fail=True))
    assert any("could not be removed" in note for note in excinfo.value.__notes__)


def test_repeat_builds_keep_the_deterministic_identity_and_tag(tmp_path, tracked_tmp):
    bundle, summary = export_gold(tmp_path)
    fake = FakeDocker()
    first = candidate.build_candidate(bundle, docker=fake)
    second = candidate.build_candidate(bundle, docker=fake, no_cache=True)
    assert first == second == summary
    assert [entry["tag"] for entry in fake.builds] == [summary.tag, summary.tag]
    assert fake.builds[1]["no_cache"] is True


def test_a_damaged_archived_and_restored_bundle_is_refused(tmp_path):
    """Archive round-trip fidelity: an exact tar restore verifies; a restore
    that lost a byte or a mode fails until replaced or re-exported."""
    bundle, _ = export_gold(tmp_path)
    archive = tmp_path / "bundle.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(bundle, arcname="bundle")
    restored_root = tmp_path / "restored"
    with tarfile.open(archive) as tar:
        tar.extractall(restored_root, filter="tar")
    restored = restored_root / "bundle"
    assert candidate.verify_bundle(restored) == candidate.verify_bundle(bundle)

    damaged = restored / "knowledge" / "CLAUDE.md"
    damaged.write_bytes(damaged.read_bytes()[:-1])
    with pytest.raises(CandidateError, match="pin"):
        candidate.verify_bundle(restored)


# -- credentials (ADR-0049 Docker-compatible model) ------------------------------


def _docker_config_dir(tmp_path, payload: dict) -> Path:
    config_dir = tmp_path / "docker-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return config_dir


def test_public_base_resolution_stays_anonymous(tmp_path, monkeypatch):
    registry = FakeRegistry(private=False)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    source = make_source(tmp_path)
    summary = candidate.export_candidate(
        source, "goldtype", tmp_path / "out", docker_config=None, now=lambda: NOW
    )
    assert summary.base_digest == registry.digest
    assert registry.token_auth is None  # the token was minted with no credential


def test_private_base_resolves_through_docker_config_static_auth(tmp_path, monkeypatch):
    registry = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    encoded = base64.b64encode(b"user:tok-secret").decode("ascii")
    config_dir = _docker_config_dir(tmp_path, {"auths": {"ghcr.io": {"auth": encoded}}})
    source = make_source(tmp_path)
    summary = candidate.export_candidate(
        source, "goldtype", tmp_path / "out", docker_config=config_dir, now=lambda: NOW
    )
    assert summary.base_digest == registry.digest
    assert registry.token_auth == f"Basic {encoded}"
    # Credential hygiene: the secret reaches no bundle byte.
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"tok-secret" not in data and encoded.encode() not in data


def test_private_base_resolves_through_a_credential_helper(tmp_path, monkeypatch):
    registry = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = bin_dir / "docker-credential-fake"
    helper.write_text(
        '#!/bin/sh\nread key\nprintf \'{"Username": "user", "Secret": "helper-secret"}\\n\'\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    config_dir = _docker_config_dir(tmp_path, {"credHelpers": {"ghcr.io": "fake"}})
    source = make_source(tmp_path)
    summary = candidate.export_candidate(
        source, "goldtype", tmp_path / "out", docker_config=config_dir, now=lambda: NOW
    )
    assert summary.base_digest == registry.digest
    assert registry.token_auth == "Basic " + base64.b64encode(b"user:helper-secret").decode()


def test_hub_credentials_are_found_under_the_legacy_index_key(tmp_path):
    encoded = base64.b64encode(b"hubuser:hubtok").decode("ascii")
    config_dir = _docker_config_dir(
        tmp_path, {"auths": {"https://index.docker.io/v1/": {"auth": encoded}}}
    )
    assert candidate._registry_credential(config_dir, "registry-1.docker.io") == "hubuser:hubtok"
    assert candidate._registry_credential(config_dir, "ghcr.io") == ""


def test_missing_credential_fails_naming_host_and_remediation(tmp_path, monkeypatch):
    registry = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    source = make_source(tmp_path)
    with pytest.raises(CandidateError) as excinfo:
        candidate.export_candidate(source, "goldtype", tmp_path / "out", docker_config=None)
    message = str(excinfo.value)
    assert "ghcr.io" in message and "DOCKER_CONFIG" in message
    assert "docker login" in message


def test_refused_credential_fails_without_echoing_the_value(tmp_path, monkeypatch):
    registry = FakeRegistry(private=True, accept_credential=False)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    encoded = base64.b64encode(b"user:tok-secret").decode("ascii")
    config_dir = _docker_config_dir(tmp_path, {"auths": {"ghcr.io": {"auth": encoded}}})
    source = make_source(tmp_path)
    with pytest.raises(CandidateError) as excinfo:
        candidate.export_candidate(source, "goldtype", tmp_path / "out", docker_config=config_dir)
    message = str(excinfo.value)
    assert "ghcr.io" in message and "refused" in message
    assert "tok-secret" not in message and encoded not in message


def test_discover_docker_config_prefers_flag_then_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "from-env"))
    assert candidate.discover_docker_config(explicit) == explicit
    assert candidate.discover_docker_config(None) == tmp_path / "from-env"
    with pytest.raises(CandidateError, match="not a directory"):
        candidate.discover_docker_config(tmp_path / "missing")


# -- CLI wiring ------------------------------------------------------------------


def test_cli_candidate_export_and_verify_wire_through(tmp_path, monkeypatch, capsys):
    from theozolith_control.cli import main as cli_main

    registry = FakeRegistry(private=False)
    monkeypatch.setattr(ingest_mod, "_urlopen", registry)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    source = make_source(tmp_path)
    out = tmp_path / "cli-bundle"
    argv = ["candidate", "export", "--source", str(source), "--type", "goldtype", "--out", str(out)]
    assert cli_main(argv) == 0
    assert (out / "candidate.json").is_file()
    assert cli_main(["candidate", "verify", str(out)]) == 0
    output = capsys.readouterr().out
    assert "deterministic tag" in output and "verifies" in output

    rewrite_manifest(out, instruction_hash="f" * 64)
    with pytest.raises(SystemExit, match="error:"):
        cli_main(["candidate", "verify", str(out)])
