"""Tests for the `git_repo` extra (see extras._git_repo) and its install vs
`configme upgrade` behavior.

On install a `git_repo` entry clones a missing dir (gated by a default-no
confirm) and leaves an existing one untouched. On `upgrade` it instead refreshes
a *present* checkout with `git pull` (via Runner.pull_dir) — still gated by the
same default-no confirm, which `-y` flips to yes — and clones a missing one just
like install. The `repos` filter narrows the entries to a named subset.

These tests stub Runner.pull_dir and subprocess.run so they never touch a real
remote, and drive the handler directly the way the other extras tests do.
"""

import types

from configme import extras, install


YES = lambda question, default=True: True
NO = lambda question, default=False: False


def _entry(tmp_path, **over):
    e = {"dir": "input", "org": "cxesmc", "repo": "climber-x-input",
         "host": "gitlab.pik-potsdam.de", "protocol": "https"}
    e.update(over)
    return e


# ----------------------------------------------------------- install semantics

def test_install_existing_dir_is_left_untouched(tmp_path):
    """A re-run install over an existing dir neither clones nor pulls it."""
    (tmp_path / "input").mkdir()
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=False)
    assert out == "input=exists"


def test_install_missing_dir_clones_when_confirmed(tmp_path, monkeypatch):
    """A missing dir is cloned once the default-no prompt is accepted."""
    calls = []
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=False)
    assert out == "input=cloned"
    assert calls and calls[0][:2] == ["git", "clone"]


def test_install_missing_dir_declined_is_deferred(tmp_path):
    """Declining the clone defers it: recorded as skipped and the command is
    appended to followups for the summary."""
    followups = []
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=NO, followups=followups,
                           upgrade=False)
    assert out == "input=skipped"
    assert any("git clone" in c for c in followups)


# ----------------------------------------------------------- upgrade semantics

def test_upgrade_existing_dir_pulls_when_confirmed(tmp_path, monkeypatch):
    """On upgrade a present checkout is refreshed via Runner.pull_dir (a pull,
    not a clone) once the prompt is accepted."""
    (tmp_path / "input").mkdir()
    seen = {}
    def fake_pull_dir(self, name, dest):
        seen["name"], seen["dest"] = name, dest
        return "updated", True
    monkeypatch.setattr(install.Runner, "pull_dir", fake_pull_dir)
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=True)
    assert out == "input=updated"
    assert seen == {"name": "input", "dest": tmp_path / "input"}


def test_upgrade_existing_dir_declined_skips_pull(tmp_path, monkeypatch):
    """Declining the pull leaves the checkout untouched (no pull_dir call)."""
    (tmp_path / "input").mkdir()
    called = []
    monkeypatch.setattr(install.Runner, "pull_dir",
                        lambda self, n, d: called.append(n) or ("updated", True))
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=NO, upgrade=True)
    assert out == "input=skipped"
    assert called == []


def test_upgrade_missing_dir_clones_like_install(tmp_path, monkeypatch):
    """A dir that was never cloned is still offered a fresh clone on upgrade."""
    calls = []
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=True)
    assert out == "input=cloned"
    assert calls and calls[0][:2] == ["git", "clone"]


def test_upgrade_pull_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """A non-fast-forwardable tree (pull_dir raises) is reported as failed for
    that repo, not propagated as an exception."""
    (tmp_path / "input").mkdir()
    def boom(self, name, dest):
        raise install.InstallError("diverged")
    monkeypatch.setattr(install.Runner, "pull_dir", boom)
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=True)
    assert out == "input=failed"


# ------------------------------------------------------------- --repos filter

def test_repos_filter_skips_unnamed_entries(tmp_path, monkeypatch):
    """With a `repos` set that excludes the entry's dir, nothing happens to it."""
    (tmp_path / "input").mkdir()
    monkeypatch.setattr(install.Runner, "pull_dir",
                        lambda self, n, d: (_ for _ in ()).throw(
                            AssertionError("should not pull a filtered-out repo")))
    runner = install.Runner(dry_run=False)
    out = extras._git_repo([_entry(tmp_path)], runner, tmp_path, cfg={},
                           ask=None, confirm=YES, upgrade=True,
                           repos={"something-else"})
    assert out == ""


def test_run_extras_with_repos_skips_non_repo_extras(tmp_path, monkeypatch):
    """When `repos` is given, run_extras runs only repo-managing extras
    (git_repo) — pip_package / runme_config are not repos and are skipped."""
    ran = []
    monkeypatch.setitem(extras._HANDLERS, "git_repo",
                        lambda *a, **k: ran.append("git_repo") or "")
    monkeypatch.setitem(extras._HANDLERS, "pip_package",
                        lambda *a, **k: ran.append("pip_package") or "")
    orch = types.SimpleNamespace(extras={
        "pip_package": ["runme"],
        "git_repo": [_entry(tmp_path)],
    })
    runner = install.Runner(dry_run=False)
    extras.run_extras(orch, runner, tmp_path, cfg={}, ask=None, confirm=YES,
                      upgrade=True, repos={"input"})
    assert ran == ["git_repo"]
