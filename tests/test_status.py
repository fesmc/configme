"""Tests for the read-only `configme status` inspector.

The inspector reconstructs the install plan from the registry and probes a
directory tree. Tests build a real plan (``yelmox`` / ``climber-x``) and lay out
fake checkouts under a tmp root, asserting the per-component state.
"""

from pathlib import Path

import pytest

from configme import install, status


def checkout(path: Path) -> Path:
    """Create a fake git checkout at ``path`` (a dir carrying a ``.git`` dir)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def by_name(checks, category):
    return {c.name: c for c in checks if c.category == category}


def state_of(checks, category, name):
    return by_name(checks, category)[name].state


# --------------------------------------------------------------- repos

def test_empty_root_reports_all_repos_missing():
    plan = install.build_plan("yelmox")
    root = Path("/nonexistent-root-xyz")
    checks = status.inspect(plan, root)
    repos = by_name(checks, "repo")
    # Every cloned component (and the orchestrator) is missing on an empty root.
    assert repos["yelmo"].state == "missing"
    assert repos["fesm-utils"].state == "missing"
    assert "fesm-utils/utils" not in repos  # subpackage rides its parent
    assert status.has_problems(checks)


def test_present_repo_is_ok(tmp_path):
    plan = install.build_plan("yelmox")
    for comp in ("yelmox", "yelmo", "fesm-utils", "coordinates",
                 "FastIsostasy", "rembo1"):
        checkout(tmp_path / comp if comp != "yelmox" else tmp_path)
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "repo", "yelmo") == "ok"


def test_optional_repo_pending_not_missing():
    # climber-x carries optional private components (bgc, vilma).
    plan = install.build_plan("climber-x")
    checks = status.inspect(plan, Path("/nonexistent-root-xyz"))
    repos = by_name(checks, "repo")
    assert repos["bgc"].state == "pending"
    assert "optional" in repos["bgc"].detail


# --------------------------------------------------------------- links

def test_link_missing_when_dep_present_but_unlinked(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path)                       # yelmox
    checkout(tmp_path / "yelmo")
    checkout(tmp_path / "fesm-utils")        # dep present, but no link from yelmo
    checks = status.inspect(plan, tmp_path)
    link = by_name(checks, "link")["yelmo/fesm-utils -> fesm-utils"]
    assert link.state == "missing"
    assert "configme install yelmox" == link.hint


def test_link_ok_when_symlink_resolves(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path)
    checkout(tmp_path / "yelmo")
    checkout(tmp_path / "fesm-utils")
    (tmp_path / "yelmo" / "fesm-utils").symlink_to(tmp_path / "fesm-utils")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "link", "yelmo/fesm-utils -> fesm-utils") == "ok"


def test_link_broken_when_symlink_dangles(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path)
    checkout(tmp_path / "yelmo")
    checkout(tmp_path / "fesm-utils")
    (tmp_path / "yelmo" / "fesm-utils").symlink_to(tmp_path / "gone")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "link", "yelmo/fesm-utils -> fesm-utils") == "broken"


def test_link_pending_when_dep_absent(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path)
    checkout(tmp_path / "yelmo")             # fesm-utils dep NOT present
    checks = status.inspect(plan, tmp_path)
    link = by_name(checks, "link")["yelmo/fesm-utils -> fesm-utils"]
    assert link.state == "pending"
    assert link.hint == "configme install fesm-utils"


# --------------------------------------------------------------- builds

def test_build_pending_when_no_artifacts_present(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    checks = status.inspect(plan, tmp_path)
    builds = by_name(checks, "build")
    assert builds["fesm-utils (serial)"].state == "pending"
    assert builds["fesm-utils (omp)"].state == "pending"
    assert "--build-deps" in builds["fesm-utils (serial)"].hint


def test_build_ok_when_all_artifacts_present(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    touch(tmp_path / "fesm-utils" / "lis-serial/lib/liblis.a")
    touch(tmp_path / "fesm-utils" / "fftw-serial/lib/libfftw3.a")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "build", "fesm-utils (serial)") == "ok"
    assert state_of(checks, "build", "fesm-utils (omp)") == "pending"


def test_build_partial_when_some_artifacts_present(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    touch(tmp_path / "fesm-utils" / "lis-serial/lib/liblis.a")  # only one of two
    checks = status.inspect(plan, tmp_path)
    build = by_name(checks, "build")["fesm-utils (serial)"]
    assert build.state == "partial"
    assert "fftw-serial/lib/libfftw3.a" in build.detail
    assert status.has_problems(checks)


def test_subpackage_build_probed_under_parent(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    utils = tmp_path / "fesm-utils" / "utils"
    utils.mkdir(parents=True)
    touch(utils / "include-serial/libfesmutils.a")
    touch(utils / "include-omp/libfesmutils.a")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "build", "fesm-utils/utils (serial)") == "ok"
    assert state_of(checks, "build", "fesm-utils/utils (omp)") == "ok"


# --------------------------------------------------------------- extras

def test_data_link_extra_pending_then_ok(tmp_path):
    plan = install.build_plan("yelmox")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "extra", "data_link ice_data") == "pending"

    (tmp_path / "ice_data").symlink_to(tmp_path)  # any existing target
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "extra", "data_link ice_data") == "ok"


def test_git_repo_data_extra_pending_with_clone_hint():
    # climber-x clones an `input` data repo via a git_repo extra.
    plan = install.build_plan("climber-x")
    checks = status.inspect(plan, Path("/nonexistent-root-xyz"))
    extras = by_name(checks, "extra")
    pending = [c for c in extras.values() if c.state == "pending"
               and c.category == "extra" and "git_repo" in c.name]
    assert pending, "expected a pending git_repo data extra for climber-x"
    assert any(c.hint.startswith("git clone") for c in pending)


# --------------------------------------------------------------- rendering

def test_render_all_ok_message():
    checks = [status.Check("repo", "yelmo", "ok")]
    out = status.render(checks, Path("/x"), "yelmox")
    assert "Everything present and built." in out


def test_render_hides_ok_rows_by_default():
    checks = [status.Check("repo", "yelmo", "ok"),
              status.Check("repo", "bgc", "missing", hint="configme install bgc")]
    out = status.render(checks, Path("/x"), "yelmox")
    assert "[ok" not in out            # no ok rows rendered
    assert "bgc" in out
    assert "configme install bgc" in out


def test_render_verbose_shows_ok_rows():
    checks = [status.Check("repo", "yelmo", "ok")]
    out = status.render(checks, Path("/x"), "yelmox", verbose=True)
    assert "yelmo" in out


def test_pending_block_empty_when_all_ok():
    assert status.pending_block([status.Check("repo", "yelmo", "ok")]) == ""


def test_pending_block_lists_problems_and_commands():
    checks = [status.Check("repo", "yelmo", "ok"),
              status.Check("build", "fesm-utils (omp)", "pending",
                           hint="configme install fesm-utils --build-deps")]
    block = status.pending_block(checks)
    assert "Current status" in block
    assert "fesm-utils (omp)" in block
    assert "configme install fesm-utils --build-deps" in block
