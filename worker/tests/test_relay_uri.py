"""RFC 3986 reference splitting and the bounded redirect classification
(ADR-0057 items 5 and 8): what a ``Location`` becomes in a record — a closed
scheme class, a host literal only when valid, otherwise a length and digest
— and reference resolution against the pinned origin."""

from __future__ import annotations

import json
import re

import pytest
from theozolith_worker.relay.audit import CompletionRecord, format_ts, serialize
from theozolith_worker.relay.ingress import sha256_hex
from theozolith_worker.relay.reasons import (
    DEFAULT_BUDGETS,
    HostStatus,
    Outcome,
    Reason,
    RedirectDecision,
    Scheme,
)
from theozolith_worker.relay.upstream import _redirect_entry
from theozolith_worker.relay.uri import (
    HOST_LIMIT,
    UriParts,
    classify_host,
    classify_scheme,
    resolve_location,
    split_reference,
)

BASE = "https://api.github.com/repos/o/r/issues?page=2"
TS = format_ts(1_700_000_000.5)


def parts(text: str) -> UriParts:
    split = split_reference(text)
    assert split is not None, text
    return split


def digest_of(text: str) -> tuple[int, str]:
    data = text.encode("ascii")
    return len(data), sha256_hex(data)


# -- split_reference ---------------------------------------------------------


def test_split_reference_delimits_every_component():
    split = parts("https://user@host:8443/a/b?q=1#frag")
    assert split == UriParts("https", "user@host:8443", "/a/b", "q=1", "frag")
    assert split.authority_raw == "user@host:8443"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/a/b", UriParts(None, None, "/a/b", None, None)),
        ("//host/a", UriParts(None, "host", "/a", None, None)),
        ("a/b", UriParts(None, None, "a/b", None, None)),
        ("", UriParts(None, None, "", None, None)),
        ("?q", UriParts(None, None, "", "q", None)),
        ("#f", UriParts(None, None, "", None, "f")),
        ("https:", UriParts("https", None, "", None, None)),
        ("https://", UriParts("https", "", "", None, None)),
    ],
)
def test_split_reference_relative_forms(text, expected):
    assert split_reference(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://api.github.com/a b",
        "https://api.github.com/a\tb",
        "https://api.github.com/a\rb",
        "https://api.github.com/a\nb",
        "https://api.github.com/a\x00b",
        "https://api.github.com/a\x1bb",
        "https://api.github.com/a\x7fb",
        "https://api.github.com/caf\u00e9",
        "https://api.github.com/\u2603",
        " https://api.github.com/",
        "https://api.github.com/\n",
    ],
)
def test_split_reference_refuses_anything_but_a_single_reference(text):
    assert split_reference(text) is None


# -- classify_scheme ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://api.github.com/x", Scheme.HTTPS),
        ("HTTPS://api.github.com/x", Scheme.HTTPS),
        ("http://api.github.com/x", Scheme.HTTP),
        ("Http://api.github.com/x", Scheme.HTTP),
        ("ftp://api.github.com/x", Scheme.OTHER),
        ("a+b-c.d://x", Scheme.OTHER),
        ("javascript:alert(1)", Scheme.OTHER),
        ("//api.github.com/x", Scheme.INVALID),
        ("/x", Scheme.INVALID),
        ("x/y", Scheme.INVALID),
        ("", Scheme.INVALID),
        ("1abc://x", Scheme.INVALID),
        ("a_b://x", Scheme.INVALID),
        ("-a://x", Scheme.INVALID),
    ],
)
def test_classify_scheme(text, expected):
    assert classify_scheme(parts(text)) is expected


@pytest.mark.parametrize(
    "token, expected",
    [
        ("a" * 5000, Scheme.OTHER),
        ("1" + "a" * 4999, Scheme.INVALID),
        ("a" * 4999 + "_", Scheme.INVALID),
    ],
)
def test_a_five_thousand_byte_scheme_records_only_its_class(token, expected):
    location = f"{token}://x/y"
    assert classify_scheme(parts(location)) is expected
    entry = _redirect_entry(1, 302, RedirectDecision.REFUSED, Reason.REDIRECT_ORIGIN, [location])
    assert entry.scheme is expected
    rendered = json.dumps(entry.to_json())
    assert token not in rendered
    assert len(rendered) < 512


