"""The single human CLI (ADR-0032): one parser carries both halves, the
deprecated alias points at the same main, the fleet-operator error
contract (``ProductError`` -> 1) survives the fold, and the old command
spelling survives only in deliberate references."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from theozolith_control import product
from theozolith_control.cli import main as cli_main

# Where `theozolith-control` may still legitimately appear on a line
# (PR #10 review round 1): the component's distribution and docker-image
# names, the deprecated-alias console script, and deprecation notes that
# say "alias" in the same breath — plus, since ADR-0034, the systemd unit
# and the system data dir, which carry the component name, not the command.
# Everything else is a missed sweep.
_ALLOWED_LINE = re.compile(
    r"theozolith-control:"  # docker image tags — the component, not the command
    r"|name = \"theozolith-control\""  # the pip distribution name
    r"|^theozolith-control = \"theozolith_control\.cli:main\"$"  # the alias script
    r"|^# theozolith-control$"  # the component README heading
    r"|theozolith-control\.service"  # the systemd unit (ADR-0034)
    r"|/var/lib/theozolith-control"  # the root-mediated data dir (ADR-0034)
    r"|alias"  # deprecation notes
)
_SKIP_PARTS = {
    ".git",
    ".venv",
    ".claude",
    "docs",  # ADRs/specs are records; they may quote the old surface
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "node_modules",
}
# Skipped like docs/ above: the index and glossary quote historical ADR
# summaries where the old surface legitimately appears (repo-authored since
# ADR-0050; CLAUDE.md is generated from AGENTS.md). uv.lock is generated
# and names the distribution.
_SKIP_FILES = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md", "uv.lock"}


def test_old_spelling_survives_only_in_deliberate_references():
    root = Path(__file__).resolve().parents[2]
    self_path = Path(__file__).resolve()
    offenders = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if set(rel.parts) & _SKIP_PARTS or any(p.endswith(".egg-info") for p in rel.parts):
            continue
        if path.name in _SKIP_FILES:
            continue
        if not path.is_file() or path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "theozolith-control" in line and not _ALLOWED_LINE.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, "old command spelling outside deliberate references:\n" + "\n".join(
        offenders
    )


def test_operator_commands_dispatch_through_the_merged_parser(monkeypatch):
    seen = {}

    def fake_update(args):
        seen["version"] = args.version
        return 0

    monkeypatch.setattr(product, "_cmd_update", fake_update)
    assert cli_main(["update", "--version", "1.2.3"]) == 0
    assert seen == {"version": "1.2.3"}


def test_product_errors_keep_their_exit_code(monkeypatch, capsys):
    def explode(args):
        raise product.ProductError("boom")

    monkeypatch.setattr(product, "_cmd_update", explode)
    assert cli_main(["update"]) == 1
    assert "error: boom" in capsys.readouterr().err


def test_product_main_delegates_to_the_merged_cli(monkeypatch):
    monkeypatch.setattr(product, "_cmd_update", lambda args: 0)
    assert product.main(["update"]) == 0


def test_both_entry_points_share_one_main():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["theozolith"] == "theozolith_control.cli:main"
    assert scripts["theozolith-control"] == scripts["theozolith"]


def test_config_ingest_dispatches_with_the_default_source(monkeypatch, tmp_path):
    """`theozolith config ingest` (ADR-0048): no positional -> the scaffolded
    config-src beside the data dir; an explicit path/URL wins."""
    from theozolith_control import cli, ingest

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(
        ingest, "ingest", lambda source, pinned, **kw: calls.append((source, pinned)) or None
    )
    monkeypatch.setattr(cli, "_repair_partition_ownership", lambda settings: None)
    assert cli_main(["config", "ingest"]) == 0
    assert cli_main(["config", "ingest", "https://example.invalid/config.git"]) == 0
    assert calls[0][0] == str(tmp_path / "home" / "config-src")
    assert calls[0][1] == tmp_path / "home" / "configs"
    assert calls[1][0] == "https://example.invalid/config.git"


def test_config_ingest_dry_run_flag_dispatches(monkeypatch, tmp_path):
    """`config ingest --dry-run` (the config linter) threads the flag through
    and skips the partition-ownership repair — a dry run changes nothing."""
    from theozolith_control import cli, ingest

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(
        ingest, "ingest", lambda source, pinned, **kw: calls.append(kw.get("dry_run")) or None
    )

    def no_repair(settings):
        raise AssertionError("a dry run must not repair partition ownership")

    monkeypatch.setattr(cli, "_repair_partition_ownership", no_repair)
    assert cli_main(["config", "ingest", "--dry-run"]) == 0
    assert calls == [True]


def test_config_migrate_dispatches_with_deployment_defaults(monkeypatch, tmp_path):
    """`theozolith config migrate` (ADR-0048 amendment): no arguments ->
    legacy = the deployment's configs/ (the future pinned build), dest = the
    config-src location; explicit paths win."""
    from theozolith_control import cli, migrate

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(
        migrate,
        "migrate_legacy",
        lambda legacy, dest, **kw: calls.append((legacy, dest)) or None,
    )
    monkeypatch.setattr(cli, "_repair_partition_ownership", lambda settings: None)
    assert cli_main(["config", "migrate"]) == 0
    assert cli_main(["config", "migrate", str(tmp_path / "new-src"), "--legacy", "/old"]) == 0
    assert calls[0] == (tmp_path / "home" / "configs", tmp_path / "home" / "config-src")
    assert calls[1] == (Path("/old"), tmp_path / "new-src")


def test_config_migrate_refusal_exits_nonzero(monkeypatch, tmp_path):
    from theozolith_control import migrate

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))

    def refuse(legacy, dest, **kw):
        raise migrate.MigrateError("migration destination is not empty")

    monkeypatch.setattr(migrate, "migrate_legacy", refuse)
    with pytest.raises(SystemExit, match="not empty"):
        cli_main(["config", "migrate"])


def test_config_ingest_refusal_exits_nonzero(monkeypatch, tmp_path, capsys):
    from theozolith_control import ingest

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))

    def refuse(source, pinned, **kw):
        raise ingest.IngestError("pinned build has uncommitted changes")

    monkeypatch.setattr(ingest, "ingest", refuse)
    with pytest.raises(SystemExit, match="uncommitted changes"):
        cli_main(["config", "ingest"])


# -- registry pull credentials for ingest (ADR-0049) ---------------------------


def test_registry_credentials_decrypts_only_registry_prefixed_secrets(tmp_path, monkeypatch):
    """`_registry_credentials` returns host -> `<user>:<token>` for stored
    `registry:` secrets, ignores every other name, and NEVER creates store.db
    as a side effect when none exists."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    settings = make_settings(tmp_path)
    assert cli._registry_credentials(settings) == {}
    assert not settings.store_db_path.exists()  # reading created nothing
    assert not settings.key_path.exists()  # ... and no master key either

    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    settings.key_path.write_text(key + "\n", encoding="utf-8")
    box = SecretBox(key)
    store = SecretStore(settings.store_db_path)
    store.put_secret("registry:ghcr.io", box.encrypt("octocat:ghp_token"))
    store.put_secret("registry:localhost:5000", box.encrypt("u:t"))
    store.put_secret("github-implementer", box.encrypt("ghp_ignored"))

    assert cli._registry_credentials(settings) == {
        "ghcr.io": "octocat:ghp_token",
        "localhost:5000": "u:t",
    }


