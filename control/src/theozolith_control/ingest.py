"""`theozolith config ingest` (ADR-0048): human Config Repo -> pinned build.

The human-authored Config Repo (a local path or a git URL) is the source of
truth; the machine-owned pinned build (``configs/``) is what control loads and
distributes. This module is the ONLY path between them:

1. HARVEST the Config Repo at its current commit (a dirty git source is
   refused — the pinned build stamps the source commit, so the stamp must be
   truthful; a plain folder harvests under a content hash).
2. STAGE a candidate tree: config files copied verbatim (with the pinned
   build's ``product.toml`` carried forward when the source declares none —
   the update flow owns the pin, ADR-0051), ``knowledge/<name>`` trees
   compiled by the ADR-0009 compiler (compile errors surface HERE, not at
   image build or container start), ``policy/<name>`` Agent Policy trees
   validated against the safe-key allowlist (ADR-0055 — an unadmitted key
   surfaces HERE, and again at every config load), ``control.toml`` merged
   (the machine-written [control] address block is preserved from the pinned
   build; the [settings] surface is harvested from the source), and
   ``pins.toml`` written with the resolved pins.
3. RESOLVE pins where resolution is mechanical: per-knowledge-tree and
   per-policy-tree content hashes and base tag->digest. NEVER where the
   value must come out-of-band —
   a vendor-published artifact checksum in a setup line stays human-entered
   in the Config Repo, and ingest only refuses the fail-closed placeholder
   for worker types a running Stack references (computing it would sign
   whatever the network served).
4. LINT by loading the staged tree with the exact fail-loud checks config
   load applies (``configrepo.load_config``): a config that would not load is
   never committed.
5. COMMIT to the pinned build (refusing a dirty tree first) with the source
   commit stamped in the message and in ``pins.toml``. Rollback is ``git
   revert`` on the pinned build — resolved pins are decisions that exist
   nowhere else.
6. RELOAD, not restart: control re-reads the config tree per request, so the
   running server observes the new commit within one heartbeat; only
   ``control.toml`` tier-2 settings keep their documented restart requirement.

A DRY RUN (``theozolith config ingest --dry-run``) is the config LINTER: the
identical pipeline through the lint step — every refusal above fires the
same — followed by a PREVIEW of what the commit would change (per-file
adds/updates/deletes, would-be re-tags, ``control.toml`` and product-version
movement) instead of the commit itself. The preview writes NOTHING into the
pinned build, not even loose objects: the staged tree is hashed through a
throwaway object directory and diffed against HEAD via git's alternates
mechanism. Two deliberate deviations, both reported loudly in the notes: a
dirty LOCAL git source is previewed from its working tree (linting
uncommitted edits is the dry run's home case — a real ingest still
refuses), and the ignored-leftover purge and pending-marker repair are
reported, never performed. Under a pending marker the worktree may trail
HEAD, so every old-state read the preview depends on — the preserved
product pin, control.toml, worker tags — comes from a read-only snapshot
of the committed HEAD (exactly the state the real ingest's repair would
restore before staging), never from the lagging worktree.

The whole transaction — from the initial clean check through the commit and
working-tree publish — runs under the exclusive pinned-build WRITE LOCK
(``repolock``), shared with every other supported writer of ``configs/``
(the control-address and product-pin writers): a concurrent writer is
refused loudly rather than interleaved, so no transaction can ever race the
clean check, the staging, or the commit. As a backstop against UNSUPPORTED
writers (a hand-run ``git commit`` racing the transaction), the ref move is
COMPARE-AND-SWAP against the HEAD the transaction started from: a moved
HEAD fails the publish cleanly with the interloper's commit preserved,
never overwritten or orphaned.

The commit is COMMIT-FIRST git plumbing, never a working-tree mutation
followed by ``git add``: the staged tree is written into the object store
through a throwaway index (``add -A --force``/``write-tree``/
``commit-tree`` — forced, so no ignore rule can silently drop staged
content from the commit), the ref moves atomically (``update-ref`` with the
expected old value), and only then is the working tree synced to HEAD
(``reset --hard`` + ``clean -ffdx``: ignored files and nested repositories
included — the machine-owned worktree is exactly HEAD, nothing else is
loadable or distributable). A failure before the ref moves leaves the
pinned build byte-for-byte untouched; an interruption after it leaves a
marker (``.git/theozolith-ingest-pending``) that the next ingest repairs by
finishing the same working-tree sync — the working tree is never the only
copy of a half-applied state, and no partially replaced tree can survive a
crash.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_knowledge import COMPILERS, KnowledgeError, load_knowledge_root

# The ONE Agent Policy validator (ADR-0055), shared with config load: imported
# as a module and invoked through the attribute, so a single monkeypatch of
# theozolith_worker.policy.validate_policy_tree observes both sites.
from theozolith_worker import policy as agentpolicy

# The CLI Pin's adapter-owned contract (ADR-0055): the enforcement floor and
# the supported platform-package table both live on the adapter, so control
# resolves against the SAME registry of capability the identity machinery
# enforces (control already carries theozolith-worker, ADR-0015 amendment).
from theozolith_worker.adapters import make_agent_adapter

from theozolith_control import configdist, configrepo, controltoml, repolock

# A sha256 whose value is the fail-closed placeholder convention (all zeros):
# legal in a template (a scaffolded or dormant worker type), refused for any
# worker type a running Stack references (ADR-0048).
PLACEHOLDER_SHA256 = "0" * 64

# Top-level entries never copied from the source tree into the staging tree.
_SKIP_TOP_LEVEL = (".git", configdist.KNOWLEDGE_DIR, controltoml.CONTROL_TOML)

_HEX64 = re.compile(r"[0-9a-f]{64}")

# The exact-version shape a CLI Pin resolves to (ADR-0055): semver with an
# optional prerelease suffix. Shared with configrepo's pins.toml re-parse.
_CLI_VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")

# git's well-known empty tree object — always resolvable without existing on
# disk; the preview's diff base when the pinned build is unborn or missing.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Manifest media types a registry may serve for a tag; the digest of whichever
# the registry canonically serves IS the pin `docker pull <ref>@<digest>`
# resolves.
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    )
)

# The single choke point for every registry HTTP request (token realm and
# manifest HEAD). Tests monkeypatch this seam to script a fake registry
# without a network; a module attribute is the smallest diff over threading an
# ``opener=`` parameter through the resolver.
_urlopen = urllib.request.urlopen


class IngestError(RuntimeError):
    """The ingest cannot proceed; nothing has been committed."""


@dataclass
class IngestReport:
    """What one ingest did — printed by the CLI, asserted by tests."""

    source_commit: str = ""
    pinned_commit: str = ""  # the pinned build's new HEAD ("" when unchanged)
    changed: bool = False  # in a dry run: whether a real ingest WOULD commit
    resolved_bases: dict[str, str] = field(default_factory=dict)  # ref -> digest
    knowledge_pins: dict[str, str] = field(default_factory=dict)  # "tree/tool" -> hash
    policy_pins: dict[str, str] = field(default_factory=dict)  # tree -> hash (ADR-0055)
    # "<tool>/<declared>" -> {"version": ..., "platforms": {...}} (ADR-0055).
    cli_pins: dict[str, dict] = field(default_factory=dict)
    retagged: dict[str, tuple[str, str]] = field(default_factory=dict)  # type -> (old, new)
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False  # lint + preview only — nothing was committed
    changes: list[str] = field(default_factory=list)  # dry run: per-file preview

    def summary(self) -> str:
        lines = [f"source commit: {self.source_commit}"]
        if self.dry_run:
            lines.append("DRY RUN — lint and preview only, nothing was committed")
            if self.changed:
                lines.append(f"ingest would commit {len(self.changes)} change(s):")
                lines.extend(f"  {change}" for change in self.changes)
            else:
                lines.append("ingest would be a no-op (pinned build already up to date)")
        elif self.changed:
            lines.append(f"pinned build committed: {self.pinned_commit}")
        else:
            lines.append("pinned build unchanged (already up to date)")
        for ref, digest in sorted(self.resolved_bases.items()):
            lines.append(f"resolved base {ref} -> {digest[:19]}…")
        for name, tree_hash in sorted(self.knowledge_pins.items()):
            lines.append(f"knowledge/{name} pinned {tree_hash[:12]}")
        for name, tree_hash in sorted(self.policy_pins.items()):
            lines.append(f"policy/{name} pinned {tree_hash[:12]}")
        for key, pin in sorted(self.cli_pins.items()):
            lines.append(
                f"cli {key} resolved {pin.get('version', '?')}"
                f" ({len(pin.get('platforms', {}))} platform(s))"
            )
        retag_verb = "would re-tag" if self.dry_run else "re-tagged"
        for name, (old, new) in sorted(self.retagged.items()):
            lines.append(f"worker type {name} {retag_verb}: {old or '(new)'} -> {new}")
        lines.extend(self.notes)
        return "\n".join(lines)


def _run_git(
    args: list[str], cwd: Path, runner, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    kwargs: dict = {"cwd": str(cwd), "capture_output": True, "text": True, "check": False}
    if env is not None:
        kwargs["env"] = {**os.environ, **env}
    return runner(["git", *args], **kwargs)


def _git_subcommand(args: list[str]) -> str:
    """The subcommand word for an error message: the first argument that is
    neither an option nor an option's value (``-c``, ``--git-dir``,
    ``--work-tree`` all take one)."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-c", "--git-dir", "--work-tree"):
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return args[0]