# -- classify_host -----------------------------------------------------------


@pytest.mark.parametrize(
    "authority, host",
    [
        ("api.github.com", "api.github.com"),
        ("API.GitHub.com", "API.GitHub.com"),
        ("api.github.com:443", "api.github.com"),
        ("api.github.com:", "api.github.com"),
        ("[::1]", "[::1]"),
        ("[2001:db8::1]:8443", "[2001:db8::1]"),
        ("[v1.fe80::a+en1]", "[v1.fe80::a+en1]"),
        ("192.0.2.1", "192.0.2.1"),
        ("a" * HOST_LIMIT, "a" * HOST_LIMIT),
        ("!$&'()*+,;=", "!$&'()*+,;="),
    ],
)
def test_classify_host_valid_records_the_literal(authority, host):
    repr_ = classify_host(parts(f"https://{authority}/x"))
    assert repr_.status is HostStatus.VALID
    assert repr_.value == host
    assert repr_.length is None and repr_.sha256 is None
    assert repr_.to_json() == {"status": "valid", "value": host}


def test_classify_host_oversized_records_length_and_digest_of_the_host():
    host = "a" * (HOST_LIMIT + 1)
    repr_ = classify_host(parts(f"https://{host}:8443/x"))
    assert repr_.status is HostStatus.OVERSIZED
    assert repr_.value is None
    assert (repr_.length, repr_.sha256) == digest_of(host)
    rendered = json.dumps(repr_.to_json())
    assert "aaaa" not in rendered
    assert rendered == json.dumps({"status": "oversized", "len": 254, "sha256": repr_.sha256})


@pytest.mark.parametrize(
    "authority",
    [
        "user@api.github.com",
        "user:pass@api.github.com",
        "@api.github.com",
        "api%2Egithub.com",
        "%61pi.github.com",
        'api."github.com',
        "api\\github.com",
        "api\x01github.com",
        "api\x7fgithub.com",
        "",
        ":443",
        "api.github.com:abc",
        "api.github.com:4a3",
        "[::1",
        "[::1]x",
        "[]",
        "api{github}.com",
        "api<github>.com",
        "api github.com",
    ],
)
def test_classify_host_invalid_records_the_delimited_authority_digest(authority):
    reference = UriParts("https", authority, "/x", None, None)
    repr_ = classify_host(reference)
    assert repr_.status is HostStatus.INVALID
    assert repr_.value is None
    assert (repr_.length, repr_.sha256) == digest_of(authority)
    rendered = repr_.to_json()
    assert rendered == {"status": "invalid", "len": repr_.length, "sha256": repr_.sha256}
    assert re.fullmatch(r"[0-9a-f]{64}", rendered["sha256"])


def test_classify_host_invalid_digests_the_non_ascii_authority_bytes():
    authority = "caf\u00e9.example"
    repr_ = classify_host(UriParts("https", authority, "/x", None, None))
    assert repr_.status is HostStatus.INVALID
    assert repr_.length == len(authority.encode("latin-1"))
    assert "\u00e9" not in json.dumps(repr_.to_json())


@pytest.mark.parametrize("text", ["/x", "x/y", "mailto:a@b", "", "https:"])
def test_classify_host_absent_when_there_is_no_authority(text):
    repr_ = classify_host(parts(text))
    assert repr_.status is HostStatus.ABSENT
    assert (repr_.value, repr_.length, repr_.sha256) == (None, None, None)
    assert repr_.to_json() == {"status": "absent"}


# -- resolve_location --------------------------------------------------------


