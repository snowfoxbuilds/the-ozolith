"""The browser origin (ADR-0034, lazy since ADR-0036): derivation from the
persisted control address, the parsed origin-init origin, fail-closed
validation, the production serve requirement, bind-port independence (plus
the loud mismatch warning), and TLS SAN derivation."""

from __future__ import annotations

import socket

import pytest
from theozolith_control import controltoml, origin
from theozolith_control.cli import main as cli_main
from theozolith_control.origin import OriginError
from theozolith_control.tls import provision

CONTROL_IP = "192.0.2.20"


def test_derive_yields_portless_origin_for_default_https():
    """https://<ip> => Host <ip> and the same Origin — no :8443 or any
    other port anywhere."""
    derived = origin.derive_origin(CONTROL_IP)
    assert derived.origin == f"https://{CONTROL_IP}"
    assert derived.host_header == CONTROL_IP
    assert derived.port == 443


def test_derive_keeps_an_explicit_nonstandard_port():
    derived = origin.derive_origin(CONTROL_IP, 9443)
    assert derived.origin == f"https://{CONTROL_IP}:9443"
    assert derived.host_header == f"{CONTROL_IP}:9443"


def test_derive_brackets_ipv6_literals():
    derived = origin.derive_origin("2001:db8::7")
    assert derived.origin == "https://[2001:db8::7]"
    assert origin.derive_origin("2001:db8::7", 9443).host_header == "[2001:db8::7]:9443"


def test_derive_fails_closed_on_garbage():
    """Non-IP addresses and out-of-range ports raise OriginError — the
    BrowserGuard is never armed with garbage expectations."""
    for ip, port in (
        ("", 443),
        ("control.local", 443),  # hostnames are retired with the slug origin
        ("10.0.0.999", 443),
        (CONTROL_IP, 0),
        (CONTROL_IP, 65536),
        (CONTROL_IP, True),
    ):
        with pytest.raises(OriginError):
            origin.derive_origin(ip, port)


def test_parse_browser_origin_accepts_ip_and_hostname_shapes():
    """origin-init's parser (ADR-0036): the IP origin round-trips to the
    derive_origin spelling; a hostname origin is the one hostname re-entry
    point; ports and IPv6 brackets canonicalize."""
    assert origin.parse_browser_origin(f"https://{CONTROL_IP}").origin == f"https://{CONTROL_IP}"
    assert (
        origin.parse_browser_origin(f"https://{CONTROL_IP}:9443/").origin
        == f"https://{CONTROL_IP}:9443"
    )
    parsed = origin.parse_browser_origin("https://Ozolith.LAN:8443")
    assert parsed.origin == "https://ozolith.lan:8443"
    assert parsed.host_header == "ozolith.lan:8443"
    assert origin.san_host(parsed) == "ozolith.lan"
    v6 = origin.parse_browser_origin("https://[2001:db8::7]")
    assert v6.origin == "https://[2001:db8::7]"
    assert origin.san_host(v6) == "2001:db8::7"


def test_parse_browser_origin_fails_closed():
    """Every URL shape ADR-0022 refused stays refused: schemes,
    credentials, paths, queries, fragments, wildcards, bad ports."""
    for text in (
        "",
        "http://192.0.2.20",
        "https://user:pw@192.0.2.20",
        "https://192.0.2.20/dashboard",
        "https://192.0.2.20?x=1",
        "https://192.0.2.20#f",
        "https://*.ozolith.lan",
        "https://ozolith.lan:99999",
        "https://ozolith.lan:abc",
        "https://-bad-.lan",
        "https://",
    ):
        with pytest.raises(OriginError):
            origin.parse_browser_origin(text)


def test_control_address_write_read_roundtrip(tmp_path):
    """The address persists as the read-only [control] fields of
    control.toml in the Config Repo (ADR-0024/0034)."""
    path = controltoml.write_control_address(tmp_path, CONTROL_IP, port=9443)
    assert path.name == "control.toml"
    assert controltoml.read_control_ip(tmp_path) == CONTROL_IP
    assert controltoml.read_control_port(tmp_path) == 9443
    # The default port writes no line and reads back as 443.
    controltoml.write_control_address(tmp_path, CONTROL_IP, port=443)
    assert "control_port" not in controltoml.control_toml_path(tmp_path).read_text()
    assert controltoml.read_control_port(tmp_path) == 443
    # recover --ip keeps the persisted port (port=None).
    controltoml.write_control_address(tmp_path, CONTROL_IP, port=9443)
    controltoml.write_control_address(tmp_path, "192.0.2.21")
    assert controltoml.read_control_ip(tmp_path) == "192.0.2.21"
    assert controltoml.read_control_port(tmp_path) == 9443


