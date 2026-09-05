"""Codex custom agent role files: ``agents/codex/<stem>.toml``.

A role file is what codex-cli discovers under ``$CODEX_HOME/agents/``: a
native subagent definition, not a prompt. Three role metadata fields —
``name``, ``description``, ``nickname_candidates`` — sit flattened over a
complete ``config.toml`` layer (``developer_instructions``, ``model``,
``model_reasoning_effort``, ``sandbox_mode``, ``mcp_servers``,
``skills.config``, ...) that codex applies as a session-configuration layer
to the subagent it spawns with the role. Which keys of that layer a given
codex version actually applies to the child, and how they rank against the
parent session's permissions, are codex's concerns: the file is carried
byte-for-byte and never rewritten.

The loader mirrors codex-cli's standalone role parser
(``codex-rs/agent-roles/src/agent_role_config.rs`` at the baseline tag):

- ``name`` is trimmed (Rust ``str::trim``) and must be non-empty; the trimmed
  value is the role's identity, unique across the root. Codex puts no
  character rule on the name itself.
- ``description`` and ``developer_instructions`` are required and non-blank
  after trimming (codex requires both for a discovered role file).
- ``nickname_candidates``, when present, is a non-empty array whose entries
  are trimmed, non-blank, unique after trimming, and limited to ASCII
  letters, digits, spaces, hyphens, and underscores.
- Everything else is the configuration layer, validated against the vendored
  codex ``config.toml`` schema: the file's top level is closed (codex denies
  unknown fields on the role wrapper), nested tables are open or closed as
  the schema says, and every value must have the type codex deserializes.

Relative paths in the layer (``model_instructions_file``, ``skills.config[].path``,
...) keep codex's own semantics: codex resolves them against the role file's
directory, ``$CODEX_HOME/agents/`` in the compiled view, and never checks at
load that they exist. Ozolith preserves the values; whatever they reference
must exist in the environment the view is installed into.

Schema baseline
---------------
Validation is offline, deterministic, and versioned: the schema is the
``config.schema.json`` codex generates from its ``ConfigToml`` type
(``codex-rs/config-schema``), vendored verbatim from the supported codex-cli
baseline and pinned by digest in ``CODEX_SCHEMA_BASELINE``. Nothing is
fetched at load or ingest. A role using a key a newer codex adds is refused
until the baseline moves — that is deliberate: codex warn-and-skips a role
file it cannot parse, so a key the supported codex does not know would
silently shrink a deck's roster.

To move the baseline to codex-cli ``X.Y.Z``:

1. Fetch ``codex-rs/core/config.schema.json`` at tag ``rust-vX.Y.Z`` of
   github.com/openai/codex and replace ``codex_schema/config.schema.json``
   byte-for-byte.
2. Record the tag, its commit, and the file's sha256 in
   ``CODEX_SCHEMA_BASELINE``; re-read ``agent_role_config.rs`` at that tag
   and fold any change in the metadata rules above into this module.
3. Run the knowledge tests: the schema test proves the digest and that the
   checker supports every keyword the new schema uses (``schemacheck``
   refuses vocabulary it does not implement), and the role tests prove the
   representative roles still load.
4. Move the deck example's CLI Pin and the adapter floor in their own
   validated-CLI review; the baseline names what the compiler validates
   against, not what a deck may run.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from theozolith_knowledge import schemacheck
from theozolith_knowledge.errors import KnowledgeError


@dataclass(frozen=True)
class CodexSchemaBaseline:
    cli_version: str
    git_tag: str
    commit: str
    upstream_path: str
    sha256: str


CODEX_SCHEMA_BASELINE = CodexSchemaBaseline(
    cli_version="0.153.3",
    git_tag="rust-v0.153.3",
    commit="b1a547b1f73ce86205d9222ac19cff334b3b7a2e",
    upstream_path="codex-rs/core/config.schema.json",
    sha256="692da7699367f6f4fbbd46c0021278c1311440bcebf0bcb9b836690c05e56196",
)
SCHEMA_RESOURCE = "codex_schema/config.schema.json"

ROLE_METADATA_FIELDS = ("name", "description", "nickname_candidates")
ROLE_REQUIRED_FIELDS = ("name", "description", "developer_instructions")

# What Rust's str::trim strips — the Unicode White_Space property — which is
# not Python's str.strip() set (U+001C..U+001F are Python whitespace only).
RUST_WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


@dataclass(frozen=True)
class CodexRole:
    """A loaded role's identity: the trimmed ``name`` codex keys it by and
    its trimmed nickname candidates. The file itself ships verbatim."""

    name: str
    nickname_candidates: tuple[str, ...]


def rust_trim(text: str) -> str:
    return text.strip(RUST_WHITESPACE)


@cache
def codex_config_schema() -> dict:
    """The vendored schema, digest-checked against the baseline and proven
    fully supported by the checker before the first role is validated."""
    data = resources.files("theozolith_knowledge").joinpath(SCHEMA_RESOURCE).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    baseline = CODEX_SCHEMA_BASELINE
    if digest != baseline.sha256:
        raise KnowledgeError(
            f"vendored codex config schema {SCHEMA_RESOURCE} has sha256 {digest}, not the "
            f"{baseline.sha256} recorded for codex-cli {baseline.cli_version}: the package is "
            "damaged or the schema moved without re-recording CODEX_SCHEMA_BASELINE"
        )
    schema = json.loads(data)
    problems = schemacheck.schema_problems(schema)
    if problems:
        raise KnowledgeError(
            f"vendored codex config schema for codex-cli {baseline.cli_version} uses vocabulary "
            f"the checker does not implement: {'; '.join(problems[:5])}"
        )
    return schema


def parse_codex_role(path: Path) -> CodexRole:
    """Validate one role file the way codex-cli at the baseline would parse
    it, refusing anything codex would warn-and-skip. Raises KnowledgeError."""
    what = f"codex agent role {path}"
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise KnowledgeError(f"{what} is not valid TOML: {exc}") from exc
    except RecursionError:
        raise KnowledgeError(f"{what} is not valid TOML: it nests too deeply") from None
    try:
        return _role_from_table(table, what)
    except RecursionError:
        raise KnowledgeError(f"{what} nests too deeply to validate") from None


def _role_from_table(table: dict, what: str) -> CodexRole:
    for field in ("name", "description"):
        if field in table and not isinstance(table[field], str):
            kind = schemacheck.kind_of(table[field])
            raise KnowledgeError(f"{what}: {field!r} must be a string, got {kind}")
    nicknames = table.get("nickname_candidates")
    if nicknames is not None and (
        not isinstance(nicknames, list) or not all(isinstance(n, str) for n in nicknames)
    ):
        raise KnowledgeError(f"{what}: 'nickname_candidates' must be an array of strings")

    layer = {key: value for key, value in table.items() if key not in ROLE_METADATA_FIELDS}
    try:
        schemacheck.check(layer, codex_config_schema())
    except schemacheck.SchemaViolation as exc:
        raise KnowledgeError(f"{what}: {exc}") from exc
    except schemacheck.UnsupportedSchema as exc:
        raise KnowledgeError(
            f"vendored codex config schema for codex-cli {CODEX_SCHEMA_BASELINE.cli_version} "
            f"uses vocabulary the checker does not implement: {exc}"
        ) from exc

    if not rust_trim(table.get("description") or ""):
        raise KnowledgeError(f"{what}: 'description' must be a non-blank string")
    if not rust_trim(layer.get("developer_instructions") or ""):
        raise KnowledgeError(f"{what}: 'developer_instructions' must be a non-blank string")
    name = rust_trim(table.get("name") or "")
    if not name:
        raise KnowledgeError(f"{what}: 'name' must be a non-blank string")
    return CodexRole(name=name, nickname_candidates=_nickname_candidates(nicknames, what))


def _nickname_candidates(nicknames: list[str] | None, what: str) -> tuple[str, ...]:
    if nicknames is None:
        return ()
    if not nicknames:
        raise KnowledgeError(f"{what}: 'nickname_candidates' must contain at least one name")
    trimmed: list[str] = []
    for raw in nicknames:
        nickname = rust_trim(raw)
        if not nickname:
            raise KnowledgeError(f"{what}: 'nickname_candidates' cannot contain blank names")
        if nickname in trimmed:
            raise KnowledgeError(
                f"{what}: 'nickname_candidates' has duplicates after trimming: {nickname!r}"
            )
        if not all((c.isascii() and c.isalnum()) or c in " -_" for c in nickname):
            raise KnowledgeError(
                f"{what}: nickname {nickname!r} may contain only ASCII letters, digits, spaces, "
                "hyphens, and underscores"
            )
        trimmed.append(nickname)
    return tuple(trimmed)
