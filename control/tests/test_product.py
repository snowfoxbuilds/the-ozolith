"""The two update paths' machinery (ADR-0015 amendment 2026-07-22): release
resolution, the committed pin, the source build's SHA-stamped version, and
the wheel build sandbox."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from theozolith_control import product


def _git(args: list[str], cwd: Path) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    proc = subprocess.run(
        ["git", *identity, *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


# -- release resolution (user path) ----------------------------------------------


def test_resolve_latest_release_reads_the_index():
    def fake_get(url: str) -> bytes:
        assert url == product.RELEASE_INDEX_URL
        return json.dumps({"info": {"version": "0.4.2"}}).encode()

    assert product.resolve_latest_release(fake_get) == "0.4.2"


def test_resolve_latest_release_honors_the_index_override():
    seen = []

    def fake_get(url: str) -> bytes:
        seen.append(url)
        return json.dumps({"info": {"version": "9.9.9"}}).encode()

    version = product.resolve_latest_release(
        fake_get, environ={"THEOZOLITH_RELEASE_INDEX_URL": "https://mirror.internal/x/json"}
    )
    assert version == "9.9.9"
    assert seen == ["https://mirror.internal/x/json"]


def test_resolve_latest_release_failures_are_clean_errors():
    def broken(url: str) -> bytes:
        raise OSError("index unreachable")

    with pytest.raises(product.ProductError, match="cannot resolve"):
        product.resolve_latest_release(broken)
    with pytest.raises(product.ProductError, match="no version"):
        product.resolve_latest_release(lambda url: b"{}")


# -- the pin (product.toml, committed) --------------------------------------------


def test_write_pin_commits_to_a_git_backed_config_repo(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    _git(["init", "--quiet"], repo)
    (repo / "README").write_text("x")
    _git(["add", "-A"], repo)
    _git(["commit", "--quiet", "-m", "seed"], repo)

    product.write_pin(repo, "0.4.0", log=lambda *_: None)

    assert product.read_pin(repo) == "0.4.0"
    subject = _git(["log", "-1", "--format=%s"], repo)
    assert "pin product version 0.4.0" in subject
    assert _git(["status", "--porcelain"], repo) == ""  # committed, not loose

    # Re-pinning the same version is a no-op commit-wise (idempotent).
    head = _git(["rev-parse", "HEAD"], repo)
    product.write_pin(repo, "0.4.0", log=lambda *_: None)
    assert _git(["rev-parse", "HEAD"], repo) == head

    # Rollback = re-pinning a previous version with the same machinery.
    product.write_pin(repo, "0.3.0", log=lambda *_: None)
    assert product.read_pin(repo) == "0.3.0"


def test_write_pin_works_in_folder_mode_and_refuses_empty(tmp_path):
    repo = tmp_path / "configs"  # no .git: the file itself is the record
    product.write_pin(repo, "0.4.0", log=lambda *_: None)
    assert product.read_pin(repo) == "0.4.0"
    with pytest.raises(product.ProductError, match="unrecorded"):
        product.write_pin(repo, "")


def test_ensure_pin_resolves_and_writes_only_when_absent(tmp_path):
    repo = tmp_path / "configs"
    calls = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        return json.dumps({"info": {"version": "0.5.0"}}).encode()

    # Fresh install, no pin: resolve the latest release and write it.
    assert product.ensure_pin(repo, http_get=fake_get, log=lambda *_: None) == "0.5.0"
    assert product.read_pin(repo) == "0.5.0"
    # An existing pin is authoritative: no resolution happens at all.
    assert product.ensure_pin(repo, http_get=fake_get, log=lambda *_: None) == "0.5.0"
    assert len(calls) == 1


# -- the source build (developer path) ---------------------------------------------


def _checkout(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    for component in product.COMPONENTS:
        pkg = source / component / "src" / f"theozolith_{component}"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "0.3.0"\n')
        (source / component / "pyproject.toml").write_text(
            f'[project]\nname = "theozolith-{component}"\nversion = "0.3.0"\n'
        )
    _git(["init", "--quiet"], source)
    _git(["add", "-A"], source)
    _git(["commit", "--quiet", "-m", "seed"], source)
    return source


def test_source_version_stamps_the_committed_sha(tmp_path):
    source = _checkout(tmp_path)
    sha = _git(["rev-parse", "--short=12", "HEAD"], source)

    assert product.source_version(source) == f"0.3.0+g{sha}"

    with pytest.raises(product.ProductError, match="not a git checkout"):
        product.source_version(tmp_path / "not-a-repo-at-all")


def test_a_dirty_tree_is_refused_outright(tmp_path):
    """Revision ruling (amends ADR-0015): every pin names a committed SHA.
    No .dirty version can be produced, so none can be uploaded or pinned."""
    source = _checkout(tmp_path)
    (source / "worker" / "uncommitted.txt").write_text("wip")

    with pytest.raises(product.ProductError, match="dirty tree"):
        product.source_version(source)
    with pytest.raises(product.ProductError, match="theozolith test"):
        product.build_distribution(source, tmp_path / "wheels", log=lambda *_: None)
    assert not (tmp_path / "wheels").exists() or not list((tmp_path / "wheels").iterdir())


def test_build_distribution_stamps_versions_and_collects_wheels(tmp_path):
    source = _checkout(tmp_path)
    sha = _git(["rev-parse", "--short=12", "HEAD"], source)
    expected = f"0.3.0+g{sha}"
    out = tmp_path / "wheels"
    stamped: dict[str, str] = {}

    def fake_runner(args, **kwargs):
        if args[:3] == [__import__("sys").executable, "-m", "pip"]:
            component_dir = Path(args[-1])
            wheel_dir = Path(args[args.index("--wheel-dir") + 1])
            text = (component_dir / "pyproject.toml").read_text()
            name = component_dir.name
            stamped[name] = text
            (wheel_dir / f"theozolith_{name}-{expected}-py3-none-any.whl").write_bytes(b"whl")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, **kwargs)  # the git subcommands are real

    version, wheels = product.build_distribution(
        source, out, runner=fake_runner, log=lambda *_: None
    )

    assert version == expected
    assert len(wheels) == len(product.COMPONENTS)
    # Every component's sandbox pyproject carried the stamped version, so
    # the installed distributions report it back in heartbeats.
    for component in product.COMPONENTS:
        assert f'version = "{expected}"' in stamped[component]
    # The source checkout itself was never touched.
    assert 'version = "0.3.0"' in (source / "worker" / "pyproject.toml").read_text()


def test_build_distribution_names_only_this_runs_wheels(tmp_path):
    """A persistent dist/ (the bootstrap shim's out_dir, unlike the temp dir
    the `theozolith build` path uses) can still hold a previous SHA's wheels.
    build_distribution must name ONLY the wheels it just built — else the shim
    hands pip two versions of every package and pip refuses to resolve."""
    source = _checkout(tmp_path)
    sha = _git(["rev-parse", "--short=12", "HEAD"], source)
    expected = f"0.3.0+g{sha}"
    out = tmp_path / "dist"
    out.mkdir()
    # A previous build at a different SHA left its full wheel set behind.
    stale = "0.3.0+g0000deadbeef"
    for name in product.COMPONENTS:
        (out / f"theozolith_{name}-{stale}-py3-none-any.whl").write_bytes(b"old")

    def fake_runner(args, **kwargs):
        if args[:3] == [__import__("sys").executable, "-m", "pip"]:
            component_dir = Path(args[-1])
            wheel_dir = Path(args[args.index("--wheel-dir") + 1])
            name = component_dir.name
            (wheel_dir / f"theozolith_{name}-{expected}-py3-none-any.whl").write_bytes(b"whl")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, **kwargs)  # the git subcommands are real

    version, wheels = product.build_distribution(
        source, out, runner=fake_runner, log=lambda *_: None
    )

    assert version == expected
    # Exactly the fresh set — the stale wheels are neither returned nor installed.
    assert wheels == sorted(
        f"theozolith_{name}-{expected}-py3-none-any.whl" for name in product.COMPONENTS
    )
    assert not any(stale in name for name in wheels)


def test_safe_segment_rejects_traversal():
    assert product.safe_segment("0.3.0+gabc123def456")
    assert product.safe_segment("theozolith_worker-0.3.0-py3-none-any.whl")
    for hostile in ("", "..", "a/../b", "a/b", ".hidden", "-flag", "a\x00b"):
        assert not product.safe_segment(hostile), hostile


def test_product_module_imports_nothing_from_the_worker_component():
    """Component separability: theozolith_control.product must be importable
    without theozolith_worker (function-scope imports of sibling control
    modules are fine; module-level worker imports are not)."""
    source = Path(product.__file__).read_text(encoding="utf-8")
    offending = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "theozolith_worker" in line
    ]
    assert offending == []


def test_prune_artifacts_keeps_only_the_named_versions(tmp_path):
    store = tmp_path / "artifacts"
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        (store / version).mkdir(parents=True)
        (store / version / "x.whl").write_bytes(b"w")

    pruned = product.prune_artifacts(store, {"0.3.0", "0.2.0"})

    assert pruned == ["0.1.0"]
    assert sorted(p.name for p in store.iterdir()) == ["0.2.0", "0.3.0"]
    assert product.prune_artifacts(tmp_path / "absent", {"x"}) == []  # no dir, no crash


# -- an unreachable Control Node is an error, not a traceback --------------------


def test_upload_reports_an_unreachable_control_node_cleanly(tmp_path, monkeypatch):
    wheel = tmp_path / "theozolith_worker-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    def refuse(*a, **kw):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(product.urllib.request, "urlopen", refuse)

    with pytest.raises(SystemExit, match=r"cannot reach https://10\.0\.0\.2:8443"):
        product._upload_artifact("https://10.0.0.2:8443", "t", None, "0.3.0", wheel)


def _build_args(**overrides) -> argparse.Namespace:
    """`theozolith build`'s full parsed-argument surface, defaulted."""
    defaults: dict = {"source": ".", "url": None, "ca": None, "dist": None, "if_initialized": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_build_pre_flights_the_control_node_before_building(monkeypatch):
    """Four wheels take minutes; none of them are worth building if the
    Control Node that would serve them is down."""
    from theozolith_control import cli, statuscli

    monkeypatch.setattr(
        statuscli, "resolve_target", lambda url, ca: ("https://10.0.0.2:8443", "t", None)
    )

    def unreachable(url, path, **kw):
        assert path == "/api/v1/healthz"
        raise SystemExit(f"error: cannot reach {url}: [Errno 111] Connection refused")

    monkeypatch.setattr(cli, "_call", unreachable)
    monkeypatch.setattr(
        product, "build_distribution", lambda *a, **kw: pytest.fail("built before pre-flight")
    )

    with pytest.raises(SystemExit, match="cannot reach"):
        product._cmd_build(_build_args())


def test_build_dist_reuses_prebuilt_wheels(tmp_path, monkeypatch):
    """--dist (ADR-0051): the bootstrap shim's wheels upload as-is — the
    build must NOT run a second time — selected by the checkout's exact
    version in COMPONENTS order (a stale prior-SHA wheel in the persistent
    dist/ is simply never matched), then the pin update fires."""
    from theozolith_control import cli, statuscli

    version = "0.3.0+gabc123def456"
    monkeypatch.setattr(
        statuscli, "resolve_target", lambda url, ca: ("https://10.0.0.2:8443", "t", None)
    )
    dialed: list[str] = []
    monkeypatch.setattr(cli, "_call", lambda url, path, **kw: dialed.append(path) or {})
    monkeypatch.setattr(product, "source_version", lambda source: version)
    monkeypatch.setattr(
        product, "build_distribution", lambda *a, **kw: pytest.fail("must not rebuild")
    )
    uploads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        product,
        "_upload_artifact",
        lambda url, token, ca, ver, path: uploads.append((ver, path.name)),
    )
    monkeypatch.setattr(product, "_update_via_api", lambda args, ver: 0)
    for component in product.COMPONENTS:
        (tmp_path / f"theozolith_{component}-{version}-py3-none-any.whl").write_bytes(b"w")
    stale = "theozolith_worker-0.3.0+gdeadbeef0000-py3-none-any.whl"
    (tmp_path / stale).write_bytes(b"old")

    assert product._cmd_build(_build_args(dist=str(tmp_path))) == 0
    assert dialed == ["/api/v1/healthz"]
    assert uploads == [
        (version, f"theozolith_{c}-{version}-py3-none-any.whl") for c in product.COMPONENTS
    ]


