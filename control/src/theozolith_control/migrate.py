"""`theozolith config migrate` (ADR-0048 amendment): the code-assisted upgrade
path for deployments that predate config ingestion.

A pre-ADR-0048 deployment's ``configs/`` tree is BOTH things at once: the
operator's hand-edited config repo AND what the service loads. ADR-0048 splits
those roles — the human Config Repo is the source of truth, ``configs/``
becomes the machine-owned pinned build written only by ``theozolith config
ingest`` — and retires the ``knowledge_source``/``knowledge_pin`` worker-type
fields, which the upgraded ``load_config`` refuses loudly. This command runs
BEFORE the upgraded service must load that now-incompatible configuration:

1. It READS the legacy tree and never modifies it — the legacy ``configs/``
   keeps its git history, its machine-written ``control.toml`` [control]
   block, and any product pin exactly as they are; the FIRST ingest then
   commits the pinned build onto that same history (and preserves the
   [control] block per the ingest contract), so nothing machine-owned is lost.
2. It WRITES a fresh human Config Repo: every config file copied (stacks,
   worker types, drivers, compose files, ``product.toml``), except the
   machine-owned pieces — ``control.toml`` is reduced to its operator-authored
   [settings] surface (the [control] block is machine state the Config Repo
   may not author), and a ``pins.toml`` is never copied.
3. Retired ``knowledge_source``/``knowledge_pin`` lines are removed from
   worker-type files and preserved as a MIGRATION comment naming the old
   values, with a report note per type: the knowledge content itself must move
   into the Config Repo ``knowledge/<name>/`` tree (this command cannot fetch
   a remote knowledge repo — credentials and history belong to the operator)
   and be referenced as ``knowledge = "knowledge/<name>"``.
4. Legacy Flight Deck writable-clone wiring (ADR-0043 ``clone-init`` /
   ``knowledge-<type>`` volumes) is detected and reported — the replacement is
   the read-only mount pattern in ``deploy/configs-example`` — but never
   rewritten: a start script is operator content.

The migrated repo is git-initialized and committed (machine identity) when it
is not already a git repo, and the report ends with the exact next steps:
review, place knowledge trees, ``theozolith config ingest``, restart.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_control import configdist, controltoml

# The machine-owned top-level entries a human Config Repo must not start with.
_SKIP_TOP_LEVEL = (".git", controltoml.CONTROL_TOML, "pins.toml")

_LEGACY_KEYS = ("knowledge_source", "knowledge_pin")
_LEGACY_LINE = re.compile(r"^\s*(knowledge_source|knowledge_pin)\s*=")

# The retired knowledge-clone credential (ADR-0048): its stored secret can be
# deleted once no worker type references it.
_RETIRED_SECRET = "KNOWLEDGE_GIT_TOKEN"


class MigrateError(RuntimeError):
    """The migration cannot proceed; nothing has been written."""


@dataclass
class MigrateReport:
    """What one migration did — printed by the CLI, asserted by tests."""

    dest: Path
    written: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"migrated {len(self.written)} file(s) into {self.dest}"]
        lines.extend(self.notes)
        lines += [
            "",
            "Next steps (see deploy/README.md, 'Upgrading a pre-ingest deployment'):",
            "  1. For each worker type noted above, place its knowledge root in",
            '     this repo under knowledge/<name>/ and set knowledge = "knowledge/<name>".',
            "  2. Review the migrated files, then commit.",
            f"  3. Run: theozolith config ingest {self.dest}",
            "     (writes the pinned build the service loads; the legacy configs",
            "     tree keeps its history and machine-written control address)",
            "  4. Restart the control service so the upgraded code serves the",
            "     ingested build.",
        ]
        return "\n".join(lines)


def _copy_tree(legacy: Path, dest: Path, written: list[str]) -> None:
    """Copy the legacy tree verbatim minus machine-owned top-level entries and
    excluded names. Symlinks and irregular entries are refused loudly — the
    same fail-closed copy rule ingest applies to a Config Repo."""
    stack: list[tuple[Path, Path]] = [(legacy, dest)]
    while stack:
        current, target_dir = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            if configdist.excluded_part(entry.name):
                continue
            if current == legacy and entry.name in _SKIP_TOP_LEVEL:
                continue
            relpath = entry.relative_to(legacy).as_posix()
            if entry.is_symlink():
                raise MigrateError(f"legacy config entry {relpath} is a symlink — refused")
            if entry.is_dir():
                (target_dir / entry.name).mkdir(parents=True, exist_ok=True)
                stack.append((entry, target_dir / entry.name))
            elif entry.is_file():
                target = target_dir / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
                written.append(relpath)
            else:
                raise MigrateError(f"legacy config entry {relpath} is not a regular file — refused")


def _strip_legacy_knowledge(path: Path, name: str, report: MigrateReport) -> None:
    """Remove retired knowledge_source/knowledge_pin lines from one migrated
    worker-type file, preserving the old values in a MIGRATION comment at the
    first removed line, and note the required follow-up."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return  # unparseable: copied verbatim; the first ingest will fail loud
    legacy = {key: data[key] for key in _LEGACY_KEYS if key in data}
    if not legacy:
        return
    comment = [
        "# MIGRATION(ADR-0048): knowledge_source/knowledge_pin are retired —",
        "# knowledge now lives IN this Config Repo. This type referenced:",
        *(f"#   {key} = {value!r}" for key, value in legacy.items()),
        "# Place that knowledge root under knowledge/<name>/ here and set:",
        '#   knowledge = "knowledge/<name>"',
    ]
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        if _LEGACY_LINE.match(line):
            if not inserted:
                out.extend(comment)
                inserted = True
            continue
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    source = legacy.get("knowledge_source", "")
    report.notes.append(
        f"worker-types/{name}.toml: removed retired knowledge_source/knowledge_pin"
        + (f" (was {source!r})" if source else "")
        + " — move that knowledge into knowledge/<name>/ and set"
        ' knowledge = "knowledge/<name>"'
    )


