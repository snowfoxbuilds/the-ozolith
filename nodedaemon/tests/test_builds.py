"""Derived-image builds: Dockerfile rendering and the build/skip/force rules."""

from __future__ import annotations

from daemonrig import FakeDocker, image_recipe
from theozolith_nodedaemon.builds import dockerfile_for, ensure_image, image_status


def test_dockerfile_renders_base_setup_bake_and_labels():
    recipe = image_recipe(setup=["apt-get install -y jq", "pip install uv"])
    recipe["knowledge_source"] = "https://github.com/acme/knowledge.git"
    recipe["knowledge_pin"] = "abc123"
    text = dockerfile_for(recipe, built_at="2026-07-16T00:00:00Z")

    assert f"FROM {recipe['base']}" in text
    assert "RUN apt-get install -y jq" in text
    assert "RUN pip install uv" in text
    # Knowledge Source bakes at BUILD time via the M1 CLI (NODE-SUBSTRATE.md).
    assert "theozolith-knowledge bake" in text
    assert "--source https://github.com/acme/knowledge.git" in text
    assert "--pin abc123" in text
    assert f'LABEL theozolith.instruction-hash="{recipe["instruction_hash"]}"' in text
    assert f'LABEL theozolith.base-digest="{recipe["base_digest"]}"' in text
    assert 'LABEL theozolith.built-at="2026-07-16T00:00:00Z"' in text
    # The build escalates for setup and drops privileges again.
    assert text.index("USER root") < text.index("RUN apt-get")
    assert text.rstrip().endswith("USER ozolith")


def test_no_knowledge_source_means_no_bake_line():
    text = dockerfile_for(image_recipe(), built_at="t")
    assert "theozolith-knowledge bake" not in text


def test_ensure_image_builds_skips_and_forces():
    docker = FakeDocker()
    recipe = image_recipe()

    assert ensure_image(docker, recipe, log=lambda *_: None) is True
    assert ensure_image(docker, recipe, log=lambda *_: None) is False  # tag exists
    assert ensure_image(docker, recipe, force=True, log=lambda *_: None) is True
    assert [b["no_cache"] for b in docker.builds] == [False, True]


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