def _require_git(
    args: list[str], cwd: Path, runner, what: str, env: dict[str, str] | None = None
) -> str:
    proc = _run_git(args, cwd, runner, env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise IngestError(f"{what}: git {_git_subcommand(args)} failed: {detail}")
    return (proc.stdout or "").strip()


# The crash-recovery plumbing (ADR-0048 amendment): no interruption may leave
# a partially replaced pinned tree. (The write lock itself is `repolock` —
# shared with every other supported pinned-build writer.)

# Written just before the ref moves, removed once the working tree matches the
# new HEAD; its presence means an ingest died between those two points.
PENDING_MARKER = "theozolith-ingest-pending"


def _sync_worktree_to_head(pinned_dir: Path, runner) -> bool:
    """Make the machine-owned working tree match HEAD EXACTLY (``reset
    --hard`` + ``clean -ffdx``: ignored files and nested repositories
    removed too — nothing outside committed HEAD may remain loadable or
    distributable) — the publish half of a committed transaction, and the
    repair a crashed one needs. True on success; an unborn HEAD is vacuously
    in sync (nothing was ever committed, so nothing was ever
    half-published)."""
    if _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], pinned_dir, runner).returncode != 0:
        return True
    for args in (["reset", "--hard", "--quiet"], ["clean", "-ffdxq"]):
        if _run_git(args, pinned_dir, runner).returncode != 0:
            return False
    return True


def _recover_pending(pinned_dir: Path, runner, report: IngestReport) -> None:
    """Finish a transaction that died between ``update-ref`` and the
    working-tree publish: the marker proves the dirt (if any) is ours, so the
    tree is restored to HEAD — never refused as a hand edit. An unrepairable
    tree fails loudly with the marker left in place for the next attempt."""
    marker = pinned_dir / ".git" / PENDING_MARKER
    if not marker.exists():
        return
    if not _sync_worktree_to_head(pinned_dir, runner):
        raise IngestError(
            f"pinned build {pinned_dir}: an interrupted ingest left the working"
            " tree behind HEAD and it could not be restored (git reset --hard"
            " failed) — repair the repository, then re-run ingest"
        )
    with contextlib.suppress(OSError):
        marker.unlink()
    report.notes.append(
        "recovered an interrupted ingest: the working tree was restored to the"
        " committed HEAD before this run"
    )


def _snapshot_committed_head(pinned_dir: Path, head: str, dest: Path, runner) -> None:
    """A read-only extraction of the committed HEAD tree into ``dest``, for
    the dry run's old-state reads when a pending marker means the worktree
    may trail HEAD: a real ingest repairs the tree to HEAD before reading
    it, so the preview must read what that repair would produce — never the
    lagging worktree. ``git archive`` only READS objects (no index, worktree,
    ref, or object write), keeping the dry run's nothing-touched guarantee
    intact. An unborn HEAD snapshots to an empty tree — nothing was ever
    committed, so the old state is empty."""
    dest.mkdir()
    if not head:
        return
    tar_path = dest.parent / "pinned-head.tar"
    _require_git(
        ["archive", "--format=tar", "-o", str(tar_path), head],
        pinned_dir,
        runner,
        f"{pinned_dir} (dry run)",
    )
    with tarfile.open(tar_path) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, filter="data")
        else:  # pragma: no cover — Python < 3.11.4
            archive.extractall(dest)
    tar_path.unlink()


