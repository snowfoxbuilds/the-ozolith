"""`theozolith config ingest` (ADR-0048): human Config Repo -> pinned build.

The human-authored Config Repo (a local path or a git URL) is the source of
truth; the machine-owned pinned build (``configs/``) is what control loads and
distributes. This module is the ONLY path between them:

1. HARVEST the Config Repo at its current commit (a dirty git source is
   refused — the pinned build stamps the source commit, so the stamp must be
   truthful; a plain folder harvests under a content hash).
2. STAGE a candidate tree: config files copied verbatim, ``knowledge/<name>``
   trees compiled by the ADR-0009 compiler (compile errors surface HERE, not
   at image build or container start), ``control.toml`` merged (the machine-
   written [control] address block is preserved from the pinned build; the
   [settings] surface is harvested from the source), and ``pins.toml``
   written with the resolved pins.
3. RESOLVE pins where resolution is mechanical: per-knowledge-tree content
   hashes and base tag->digest. NEVER where the value must come out-of-band —
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
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_knowledge import KnowledgeError, compile_claude, load_knowledge_root

from theozolith_control import configdist, configrepo, controltoml

# A sha256 whose value is the fail-closed placeholder convention (all zeros):
# legal in a template (a scaffolded or dormant worker type), refused for any
# worker type a running Stack references (ADR-0048).
PLACEHOLDER_SHA256 = "0" * 64

# Top-level entries never copied from the source tree into the staging tree.
_SKIP_TOP_LEVEL = (".git", configdist.KNOWLEDGE_DIR, controltoml.CONTROL_TOML)

_HEX64 = re.compile(r"[0-9a-f]{64}")

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


class IngestError(RuntimeError):
    """The ingest cannot proceed; nothing has been committed."""


@dataclass
class IngestReport:
    """What one ingest did — printed by the CLI, asserted by tests."""

    source_commit: str = ""
    pinned_commit: str = ""  # the pinned build's new HEAD ("" when unchanged)
    changed: bool = False
    resolved_bases: dict[str, str] = field(default_factory=dict)  # ref -> digest
    knowledge_pins: dict[str, str] = field(default_factory=dict)  # tree -> hash
    retagged: dict[str, tuple[str, str]] = field(default_factory=dict)  # type -> (old, new)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"source commit: {self.source_commit}"]
        if self.changed:
            lines.append(f"pinned build committed: {self.pinned_commit}")
        else:
            lines.append("pinned build unchanged (already up to date)")
        for ref, digest in sorted(self.resolved_bases.items()):
            lines.append(f"resolved base {ref} -> {digest[:19]}…")
        for name, tree_hash in sorted(self.knowledge_pins.items()):
            lines.append(f"knowledge/{name} pinned {tree_hash[:12]}")
        for name, (old, new) in sorted(self.retagged.items()):
            lines.append(f"worker type {name} re-tagged: {old or '(new)'} -> {new}")
        lines.extend(self.notes)
        return "\n".join(lines)


def _run_git(args: list[str], cwd: Path, runner) -> subprocess.CompletedProcess:
    return runner(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _require_git(args: list[str], cwd: Path, runner, what: str) -> str:
    proc = _run_git(args, cwd, runner)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise IngestError(f"{what}: git {args[0]} failed: {detail}")
    return (proc.stdout or "").strip()


def _is_git_url(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def _folder_commit(root: Path) -> str:
    """Content stamp for a non-git source folder — same convention as the
    pinned build's own folder mode (``configrepo._commit``)."""
    digest = hashlib.sha256()
    for path in configdist.regular_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return f"folder-{digest.hexdigest()[:12]}"