def test_leftover_public_origin_is_ignored_and_dropped(tmp_path):
    """A pre-ADR-0034 control.toml still parses: the retired field is
    ignored on read and dropped on the next regeneration."""
    controltoml.control_toml_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    controltoml.control_toml_path(tmp_path).write_text(
        f'[control]\npublic_origin = "https://x.theozolith.internal"\ncontrol_ip = "{CONTROL_IP}"\n'
    )
    assert controltoml.read_control_ip(tmp_path) == CONTROL_IP
    assert controltoml.read_control_port(tmp_path) == 443
    controltoml.set_value(tmp_path, "heartbeat_seconds", "30")
    assert "public_origin" not in controltoml.control_toml_path(tmp_path).read_text()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("THEOZOLITH_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(data))
    # No admin password: production serve no longer requires one (ADR-0036
    # — the browser surface is lazy). The bootstrap listener gets a
    # per-test free port.
    monkeypatch.setenv("THEOZOLITH_BOOTSTRAP_PORT", str(_free_port()))
    (data / "secrets").mkdir(parents=True)
    return data


def test_production_serve_requires_the_control_address(cli_env):
    """With TLS material present but no persisted control address,
    production startup refuses with instructions."""
    provision(cli_env / "secrets" / "tls", [CONTROL_IP])
    with pytest.raises(SystemExit, match="theozolith init"):
        cli_main(["serve"])


def test_production_serve_fails_closed_on_a_malformed_persisted_port(cli_env, capsys):
    """A present-but-invalid control_port is a configuration error at
    startup — never a silent redirect of browsers and nodes to 443."""
    provision(cli_env / "secrets" / "tls", [CONTROL_IP])
    configs = cli_env / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "control.toml").write_text(
        f'[control]\ncontrol_ip = "{CONTROL_IP}"\ncontrol_port = "443"\n'
    )
    assert cli_main(["serve"]) == 2
    assert "control_port" in capsys.readouterr().err


def _capture_serve(monkeypatch) -> dict:
    """Run `serve` up to (a fake) uvicorn.run and capture the built app."""
    import uvicorn

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return captured


def test_serve_derives_the_guard_and_ignores_the_bind_port(cli_env, monkeypatch, capsys):
    """Production boots from the persisted address; the guard arms from the
    persisted browser origin (ADR-0036); a different internal serve --port
    changes only the bind, never the accepted Host/Origin — but the
    mismatch is called out loudly (ADR-0034)."""
    controltoml.write_control_address(cli_env / "configs", CONTROL_IP, port=443)
    controltoml.write_browser_origin(cli_env / "configs", f"https://{CONTROL_IP}")
    assert cli_main(["tls-init"]) == 0
    captured = _capture_serve(monkeypatch)
    # The warning is bare-metal-only (a container bind is never the
    # external truth) — and this test suite itself runs in a container.
    monkeypatch.setattr("theozolith_control.cli._running_in_container", lambda: False)
    assert cli_main(["serve", "--port", "9090"]) == 0
    guard = captured["app"].state.browser_guard
    assert guard.expected_origin == f"https://{CONTROL_IP}"
    assert guard.expected_host == CONTROL_IP  # the bind port leaked nowhere
    assert captured["kwargs"]["port"] == 9090  # …but uvicorn does bind it
    out = capsys.readouterr().out
    assert "WARNING" in out and "persisted external port is 443" in out


def test_serve_matching_bind_warns_nothing(cli_env, monkeypatch, capsys):
    controltoml.write_control_address(cli_env / "configs", CONTROL_IP, port=9443)
    controltoml.write_browser_origin(cli_env / "configs", f"https://{CONTROL_IP}:9443")
    assert cli_main(["tls-init"]) == 0
    captured = _capture_serve(monkeypatch)
    monkeypatch.setattr("theozolith_control.cli._running_in_container", lambda: False)
    assert cli_main(["serve", "--port", "9443"]) == 0
    assert captured["app"].state.browser_guard.expected_host == f"{CONTROL_IP}:9443"
    assert "WARNING" not in capsys.readouterr().out


def test_serve_without_browser_origin_refuses_the_web_surface(cli_env, monkeypatch):
    """ADR-0036 fail-closed, moved into the request path: with no persisted
    browser origin the HTML/cookie surface answers 503 with a pointer at
    origin-init and the terminal websocket refuses — while the bearer API
    keeps working."""
    from fastapi.testclient import TestClient

    controltoml.write_control_address(cli_env / "configs", CONTROL_IP, port=443)
    assert cli_main(["tls-init"]) == 0
    captured = _capture_serve(monkeypatch)
    assert cli_main(["serve"]) == 0
    client = TestClient(captured["app"], base_url=f"https://{CONTROL_IP}")
    for path in ("/login", "/", "/secrets", "/settings", "/join", "/terminal"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert "origin-init" in response.text
    assert client.post("/login", data={"password": "x"}).status_code == 503
    # The machine surface is untouched (the bearer API works; ADR-0036).
    healthz = client.get("/api/v1/healthz")
    assert healthz.status_code == 200


def test_tls_init_puts_the_control_ip_in_the_san(cli_env):
    """The TLS SAN derives from the persisted control IP alone — nodes and
    CA-trusting browsers both verify against it (ADR-0031/0034)."""
    controltoml.write_control_address(cli_env / "configs", CONTROL_IP, port=9443)
    assert cli_main(["tls-init"]) == 0  # no --host needed once persisted
    import ipaddress

    from cryptography import x509

    cert = x509.load_pem_x509_certificate((cli_env / "secrets" / "tls" / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address(CONTROL_IP) in san.get_values_for_type(x509.IPAddress)
