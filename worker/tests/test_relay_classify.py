"""The GraphQL lexer, the REST classifier, and the admin-read denylist
(ADR-0057 items 2 and 4), driven by the pinned-`gh` fixture corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from theozolith_worker.relay.classify import (
    GRAPHQL_PATH,
    GraphQLVariable,
    canonical_json,
    classify_graphql,
    classify_rest,
    is_admin_read,
)
from theozolith_worker.relay.reasons import MethodClass, Reason

FIXTURES = Path(__file__).parent / "relay_fixtures" / "graphql"
CASES = sorted(
    path.name[: -len(".json")] for path in FIXTURES.glob("*.json") if ".expected" not in path.name
)


def load(name: str) -> tuple[bytes, dict]:
    return (FIXTURES / f"{name}.json").read_bytes(), json.loads(
        (FIXTURES / f"{name}.expected.json").read_text()
    )


def test_corpus_is_present():
    assert len(CASES) >= 40
    assert any(name.startswith("gh-") for name in CASES)
    assert any(name.startswith("adv-") for name in CASES)


@pytest.mark.parametrize("name", CASES)
def test_corpus_classification(name):
    body, expected = load(name)
    result = classify_graphql(body)
    assert result.parsed is expected["parsed"], name
    assert result.operation_type == expected["operation_type"], name
    assert result.operation_name == expected["operation_name"], name
    refusal = None if result.refusal is None else result.refusal.value
    assert refusal == expected["refusal"], name
    assert [variable.name for variable in result.variables] == expected["variables"], name


@pytest.mark.parametrize("name", [name for name in CASES if name.startswith("gh-")])
def test_every_pinned_gh_wire_shape_is_a_permitted_single_query(name):
    body, _ = load(name)
    result = classify_graphql(body)
    assert (result.parsed, result.operation_type, result.refusal) == (True, "query", None)


def test_hidden_keywords_do_not_flip_a_permitted_query_but_a_real_operation_does():
    for name in (
        "adv-comment-hidden-mutation",
        "adv-comment-hidden-mutation-multiline",
        "adv-string-hidden-mutation",
        "adv-string-escaped-quote-hidden-mutation",
        "adv-block-string-hidden-mutation",
    ):
        body, _ = load(name)
        assert classify_graphql(body).refusal is None, name
    for name in ("adv-query-then-mutation", "adv-mutation-after-comment", "adv-multi-operation"):
        body, _ = load(name)
        assert classify_graphql(body).refusal is Reason.GRAPHQL_MULTI_OPERATION, name


def test_refusal_classes():
    assert classify_graphql(b"not json").refusal is Reason.GRAPHQL_UNPARSEABLE
    assert classify_graphql(b"[]").refusal is Reason.GRAPHQL_UNPARSEABLE
    assert classify_graphql(b'{"query": 1}').refusal is Reason.GRAPHQL_UNPARSEABLE
    assert (
        classify_graphql(b'{"query": "fragment F on T { a }"}').refusal
        is Reason.GRAPHQL_UNPARSEABLE
    )
    single_mutation = classify_graphql(b'{"query": "mutation { x { y } }"}')
    assert (single_mutation.parsed, single_mutation.operation_type) == (True, "mutation")
    assert single_mutation.refusal is Reason.GRAPHQL_NON_QUERY
    assert classify_graphql(b'{"query": "subscription { x }"}').refusal is Reason.GRAPHQL_NON_QUERY
    multi = classify_graphql(b'{"query": "query A { a } query B { b }"}')
    assert multi.refusal is Reason.GRAPHQL_MULTI_OPERATION
    assert (multi.parsed, multi.operation_type, multi.operation_name) == (False, None, None)


def test_unparseable_carries_nothing():
    result = classify_graphql(b"\xff\xfe")
    assert result.parsed is False
    assert result.operation_type is None and result.operation_name is None
    assert result.variables == ()
    assert result.refusal is Reason.GRAPHQL_UNPARSEABLE


def test_variables_come_from_the_json_body_independent_of_the_query_text():
    body = json.dumps(
        {
            "query": "query Q($owner: String!) { repository(owner: $owner) { id } }",
            "variables": {
                "owner": "OWNER",
                "number": 7,
                "flag": True,
                "nothing": None,
                "states": ["OPEN", "CLOSED"],
                "filter": {"b": 1, "a": [1, 2]},
                "ratio": 0.5,
            },
        }
    ).encode()
    result = classify_graphql(body)
    assert result.parsed and result.operation_name == "Q"
    by_name = {variable.name: variable for variable in result.variables}
    assert by_name["owner"] == GraphQLVariable("owner", "string", b'"OWNER"')
    assert by_name["number"] == GraphQLVariable("number", "number", b"7")
    assert by_name["flag"] == GraphQLVariable("flag", "boolean", b"true")
    assert by_name["nothing"] == GraphQLVariable("nothing", "null", b"null")
    assert by_name["states"] == GraphQLVariable("states", "array", b'["OPEN","CLOSED"]')
    assert by_name["filter"].json_type == "object"
    assert by_name["filter"].canonical == b'{"a":[1,2],"b":1}'  # sorted keys, no spaces
    assert by_name["ratio"] == GraphQLVariable("ratio", "number", b"0.5")
    assert canonical_json({"z": "é"}) == b'{"z":"\\u00e9"}'  # ASCII-only by construction
    absent = classify_graphql(b'{"query": "{ viewer { id } }"}')
    assert absent.parsed and absent.variables == () and absent.refusal is None
    empty = classify_graphql(b'{"query": "{ viewer { id } }", "variables": {}}')
    assert empty.parsed and empty.variables == () and empty.refusal is None


def test_a_present_variables_member_must_be_an_object():
    # Omission is an empty set; presence with any non-object value — null
    # included — refuses the body before the document is ever lexed.
    for value in (b"null", b"[1]", b"[]", b'"x"', b"1", b"0", b"true", b"false"):
        result = classify_graphql(b'{"query": "{ viewer { id } }", "variables": ' + value + b"}")
        assert result.parsed is False, value
        assert result.refusal is Reason.GRAPHQL_UNPARSEABLE, value
        assert (result.operation_type, result.operation_name, result.variables) == (
            None,
            None,
            (),
        ), value


def test_lexer_precision_on_names_and_literals():
    permitted = classify_graphql(
        b'{"query": "query mutation @dir(a: \\"}\\") { a(b: -1.5e3, c: \\"\\\\u00e9\\") { d } }"}'
    )
    assert permitted.refusal is None and permitted.operation_name == "mutation"
    assert (
        classify_graphql(b'{"query": "query { a(b: 1x) { d } }"}').refusal
        is Reason.GRAPHQL_UNPARSEABLE
    )
    assert classify_graphql(b'{"query": "query { a(b: \\"\\\\q\\") { d } }"}').refusal is (
        Reason.GRAPHQL_UNPARSEABLE
    )
    assert classify_graphql(b'{"query": "\\ufeff{ viewer { id } }"}').refusal is None
    assert classify_graphql(b'{"query": "query Q # trailing comment"}').refusal is (
        Reason.GRAPHQL_UNPARSEABLE
    )


def test_rest_classification_matrix():
    assert classify_rest(MethodClass.GET, "/repos/o/r/issues/1") is None
    assert classify_rest(MethodClass.HEAD, "/repos/o/r") is None
    assert classify_rest(MethodClass.GET, "/user") is None
    assert classify_rest(MethodClass.POST, GRAPHQL_PATH) is None
    assert classify_rest(MethodClass.POST, "/repos/o/r/issues") is Reason.MUTATION
    assert classify_rest(MethodClass.POST, "/graphql/") is Reason.MUTATION
    for method in (MethodClass.PUT, MethodClass.PATCH, MethodClass.DELETE):
        assert classify_rest(method, "/repos/o/r/issues/1") is Reason.MUTATION
        assert classify_rest(method, GRAPHQL_PATH) is Reason.MUTATION
    assert classify_rest(MethodClass.GET, "/repos/o/r/hooks") is Reason.ADMIN_READ
    assert classify_rest(MethodClass.HEAD, "/orgs/o/members") is Reason.ADMIN_READ
    for method in (MethodClass.CONNECT, MethodClass.OPTIONS, MethodClass.TRACE, MethodClass.other):
        assert classify_rest(method, "/user") is Reason.METHOD
        assert classify_rest(method, GRAPHQL_PATH) is Reason.METHOD


DENYLISTED = [
    "/repos/o/r/hooks",
    "/repos/o/r/hooks/1",
    "/repos/o/r/hooks/1/deliveries",
    "/repos/o/r/keys",
    "/repos/o/r/keys/2",
    "/repos/o/r/actions/secrets",
    "/repos/o/r/actions/secrets/NAME",
    "/repos/o/r/actions/secrets/public-key",
    "/repos/o/r/actions/variables",
    "/repos/o/r/actions/variables/NAME",
    "/repos/o/r/collaborators",
    "/repos/o/r/collaborators/octocat/permission",
    "/repos/o/r/invitations",
    "/repositories/1296269/hooks",
    "/repositories/1296269/actions/secrets",
    "/orgs/o",
    "/orgs/o/members",
    "/orgs/o/actions/secrets",
    "/orgs",
    "/enterprises/e",
    "/enterprises/e/settings",
    "/organizations/1/repos",
    "/user/keys",
    "/user/emails",
    "/user/repos",
    "/user/orgs",
]

PERMITTED = [
    "/",
    "/user",
    "/users/octocat",
    "/users/octocat/keys",
    "/repos/o/r",
    "/repos/o/r/issues/1",
    "/repos/o/r/issues/1/comments",
    "/repos/o/r/pulls/1",
    "/repos/o/r/pulls/1/files",
    "/repos/o/r/commits/abc/check-runs",
    "/repos/o/r/commits/abc/status",
    "/repos/o/r/actions/runs",
    "/repos/o/r/actions/runs/1/jobs",
    "/repos/o/r/actions/workflows",
    "/repos/o/r/hooksx",
    "/repos/o/r/contents/hooks",
    "/repos/o/r/contents/actions/secrets",
    "/repos/o",
    "/repos",
    "/repositories/1296269",
    "/repositories/1296269/issues",
    "/organizations",
    "/search/issues",
    "/graphql",
    "/rate_limit",
]


@pytest.mark.parametrize("path", DENYLISTED)
def test_denylist_refuses_the_canonical_spelling_of_each_admin_resource(path):
    assert is_admin_read(path)
    assert classify_rest(MethodClass.GET, path) is Reason.ADMIN_READ
    assert classify_rest(MethodClass.HEAD, path) is Reason.ADMIN_READ


@pytest.mark.parametrize("path", PERMITTED)
def test_denylist_permits_ordinary_repo_issue_and_pr_reads(path):
    assert not is_admin_read(path)
    assert classify_rest(MethodClass.GET, path) is None
