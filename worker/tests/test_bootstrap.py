from __future__ import annotations

from typing import Any

import yaml
from theozolith_worker.bootstrap.core import bootstrap_repo
from theozolith_worker.bootstrap.vocabulary import LABELS, form_files

EXPECTED_LABEL_NAMES = {
    "draft",
    "plan_ready",
    "in_progress",
    "pr_ready",
    "blocked",
    "failed",
    "needs_human",
    "risk:low",
    "risk:medium",
    "risk:high",
    "deviation:low",
    "deviation:medium",
    "deviation:high",
    "attempt-1",
    "attempt-2",
    "attempt-3",
}

FORM_PATH = ".github/ISSUE_TEMPLATE/task.yml"


class FakeRepoClient:
    """In-memory RepoClient recording every mutation."""

    def __init__(self) -> None:
        self.labels: dict[str, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.mutations: list[tuple[str, str]] = []

    def list_labels(self) -> list[dict[str, Any]]:
        return [dict(label) for label in self.labels.values()]

    def create_label(self, name: str, color: str, description: str) -> None:
        self.mutations.append(("create_label", name))
        self.labels[name] = {"name": name, "color": color, "description": description}

    def update_label(self, name: str, color: str, description: str) -> None:
        self.mutations.append(("update_label", name))
        self.labels[name] = {"name": name, "color": color, "description": description}

    def get_file(self, path: str) -> bytes | None:
        return self.files.get(path)

    def put_file(self, path: str, content: bytes, message: str) -> None:
        self.mutations.append(("put_file", path))
        self.files[path] = content


def test_vocabulary_is_the_verbatim_label_set():
    assert {label.name for label in LABELS} == EXPECTED_LABEL_NAMES


def test_bootstrap_empty_repo_creates_exact_labels_and_forms():
    """Acceptance 4 (first half): an empty repo gets the exact label set and
    issue forms."""
    client = FakeRepoClient()

    report = bootstrap_repo(client)

    assert set(client.labels) == EXPECTED_LABEL_NAMES
    for label in LABELS:
        assert client.labels[label.name]["color"] == label.color
        assert client.labels[label.name]["description"] == label.description
    assert set(client.files) == {FORM_PATH}
    assert client.files[FORM_PATH] == form_files()[FORM_PATH]
    assert sorted(report.labels_created) == sorted(EXPECTED_LABEL_NAMES)
    assert report.files_created == [FORM_PATH]


def test_rerun_is_a_noop():
    """Acceptance 4 (second half): re-running is a no-op."""
    client = FakeRepoClient()
    bootstrap_repo(client)
    client.mutations.clear()

    report = bootstrap_repo(client)

    assert not report.changed
    assert client.mutations == []


def test_drift_is_repaired_and_foreign_labels_left_alone():
    client = FakeRepoClient()
    bootstrap_repo(client)
    client.labels["plan_ready"]["color"] = "ffffff"
    client.labels["bug"] = {"name": "bug", "color": "ee0701", "description": "not ours"}
    client.files[FORM_PATH] = b"hand-edited\n"
    client.mutations.clear()

    report = bootstrap_repo(client)

    assert report.labels_updated == ["plan_ready"]
    assert report.files_updated == [FORM_PATH]
    assert client.labels["plan_ready"]["color"] == "0e8a16"
    assert client.labels["bug"]["description"] == "not ours"
    assert client.files[FORM_PATH] == form_files()[FORM_PATH]


def test_check_mode_reports_without_writing():
    client = FakeRepoClient()

    report = bootstrap_repo(client, check=True)

    assert report.changed
    assert client.mutations == []
    assert client.labels == {}
    assert client.files == {}


def test_issue_form_prompts_and_hard_artifacts():
    form = yaml.safe_load(form_files()[FORM_PATH])
    assert form["name"] == "Task"
    assert form["labels"] == ["draft"]

    fields = {item["id"]: item for item in form["body"]}
    assert set(fields) == {
        "objective",
        "acceptance-criteria",
        "baseline-risk",
        "out-of-scope",
        "pointers",
    }
    # The two hard artifacts are required; everything else is a prompt,
    # never enforced (the human is the lint).
    assert fields["acceptance-criteria"]["validations"]["required"] is True
    assert fields["baseline-risk"]["validations"]["required"] is True
    assert fields["baseline-risk"]["attributes"]["options"] == ["low", "medium", "high"]
    for optional in ("objective", "out-of-scope", "pointers"):
        assert fields[optional]["validations"]["required"] is False


def test_cli_requires_token(monkeypatch, capsys):
    from theozolith_worker.bootstrap.cli import main

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert main(["--repo", "owner/name"]) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