def _is_git_url(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def _folder_commit(root: Path) -> str:
    """Content stamp for a non-git source folder — same convention as the
    pinned build's own folder mode (``configrepo._commit``), normalized
    executable state included (a chmod-only source edit changes the compiled
    output, so the provenance stamp must move with it)."""
    digest = hashlib.sha256()
    for path in configdist.regular_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(configdist.entry_mode(path.stat().st_mode).encode())
        digest.update(path.read_bytes())
    return f"folder-{digest.hexdigest()[:12]}"


def _harvest_source(
    source: str, workdir: Path, runner, *, dirty_ok: bool = False
) -> tuple[Path, str, bool]:
    """The source tree to stage from, its provenance stamp, and whether the
    stamp covers an uncommitted WORKING TREE — only ever True under
    ``dirty_ok`` (the dry run previews uncommitted local edits under a folder
    content stamp; a real ingest refuses them — the stamp it commits must be
    truthful)."""
    if _is_git_url(source):
        clone = workdir / "source"
        proc = runner(
            ["git", "clone", "--quiet", "--depth", "1", source, str(clone)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise IngestError(f"cannot clone Config Repo {source}: {detail}")
        return clone, _require_git(["rev-parse", "HEAD"], clone, runner, source), False
    root = Path(source).expanduser()
    if not root.is_dir():
        raise IngestError(f"Config Repo {source} is not a directory (or a git URL)")
    if (root / ".git").exists():
        dirty = _require_git(["status", "--porcelain"], root, runner, str(root))
        if dirty and not dirty_ok:
            raise IngestError(
                f"Config Repo {root} has uncommitted changes — commit them first;"
                " the pinned build stamps the source commit, so the stamp must"
                " be truthful (ADR-0048):\n" + dirty
            )
        if dirty:
            try:
                return root, _folder_commit(root), True
            except configdist.ConfigDistError as exc:
                raise IngestError(f"cannot hash Config Repo folder {root}: {exc}") from exc
        head = _run_git(["rev-parse", "HEAD"], root, runner)
        if head.returncode == 0:
            return root, (head.stdout or "").strip(), False
        # A clean repo with no commits yet (init just scaffolded an empty
        # Config Repo): harvest it as the (empty) folder it is.
        return root, _folder_commit(root), False
    try:
        return root, _folder_commit(root), False
    except configdist.ConfigDistError as exc:
        raise IngestError(f"cannot hash Config Repo folder {root}: {exc}") from exc


def _copy_config_files(source_dir: Path, staging: Path) -> None:
    """Copy the source tree verbatim, minus the specially handled top-level
    entries and the content-exclusion names (dot-prefixed, ``__pycache__``,
    ``*.pyc``). Symlinks and other irregular entries are refused loudly — a
    symlink could smuggle content from outside the repo into the pinned build,
    and the distribution would fail closed on it later anyway."""
    stack: list[tuple[Path, Path]] = [(source_dir, staging)]
    while stack:
        current, dest = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            if configdist.excluded_part(entry.name):
                continue
            if current == source_dir and entry.name in _SKIP_TOP_LEVEL:
                continue
            relpath = entry.relative_to(source_dir).as_posix()
            if entry.is_symlink():
                raise IngestError(f"Config Repo entry {relpath} is a symlink — refused")
            if entry.is_dir():
                (dest / entry.name).mkdir(parents=True, exist_ok=True)
                stack.append((entry, dest / entry.name))
            elif entry.is_file():
                target = dest / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
            else:
                raise IngestError(f"Config Repo entry {relpath} is not a regular file — refused")


def _path_shape(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISLNK(mode):
        return "a symlink"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "a device node"
    return "not a regular file"


def _preserve_product_pin(
    source_dir: Path, pinned_state: Path, staging: Path, report: IngestReport
) -> None:
    """A pin-less source never deletes the pin (ADR-0051): when the Config
    Repo carries no ``product.toml``, the update flow (``theozolith build``/
    ``theozolith update``) owns the product pin, so the pinned build's
    current file is carried forward into staging verbatim. A source that HAS
    ``product.toml`` still wins (ADR-0048; the divergence note downstream
    covers it) — but "has" means a REGULAR FILE: the pin's two valid states
    are absent (update flow owns it) or a plain TOML file (the Config Repo
    declares it), and any other shape is refused loudly BEFORE the pinned
    copy — silently preserving into e.g. a ``product.toml/`` directory would
    ship a ``product.toml/product.toml`` no loader reads while this note
    claims the pin survived. ``pinned_state`` is the old pinned state to
    preserve from: the worktree normally (a real ingest reset it to HEAD
    under the shared write lock before staging), or the dry run's read-only
    committed-HEAD snapshot when a pending marker means the worktree may
    trail HEAD."""
    source_pin = source_dir / "product.toml"
    try:
        mode = source_pin.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISREG(mode):
            raise IngestError(
                f"Config Repo product.toml is {_path_shape(mode)} — product.toml"
                " has exactly two valid states: absent (the update flow owns the"
                " pin, ADR-0051) or a regular TOML file (the Config Repo declares"
                " the pin); replace it with a plain file or delete it"
            )
        return
    pinned_product = pinned_state / "product.toml"
    if not pinned_product.is_file():
        return  # absent in both trees stays absent
    shutil.copy2(pinned_product, staging / "product.toml")
    version = ""
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
        data = tomllib.loads(pinned_product.read_text(encoding="utf-8"))
        version = str(data.get("product", {}).get("version", ""))
    verb = "a real ingest preserves" if report.dry_run else "preserved"
    report.notes.append(
        f"source has no product.toml — {verb} the pinned build's product pin"
        f" {version or '(unversioned)'} (the update flow owns it; declare"
        " product.toml in the Config Repo for declarative release pinning)"
    )


def _compile_knowledge(source_dir: Path, staging: Path) -> dict[str, str]:
    """Compile every ``knowledge/<name>`` source tree once per registered
    compiler into the staging tree (ADR-0009 at ingest; per-tool layout
    ``knowledge/<name>/<tool>/`` since ADR-0052) and return the per-tree
    content pins keyed ``<name>/<tool>``. The pinned build stays a pure
    function of the knowledge source: ingest never inspects which worker
    types reference a tree. An empty per-tool fileset writes no directory
    and records no pin — a worker type joining an absent pin fails config
    load with an actionable error instead."""
    source_root = source_dir / configdist.KNOWLEDGE_DIR
    pins: dict[str, str] = {}
    if not source_root.is_dir():
        return pins
    for entry in sorted(source_root.iterdir(), key=lambda p: p.name):
        if configdist.excluded_part(entry.name):
            continue
        if not entry.is_dir() or entry.is_symlink():
            raise IngestError(
                f"knowledge/{entry.name} must be a directory (one knowledge root"
                " per name, ADR-0048)"
            )
        if not configrepo.KNOWLEDGE_TREE_NAME.fullmatch(entry.name):
            raise IngestError(
                f"knowledge/{entry.name}: tree names must match"
                " ^[A-Za-z0-9][A-Za-z0-9._-]*$ (ADR-0048)"
            )
        try:
            root = load_knowledge_root(entry)
        except KnowledgeError as exc:
            raise IngestError(f"knowledge/{entry.name} is not a knowledge root: {exc}") from exc
        for tool, compiler in sorted(COMPILERS.items()):
            try:
                fileset = compiler(root, "global")
            except KnowledgeError as exc:
                raise IngestError(
                    f"knowledge/{entry.name} does not compile for {tool}: {exc}"
                ) from exc
            if not fileset:
                continue
            tree = f"{entry.name}/{tool}"
            for relpath, file_entry in fileset.items():
                target = staging / configdist.KNOWLEDGE_DIR / tree / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file_entry.content)
                if file_entry.executable:
                    target.chmod(0o755)
            try:
                pins[tree] = configdist.knowledge_tree_hash(staging, tree)
            except configdist.ConfigDistError as exc:
                raise IngestError(f"knowledge/{tree}: {exc}") from exc
    return pins


def _pin_policy(source_dir: Path, staging: Path) -> dict[str, str]:
    """Validate every ``policy/<name>`` Agent Policy tree and return the
    per-tree content pins (ADR-0055). Validation runs over the SOURCE tree —
    so a dot-prefixed drop-in is refused rather than silently dropped by
    ``_copy_config_files``' exclusion filter — with the same
    ``theozolith_worker.policy`` validator config load applies (the two
    sites provably share one allowlist); the pin is then computed over the
    already-copied STAGED tree, which validation guarantees is byte-for-byte
    the source (``policy/`` copies verbatim — it is not in
    ``_SKIP_TOP_LEVEL``). An empty tree records no pin — a worker type
    joining an absent pin fails config load with an actionable error
    instead. The pinned build stays a pure function of the source: ingest
    never inspects which worker types reference a tree."""
    source_root = source_dir / configdist.POLICY_DIR
    pins: dict[str, str] = {}
    if not source_root.is_dir():
        return pins
    for entry in sorted(source_root.iterdir(), key=lambda p: p.name):
        if configdist.excluded_part(entry.name):
            continue
        if not entry.is_dir() or entry.is_symlink():
            raise IngestError(
                f"policy/{entry.name} must be a directory (one Agent Policy"
                " tree per name, ADR-0055)"
            )
        if not configrepo.KNOWLEDGE_TREE_NAME.fullmatch(entry.name):
            raise IngestError(
                f"policy/{entry.name}: tree names must match"
                " ^[A-Za-z0-9][A-Za-z0-9._-]*$ (ADR-0055)"
            )
        try:
            agentpolicy.validate_policy_tree(entry, label=f"policy/{entry.name}")
        except agentpolicy.PolicyError as exc:
            raise IngestError(str(exc)) from exc
        try:
            pin = configdist.policy_tree_hash(staging, entry.name)
        except configdist.ConfigDistError as exc:
            raise IngestError(f"policy/{entry.name}: {exc}") from exc
        if pin:
            pins[entry.name] = pin
    return pins


def _live_worker_types(staging: Path) -> set[str]:
    """Worker types referenced by a Stack whose desired state is running —
    the scope of the placeholder refusal: a template (dormant or stopped) may
    carry the fail-closed placeholder; a live type may not (ADR-0048)."""
    live: set[str] = set()
    for path in sorted((staging / "stacks").glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue  # the staged lint will fail loudly on this file
        worker_type = data.get("worker_type")
        state = data.get("state", "running")
        if isinstance(worker_type, str) and worker_type and state == "running":
            live.add(worker_type)
    return live


def _refuse_live_placeholders(staging: Path) -> None:
    live = _live_worker_types(staging)
    for name in sorted(live):
        path = staging / "worker-types" / f"{name}.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue  # the staged lint will fail loudly on this file
        candidates = [data.get("base", "")]
        setup = data.get("setup", [])
        if isinstance(setup, list):
            candidates.extend(str(line) for line in setup)
        for text in candidates:
            if isinstance(text, str) and PLACEHOLDER_SHA256 in text:
                raise IngestError(
                    f"worker-types/{name}.toml carries the all-zero placeholder"
                    " sha256 while a running Stack references it — fill in the"
                    " real checksum (checksums that must come out-of-band are"
                    " never computed by ingest, ADR-0048)"
                )


def _resolve_bases(staging: Path, resolve_digest: Callable[[str], str]) -> dict[str, str]:
    """tag->digest resolutions for every worker-type base not already pinned."""
    resolved: dict[str, str] = {}
    for path in sorted((staging / "worker-types").glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue  # the staged lint will fail loudly on this file
        base = data.get("base", "")
        if not isinstance(base, str) or not base or "@sha256:" in base or base in resolved:
            continue
        try:
            digest = resolve_digest(base)
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(
                f"cannot resolve base tag {base!r}: {exc} — pin it by digest in"
                " the Config Repo if registry resolution is unavailable"
            ) from exc
        if not isinstance(digest, str) or not (
            digest.startswith("sha256:") and _HEX64.fullmatch(digest[len("sha256:") :])
        ):
            raise IngestError(
                f"resolver returned {digest!r} for base tag {base!r} — expected 'sha256:<64 hex>'"
            )
        resolved[base] = digest
    return resolved


def _resolve_cli_pins(staging: Path, resolve_cli: Callable[[str], dict]) -> dict[str, dict]:
    """CLI Pin resolutions for every driverless claude worker type declaring
    ``cli`` (ADR-0055), keyed ``claude/<declared>`` — the ``_resolve_bases``
    twin. Each distinct declared value resolves once. Definitions carrying
    ``cli`` with a driver or a non-claude adapter are deliberately skipped
    here: the staged config load refuses them with the precise error, which
    the ingest LINT step surfaces before anything commits."""
    resolved: dict[str, dict] = {}
    for path in sorted((staging / "worker-types").glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue  # the staged lint will fail loudly on this file
        declared = data.get("cli", "")
        if not isinstance(declared, str) or not declared:
            continue
        adapter = data.get("adapter", "claude")
        if data.get("driver") or adapter != "claude":
            continue  # refused by the load lint with the precise message
        key = f"claude/{declared}"
        if key in resolved:
            continue
        try:
            pin = resolve_cli(declared)
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(f"cannot resolve CLI pin {declared!r}: {exc}") from exc
        version = pin.get("version", "") if isinstance(pin, dict) else ""
        platforms = pin.get("platforms") if isinstance(pin, dict) else None
        if (
            not isinstance(version, str)
            or not _CLI_VERSION.fullmatch(version)
            or not isinstance(platforms, dict)
            or not platforms
        ):
            raise IngestError(
                f"CLI resolver returned a malformed pin for {declared!r} —"
                " expected {'version': <semver>, 'platforms': {...}}"
            )
        resolved[key] = pin
    return resolved


def _write_pins(
    staging: Path,
    source_commit: str,
    bases: dict[str, str],
    knowledge: dict[str, str],
    policy: dict[str, str],
    cli: dict[str, dict] | None = None,
) -> None:
    lines = [
        "# Machine-written by `theozolith config ingest` (ADR-0048) — never hand-edit.",
        "# Resolved pins are decisions that exist nowhere else: rollback is",
        "# `git revert` on the pinned build, not a re-ingest of an old source commit.",
        "",
        "[source]",
        f'commit = "{source_commit}"',
    ]
    if bases:
        lines += ["", "[base]"]
        lines += [f'"{ref}" = "{digest}"' for ref, digest in sorted(bases.items())]
    if knowledge:
        lines += ["", "[knowledge]"]
        lines += [f'"{name}" = "{tree_hash}"' for name, tree_hash in sorted(knowledge.items())]
    if policy:
        lines += ["", "[policy]"]
        lines += [f'"{name}" = "{tree_hash}"' for name, tree_hash in sorted(policy.items())]
    for key, pin in sorted((cli or {}).items()):
        lines += ["", f'[cli."{key}"]', f'version = "{pin["version"]}"']
        lines += ["", f'[cli."{key}".platforms]']
        lines += [
            f'"{tuple_key}" = {{ package = "{entry["package"]}",'
            f' integrity = "{entry["integrity"]}" }}'
            for tuple_key, entry in sorted(pin["platforms"].items())
        ]
    (staging / configrepo.PINS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_control_toml(source_dir: Path, pinned_dir: Path, staging: Path) -> None:
    """control.toml goes through ingest too (ADR-0048), split by ownership:
    the [settings] surface is harvested from the source; the [control] address
    block is MACHINE state written by init/origin-init/recover and is
    preserved from the pinned build — a Config Repo that tries to author it is
    refused."""
    source_toml = source_dir / controltoml.CONTROL_TOML
    if source_toml.is_file():
        try:
            data = tomllib.loads(source_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise IngestError(f"{controltoml.CONTROL_TOML}: {exc}") from exc
        if isinstance(data, dict) and "control" in data:
            raise IngestError(
                f"{controltoml.CONTROL_TOML}: the [control] table is machine"
                " state (written by init / origin-init / recover) — remove it"
                " from the Config Repo; only [settings] is authored there"
                " (ADR-0048)"
            )
    try:
        values = controltoml.read_values(source_dir)
        merged = controltoml.render(
            controltoml.read_control_ip(pinned_dir),
            controltoml.read_control_port(pinned_dir),
            controltoml.read_browser_origin(pinned_dir),
            values,
        )
    except controltoml.ControlTomlError as exc:
        raise IngestError(str(exc)) from exc
    (staging / controltoml.CONTROL_TOML).write_text(merged, encoding="utf-8")


def _commit_staging(
    staging: Path, pinned_dir: Path, work: Path, runner, message: str, parent: str
) -> str:
    """Write the staged tree into the pinned build's object store through a
    THROWAWAY index (never the repo's own) and create the commit object with
    ``parent`` — the HEAD the transaction started from (``""`` for unborn),
    so the commit and the later compare-and-swap ref move agree on what they
    extend. The ``add`` is FORCED: no ignore rule (``.git/info/exclude``, a
    user-global excludes file) may silently drop staged content from the
    commit. Returns the new commit id, or ``""`` when the staged tree is
    identical to the parent's tree (nothing to commit). Nothing observable
    moves here: only loose objects and the commit object are written, the ref
    does NOT — so a failure at any point leaves the pinned build
    byte-for-byte untouched."""
    git_dir = str((pinned_dir / ".git").resolve())
    env = {"GIT_INDEX_FILE": str(work / "ingest-index")}
    scoped = ["--git-dir", git_dir, "--work-tree", "."]
    _require_git([*scoped, "add", "-A", "--force"], staging, runner, str(pinned_dir), env=env)
    tree = _require_git([*scoped, "write-tree"], staging, runner, str(pinned_dir), env=env)
    if parent:
        head = _run_git(
            ["rev-parse", "--verify", "--quiet", f"{parent}^{{tree}}"], pinned_dir, runner
        )
        if head.returncode == 0 and (head.stdout or "").strip() == tree:
            return ""
    commit_args = [
        "-c",
        f"user.name={controltoml.COMMIT_AUTHOR_NAME}",
        "-c",
        f"user.email={controltoml.COMMIT_AUTHOR_EMAIL}",
        "commit-tree",
    ]
    if parent:
        commit_args += ["-p", parent]
    commit_args += ["-m", message, tree]
    return _require_git(commit_args, pinned_dir, runner, str(pinned_dir))


def _preview_changes(staging: Path, pinned_dir: Path, work: Path, runner, parent: str) -> list[str]:
    """What a commit of the staged tree would change relative to ``parent``
    (the committed HEAD; ``""`` = unborn or no pinned build at all), as
    ``git diff-tree`` name-status lines — ``[]`` means a real ingest would be
    a no-op. The staged tree is hashed EXACTLY as ``_commit_staging`` would
    hash it (same throwaway index, same forced add, git's own mode
    normalization — the identical tree object id) but NOT ONE BYTE lands in
    the pinned build: blobs and trees go to a throwaway object directory in
    the workdir, and the pinned build's own objects are readable through
    git's alternates mechanism."""
    throwaway = work / "preview-git"
    throwaway.mkdir()
    what = f"{pinned_dir} (dry run)"
    _require_git(["init", "--quiet"], throwaway, runner, what)
    objects = work / "preview-objects"
    objects.mkdir()
    env = {
        "GIT_INDEX_FILE": str(work / "preview-index"),
        "GIT_OBJECT_DIRECTORY": str(objects),
    }
    pinned_objects = pinned_dir / ".git" / "objects"
    if pinned_objects.is_dir():
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(pinned_objects.resolve())
    scoped = ["--git-dir", str((throwaway / ".git").resolve()), "--work-tree", "."]
    _require_git([*scoped, "add", "-A", "--force"], staging, runner, what, env=env)
    tree = _require_git([*scoped, "write-tree"], staging, runner, what, env=env)
    base = _EMPTY_TREE
    if parent:
        base = _require_git(
            [*scoped, "rev-parse", f"{parent}^{{tree}}"], staging, runner, what, env=env
        )
    if base == tree:
        return []
    diff = _require_git(
        [*scoped, "diff-tree", "-r", "--name-status", base, tree], staging, runner, what, env=env
    )
    verbs = {"A": "add", "M": "update", "D": "delete", "T": "replace"}
    changes = []
    for line in diff.splitlines():
        if "\t" not in line:
            continue
        letter, _, path = line.partition("\t")
        changes.append(f"{verbs.get(letter, letter)} {path}")
    return changes


def _tags_of(repo_dir: Path) -> dict[str, str]:
    """Worker-type tags of a loadable tree; {} when it does not load (a fresh
    or broken pinned build must not block reporting)."""
    try:
        return {name: wt.tag for name, wt in configrepo.load_config(repo_dir).worker_types.items()}
    except configrepo.ConfigRepoError:
        return {}


def _product_version_of(repo_dir: Path) -> str:
    try:
        return configrepo.load_config(repo_dir).product_version
    except configrepo.ConfigRepoError:
        return ""


def ingest(
    source: str,
    pinned_dir: Path,
    *,
    resolve_digest: Callable[[str], str] | None = None,
    resolve_cli: Callable[[str], dict] | None = None,
    registry_credentials: dict[str, str] | None = None,
    dry_run: bool = False,
    runner=None,
    log=print,
) -> IngestReport:
    """Run the full pipeline; raises ``IngestError`` with nothing committed on
    any refusal. See the module docstring for the steps. The whole transaction
    — clean check through commit and working-tree publish — holds the shared
    pinned-build write lock; a concurrent writer is refused loudly. A DRY RUN
    runs the identical pipeline through the lint step and reports what the
    commit would change instead of committing. It holds the same lock — the
    preview must read one consistent build, and a refusal on contention is
    itself an honest preview (a real ingest would be refused right now too) —
    but writes nothing, not even the empty repository a first real ingest
    would initialize.

    ``registry_credentials`` (host -> ``<user>:<token>``, ADR-0049) authorizes
    the default resolver's private-base resolution; it is IGNORED when
    ``resolve_digest`` is injected (a test's fake resolver stands in for the
    whole registry). A dry run resolves live too, so it also validates the
    credential. ``resolve_cli`` is the same seam for CLI Pin resolution
    (ADR-0055): the default is the live npm resolver ``resolve_cli_pin``."""
    # Resolved at call time (not as a def-time default) so test rigs that
    # monkeypatch subprocess.run fake the git layer here too.
    runner = runner or subprocess.run
    pinned_dir = Path(pinned_dir)
    if dry_run and not pinned_dir.is_dir():
        # No pinned build at all: nothing to lock or race (taking the lock
        # would CREATE the directory), and the preview is simply "everything
        # would be added".
        return _ingest_locked(
            source,
            pinned_dir,
            resolve_digest,
            runner,
            log,
            dry_run=True,
            registry_credentials=registry_credentials,
            resolve_cli=resolve_cli,
        )
    pinned_dir.mkdir(parents=True, exist_ok=True)
    writer = "theozolith config ingest" + (" --dry-run" if dry_run else "")
    try:
        with repolock.pinned_write_lock(pinned_dir, writer=writer):
            return _ingest_locked(
                source,
                pinned_dir,
                resolve_digest,
                runner,
                log,
                dry_run=dry_run,
                registry_credentials=registry_credentials,
                resolve_cli=resolve_cli,
            )
    except repolock.RepoLockError as exc:
        raise IngestError(str(exc)) from exc


def _ingest_locked(
    source: str,
    pinned_dir: Path,
    resolve_digest: Callable[[str], str] | None,
    runner,
    log,
    dry_run: bool = False,
    registry_credentials: dict[str, str] | None = None,
    resolve_cli: Callable[[str], dict] | None = None,
) -> IngestReport:
    report = IngestReport(dry_run=dry_run)
    # The injected resolver seam is untouched (every existing fake keeps
    # working); the default resolver is bound to the stored credentials so a
    # private base resolves (ADR-0049).
    resolve = resolve_digest or functools.partial(
        resolve_image_digest, credentials=registry_credentials
    )
    resolve_cli = resolve_cli or resolve_cli_pin

    # The pinned build must exist as a clean git repo before anything is
    # staged. A marker left by an interrupted ingest is repaired FIRST — that
    # dirt is provably ours — and only then does the clean check refuse what
    # remains (a hand edit). A dry run performs neither mutation: the repair
    # and the ignored-leftover purge are REPORTED instead, and the preview is
    # computed against the committed HEAD either way.
    git_exists = (pinned_dir / ".git").exists()
    if not git_exists and not dry_run:
        _require_git(["init", "--quiet"], pinned_dir, runner, str(pinned_dir))
        git_exists = True
    pending_preview = git_exists and dry_run and (pinned_dir / ".git" / PENDING_MARKER).exists()
    if pending_preview:
        report.notes.append(
            "an interrupted ingest left the working tree behind HEAD — a real"
            " ingest repairs this first; the preview is computed against the"
            " committed HEAD"
        )
    elif git_exists:
        if not dry_run:
            _recover_pending(pinned_dir, runner, report)
        dirty = _require_git(["status", "--porcelain"], pinned_dir, runner, str(pinned_dir))
        if dirty:
            raise IngestError(
                f"pinned build {pinned_dir} has uncommitted changes — it is machine-"
                "owned and committed only by ingest; restore it (git checkout/reset)"
                " before ingesting (ADR-0048):\n" + dirty
            )
        # IGNORED leftovers pass the clean check (status never shows them) but
        # would stay loadable and distributable from the worktree — a file only
        # a `.git/info/exclude` rule hides is not committed content and can
        # never be a lost hand edit, so it is purged (a dry run reports what
        # would go), not refused. After this the worktree is exactly HEAD on
        # EVERY successful ingest, the no-op path included.
        purge = _run_git(["clean", "-ffdxn" if dry_run else "-ffdx"], pinned_dir, runner)
        if purge.returncode != 0:
            raise IngestError(
                f"pinned build {pinned_dir}: cannot remove ignored leftover files"
                f" (git clean failed): {(purge.stderr or '').strip()[:300]}"
            )
        purged = (purge.stdout or "").strip()
        if purged:
            names = "; ".join(
                line.removeprefix("Would remove ").removeprefix("Removing ")
                for line in purged.splitlines()
            )
            report.notes.append(
                ("a real ingest will remove" if dry_run else "removed")
                + " ignored leftovers from the machine-owned worktree (only"
                " committed HEAD is loadable/distributable): " + names
            )
    # The HEAD this transaction extends — the commit parent AND the expected
    # old value of the compare-and-swap ref move at publish ("" = unborn).
    original_head = ""
    if git_exists:
        head = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], pinned_dir, runner)
        original_head = (head.stdout or "").strip() if head.returncode == 0 else ""

    with tempfile.TemporaryDirectory(prefix="theozolith-ingest-") as workdir:
        work = Path(workdir)
        # The OLD pinned state every preservation read and preview comparison
        # uses. Normally the worktree IS that state (above, a real ingest
        # repaired or reset it to HEAD under the shared lock). The one
        # exception: a dry run over a pending marker, where the worktree may
        # trail HEAD — the real ingest would repair first, so the preview
        # reads a read-only committed-HEAD snapshot instead of the lagging
        # tree (and mutates nothing).
        pinned_state = pinned_dir
        if pending_preview:
            pinned_state = work / "pinned-head"
            _snapshot_committed_head(pinned_dir, original_head, pinned_state, runner)
        old_control_toml = ""
        old_control_path = pinned_state / controltoml.CONTROL_TOML
        if old_control_path.is_file():
            old_control_toml = old_control_path.read_text(encoding="utf-8")
        old_tags = _tags_of(pinned_state)
        old_product = _product_version_of(pinned_state)

        source_dir, report.source_commit, worktree_preview = _harvest_source(
            source, work, runner, dirty_ok=dry_run
        )
        if worktree_preview:
            report.notes.append(
                "source has uncommitted changes — this preview reflects the"
                " WORKING TREE; a real ingest will refuse until they are"
                " committed"
            )
        staging = work / "staging"
        staging.mkdir()
        _copy_config_files(source_dir, staging)
        _preserve_product_pin(source_dir, pinned_state, staging, report)
        report.knowledge_pins = _compile_knowledge(source_dir, staging)
        report.policy_pins = _pin_policy(source_dir, staging)
        _refuse_live_placeholders(staging)
        report.resolved_bases = _resolve_bases(staging, resolve)
        report.cli_pins = _resolve_cli_pins(staging, resolve_cli)
        _write_pins(
            staging,
            report.source_commit,
            report.resolved_bases,
            report.knowledge_pins,
            report.policy_pins,
            report.cli_pins,
        )
        _merge_control_toml(source_dir, pinned_state, staging)

        # LINT: the staged tree must load under the exact fail-loud checks the
        # server applies — a config that would not load is never committed.
        try:
            staged_config = configrepo.load_config(staging)
        except configrepo.ConfigRepoError as exc:
            raise IngestError(f"staged config does not load — nothing committed: {exc}") from exc
        for warning in staged_config.warnings:
            report.notes.append(f"warning: {warning}")

        if dry_run:
            # PREVIEW instead of commit: the staged tree is hashed exactly as
            # the real commit would hash it, so the no-op answer here IS the
            # answer a real ingest would give.
            report.changes = _preview_changes(staging, pinned_dir, work, runner, original_head)
            report.changed = bool(report.changes)
            if report.changed:
                new_tags = {name: wt.tag for name, wt in staged_config.worker_types.items()}
                report.retagged = {
                    name: (old_tags.get(name, ""), tag)
                    for name, tag in sorted(new_tags.items())
                    if old_tags.get(name, "") != tag
                }
                if staged_config.product_version != old_product:
                    report.notes.append(
                        f"product version would move: {old_product or '(none)'} ->"
                        f" {staged_config.product_version or '(none)'}"
                    )
                staged_control = (staging / controltoml.CONTROL_TOML).read_text(encoding="utf-8")
                if staged_control != old_control_toml:
                    report.notes.append(
                        "control.toml would change: tier-2 settings apply on the"
                        " next service restart"
                    )
            log(report.summary())
            return report

        # COMMIT FIRST (from the staging tree, through a throwaway index):
        # until update-ref below, nothing observable has moved and any failure
        # leaves the pinned build untouched.
        commit = _commit_staging(
            staging,
            pinned_dir,
            work,
            runner,
            f"theozolith config ingest: source {report.source_commit}",
            original_head,
        )
    if not commit:
        report.changed = False
        log(report.summary())
        return report

    # Publish: move the ref atomically — COMPARE-AND-SWAP against the HEAD
    # the transaction started from, so a commit an unsupported writer slipped
    # in (supported writers share the repolock and cannot get here) fails the
    # publish cleanly instead of being overwritten or orphaned — then sync
    # the working tree to it. The marker brackets the only window in which an
    # interruption can leave the working tree behind the ref — the next
    # ingest finishes the sync from the marker; a same-process failure
    # attempts the same repair immediately.
    marker = pinned_dir / ".git" / PENDING_MARKER
    marker.write_text(commit + "\n", encoding="utf-8")
    try:
        moved = _run_git(
            ["update-ref", "HEAD", commit, original_head or "0" * 40], pinned_dir, runner
        )
        if moved.returncode != 0:
            now = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], pinned_dir, runner)
            current = (now.stdout or "").strip() if now.returncode == 0 else ""
            if current != original_head:
                raise IngestError(
                    f"pinned build {pinned_dir}: HEAD moved during the ingest"
                    " transaction — a concurrent writer committed to the pinned"
                    " build outside the shared write lock. Nothing was"
                    " published; the concurrent commit is preserved. Re-run"
                    " ingest."
                )
            detail = (moved.stderr or moved.stdout or "").strip()[:300]
            raise IngestError(f"{pinned_dir}: git update-ref failed: {detail}")
        _require_git(["reset", "--hard", "--quiet"], pinned_dir, runner, str(pinned_dir))
        _require_git(["clean", "-ffdxq"], pinned_dir, runner, str(pinned_dir))
    except BaseException:
        if _sync_worktree_to_head(pinned_dir, runner):
            with contextlib.suppress(OSError):
                marker.unlink()
        raise
    with contextlib.suppress(OSError):
        marker.unlink()
    report.changed = True
    report.pinned_commit = commit

    new_tags = _tags_of(pinned_dir)
    report.retagged = {
        name: (old_tags.get(name, ""), tag)
        for name, tag in sorted(new_tags.items())
        if old_tags.get(name, "") != tag
    }
    new_product = _product_version_of(pinned_dir)
    if new_product != old_product:
        # The product-update flow (`theozolith update`) also writes the
        # product.toml pin into the pinned build; when the Config Repo has
        # one, ingest overwrites it with the source's value, so a divergence
        # is surfaced, never silent. A pin-less source preserves the pinned
        # build's pin instead (ADR-0051; _preserve_product_pin) — an absent
        # declaration never deletes the deployed pin, so this note only
        # fires for a declared file that actually moved the version.
        report.notes.append(
            f"product version: {old_product or '(none)'} -> {new_product or '(none)'}"
            " — the Config Repo's product.toml wins over any pin the update"
            " flow wrote since the last ingest"
        )
    new_control_path = pinned_dir / controltoml.CONTROL_TOML
    new_control = new_control_path.read_text(encoding="utf-8") if new_control_path.is_file() else ""
    if new_control != old_control_toml:
        report.notes.append(
            "control.toml changed: tier-2 settings apply on the next service restart"
        )
    report.notes.append(
        "reload: the running control service re-reads the pinned build per"
        " request; nodes converge over the hash ladder"
    )
    log(report.summary())
    return report


# -- registry digest resolution ---------------------------------------------------


def _split_image_ref(ref: str) -> tuple[str, str, str]:
    """``registry.example/name/space:tag`` -> (registry, repository, tag).
    Docker Hub shorthand (no dotted/ported first component) resolves against
    registry-1.docker.io with the ``library/`` convention."""
    if "@" in ref:
        raise IngestError(f"base ref {ref!r} already carries a digest")
    head, _, rest = ref.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        registry, remainder = head, rest
    else:
        registry, remainder = "registry-1.docker.io", ref
    repo, sep, tag = remainder.rpartition(":")
    if not sep:
        repo, tag = remainder, "latest"
    if not repo or not tag or "/" in tag:
        raise IngestError(f"cannot parse base ref {ref!r} as registry/repository:tag")
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    return registry, repo, tag


def _registry_token(www_authenticate: str, credential: str = "") -> str:
    """The bearer-token flow a registry answers 401 with. Anonymous by
    default (the public-pull fast path); when a ``<user>:<token>`` credential
    is supplied it is sent to the token realm as HTTP Basic (ADR-0049) — which
    is how GHCR mints a token carrying pull rights for a PRIVATE package
    (anonymously it mints a token fine, then the manifest HEAD 403s)."""
    if not www_authenticate.lower().startswith("bearer "):
        raise IngestError(f"registry auth challenge not understood: {www_authenticate!r}")
    params = dict(re.findall(r'(\w+)="([^"]*)"', www_authenticate[len("bearer ") :]))
    realm = params.get("realm", "")
    if not realm.startswith("https://"):
        raise IngestError(f"registry auth realm not https: {realm!r}")
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    url = realm + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(url)
    if credential:
        if ":" not in credential:
            raise IngestError(
                "registry credential must be '<user>:<token>' (e.g. a GitHub"
                " username and a PAT with read:packages)"
            )
        basic = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {basic}")
    with _urlopen(request, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise IngestError("registry token endpoint returned a malformed response")
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str) or not token:
        raise IngestError("registry token endpoint returned no token")
    return token


def _resolve_http_hint(ref: str, registry: str, code: int, credential: str) -> str:
    """The terminal HTTP-error message for a failed base resolution. The hint
    lives HERE (not in ``_resolve_bases``) so it names the exact host and the
    exact ``secret set`` command — the host is known only at the resolver
    (this fixes the pre-ADR-0049 bug where ``_resolve_bases`` re-raised
    ``IngestError`` verbatim and the pin-by-digest hint never fired)."""
    base = f"cannot resolve base tag {ref!r}: HTTP {code}"
    if code not in (401, 403):
        return base
    if credential:
        return (
            f"{base} — the stored {configrepo.REGISTRY_SECRET_PREFIX}{registry} credential was"
            " refused; check the value and its pull scope"
        )
    return (
        f"{base} — the image may be private; store a pull credential with"
        f" `theozolith secret set {configrepo.REGISTRY_SECRET_PREFIX}{registry}` (value"
        " `<user>:<token>`, e.g. a GHCR PAT with read:packages) or pin the base"
        " by digest in the Config Repo"
    )


def resolve_image_digest(
    ref: str,
    credentials: dict[str, str] | None = None,
    *,
    hint: Callable[[str, str, int, str], str] | None = None,
) -> str:
    """Resolve a tag-only image ref to its manifest digest via the registry
    HTTP API. This is MECHANICAL pin resolution (ADR-0048): the registry is
    the same authority ``docker pull`` trusts, and the resulting digest-pinned
    ref is what every node build verifies against.

    Attempt 1 is ALWAYS unauthenticated (the anonymous fast path public bases
    keep). On the 401 challenge, when a ``registry:<host>`` credential is
    stored for the challenged host (ADR-0049), the token-realm request carries
    it as HTTP Basic; anonymous otherwise. There is never a third manifest
    attempt — by a 403 the challenge is already gone (GHCR's private-package
    failure mode), so a refused credential surfaces an actionable message.

    ``hint`` overrides the terminal 401/403 message builder: candidate export
    (ADR-0054) discovers its credential from a caller-supplied DOCKER_CONFIG,
    so its remediation must not name the control-node Fernet store."""
    describe = hint or _resolve_http_hint
    registry, repo, tag = _split_image_ref(ref)
    credential = (credentials or {}).get(registry, "")
    url = f"https://{registry}/v2/{repo}/manifests/{urllib.parse.quote(tag, safe='')}"
    token = ""
    for attempt in (1, 2):
        request = urllib.request.Request(url, method="HEAD", headers={"Accept": _MANIFEST_ACCEPT})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with _urlopen(request, timeout=30) as resp:
                digest = resp.headers.get("Docker-Content-Digest", "")
                if not (
                    digest.startswith("sha256:") and _HEX64.fullmatch(digest[len("sha256:") :])
                ):
                    raise IngestError(
                        f"registry returned no usable digest for {ref!r} ({digest!r})"
                    )
                return digest
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                # The token-realm round trip happens INSIDE this handler, so
                # its failures would escape the sibling except clauses — wrap
                # them here, where ref/registry/credential are known. A realm
                # 401/403 is the credential refused at token-mint time (vs the
                # manifest 403, where the anonymous token lacked pull scope);
                # both get the same actionable hint because the operator
                # remedy is the same. Never echo the Authorization header.
                try:
                    token = _registry_token(
                        exc.headers.get("WWW-Authenticate", "") or "", credential
                    )
                except urllib.error.HTTPError as token_exc:
                    if token_exc.code in (401, 403):
                        raise IngestError(
                            describe(ref, registry, token_exc.code, credential)
                        ) from token_exc
                    raise IngestError(
                        f"cannot resolve base tag {ref!r}: registry token endpoint"
                        f" HTTP {token_exc.code}"
                    ) from token_exc
                except urllib.error.URLError as token_exc:
                    raise IngestError(
                        f"cannot resolve base tag {ref!r}: registry token endpoint"
                        f" unreachable: {token_exc.reason}"
                    ) from token_exc
                except ValueError as token_exc:  # json.JSONDecodeError / UnicodeDecodeError
                    raise IngestError(
                        f"cannot resolve base tag {ref!r}: registry token endpoint"
                        " returned a malformed response"
                    ) from token_exc
                continue
            raise IngestError(describe(ref, registry, exc.code, credential)) from exc
        except urllib.error.URLError as exc:
            raise IngestError(f"cannot resolve base tag {ref!r}: {exc.reason}") from exc
    raise IngestError(f"cannot resolve base tag {ref!r}")  # pragma: no cover


# -- CLI Pin resolution (ADR-0055) -------------------------------------------------

_NPM_REGISTRY = "https://registry.npmjs.org"


def _npm_document(url: str, declared: str, what: str) -> dict:
    """One npm registry GET through the shared ``_urlopen`` seam; every
    failure is an ``IngestError`` naming the declared value and the failing
    step (the registry is public — no credential is ever involved)."""
    try:
        with _urlopen(urllib.request.Request(url), timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise IngestError(f"cannot resolve CLI pin {declared!r}: {what}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: {what}: npm registry unreachable: {exc.reason}"
        ) from exc
    except ValueError as exc:  # json.JSONDecodeError / UnicodeDecodeError
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: {what}: npm registry returned"
            " a malformed response"
        ) from exc
    if not isinstance(data, dict):
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: {what}: npm registry returned"
            " a malformed response"
        )
    return data


def resolve_cli_pin(declared: str, *, tool: str = "claude") -> dict:
    """Resolve a CLI Pin declaration (an exact version or an npm dist-tag,
    ADR-0055) to the exact version plus the COMPLETE per-platform
    ``{package, integrity}`` map — the base-tag doctrine applied to the CLI.
    MECHANICAL pin resolution (ADR-0048): the registry npm itself trusts
    answers, and every network-derived trust decision lands here at ingest —
    nodes later select from the pinned map and verify against the pinned
    integrity, never against fetched metadata. A dist-tag re-resolves on
    every ingest, exactly like a moving base tag."""
    adapter = make_agent_adapter(tool)
    packages = getattr(adapter, "CLI_PLATFORM_PACKAGES", None)
    wrapper = getattr(adapter, "CLI_WRAPPER_PACKAGE", "")
    if not packages or not wrapper:
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: adapter {tool!r} declares no"
            " supported CLI platform table (ADR-0055)"
        )
    # npm serves both an exact version and a dist-tag at this endpoint.
    document = _npm_document(
        f"{_NPM_REGISTRY}/{urllib.parse.quote(wrapper, safe='')}"
        f"/{urllib.parse.quote(declared, safe='')}",
        declared,
        f"version document for {wrapper}",
    )
    version = document.get("version", "")
    if not isinstance(version, str) or not _CLI_VERSION.fullmatch(version):
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: {wrapper} resolved to"
            f" {version!r}, not an exact <major>.<minor>.<patch> version"
        )
    floor = adapter.MIN_ENFORCING_CLI
    if _cli_version_tuple(version) < tuple(floor):
        floor_text = ".".join(str(part) for part in floor)
        raise IngestError(
            f"cannot resolve CLI pin {declared!r}: resolved version {version} is"
            f" below the {tool} adapter's enforcement floor {floor_text}"
            " (ADR-0055) — pin a newer version"
        )
    platforms: dict[str, dict[str, str]] = {}
    for tuple_key, package in sorted(packages.items()):
        # A supported tuple the registry cannot supply fails the ingest
        # (ADR-0055): the pinned map must be COMPLETE, or a node of that
        # platform would discover the gap only at install time.
        entry = _npm_document(
            f"{_NPM_REGISTRY}/{urllib.parse.quote(package, safe='')}/{version}",
            declared,
            f"platform package {package} ({tuple_key}) at {version}",
        )
        dist = entry.get("dist")
        integrity = dist.get("integrity", "") if isinstance(dist, dict) else ""
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            raise IngestError(
                f"cannot resolve CLI pin {declared!r}: platform package"
                f" {package} ({tuple_key}) at {version} carries no sha512 SRI"
                " integrity — a supported tuple the registry cannot supply"
                " fails the ingest (ADR-0055)"
            )
        platforms[tuple_key] = {"package": package, "integrity": integrity}
    return {"version": version, "platforms": platforms}


def _cli_version_tuple(version: str) -> tuple[int, int, int]:
    """The comparable (major, minor, patch) of a validated version string."""
    core = version.split("-", 1)[0]
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch)