def _harvest_source(source: str, workdir: Path, runner) -> tuple[Path, str]:
    """The source tree to stage from, plus its provenance stamp."""
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
        return clone, _require_git(["rev-parse", "HEAD"], clone, runner, source)
    root = Path(source).expanduser()
    if not root.is_dir():
        raise IngestError(f"Config Repo {source} is not a directory (or a git URL)")
    if (root / ".git").exists():
        dirty = _require_git(["status", "--porcelain"], root, runner, str(root))
        if dirty:
            raise IngestError(
                f"Config Repo {root} has uncommitted changes — commit them first;"
                " the pinned build stamps the source commit, so the stamp must"
                " be truthful (ADR-0048):\n" + dirty
            )
        head = _run_git(["rev-parse", "HEAD"], root, runner)
        if head.returncode == 0:
            return root, (head.stdout or "").strip()
        # A clean repo with no commits yet (init just scaffolded an empty
        # Config Repo): harvest it as the (empty) folder it is.
        return root, _folder_commit(root)
    try:
        return root, _folder_commit(root)
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


def _compile_knowledge(source_dir: Path, staging: Path) -> dict[str, str]:
    """Compile every ``knowledge/<name>`` source tree into the staging tree
    (ADR-0009 at ingest) and return the per-tree content pins."""
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
            fileset = compile_claude(load_knowledge_root(entry), scope="global")
        except KnowledgeError as exc:
            raise IngestError(f"knowledge/{entry.name} does not compile: {exc}") from exc
        for relpath, file_entry in fileset.items():
            target = staging / configdist.KNOWLEDGE_DIR / entry.name / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file_entry.content)
            if file_entry.executable:
                target.chmod(0o755)
        try:
            pins[entry.name] = configdist.knowledge_tree_hash(staging, entry.name)
        except configdist.ConfigDistError as exc:
            raise IngestError(f"knowledge/{entry.name}: {exc}") from exc
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


def _write_pins(
    staging: Path, source_commit: str, bases: dict[str, str], knowledge: dict[str, str]
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


def _sync_into_pinned(staging: Path, pinned_dir: Path) -> None:
    """Replace the pinned working tree's content with the staged tree (the
    ``.git`` directory stays). Content-identical files produce no git diff, so
    the commit below records exactly the real change set."""
    for entry in sorted(pinned_dir.iterdir(), key=lambda p: p.name):
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in sorted(staging.iterdir(), key=lambda p: p.name):
        if entry.is_dir():
            shutil.copytree(entry, pinned_dir / entry.name, symlinks=False)
        else:
            shutil.copy2(entry, pinned_dir / entry.name)


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
    runner=None,
    log=print,
) -> IngestReport:
    """Run the full pipeline; raises ``IngestError`` with nothing committed on
    any refusal. See the module docstring for the steps."""
    # Resolved at call time (not as a def-time default) so test rigs that
    # monkeypatch subprocess.run fake the git layer here too.
    runner = runner or subprocess.run
    pinned_dir = Path(pinned_dir)
    report = IngestReport()
    resolve = resolve_digest or resolve_image_digest

    # The pinned build must exist as a clean git repo before anything is staged.
    pinned_dir.mkdir(parents=True, exist_ok=True)
    if not (pinned_dir / ".git").exists():
        _require_git(["init", "--quiet"], pinned_dir, runner, str(pinned_dir))
    dirty = _require_git(["status", "--porcelain"], pinned_dir, runner, str(pinned_dir))
    if dirty:
        raise IngestError(
            f"pinned build {pinned_dir} has uncommitted changes — it is machine-"
            "owned and committed only by ingest; restore it (git checkout/reset)"
            " before ingesting (ADR-0048):\n" + dirty
        )
    old_control_toml = ""
    control_path = pinned_dir / controltoml.CONTROL_TOML
    if control_path.is_file():
        old_control_toml = control_path.read_text(encoding="utf-8")
    old_tags = _tags_of(pinned_dir)
    old_product = _product_version_of(pinned_dir)

    with tempfile.TemporaryDirectory(prefix="theozolith-ingest-") as workdir:
        work = Path(workdir)
        source_dir, report.source_commit = _harvest_source(source, work, runner)
        staging = work / "staging"
        staging.mkdir()
        _copy_config_files(source_dir, staging)
        report.knowledge_pins = _compile_knowledge(source_dir, staging)
        _refuse_live_placeholders(staging)
        report.resolved_bases = _resolve_bases(staging, resolve)
        _write_pins(staging, report.source_commit, report.resolved_bases, report.knowledge_pins)
        _merge_control_toml(source_dir, pinned_dir, staging)

        # LINT: the staged tree must load under the exact fail-loud checks the
        # server applies — a config that would not load is never committed.
        try:
            staged_config = configrepo.load_config(staging)
        except configrepo.ConfigRepoError as exc:
            raise IngestError(f"staged config does not load — nothing committed: {exc}") from exc
        for warning in staged_config.warnings:
            report.notes.append(f"warning: {warning}")

        _sync_into_pinned(staging, pinned_dir)

    _require_git(["add", "-A"], pinned_dir, runner, str(pinned_dir))
    staged = _require_git(["status", "--porcelain"], pinned_dir, runner, str(pinned_dir))
    if not staged:
        report.changed = False
        log(report.summary())
        return report
    _require_git(
        [
            "-c",
            f"user.name={controltoml.COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={controltoml.COMMIT_AUTHOR_EMAIL}",
            "commit",
            "--quiet",
            "-m",
            f"theozolith config ingest: source {report.source_commit}",
        ],
        pinned_dir,
        runner,
        str(pinned_dir),
    )
    report.changed = True
    report.pinned_commit = _require_git(["rev-parse", "HEAD"], pinned_dir, runner, str(pinned_dir))

    new_tags = _tags_of(pinned_dir)
    report.retagged = {
        name: (old_tags.get(name, ""), tag)
        for name, tag in sorted(new_tags.items())
        if old_tags.get(name, "") != tag
    }
    new_product = _product_version_of(pinned_dir)
    if new_product != old_product:
        # The product-update flow (`theozolith update`) also writes the
        # product.toml pin into the pinned build; ingest overwrites it with
        # the source's value, so a divergence is surfaced, never silent.
        report.notes.append(
            f"product version: {old_product or '(none)'} -> {new_product or '(none)'}"
            " — the Config Repo's product.toml wins over any pin the update"
            " flow wrote since the last ingest"
        )
    new_control = control_path.read_text(encoding="utf-8") if control_path.is_file() else ""
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


