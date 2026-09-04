"""Read-or-mutation classification for the GitHub Relay (ADR-0057 items 2, 4).

Nearly every high-level ``gh`` command tunnels through ``POST /graphql``, so
"which commands are allowed" is a GraphQL-body classification: a hand-written
lexer (stdlib only, ADR-0010) strips comments and string literals, walks the
top-level definitions, and permits exactly one ``query`` operation. REST is
classified by method, and an explicit denylist refuses admin-class reads of a
repository, organization, enterprise, or the authenticated user regardless of
the credential's scope. Every decision here sees the canonical path alone;
no query value ever changes a classification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from theozolith_worker.relay.reasons import MethodClass, Reason

GRAPHQL_PATH = "/graphql"

_OPERATION_KEYWORDS = frozenset({"query", "mutation", "subscription"})
_DEFINITION_KEYWORDS = _OPERATION_KEYWORDS | {"fragment"}

# GraphQL lexical grammar (the October 2021 spec): ignored tokens, punctuators,
# names, numbers, and the two string forms. Anything the patterns below do
# not match is a lexical error, and a lexical error refuses the document.
_IGNORED = frozenset(" \t\n\r,\ufeff")
_PUNCTUATORS = frozenset("!$&():=@[]{}|")
_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_STRING = re.compile(r'"(?:[^"\\\n\r]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4}))*"')
_CLOSERS = {"(": ")", "[": "]", "{": "}"}


@dataclass(frozen=True)
class GraphQLVariable:
    name: str
    json_type: str
    canonical: bytes


@dataclass(frozen=True)
class GraphQLClassification:
    parsed: bool
    operation_type: str | None
    operation_name: str | None
    variables: tuple[GraphQLVariable, ...]
    refusal: Reason | None


_UNPARSEABLE = GraphQLClassification(False, None, None, (), Reason.GRAPHQL_UNPARSEABLE)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object with a repeated key is ambiguous — two parsers may keep
    different members — so the body is refused rather than read either way."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-JSON constant {name}")


def _load_json_object(body: bytes) -> dict | None:
    try:
        data = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "number"


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode()


def _tokenize(text: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _IGNORED:
            i += 1
        elif ch == "#":
            while i < n and text[i] not in "\n\r":
                i += 1
        elif text.startswith('"""', i):
            j = i + 3
            while not text.startswith('"""', j):
                if j >= n:
                    return None
                j += 4 if text.startswith('\\"""', j) else 1
            tokens.append(("string", text[i : j + 3]))
            i = j + 3
        elif ch == '"':
            match = _STRING.match(text, i)
            if match is None:
                return None
            tokens.append(("string", match.group()))
            i = match.end()
        elif text.startswith("...", i):
            tokens.append(("punct", "..."))
            i += 3
        elif ch in _PUNCTUATORS:
            tokens.append(("punct", ch))
            i += 1
        elif (match := _NAME.match(text, i)) is not None:
            tokens.append(("name", match.group()))
            i = match.end()
        elif (match := _NUMBER.match(text, i)) is not None and match.end() > i:
            end = match.end()
            if end < n and (text[end] == "." or _NAME.match(text, end) is not None):
                return None
            tokens.append(("number", match.group()))
            i = end
        else:
            return None
    return tokens


def _group_end(tokens: list[tuple[str, str]], start: int) -> int | None:
    """The index just past the bracket group opening at ``start``; ``None``
    when the group is unbalanced or a closer mismatches its opener."""
    stack: list[str] = []
    for i in range(start, len(tokens)):
        kind, value = tokens[i]
        if kind != "punct":
            continue
        if value in _CLOSERS:
            stack.append(_CLOSERS[value])
        elif value in ")]}":
            if not stack or stack[-1] != value:
                return None
            stack.pop()
            if not stack:
                return i + 1
    return None


def _skip_directives(tokens: list[tuple[str, str]], i: int) -> int | None:
    while i < len(tokens) and tokens[i] == ("punct", "@"):
        i += 1
        if i >= len(tokens) or tokens[i][0] != "name":
            return None
        i += 1
        if i < len(tokens) and tokens[i] == ("punct", "("):
            i = _group_end(tokens, i)
            if i is None:
                return None
    return i


