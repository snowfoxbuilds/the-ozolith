"""Agent Policy validation for the Claude adapter (ADR-0055).

An Agent Policy tree (``policy/<name>`` in the Config Repo) is a set of
verbatim Claude Code managed-settings drop-ins delivered into Flight Decks
live and baked into driver-type images. Verbatim delivery is only safe
because admission is ALLOWLIST-restricted: a managed-settings document can
carry executable references (``hooks``, helper commands, a status line),
identity/steering keys, and dynamically fetched configuration — so this
module admits a document only when every top-level key is positively
classified as static and declarative, and validates each admitted key by a
RECURSIVELY CLOSED schema. Everything else refuses, naming the file and the
offending dotted key path and never echoing document values. The default
posture is refusal: an unclassified key — today's or a future vendor
setting — can never silently become an execution or identity surface.

This is adapter-owned product code beside the claude adapter's identity
machinery (``identity.py``): the allowlist advances ONLY through the same
deliberate classification review that moves
``ClaudeAdapter.MIN_ENFORCING_CLI`` (ADR-0055 §2) — a review that
classifies the full nested schema of a key, never just its name. The one
validator runs at ingest AND at config load (ADR-0055 §2), byte-identical
rules at both sites; the ADR-0045 conflict scan
(``identity.scan_managed_conflicts``) keeps its driver-image build-gate
site unchanged, as defense in depth.
"""

from __future__ import annotations

import json
from pathlib import Path

from theozolith_worker import identity


class PolicyError(RuntimeError):
    """An Agent Policy tree or drop-in violates the safe-key allowlist or shape."""


# v1 safe-key allowlist (ADR-0055 §2). Each admitted top-level key maps to a
# RECURSIVELY CLOSED schema: a dict value is a JSON object closed to exactly
# the enumerated members (an unknown nested member, a wrong type, or extra
# nesting depth refuses exactly as an unclassified top-level key does); a
# Python type value is a scalar of exactly that JSON type. bool is checked
# by identity (type(v) is bool) — JSON true is not 1.
SAFE_KEYS: dict[str, object] = {
    "attribution": {"sessionUrl": bool},
}

# Top-level managed keys that reference something the CLI EXECUTES or
# resolves dynamically — hooks and helper commands, the status-line command,
# and plugin/MCP registration. Named separately from the identity keys so a
# refusal can say WHY the key can never be admitted; the refusal itself
# would fire anyway (nothing outside SAFE_KEYS is admitted).
EXECUTABLE_REFERENCE_KEYS = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "hooks",
    "otelHeadersHelper",
    "statusLine",
)

_JSON_TYPE_NAMES = {
    bool: "boolean",
}


def _refuse_key(label: str, key: str) -> PolicyError:
    """The classified refusal for one unadmitted TOP-LEVEL key. The message
    names the file and key only — never a value."""
    if key in identity.IDENTITY_SETTING_KEYS:
        detail = (
            "an identity/steering key — a policy tree can never select or"
            " constrain the model/effort identity (ADR-0045/ADR-0055)"
        )
    elif key in EXECUTABLE_REFERENCE_KEYS:
        detail = (
            "an executable-reference key — a live-delivered policy document"
            " can never name a command, hook, or helper the CLI would execute"
            " (ADR-0055)"
        )
    elif key == "env":
        detail = (
            "no admitted env classes exist — a managed env block can steer"
            " models, providers, and helpers, so none of it is admissible"
            " (ADR-0055)"
        )
    else:
        detail = (
            "not on the safe-key allowlist — only positively classified"
            " declarative keys are admitted, and the allowlist advances only"
            " through the adapter's classification review (ADR-0055)"
        )
    return PolicyError(f"{label}: key '{key}' is {detail}")


