"""Tests for the `pip_package` extra and its shared helpers (see
extras._pip_package, extras.pip_tool_url, extras.pip_tool_satisfied).

A `pip_package` entry installs a fesmc tool via `pip install -U` of its git URL.
A `name:ref` pin appends `@ref` to the URL; a pinned *version* that already
matches the installed package metadata is skipped (branch/commit refs cannot be
confirmed installed, so they always (re)install). Tests mock `subprocess.run`
so they never touch real pip, and mock `_installed_version` for the skip-check.
"""

from configme import extras, install


# ------------------------------------------------------------- pip_tool_url

def test_pip_tool_url_unpinned():
    assert (extras.pip_tool_url("runme")
            == "git+https://github.com/fesmc/runme")


def test_pip_tool_url_pinned_appends_at_ref():
    assert (extras.pip_tool_url("runme", "v0.3.1")
            == "git+https://github.com/fesmc/runme@v0.3.1")


# -------------------------------------------------------- pip_tool_satisfied

def test_satisfied_unpinned_is_never_satisfied(monkeypatch):
    """No ref → always (re)install latest; never reports satisfied."""
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.3.1")
    assert extras.pip_tool_satisfied("runme", None) is False


def test_satisfied_when_version_matches_ignoring_v(monkeypatch):
    """A `vX.Y.Z` tag matches installed `X.Y.Z` (leading `v` ignored)."""
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.3.1")
    assert extras.pip_tool_satisfied("runme", "v0.3.1") is True
    assert extras.pip_tool_satisfied("runme", "0.3.1") is True


def test_not_satisfied_when_version_differs(monkeypatch):
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.4.0")
    assert extras.pip_tool_satisfied("runme", "v0.3.1") is False


def test_not_satisfied_when_not_installed(monkeypatch):
    monkeypatch.setattr(extras, "_installed_version", lambda n: None)
    assert extras.pip_tool_satisfied("runme", "v0.3.1") is False


def test_branch_ref_never_matches_a_version(monkeypatch):
    """A branch ref can't equal a version string, so it always (re)installs."""
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.3.1")
    assert extras.pip_tool_satisfied("runme", "dev") is False


# ------------------------------------------------------------ _pip_package

def test_pip_package_unpinned_installs_with_U(monkeypatch):
    """Bare `runme` → `pip install -U` of the unpinned URL (today's behavior)."""
    seen = {}
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda cmd, check: seen.setdefault("cmd", list(cmd)))
    runner = install.Runner(dry_run=False)
    out = extras._pip_package(["runme"], runner, None, cfg={}, ask=None)
    assert out == "runme=ok"
    assert seen["cmd"][-3:] == ["install", "-U",
                                "git+https://github.com/fesmc/runme"]


def test_pip_package_pinned_mismatch_installs_at_ref(monkeypatch):
    """Pinned to a version not installed → `pip install -U` of the `@ref` URL."""
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.2.0")
    seen = {}
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda cmd, check: seen.setdefault("cmd", list(cmd)))
    runner = install.Runner(dry_run=False)
    out = extras._pip_package(["runme:v0.3.1"], runner, None, cfg={}, ask=None)
    assert out == "runme=ok"
    assert seen["cmd"][-1] == "git+https://github.com/fesmc/runme@v0.3.1"


def test_pip_package_pinned_satisfied_skips(monkeypatch):
    """Pinned to the already-installed version → no pip shellout."""
    monkeypatch.setattr(extras, "_installed_version", lambda n: "0.3.1")
    called = []
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda *a, **kw: called.append(a))
    runner = install.Runner(dry_run=False)
    out = extras._pip_package(["runme:v0.3.1"], runner, None, cfg={}, ask=None)
    assert out == "runme=present"
    assert called == []
    assert "already installed" in "\n".join(runner.log)