@pytest.mark.parametrize(
    "location, expected",
    [
        ("https://api.github.com/zen", "https://api.github.com/zen"),
        ("https://evil.example/zen?x=1#f", "https://evil.example/zen?x=1#f"),
        ("HTTPS://API.GITHUB.COM/ZEN", "HTTPS://API.GITHUB.COM/ZEN"),
        ("//api.github.com/zen", "https://api.github.com/zen"),
        ("//evil.example/zen?q", "https://evil.example/zen?q"),
        ("/zen", "https://api.github.com/zen"),
        ("/zen?a=b", "https://api.github.com/zen?a=b"),
        ("zen", "https://api.github.com/repos/o/r/zen"),
        ("zen?x", "https://api.github.com/repos/o/r/zen?x"),
        ("../zen", "https://api.github.com/repos/o/r/../zen"),
        ("./zen", "https://api.github.com/repos/o/r/./zen"),
        ("", "https://api.github.com/repos/o/r/issues?page=2"),
        ("?q=1", "https://api.github.com/repos/o/r/issues?q=1"),
        ("#top", "https://api.github.com/repos/o/r/issues?page=2#top"),
        ("/zen#frag", "https://api.github.com/zen#frag"),
    ],
)
def test_resolve_location_against_the_pinned_origin(location, expected):
    assert resolve_location(BASE, location) == expected


def test_resolve_location_merges_against_an_empty_base_path():
    assert resolve_location("https://api.github.com", "zen") == "https://api.github.com/zen"


@pytest.mark.parametrize("location", ["/a b", "https://x/\n", "/\x00", "/caf\u00e9"])
def test_resolve_location_returns_none_for_a_non_reference(location):
    assert resolve_location(BASE, location) is None


def test_resolve_location_requires_an_absolute_base():
    with pytest.raises(ValueError):
        resolve_location("/relative", "/zen")


# -- the RedirectEntry a Location becomes ------------------------------------


def entry(locations: list[str]):
    return _redirect_entry(1, 302, RedirectDecision.REFUSED, Reason.REDIRECT_LOCATION, locations)


def test_a_missing_location_is_absent_scheme_and_absent_host():
    result = entry([])
    assert (result.scheme, result.host.status) == (Scheme.ABSENT, HostStatus.ABSENT)
    assert result.host.to_json() == {"status": "absent"}


def test_a_duplicated_location_is_invalid_with_the_first_host():
    result = entry(["https://api.github.com/a", "https://evil.example/b"])
    assert result.scheme is Scheme.INVALID
    assert result.host.status is HostStatus.VALID
    assert result.host.value == "api.github.com"


def test_an_unparseable_location_is_invalid_with_no_host():
    result = entry(["https://evil.example/a b"])
    assert (result.scheme, result.host.status) == (Scheme.INVALID, HostStatus.ABSENT)


def test_a_scheme_relative_location_is_invalid_with_its_host_classified():
    result = entry(["//evil.example/a"])
    assert result.scheme is Scheme.INVALID
    assert result.host.status is HostStatus.VALID
    assert result.host.value == "evil.example"
    relative = entry(["/a/b"])
    assert (relative.scheme, relative.host.status) == (Scheme.INVALID, HostStatus.ABSENT)


def test_a_followed_entry_carries_scheme_host_and_nothing_else():
    result = _redirect_entry(
        2, 307, RedirectDecision.FOLLOWED, None, ["https://api.github.com/zen?token=secret#f"]
    )
    assert result.to_json() == {
        "hop": 2,
        "status": 307,
        "decision": "followed",
        "reason": None,
        "scheme": "https",
        "host": {"status": "valid", "value": "api.github.com"},
    }


def test_no_location_path_or_query_byte_reaches_a_completion_record():
    locations = [
        "https://evil.example/secret-path?token=SECRET-QUERY#SECRET-FRAG",
        "https://user:SECRET-PASS@api.github.com/x",
        "https://api.github.com:8443/SECRET-PORT-PATH",
        "//evil.example/SECRET-REL",
    ]
    entries = tuple(
        _redirect_entry(hop, 302, RedirectDecision.REFUSED, Reason.REDIRECT_ORIGIN, [loc])
        for hop, loc in enumerate(locations, 1)
    )
    line = serialize(CompletionRecord(7, TS, Outcome.REFUSED_REDIRECT, 302, 100, 0, entries))
    for secret in ("secret-path", "SECRET", "8443", "user", "token"):
        assert secret.encode() not in line
    assert b'"host":{"status":"invalid","len":' in line
    assert len(line) <= DEFAULT_BUDGETS.record_cap
