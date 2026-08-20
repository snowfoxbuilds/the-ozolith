"""Derived-image builds: Dockerfile rendering, the build/skip/force rules, and
the ADR-0048 knowledge gate (COPY from the verified applied distribution)."""

from __future__ import annotations

import base64
import json

from daemonrig import FakeDocker, image_recipe
from theozolith_nodedaemon import configdist
from theozolith_nodedaemon.builds import dockerfile_for, ensure_image, image_status


def _applied_tree(tmp_path, name: str = "dev", files: dict[str, str] | None = None):
    """A verified-shaped applied config-distribution root carrying one
    compiled knowledge tree; returns (dist_root, per-tree pin)."""
    root = tmp_path / "config-dist" / ("f" * 64)
    for relpath, text in (files or {"CLAUDE.md": "# k\n", "skills/s/SKILL.md": "s\n"}).items():
        target = root / configdist.KNOWLEDGE_DIR / name / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root, configdist.knowledge_tree_hash(root, name)


def test_dockerfile_renders_base_setup_copy_and_labels():
    recipe = image_recipe(
        setup=["apt-get install -y jq", "pip install uv"],
        knowledge="knowledge/dev",
        knowledge_pin="a" * 64,
    )
    text = dockerfile_for(recipe, built_at="2026-07-16T00:00:00Z")

    assert f"FROM {recipe['base']}" in text
    assert "RUN apt-get install -y jq" in text
    assert "RUN pip install uv" in text
    # Compiled knowledge is COPIED from the staged build context at BUILD
    # time (ADR-0048) — never cloned, never installed at container start.
    assert "COPY --chown=ozolith:ozolith knowledge/ /home/ozolith/.claude/" in text
    assert "bake" not in text
    assert f'LABEL theozolith.instruction-hash="{recipe["instruction_hash"]}"' in text
    assert f'LABEL theozolith.base-digest="{recipe["base_digest"]}"' in text
    assert 'LABEL theozolith.built-at="2026-07-16T00:00:00Z"' in text
    # The build escalates for setup and drops privileges again.
    assert text.index("USER root") < text.index("RUN apt-get")
    assert text.rstrip().endswith("USER ozolith")


def test_no_knowledge_reference_means_no_copy_line():
    text = dockerfile_for(image_recipe(), built_at="t")
    assert "COPY" not in text


def test_materialize_instruction_rides_setup_verbatim():
    """ADR-0045 needs NO daemon change: control appends the synthesized
    theozolith-adapter line to the recipe's setup, and this renderer emits it
    as an ordinary RUN — the daemon stays adapter-blind, and an un-upgraded
    daemon still builds the correct bytes under the new tag."""
    materialize = (
        "theozolith-adapter materialize --adapter claude --model claude-sonnet-5 --scope managed"
    )
    recipe = image_recipe(setup=["pip install uv", materialize])
    text = dockerfile_for(recipe, built_at="t")
    assert f"RUN {materialize}" in text
    # Ordered like any setup: after the operator line, before the privilege drop.
    assert text.index("RUN pip install uv") < text.index(f"RUN {materialize}")
    assert text.index(f"RUN {materialize}") < text.index("USER ozolith")


def test_ensure_image_builds_skips_and_forces():
    docker = FakeDocker()
    recipe = image_recipe()

    assert ensure_image(docker, recipe, log=lambda *_: None) is True
    assert ensure_image(docker, recipe, log=lambda *_: None) is False  # tag exists
    assert ensure_image(docker, recipe, force=True, log=lambda *_: None) is True
    assert [b["no_cache"] for b in docker.builds] == [False, True]


def test_knowledge_recipe_builds_when_the_applied_tree_matches_the_pin(tmp_path):
    docker = FakeDocker()
    root, pin = _applied_tree(tmp_path)
    recipe = image_recipe(knowledge="knowledge/dev", knowledge_pin=pin)
    assert ensure_image(docker, recipe, log=lambda *_: None, dist_root=root) is True
    assert docker.builds  # the context was staged and the build ran


def test_knowledge_recipe_defers_without_an_applied_tree(tmp_path):
    docker = FakeDocker()
    logs: list[str] = []
    recipe = image_recipe(knowledge="knowledge/dev", knowledge_pin="a" * 64)
    assert ensure_image(docker, recipe, log=logs.append, dist_root=None) is False
    assert not docker.builds
    assert any("deferring" in line and "no verified" in line for line in logs)


def test_knowledge_recipe_defers_on_a_pin_mismatch(tmp_path):
    """The image must never bake content that disagrees with the identity it
    would be tagged under (ADR-0048): an off-pin applied tree defers — the
    distribution converges first, then the build runs."""
    docker = FakeDocker()
    logs: list[str] = []
    root, _ = _applied_tree(tmp_path)
    recipe = image_recipe(knowledge="knowledge/dev", knowledge_pin="b" * 64)
    assert ensure_image(docker, recipe, log=logs.append, dist_root=root) is False
    assert not docker.builds
    assert any("disagree" in line for line in logs)


def test_knowledge_recipe_defers_when_the_tree_is_absent(tmp_path):
    docker = FakeDocker()
    logs: list[str] = []
    root, pin = _applied_tree(tmp_path, name="other")
    recipe = image_recipe(knowledge="knowledge/dev", knowledge_pin=pin)
    assert ensure_image(docker, recipe, log=logs.append, dist_root=root) is False
    assert any("no knowledge/dev" in line for line in logs)


def test_ensure_image_threads_the_docker_config_to_build(tmp_path):
    """A private base needs the registry pull credential at build time
    (ADR-0049): ensure_image forwards the DOCKER_CONFIG dir to docker.build;
    None (the default) builds anonymously."""
    docker = FakeDocker()
    config_dir = tmp_path / "docker"
    config_dir.mkdir()
    # Compute the auth rather than embed the base64 literal — the encoded form
    # is exactly the daemon's `b64(user:token)`, and a static high-entropy
    # blob would trip the secret scanner on a throwaway fixture.
    auth = base64.b64encode(b"user:token").decode()
    (config_dir / "config.json").write_text(
        json.dumps({"auths": {"ghcr.io": {"auth": auth}}}), encoding="utf-8"
    )
    assert ensure_image(docker, image_recipe(), log=lambda *_: None, docker_config=config_dir)
    assert docker.builds[0]["docker_config"] == config_dir
    assert docker.builds[0]["auths"] == {"ghcr.io": {"auth": auth}}

    plain = FakeDocker()
    ensure_image(plain, image_recipe(setup=["pip install other"]), log=lambda *_: None)
    assert plain.builds[0]["docker_config"] is None


def test_image_status_reads_the_stamped_labels():
    docker = FakeDocker()
    recipe = image_recipe()
    ensure_image(docker, recipe, log=lambda *_: None)

    status = image_status(docker, recipe)
    assert status["name"] == recipe["name"]
    assert status["tag"] == recipe["tag"]
    assert status["instruction_hash"] == recipe["instruction_hash"]
    assert status["base_digest"] == recipe["base_digest"]
    assert status["built_at"]

    missing = image_status(docker, image_recipe(setup=["something else"]))
    assert missing["tag"] == "" and missing["instruction_hash"] == ""