def _anonymous_token(www_authenticate: str) -> str:
    """The anonymous bearer-token flow a public registry answers 401 with."""
    if not www_authenticate.lower().startswith("bearer "):
        raise IngestError(f"registry auth challenge not understood: {www_authenticate!r}")
    params = dict(re.findall(r'(\w+)="([^"]*)"', www_authenticate[len("bearer ") :]))
    realm = params.get("realm", "")
    if not realm.startswith("https://"):
        raise IngestError(f"registry auth realm not https: {realm!r}")
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    url = realm + ("?" + urllib.parse.urlencode(query) if query else "")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("token") or data.get("access_token") or ""
    if not token:
        raise IngestError("registry token endpoint returned no token")
    return token


def resolve_image_digest(ref: str) -> str:
    """Resolve a tag-only image ref to its manifest digest via the registry
    HTTP API (anonymous pull scope). This is MECHANICAL pin resolution
    (ADR-0048): the registry is the same authority ``docker pull`` trusts, and
    the resulting digest-pinned ref is what every node build verifies against."""
    registry, repo, tag = _split_image_ref(ref)
    url = f"https://{registry}/v2/{repo}/manifests/{urllib.parse.quote(tag, safe='')}"
    token = ""
    for attempt in (1, 2):
        request = urllib.request.Request(url, method="HEAD", headers={"Accept": _MANIFEST_ACCEPT})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
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
                token = _anonymous_token(exc.headers.get("WWW-Authenticate", "") or "")
                continue
            raise IngestError(f"cannot resolve base tag {ref!r}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise IngestError(f"cannot resolve base tag {ref!r}: {exc.reason}") from exc
    raise IngestError(f"cannot resolve base tag {ref!r}")  # pragma: no cover