def _validate_member(label: str, path: str, schema: object, value: object) -> None:
    """One admitted member against its recursively closed schema. Unknown
    nested members, wrong types, and extra nesting depth all refuse like an
    unclassified top-level key (ADR-0055): confirming a key holds an object
    proves nothing about what the object carries, so the interior closes
    too."""
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise PolicyError(
                f"{label}: key '{path}' must be a JSON object closed to {sorted(schema)} (ADR-0055)"
            )
        for member in value:
            if not isinstance(member, str) or member not in schema:
                shown = member if isinstance(member, str) else repr(type(member).__name__)
                raise PolicyError(
                    f"{label}: key '{path}.{shown}' is not an enumerated member"
                    f" of '{path}' — the schema is recursively closed to"
                    f" {sorted(schema)}, and an unknown nested member refuses"
                    " exactly as an unclassified top-level key does (ADR-0055)"
                )
        for member, nested in schema.items():
            if member in value:
                _validate_member(label, f"{path}.{member}", nested, value[member])
        return
    # A scalar schema: exactly that JSON type. bool by identity — in Python,
    # True is an int, and a JSON 1 must never pass as a boolean.
    expected = _JSON_TYPE_NAMES.get(schema, getattr(schema, "__name__", str(schema)))
    if type(value) is not schema:
        raise PolicyError(
            f"{label}: key '{path}' must be a JSON {expected} — a wrong type"
            " or extra nesting depth refuses like an unclassified key"
            " (ADR-0055)"
        )


def validate_policy_document(label: str, document: object) -> None:
    """Validate ONE parsed policy drop-in against the safe-key allowlist.

    ``label`` is the display path (e.g. ``policy/claude-defaults/
    attribution.json``). Raises ``PolicyError`` on the first violation:
    a non-object document, any top-level key outside ``SAFE_KEYS``, or any
    schema violation under an admitted key. Every message names ``label``
    plus the dotted key path and never echoes document values."""
    if not isinstance(document, dict):
        raise PolicyError(
            f"{label}: a policy drop-in must be a JSON object, not"
            f" {type(document).__name__} (ADR-0055)"
        )
    for key in document:
        if not isinstance(key, str) or key not in SAFE_KEYS:
            shown = key if isinstance(key, str) else type(key).__name__
            raise _refuse_key(label, shown)
    for key, schema in SAFE_KEYS.items():
        if key in document:
            _validate_member(label, key, schema, document[key])


def validate_policy_tree(root: Path, *, label: str) -> None:
    """Validate one Agent Policy tree's STRICT shape plus every document
    (ADR-0055 §1): ``root`` must be a real, non-symlink directory whose
    every entry is a top-level regular non-symlink ``*.json`` file with a
    non-dot-prefixed name, each parsing as JSON and passing
    ``validate_policy_document``. Subdirectories, non-JSON names,
    dot-prefixed names, symlinks, and irregular entries refuse by name — a
    tree the CLI would partially ignore, or that could alias content from
    outside the repo, is never admitted. An empty directory passes the
    shape check (it simply pins nothing). Raises ``PolicyError`` on the
    first violation."""
    root = Path(root)
    if root.is_symlink():
        raise PolicyError(f"{label}: the policy tree root is a symlink — refused (ADR-0055)")
    if not root.is_dir():
        raise PolicyError(f"{label}: the policy tree root is not a directory (ADR-0055)")
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise PolicyError(f"{label}: cannot enumerate the policy tree: {exc}") from exc
    for entry in entries:
        entry_label = f"{label}/{entry.name}"
        if entry.is_symlink():
            raise PolicyError(f"{entry_label}: symlinks are refused in a policy tree (ADR-0055)")
        if entry.is_dir():
            raise PolicyError(
                f"{entry_label}: a policy tree holds top-level *.json drop-ins"
                " only — subdirectories are refused (ADR-0055)"
            )
        if not entry.is_file():
            raise PolicyError(f"{entry_label}: not a regular file — refused (ADR-0055)")
        if entry.name.startswith("."):
            raise PolicyError(
                f"{entry_label}: dot-prefixed names are refused — the CLI"
                " ignores them, so the file would be dead policy (ADR-0055)"
            )
        if not entry.name.endswith(".json"):
            raise PolicyError(
                f"{entry_label}: a policy drop-in must be named *.json — the"
                " CLI reads nothing else from the managed drop-in directory"
                " (ADR-0055)"
            )
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PolicyError(f"{entry_label}: unreadable policy drop-in ({exc})") from exc
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            # JSONDecodeError messages carry positions, never content —
            # safe to include (the identity.py redaction precedent).
            raise PolicyError(f"{entry_label}: not valid JSON ({exc})") from exc
        validate_policy_document(entry_label, document)
