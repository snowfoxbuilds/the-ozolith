"""Idempotent bootstrap of the pipeline substrate onto a target repo.

Missing labels are created and drifted ones corrected; labels outside the
vocabulary are left alone. Issue-form files are created or restored to the
shipped content. Re-running against an already-bootstrapped repo is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from theozolith_worker.bootstrap.github import RepoClient
from theozolith_worker.bootstrap.vocabulary import LABELS, form_files

COMMIT_MESSAGE = "Bootstrap TheOzolith pipeline substrate"


@dataclass
class BootstrapReport:
    labels_created: list[str] = field(default_factory=list)
    labels_updated: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    files_updated: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.labels_created or self.labels_updated or self.files_created or self.files_updated
        )

    def summary(self) -> str:
        return (
            f"labels: {len(self.labels_created)} created, {len(self.labels_updated)} updated; "
            f"issue forms: {len(self.files_created)} created, {len(self.files_updated)} updated"
        )


def bootstrap_repo(client: RepoClient, *, check: bool = False) -> BootstrapReport:
    """Apply the label vocabulary and issue forms via `client`.

    check: report what would change without writing anything.
    """
    report = BootstrapReport()

    existing = {label["name"]: label for label in client.list_labels()}
    for label in LABELS:
        current = existing.get(label.name)
        if current is None:
            report.labels_created.append(label.name)
            if not check:
                client.create_label(label.name, label.color, label.description)
        elif (
            current.get("color", "").lower() != label.color
            or (current.get("description") or "") != label.description
        ):
            report.labels_updated.append(label.name)
            if not check:
                client.update_label(label.name, label.color, label.description)

    for path, content in sorted(form_files().items()):
        current = client.get_file(path)
        if current is None:
            report.files_created.append(path)
            if not check:
                client.put_file(path, content, COMMIT_MESSAGE)
        elif current != content:
            report.files_updated.append(path)
            if not check:
                client.put_file(path, content, COMMIT_MESSAGE)

    return report
