"""Tests for the read-only `configme status` inspector.

The inspector reconstructs the install plan from the registry and probes a
directory tree. Tests build a real plan (``yelmox`` / ``climber-x``) and lay out
fake checkouts under a tmp root, asserting the per-component state.
"""

from pathlib import Path

import pytest

from configme import data, install, status


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
                 "FastIsostasy", "FastHydrology", "rembo1"):
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
    root = tmp_path / "fesm-utils"
    checkout(root)
    # All serial artifacts present (utils lib + fftw/lis/shtns), omp none.
    for p in data.packages()["fesm-utils"].artifacts["serial"]:
        touch(root / p)
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "build", "fesm-utils (serial)") == "ok"
    assert state_of(checks, "build", "fesm-utils (omp)") == "pending"


def test_build_partial_when_some_artifacts_present(tmp_path):
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    # Only one of the serial artifacts present -> partial.
    touch(tmp_path / "fesm-utils" / "lis/lis-serial/lib/liblis.a")
    checks = status.inspect(plan, tmp_path)
    build = by_name(checks, "build")["fesm-utils (serial)"]
    assert build.state == "partial"
    assert "include-serial/libfesmutils.a" in build.detail
    assert status.has_problems(checks)


# --------------------------------------------------------------- extras

def test_data_link_extra_pending_then_ok(tmp_path):
    plan = install.build_plan("yelmox")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "extra", "data_link ice_data") == "pending"

    (tmp_path / "ice_data").symlink_to(tmp_path)  # any existing target
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "extra", "data_link ice_data") == "ok"


def test_runme_config_extra_pending_then_ok(tmp_path):
    """The runme_config extra is ``pending`` until `.runme/config.toml` exists
    (the path `runme config init` creates), then flips to ``ok``."""
    plan = install.build_plan("yelmox")
    checks = status.inspect(plan, tmp_path)
    rc = by_name(checks, "extra")["runme_config"]
    assert rc.state == "pending"
    assert ".runme/config.toml" in rc.detail

    (tmp_path / ".runme").mkdir()
    (tmp_path / ".runme" / "config.toml").write_text("")
    checks = status.inspect(plan, tmp_path)
    assert state_of(checks, "extra", "runme_config") == "ok"


def test_data_repo_pending_under_repo_not_extra():
    # climber-x's `input` data repo is a data_packages package now, so an absent
    # one is a pending *repo*, not an extra. (Its clone_policy is "prompt".)
    plan = install.build_plan("climber-x")
    checks = status.inspect(plan, Path("/nonexistent-root-xyz"))
    repos = by_name(checks, "repo")
    assert "climber-x-input" in repos
    assert repos["climber-x-input"].state == "pending"
    assert repos["climber-x-input"].hint == "configme install climber-x-input"
    # And it is no longer reported as an extra.
    extras = by_name(checks, "extra")
    assert not any("input" in name or "git_repo" in name for name in extras)


# --------------------------------------------------------------- config

def test_config_pending_until_generated_makefile(tmp_path):
    """A makefile-template package is ``config`` pending until it carries a
    configme-*generated* Makefile — a bare committed template does not count."""
    plan = install.build_plan("yelmox")
    checkout(tmp_path / "fesm-utils")
    cfg = {c.pkg: c for c in status.inspect(plan, tmp_path) if c.category == "config"}
    assert cfg["fesm-utils"].state == "pending"

    # A plain Makefile (no marker) is still "not configured".
    (tmp_path / "fesm-utils" / "Makefile").write_text("all:\n\techo hi\n")
    cfg = {c.pkg: c for c in status.inspect(plan, tmp_path) if c.category == "config"}
    assert cfg["fesm-utils"].state == "pending"

    # The generated marker flips it to ok.
    (tmp_path / "fesm-utils" / "Makefile").write_text(
        "## Generated by configme — machine=x, compiler=y.\nall:\n")
    cfg = {c.pkg: c for c in status.inspect(plan, tmp_path) if c.category == "config"}
    assert cfg["fesm-utils"].state == "ok"


# --------------------------------------------------------------- rendering

def _yelmox_layout(root: Path):
    """A partially-present yelmox: orchestrator + fesm-utils + yelmo checkouts
    (fesm-utils unbuilt/unconfigured, yelmo not yet linked to it)."""
    checkout(root)
    checkout(root / "fesm-utils")
    checkout(root / "yelmo")
    return install.build_plan("yelmox")


def test_render_is_package_table_with_trailing_blank_line(tmp_path):
    plan = _yelmox_layout(tmp_path)
    out = status.render(plan, status.inspect(plan, tmp_path), tmp_path)
    assert "package" in out and "branch" in out      # column titles
    assert "present" in out and "build" in out
    assert "fesm-utils" in out
    assert "↳" in out                                # nested rows use the connector
    assert out.endswith("\n")                        # blank line before the prompt


def test_render_all_ok_message(tmp_path):
    # No outstanding checks -> the green all-clear summary line.
    plan = install.build_plan("yelmox")
    out = status.render(plan, [], tmp_path)
    assert "all present and built" in out


def test_render_verbose_shows_check_detail(tmp_path):
    plan = _yelmox_layout(tmp_path)                  # fesm-utils present but unbuilt
    out = status.render(plan, status.inspect(plan, tmp_path), tmp_path, verbose=True)
    assert "not built" in out                        # per-check detail line


def test_pending_block_empty_when_all_ok(tmp_path):
    plan = install.build_plan("yelmox")
    assert status.pending_block(plan, [], tmp_path) == ""


def test_pending_block_lists_outstanding_and_commands(tmp_path):
    plan = _yelmox_layout(tmp_path)
    block = status.pending_block(plan, status.inspect(plan, tmp_path), tmp_path)
    assert "Current status" in block
    assert "fesm-utils" in block
    assert "configme install fesm-utils --build-deps" in block


def test_resolve_commands_drops_dependency_covered_by_build_deps():
    """A pending build is dropped from the command list when another listed
    ``--build-deps`` install would already build it (yelmo's install builds its
    dependency fesm-utils)."""
    checks = [
        status.Check("build", "fesm-utils", "fesm-utils (omp)", "pending",
                     hint="configme install fesm-utils --build-deps", variant="omp"),
        status.Check("build", "yelmo", "yelmo (serial)", "pending",
                     hint="configme install yelmo --build-deps", variant="serial"),
    ]
    cmds = status._resolve_commands(checks)
    assert "configme install yelmo --build-deps" in cmds
    assert "configme install fesm-utils --build-deps" not in cmds


def test_command_groups_nest_specifics_under_umbrella():
    """A broad plain install (the orchestrator) becomes the umbrella; the
    per-component installs it subsumes are nested as individual alternatives."""
    checks = [
        status.Check("link", "elsa", "elsa/... -> x", "missing",
                     hint="configme install yelmox"),
        status.Check("config", "elsa", "elsa", "pending",
                     hint="configme install elsa"),
        status.Check("config", "tracer", "tracer", "pending",
                     hint="configme install tracer"),
    ]
    groups = status._command_groups(checks)
    umbrella = dict(groups)
    assert "configme install yelmox" in umbrella
    kids = umbrella["configme install yelmox"]
    assert "configme install elsa" in kids
    assert "configme install tracer" in kids


def test_render_footer_shows_or_run_individually(tmp_path):
    plan = _yelmox_layout(tmp_path)
    out = status.render(plan, status.inspect(plan, tmp_path), tmp_path)
    assert "or run individually:" in out