def _definitions(tokens: list[tuple[str, str]]) -> list[tuple[str, str | None]] | None:
    """The top-level definitions as ``(kind, name)`` pairs, ``kind`` one of
    ``query``, ``mutation``, ``subscription``, ``fragment``; ``None`` for a
    document that is not a sequence of well-formed executable definitions."""
    definitions: list[tuple[str, str | None]] = []
    i, n = 0, len(tokens)
    while i < n:
        kind, value = tokens[i]
        if (kind, value) == ("punct", "{"):
            end = _group_end(tokens, i)
            if end is None:
                return None
            definitions.append(("query", None))
            i = end
            continue
        if kind != "name" or value not in _DEFINITION_KEYWORDS:
            return None
        name: str | None = None
        j = i + 1
        if value == "fragment":
            if j >= n or tokens[j][0] != "name" or tokens[j][1] == "on":
                return None
            name = tokens[j][1]
            j += 1
            if j >= n or tokens[j] != ("name", "on"):
                return None
            j += 1
            if j >= n or tokens[j][0] != "name":
                return None
            j += 1
        else:
            if j < n and tokens[j][0] == "name":
                name = tokens[j][1]
                j += 1
            if j < n and tokens[j] == ("punct", "("):
                j = _group_end(tokens, j)
                if j is None:
                    return None
        j = _skip_directives(tokens, j)
        if j is None or j >= n or tokens[j] != ("punct", "{"):
            return None
        end = _group_end(tokens, j)
        if end is None:
            return None
        definitions.append((value, name))
        i = end
    return definitions


def classify_graphql(body: bytes) -> GraphQLClassification:
    """Classify one ``POST /graphql`` body. Only ``parsed=True`` with
    ``refusal=None`` (a single ``query`` operation) permits; everything else
    carries the reason the document was refused, and ``parsed`` is ``True``
    only for a classifiable single-operation document."""
    data = _load_json_object(body)
    if data is None or not isinstance(data.get("query"), str):
        return _UNPARSEABLE
    raw_variables = data.get("variables")
    if raw_variables is None:
        variables: tuple[GraphQLVariable, ...] = ()
    elif isinstance(raw_variables, dict):
        variables = tuple(
            GraphQLVariable(name, _json_type(value), canonical_json(value))
            for name, value in raw_variables.items()
        )
    else:
        return _UNPARSEABLE

    def refuse(reason: Reason) -> GraphQLClassification:
        return GraphQLClassification(False, None, None, variables, reason)

    tokens = _tokenize(data["query"])
    if tokens is None:
        return refuse(Reason.GRAPHQL_UNPARSEABLE)
    definitions = _definitions(tokens)
    if definitions is None:
        return refuse(Reason.GRAPHQL_UNPARSEABLE)
    operations = [(kind, name) for kind, name in definitions if kind != "fragment"]
    if not operations:
        return refuse(Reason.GRAPHQL_UNPARSEABLE)
    if len(operations) > 1:
        return refuse(Reason.GRAPHQL_MULTI_OPERATION)
    operation_type, operation_name = operations[0]
    refusal = None if operation_type == "query" else Reason.GRAPHQL_NON_QUERY
    return GraphQLClassification(True, operation_type, operation_name, variables, refusal)


# Admin-class repository sub-resources (ADR-0057 item 4), matched on canonical
# path segments under ``/repos/{owner}/{repo}/`` and under GitHub's numeric
# alias ``/repositories/{id}/`` for the same resources.
_REPO_ADMIN_SEGMENTS = frozenset({"hooks", "keys", "collaborators", "invitations"})
_REPO_ADMIN_ACTIONS = frozenset({"secrets", "variables"})
_REPO_ROOTS = frozenset({"repos", "repositories"})
_REPO_ROOT_LENGTH = {"repos": 3, "repositories": 2}


def is_admin_read(canonical_path: str) -> bool:
    """The explicit denylist over an already-canonical path."""
    segments = canonical_path.split("/")[1:] if canonical_path != "/" else []
    if not segments:
        return False
    head = segments[0]
    if head in ("orgs", "enterprises"):
        return True
    if head in ("user", "organizations"):
        return len(segments) > 1
    if head in _REPO_ROOTS:
        sub = segments[_REPO_ROOT_LENGTH[head] :]
        if not sub:
            return False
        if sub[0] in _REPO_ADMIN_SEGMENTS:
            return True
        return sub[0] == "actions" and len(sub) > 1 and sub[1] in _REPO_ADMIN_ACTIONS
    return False


def classify_rest(method: MethodClass, canonical_path: str) -> Reason | None:
    """``None`` permits. Total over ``MethodClass`` for defense in depth: the
    ingress parser has already refused every method but GET/HEAD/POST."""
    if method in (MethodClass.PUT, MethodClass.PATCH, MethodClass.DELETE):
        return Reason.MUTATION
    if method is MethodClass.POST:
        return None if canonical_path == GRAPHQL_PATH else Reason.MUTATION
    if method in (MethodClass.GET, MethodClass.HEAD):
        return Reason.ADMIN_READ if is_admin_read(canonical_path) else None
    return Reason.METHOD