def test_registry_credentials_fails_loud_on_a_corrupt_credential(tmp_path, monkeypatch):
    """A stored credential that will not decrypt is FATAL — never a silent
    degrade to anonymous resolution that then 403s with a misleading message."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    settings = make_settings(tmp_path)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    box = SecretBox(generate_key())  # a DIFFERENT key than the one on disk
    store = SecretStore(settings.store_db_path)
    store.put_secret("registry:ghcr.io", box.encrypt("octocat:ghp_token"))
    settings.key_path.write_text(generate_key() + "\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="does not decrypt"):
        cli._registry_credentials(settings)


def test_registry_credentials_without_registry_secrets_never_touches_the_key(tmp_path, monkeypatch):
    """A store holding only ordinary secrets is the common public-base
    deployment: discovery returns {} WITHOUT loading — or creating — any key
    material (the throwaway box below never touches disk)."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY_FILE", raising=False)
    settings = make_settings(tmp_path)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    box = SecretBox(generate_key())
    store = SecretStore(settings.store_db_path)
    store.put_secret("github-implementer", box.encrypt("ghp_ordinary"))

    assert cli._registry_credentials(settings) == {}
    assert not settings.key_path.exists()  # no key was read OR generated


def test_registry_credentials_missing_key_fails_without_creating_one(tmp_path, monkeypatch):
    """A registry secret with no master key anywhere is FATAL and actionable —
    and the read path must never manufacture a replacement key: a lost key
    stays lost until the operator restores it (recovery material is sacred)."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY_FILE", raising=False)
    settings = make_settings(tmp_path)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    box = SecretBox(generate_key())
    store = SecretStore(settings.store_db_path)
    store.put_secret("registry:ghcr.io", box.encrypt("octocat:ghp_token"))

    with pytest.raises(SystemExit, match="master key is missing"):
        cli._registry_credentials(settings)
    assert not settings.key_path.exists()  # nothing was created or replaced


def test_registry_credentials_invalid_key_material_is_a_clean_exit(tmp_path, monkeypatch):
    """Garbage in master.key surfaces as an actionable SystemExit, never a raw
    CryptoError traceback — and the file is left exactly as found."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY_FILE", raising=False)
    settings = make_settings(tmp_path)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    box = SecretBox(generate_key())
    store = SecretStore(settings.store_db_path)
    store.put_secret("registry:ghcr.io", box.encrypt("octocat:ghp_token"))
    settings.key_path.write_text("not-a-fernet-key\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="invalid master key"):
        cli._registry_credentials(settings)
    assert settings.key_path.read_text(encoding="utf-8") == "not-a-fernet-key\n"


def test_registry_credentials_env_key_needs_no_key_file(tmp_path, monkeypatch):
    """THEOZOLITH_MASTER_KEY alone decrypts stored registry credentials — the
    key-file read is the fallback, and the env path writes no file either."""
    from controlrig import make_settings
    from theozolith_control import cli
    from theozolith_control.crypto import SecretBox, generate_key
    from theozolith_control.secretstore import SecretStore

    key = generate_key()
    monkeypatch.setenv("THEOZOLITH_MASTER_KEY", key)
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY_FILE", raising=False)
    settings = make_settings(tmp_path)
    settings.secrets_dir.mkdir(parents=True, exist_ok=True)
    box = SecretBox(key)
    store = SecretStore(settings.store_db_path)
    store.put_secret("registry:ghcr.io", box.encrypt("octocat:ghp_token"))

    assert cli._registry_credentials(settings) == {"ghcr.io": "octocat:ghp_token"}
    assert not settings.key_path.exists()


def test_config_ingest_dry_run_discovery_leaves_the_filesystem_untouched(tmp_path, monkeypatch):
    """`config ingest --dry-run` on a fresh data dir: credential discovery must
    change NOTHING — no store.db, no master.key, no pinned build. The whole
    data-dir tree is byte-listed before and after."""
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY", raising=False)
    monkeypatch.delenv("THEOZOLITH_MASTER_KEY_FILE", raising=False)
    data_dir = tmp_path / "home"
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(data_dir))

    digest = "a" * 64
    src = tmp_path / "config-src"
    for relpath, text in {
        "worker-types/claude-dev.toml": (
            'driver = "builtin:implementer"\nadapter = "claude"\n'
            'model = "claude-sonnet-5"\nworkspace = "acme/sandbox"\n'
            f'base = "ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2@sha256:{digest}"\n'
            '[secrets]\nGITHUB_TOKEN = "github-implementer"\n'
        ),
        "stacks/implementer.toml": 'worker_type = "claude-dev"\nnode = "box1"\nstate = "stopped"\n',
        "control.toml": "[settings]\nheartbeat_seconds = 30\n",
        "product.toml": '[product]\nversion = "0.3.0"\n',
    }.items():
        target = src / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def tree(root: Path) -> set[str]:
        if not root.exists():
            return set()
        return {str(p.relative_to(root)) for p in root.rglob("*")}

    before = tree(data_dir)
    assert cli_main(["config", "ingest", "--dry-run", str(src)]) == 0
    assert tree(data_dir) == before
    assert not (data_dir / "secrets").exists()  # in particular: no key, no store
    assert not (data_dir / "configs").exists()  # ... and no pinned build
