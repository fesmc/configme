"""Tests for auxiliary/data repos modelled as clone-only packages referenced
from an orchestrator's `data_packages` list (Design A — see DESIGN.md sec. 9).

climber-x's `input` data repo is the live example: a `clone_policy = "prompt"`,
`config_style = "none"` package on GitLab, cloned into ./input at the climber-x
root, outside the build graph. These tests cover how it enters the plan, where
it lands on disk, and the `prompt` clone-phase / upgrade-reminder behavior.
"""

from pathlib import Path

from configme import data, install


# ------------------------------------------------------------- registry / plan

def test_climber_x_input_is_a_data_package():
    pkg = data.packages()["climber-x-input"]
    assert pkg.clone_policy == "prompt"
    assert pkg.config_style == "none"
    assert pkg.protocol == "https"
    assert pkg.host == "gitlab.pik-potsdam.de"
    assert pkg.dir == "input"


def test_climber_x_declares_input_as_data_package():
    orch = data.orchestrators()["climber-x"]
    assert "climber-x-input" in orch.data_packages
    # It is not a build component nor an optional component.
    assert "climber-x-input" not in orch.default_packages
    assert "climber-x-input" not in orch.optional_packages


def test_data_repo_is_a_clone_only_node_outside_the_build_graph():
    plan = install.build_plan("climber-x")
    node = next(n for n in plan.nodes if n.name == "climber-x-input")
    assert node.clone is True          # it is cloned...
    assert node.build is None          # ...but never built...
    assert node.config_style == "none" # ...and never configured.
    assert node.subpackages == []      # pulls in no dependencies


def test_data_repo_lands_at_its_dir_under_root():
    plan = install.build_plan("climber-x")
    node = next(n for n in plan.nodes if n.name == "climber-x-input")
    assert install.dest_of(node, plan, Path("/r")) == Path("/r/input")


def test_data_repo_is_in_the_repos_universe_for_upgrade():
    # --repos can name a data repo because it is an ordinary clone node now.
    plan = install.build_plan("climber-x")
    clone_names = [n.name for n in plan.nodes if n.clone]
    assert "climber-x-input" in clone_names


# --------------------------------------------------------- upgrade reminder

def test_upgrade_reminds_about_never_installed_data_repo(tmp_path, monkeypatch,
                                                         capsys):
    """`configme upgrade` on a tree where the (prompt) data repo was never
    cloned ends with a "Not installed" reminder naming it and the install
    command — instead of cloning it (upgrade never clones). Heavy collaborators
    are stubbed so the run touches neither the network nor make/pip."""
    (tmp_path / ".configme").mkdir(parents=True)
    (tmp_path / ".configme" / "manifest.toml").write_text('package = "climber-x"\n')
    # Neutralize configure/netcdf/extras so only the pull + reminder logic runs.
    monkeypatch.setattr(install, "configure_makefile", lambda **k: None)
    monkeypatch.setattr(install.netcdf, "detect", lambda: None)
    monkeypatch.setattr(install.extras_mod, "run_extras", lambda *a, **k: [])

    rc = install.run_upgrade(
        "climber-x", install_dir=str(tmp_path), machine="macbook",
        compiler="gfortran", build_deps=False, dry_run=False,
        confirm_fn=lambda q, d=True: d)

    out = capsys.readouterr().out
    assert "Not installed" in out
    assert "climber-x-input" in out
    assert "configme install climber-x" in out
    # The data repo was never cloned, and it is *not* lumped in with "missing".
    assert not (tmp_path / "input").exists()
    assert rc == 0