def _note_legacy_deck_wiring(data: dict, name: str, report: MigrateReport) -> None:
    """Detect the retired ADR-0043 writable-clone Flight Deck pattern and the
    retired KNOWLEDGE_GIT_TOKEN credential; report, never rewrite."""
    volumes = data.get("volumes", [])
    setup = data.get("setup", [])
    setup_text = "\n".join(str(line) for line in setup) if isinstance(setup, list) else ""
    clone_volume = isinstance(volumes, list) and any(
        isinstance(v, str) and v.startswith("knowledge-") for v in volumes
    )
    if clone_volume or "clone-init" in setup_text:
        report.notes.append(
            f"worker-types/{name}.toml: uses the retired writable-clone Flight"
            " Deck knowledge pattern (ADR-0043 clone-init / knowledge-<type>"
            " volume) — adopt the read-only mount pattern: knowledge ="
            ' "knowledge/<name>" plus the /var/lib/theozolith/knowledge:ro'
            " bind (see deploy/configs-example/worker-types/flightdeck.toml)"
        )
    secrets = data.get("secrets", {})
    if isinstance(secrets, dict) and _RETIRED_SECRET in secrets:
        report.notes.append(
            f"worker-types/{name}.toml: [secrets] {_RETIRED_SECRET} is retired"
            " (ADR-0048) — knowledge no longer clones from git at container"
            " start; remove the slot and delete the stored secret"
        )


def _write_settings_only_control_toml(legacy: Path, dest: Path, report: MigrateReport) -> None:
    """The operator-authored half of the legacy control.toml: only the
    [settings] keys off their shipped defaults. The [control] block is machine
    state — it stays in the legacy tree (the future pinned build), where the
    first ingest preserves it; a Config Repo that authored it would be
    refused."""
    if not (legacy / controltoml.CONTROL_TOML).is_file():
        return
    try:
        values = controltoml.read_values(legacy)
    except controltoml.ControlTomlError as exc:
        raise MigrateError(f"legacy {controltoml.CONTROL_TOML}: {exc}") from exc
    changed = {
        setting.key: values[setting.key]
        for setting in controltoml.SETTINGS
        if values[setting.key] != setting.default
    }
    if not changed:
        report.notes.append(
            f"{controltoml.CONTROL_TOML}: no [settings] off their defaults —"
            " nothing to migrate (the machine [control] block stays in the"
            " pinned build)"
        )
        return
    lines = [
        "# Tier-2 [settings] migrated from the pre-ADR-0048 control.toml.",
        "# The machine-written [control] address block STAYS in the pinned",
        "# build (ingest preserves it) — a Config Repo may not author it.",
        "",
        "[settings]",
    ]
    lines.extend(f"{key} = {_toml_literal(value)}" for key, value in changed.items())
    (dest / controltoml.CONTROL_TOML).write_text("\n".join(lines) + "\n", encoding="utf-8")
    report.written.append(controltoml.CONTROL_TOML)
    report.notes.append(
        f"{controltoml.CONTROL_TOML}: migrated the [settings] surface"
        f" ({', '.join(sorted(changed))}); the [control] block stays machine-owned"
    )


def _toml_literal(value) -> str:
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


def _git_init_and_commit(dest: Path, runner, report: MigrateReport) -> None:
    if (dest / ".git").exists():
        return
    for argv in (
        ["git", "init", "--quiet"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            f"user.name={controltoml.COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={controltoml.COMMIT_AUTHOR_EMAIL}",
            "commit",
            "--quiet",
            "-m",
            "theozolith config migrate: pre-ADR-0048 configs -> Config Repo",
        ],
    ):
        proc = runner(argv, cwd=str(dest), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise MigrateError(f"could not git-commit the migrated repo: {detail}")
    report.notes.append("initialized the migrated repo as git and committed the import")


def migrate_legacy(legacy_dir: Path, dest: Path, *, runner=None, log=print) -> MigrateReport:
    """Migrate a pre-ADR-0048 ``configs/`` tree into a human Config Repo at
    ``dest``. The legacy tree is never modified; ``dest`` must be missing or
    empty (this command creates a repo, it never merges into one)."""
    runner = runner or subprocess.run
    legacy_dir = Path(legacy_dir)
    dest = Path(dest)
    if not legacy_dir.is_dir():
        raise MigrateError(f"legacy config tree {legacy_dir} is not a directory")
    if dest.resolve() == legacy_dir.resolve():
        raise MigrateError("the migration destination must differ from the legacy tree")
    if dest.exists() and any(dest.iterdir()):
        raise MigrateError(
            f"migration destination {dest} is not empty — this command creates a"
            " fresh Config Repo; point it at a new directory"
        )
    report = MigrateReport(dest=dest)
    dest.mkdir(parents=True, exist_ok=True)
    _copy_tree(legacy_dir, dest, report.written)
    for path in sorted((dest / "worker-types").glob("*.toml")):
        _strip_legacy_knowledge(path, path.stem, report)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        _note_legacy_deck_wiring(data, path.stem, report)
    _write_settings_only_control_toml(legacy_dir, dest, report)
    _git_init_and_commit(dest, runner, report)
    log(report.summary())
    return report
