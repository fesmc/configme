"""Tests for install-side behavior that `configme status` does not cover:
the artifact-presence probe used to skip already-built packages, and the
idempotent `data_link` extra (an existing link is kept, not re-prompted).
"""

import sys
from pathlib import Path

from configme import cli, data, extras, install


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


# ------------------------------------------------------------ _artifacts_state

def test_artifacts_state_unbuilt_then_partial_then_built(tmp_path):
    paths = [p for ps in data.packages()["fesm-utils"].artifacts.values() for p in ps]
    assert install._artifacts_state("fesm-utils", tmp_path) == "unbuilt"
    touch(tmp_path / paths[0])
    assert install._artifacts_state("fesm-utils", tmp_path) == "partial"
    for p in paths[1:]:
        touch(tmp_path / p)
    assert install._artifacts_state("fesm-utils", tmp_path) == "built"


def test_artifacts_state_none_when_no_artifacts_declared(tmp_path):
    # yelmo declares no [package.artifacts]; the probe can't tell, returns "none".
    assert install._artifacts_state("yelmo", tmp_path) == "none"


# ------------------------------------------------------------------- data_link

def test_data_link_keeps_existing_without_prompting(tmp_path):
    (tmp_path / "ice_data").symlink_to(tmp_path)   # already linked
    runner = install.Runner(dry_run=False)
    asked = []

    def ask(label, default=None, *, complete_paths=False):
        asked.append(label)
        return None       # no path supplied for whatever is genuinely missing

    out = extras._data_link(["ice_data", "isostasy_data"], runner, tmp_path,
                            cfg={}, ask=ask)
    # The existing link is never prompted for; only the missing one is.
    assert asked == ["path to isostasy_data"]
    assert "ice_data=exists" in out
    assert "isostasy_data=pending" in out


def test_data_link_uses_config_path_when_missing(tmp_path):
    target = tmp_path / "store"
    target.mkdir()
    runner = install.Runner(dry_run=False)

    def ask(label, default=None, *, complete_paths=False):
        raise AssertionError("should not prompt when cfg supplies the path")

    out = extras._data_link(["ice_data"], runner, tmp_path,
                            cfg={"ice_data": str(target)}, ask=ask)
    assert "ice_data=linked" in out
    assert (tmp_path / "ice_data").resolve() == target.resolve()


def test_data_link_refuses_self_referential_target(tmp_path):
    # cfg points the data link at root itself; would create root/ice_data ->
    # root (a silent no-op loop). Same APFS-case-insensitive trap as
    # link_external — refuse, don't write the link.
    runner = install.Runner(dry_run=False)
    out = extras._data_link(["ice_data"], runner, tmp_path,
                            cfg={"ice_data": str(tmp_path)},
                            ask=lambda *a, **k: None)
    assert "ice_data=skipped (self-loop)" in out
    assert not (tmp_path / "ice_data").exists()


# ----------------------------------------------------- install pip-tool shortcut

def test_install_runme_dry_run_prints_pip_command(capsys, monkeypatch):
    """`configme install runme --dry-run` prints the exact pip command without
    running anything — confirms the pip-tool shortcut bypasses build_plan and
    never shells out under --dry-run."""
    called = []
    monkeypatch.setattr(
        "configme.cli.subprocess.run",
        lambda *a, **kw: called.append((a, kw)) or None,
    )
    rc = cli._install_pip_tool("runme", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert called == []                          # dry-run: no subprocess
    assert "DRY RUN" in out
    expected = (f"{sys.executable} -m pip install -U "
                "git+https://github.com/fesmc/runme")
    assert expected in out


def test_install_runme_dispatches_to_pip_not_build_plan(monkeypatch):
    """`cmd_install` with target=runme must short-circuit to the pip shortcut
    rather than calling `install.run_install`, which would fail build_plan on
    the unknown package name."""
    seen = {}

    def fake_run(cmd, check):
        seen["cmd"] = cmd
        seen["check"] = check

    def boom(*a, **kw):
        raise AssertionError("run_install must not be called for a pip tool")

    monkeypatch.setattr("configme.cli.subprocess.run", fake_run)
    monkeypatch.setattr("configme.cli.install.run_install", boom)

    class Args:
        target = "runme"
        dry_run = False
    rc = cli.cmd_install(Args())
    assert rc == 0
    assert seen["check"] is True
    assert seen["cmd"] == [sys.executable, "-m", "pip", "install", "-U",
                           "git+https://github.com/fesmc/runme"]