def test_build_dist_refuses_wheels_off_the_checkout_version(tmp_path, monkeypatch):
    """A dist dir that does not carry this checkout's wheels is refused
    loudly BEFORE any upload — never "upload whatever is lying around"."""
    from theozolith_control import cli, statuscli

    monkeypatch.setattr(
        statuscli, "resolve_target", lambda url, ca: ("https://10.0.0.2:8443", "t", None)
    )
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: {})
    monkeypatch.setattr(product, "source_version", lambda source: "0.3.0+gabc123def456")
    monkeypatch.setattr(
        product, "_upload_artifact", lambda *a, **kw: pytest.fail("uploaded a mismatched wheel")
    )
    for component in product.COMPONENTS:
        wheel = f"theozolith_{component}-0.3.0+gdeadbeef0000-py3-none-any.whl"
        (tmp_path / wheel).write_bytes(b"w")

    with pytest.raises(product.ProductError, match="does not match the checkout"):
        product._cmd_build(_build_args(dist=str(tmp_path)))


def test_build_if_initialized_skips_on_an_uninitialized_box(monkeypatch, capsys):
    """--if-initialized (ADR-0051): exactly statuscli's TargetError — the two
    uninitialized-box shapes, no URL / no admin token — becomes a printed
    skip with exit 0, so the bootstrap shim's chained publish is safe on the
    Control Node box being born; nothing is dialed, built, or uploaded."""
    from theozolith_control import cli, statuscli

    def unresolved(url, ca):
        raise statuscli.TargetError("no admin token — on the Control Node run this under sudo")

    monkeypatch.setattr(statuscli, "resolve_target", unresolved)
    monkeypatch.setattr(cli, "_call", lambda *a, **kw: pytest.fail("dialed while skipping"))
    monkeypatch.setattr(
        product, "build_distribution", lambda *a, **kw: pytest.fail("built while skipping")
    )

    assert product._cmd_build(_build_args(if_initialized=True)) == 0
    assert "publish skipped" in capsys.readouterr().out


def test_build_still_fails_loud_without_the_flag(monkeypatch):
    """Without --if-initialized the CLI contract is unchanged: an
    unresolvable target is a SystemExit refusal (the _admin_env shape) —
    a human running `theozolith build` on an uninitialized box must not
    get a silent success."""
    from theozolith_control import statuscli

    def unresolved(url, ca):
        raise statuscli.TargetError("no Control Node URL — set CONTROL_NODE_URL")

    monkeypatch.setattr(statuscli, "resolve_target", unresolved)
    with pytest.raises(SystemExit, match="no Control Node URL"):
        product._cmd_build(_build_args())
