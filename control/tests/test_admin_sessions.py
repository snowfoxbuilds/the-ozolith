"""Admin password + stateful sessions (ADR-0023/0027, acceptance 6) and the
dashboard settings/join surfaces (acceptance 7)."""

from __future__ import annotations

import io
import re
import subprocess

from controlrig import ADMIN_PASSWORD, ControlRig, make_rig
from theozolith_control import controltoml
from theozolith_control.cli import main as cli_main
from theozolith_control.tls import provision
from theozolith_control.web.auth import LOGIN_MAX_FAILURES, SESSION_COOKIE


def _login(control: ControlRig, password: str = ADMIN_PASSWORD):
    return control.client.post("/login", data={"password": password}, follow_redirects=False)


# -- acceptance 6: login, sessions, revocation ---------------------------------


def test_wrong_password_is_rejected_and_rate_limited(control: ControlRig):
    for _ in range(LOGIN_MAX_FAILURES):
        assert _login(control, "wrong").status_code == 401
    throttled = _login(control, "wrong")
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) > 0
    # The limit throttles even the RIGHT password inside the window — the
    # check runs before any verification work.
    assert _login(control).status_code == 429


def test_cookie_carries_only_an_opaque_128_bit_id(control: ControlRig):
    header = _login(control).headers["set-cookie"]
    value = re.match(rf"{re.escape(SESSION_COOKIE)}=([^;]+);", header).group(1)
    assert re.fullmatch(r"[0-9a-f]{32}", value)  # 128 bits, nothing decodable
    # Only its digest is stored: the id itself appears nowhere in cache.db.
    assert value.encode() not in control.settings.cache_db_path.read_bytes()


def test_logout_invalidates_server_side_immediately(control: ControlRig):
    header = _login(control).headers["set-cookie"]
    value = re.match(rf"{re.escape(SESSION_COOKIE)}=([^;]+);", header).group(1)
    assert control.client.get("/", follow_redirects=False).status_code == 200
    control.client.post("/logout", follow_redirects=False)
    # Replaying the OLD cookie value fails: the row is gone, not the jar.
    control.client.cookies.set(SESSION_COOKIE, value)
    replay = control.client.get("/", follow_redirects=False)
    assert replay.status_code == 303 and replay.headers["location"] == "/login"


def test_absolute_expiry_ends_the_session(control: ControlRig):
    _login(control)
    assert control.client.get("/", follow_redirects=False).status_code == 200
    control.clock.advance(control.settings.session_days * 86400 + 1)
    assert control.client.get("/", follow_redirects=False).status_code == 303


def test_password_change_invalidates_every_session(control: ControlRig, monkeypatch):
    """`theozolith-control set-password` rewrites the hash and truncates the
    session table; the new password takes effect with no server restart."""
    _login(control)
    assert control.client.get("/", follow_redirects=False).status_code == 200

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(control.settings.data_dir))
    monkeypatch.setenv("THEOZOLITH_CONFIG_REPO", str(control.settings.config_repo))
    monkeypatch.setattr("sys.stdin", io.StringIO("brand-new-password\n"))
    assert cli_main(["set-password"]) == 0

    assert control.client.get("/", follow_redirects=False).status_code == 303  # all sessions dead
    assert _login(control, ADMIN_PASSWORD).status_code == 401  # old password gone
    assert _login(control, "brand-new-password").status_code == 303  # live server, new hash


def test_unconfigured_password_fails_closed_with_instructions(tmp_path):
    rig = make_rig(tmp_path)
    rig.settings.admin_password_path.unlink()
    response = rig.client.post("/login", data={"password": "anything"})
    assert response.status_code == 503 and "init" in response.text


# -- acceptance 7: the settings form ------------------------------------------


def _git_config_repo(control: ControlRig) -> None:
    control.settings.config_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(control.settings.config_repo)], check=True)


def test_settings_form_commits_one_key_to_control_toml(control: ControlRig):
    _git_config_repo(control)
    _login(control)
    saved = control.client.post(
        "/settings", data={"key": "heartbeat_seconds", "value": "30"}, follow_redirects=False
    )
    assert saved.status_code == 303
    assert controltoml.read_values(control.settings.config_repo)["heartbeat_seconds"] == 30.0
    show = subprocess.run(
        ["git", "-C", str(control.settings.config_repo), "show", "--name-only", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert show == ["theozolith:", "settings:", "heartbeat_seconds", "=", "30", "control.toml"]

    page = control.client.get("/settings").text
    assert 'value="30.0"' in page or 'value="30"' in page


def test_settings_form_renders_the_origin_read_only_and_rejects_writes(control: ControlRig):
    _git_config_repo(control)
    _login(control)
    page = control.client.get("/settings").text
    assert "readonly" in page and control.settings.public_origin in page
    refused = control.client.post(
        "/settings", data={"key": "public_origin", "value": "https://evil.example"}
    )
    assert refused.status_code == 403
    assert controltoml.read_public_origin(control.settings.config_repo) != "https://evil.example"
    bad = control.client.post("/settings", data={"key": "made_up", "value": "1"})
    assert bad.status_code == 400


# -- the dashboard join page (the CLI's twin) ----------------------------------


def test_join_page_mints_and_revokes(control: ControlRig):
    provision(control.settings.tls_dir, ["127.0.0.1"])
    _login(control)
    page = control.client.post("/join", data={"ttl_seconds": "3600", "uses": "1"}).text
    assert "ozjoin1:" in page and "curl -fsSL" in page
    token_id = control.store.join_tokens()[0]["id"]
    revoked = control.client.post("/join/revoke", data={"id": token_id}).text
    assert "revoked" in revoked
    assert control.store.join_tokens() == []


def test_unregistered_nodes_render_on_the_fleet_fragment(control: ControlRig):
    control.heartbeat(node="lost-box", token="unknown-token")
    _login(control)
    page = control.client.get("/fragments/fleet").text
    assert "Unregistered nodes" in page and "lost-box" in page
