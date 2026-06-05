"""Tests for the ``--link`` / ``links.toml`` flow and the build stamp."""

from pathlib import Path

import pytest

from configme import context, install, links


# ---------------------------------------------------------- parse_link_args

def test_parse_link_args_single():
    out = links.parse_link_args(["fesm-utils=/abs/path"])
    assert out == {"fesm-utils": Path("/abs/path")}


def test_parse_link_args_repeatable():
    out = links.parse_link_args([
        "fesm-utils=/a/path",
        "coordinates=/b/path",
    ])
    assert out == {
        "fesm-utils": Path("/a/path"),
        "coordinates": Path("/b/path"),
    }


def test_parse_link_args_missing_equals_errors():
    with pytest.raises(links.LinkError, match="expected PKG=PATH"):
        links.parse_link_args(["fesm-utils"])


def test_parse_link_args_empty_value_errors():
    with pytest.raises(links.LinkError, match="empty"):
        links.parse_link_args(["fesm-utils="])
    with pytest.raises(links.LinkError, match="empty"):
        links.parse_link_args(["=/some/path"])


def test_parse_link_args_duplicate_errors():
    with pytest.raises(links.LinkError, match="more than once"):
        links.parse_link_args(["fesm-utils=/a", "fesm-utils=/b"])


def test_parse_link_args_expands_user(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    out = links.parse_link_args(["fesm-utils=~/repos/fesm-utils"])
    assert out["fesm-utils"] == Path("/tmp/fakehome/repos/fesm-utils")


# ---------------------------------------------------------- load + merge

def _write_links_toml(path: Path, mapping: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "[links]\n" + "\n".join(f'"{k}" = "{v}"' for k, v in mapping.items())
    path.write_text(body)


def test_load_file_links_global_and_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CONFIGME_HOME", str(home))
    _write_links_toml(home / "links.toml", {"fesm-utils": "/g/fesm-utils"})

    project = tmp_path / "project"
    _write_links_toml(project / ".configme" / "links.toml",
                      {"coordinates": "/p/coords"})

    g, p = links.load_file_links(project)
    assert g == {"fesm-utils": Path("/g/fesm-utils")}
    assert p == {"coordinates": Path("/p/coords")}


def test_load_file_links_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "nope"))
    g, p = links.load_file_links(tmp_path / "no-project")
    assert g == {} and p == {}


def test_merge_precedence_cli_over_project_over_global():
    g = {"fesm-utils": Path("/global")}
    p = {"fesm-utils": Path("/project")}
    c = {"fesm-utils": Path("/cli")}
    merged = links.merge_links(g, p, c)
    assert merged == {"fesm-utils": (Path("/cli"), "cli")}

    merged_no_cli = links.merge_links(g, p, {})
    assert merged_no_cli == {"fesm-utils": (Path("/project"), "project")}

    merged_global_only = links.merge_links(g, {}, {})
    assert merged_global_only == {"fesm-utils": (Path("/global"), "global")}


# ---------------------------------------------------------- validate

def test_validate_links_unknown_package_errors(tmp_path):
    target = tmp_path / "fesm-utils"
    target.mkdir()
    bad = {"definitely-not-a-package": (target, "cli")}
    with pytest.raises(links.LinkError, match="not a known package"):
        links.validate_links(bad, ["fesm-utils"])


def test_validate_links_missing_path_hard_errors(tmp_path):
    missing = {"fesm-utils": (tmp_path / "ghost", "cli")}
    with pytest.raises(links.LinkError, match="does not exist"):
        links.validate_links(missing, ["fesm-utils"])


def test_validate_links_file_not_directory_errors(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("")
    with pytest.raises(links.LinkError, match="not a directory"):
        links.validate_links({"fesm-utils": (f, "cli")}, ["fesm-utils"])


def test_validate_links_ok(tmp_path):
    target = tmp_path / "fesm-utils"
    target.mkdir()
    # No exception
    links.validate_links({"fesm-utils": (target, "cli")}, ["fesm-utils"])


# ---------------------------------------------------------- prompt

def test_confirm_file_links_cli_always_applied():
    asked = []
    def confirm(prompt, default):
        asked.append(prompt)
        return False
    out = links.confirm_file_links(
        {"fesm-utils": (Path("/a"), "cli")}, confirm)
    assert out == {"fesm-utils": Path("/a")}
    assert asked == []  # never prompted for CLI


def test_confirm_file_links_file_prompted_and_filtered():
    answers = {"fesm-utils": True, "coordinates": False}
    def confirm(prompt, default):
        for k in answers:
            if k in prompt:
                return answers[k]
        raise AssertionError(prompt)
    merged = {
        "fesm-utils": (Path("/a"), "project"),
        "coordinates": (Path("/b"), "global"),
    }
    out = links.confirm_file_links(merged, confirm)
    assert out == {"fesm-utils": Path("/a")}


def test_confirm_file_links_no_confirm_fn_accepts_all():
    merged = {"fesm-utils": (Path("/a"), "project")}
    out = links.confirm_file_links(merged, None)
    assert out == {"fesm-utils": Path("/a")}


# ---------------------------------------------------------- build stamp

def test_write_and_read_build_stamp(tmp_path):
    p = links.write_build_stamp(tmp_path, tool="build.py", machine="mistral",
                                compiler="ifort", variants=["serial", "omp"])
    assert p == tmp_path / ".configme-build.toml"
    stamp = links.read_build_stamp(tmp_path)
    assert stamp == {
        "tool": "build.py", "machine": "mistral", "compiler": "ifort",
        "variants": ["serial", "omp"],
    }


def test_read_build_stamp_missing_returns_none(tmp_path):
    assert links.read_build_stamp(tmp_path) is None


def test_stamp_mismatch_detects_machine_change():
    stamp = {"machine": "mistral", "compiler": "ifort"}
    assert links.stamp_mismatch(stamp, machine="pik_hpc", compiler="ifort")
    assert links.stamp_mismatch(stamp, machine="mistral", compiler="gfortran")
    assert links.stamp_mismatch(stamp, machine="mistral", compiler="ifort") is None


# ---------------------------------------------------------- link_external runner

def test_link_external_creates_symlink(tmp_path):
    target = tmp_path / "external" / "fesm-utils"
    target.mkdir(parents=True)
    runner = install.Runner(dry_run=False)
    node = install.Node("fesm-utils", "fesmc", "fesm-utils", "fesm-utils",
                        "build.py", "", [], False)
    dest = tmp_path / "yelmox" / "fesm-utils"
    dest.parent.mkdir()
    st = runner.link_external(node, dest, target)
    assert st == "linked"
    assert dest.is_symlink()
    assert dest.resolve() == target.resolve()


def test_link_external_idempotent_when_already_linked_correctly(tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    dest = tmp_path / "yelmox" / "fesm-utils"
    dest.parent.mkdir()
    dest.symlink_to(target.resolve())
    runner = install.Runner(dry_run=False)
    node = install.Node("fesm-utils", "fesmc", "fesm-utils", "fesm-utils",
                        "build.py", "", [], False)
    st = runner.link_external(node, dest, target)
    assert st == "exists"


def test_link_external_refuses_to_clobber(tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    dest = tmp_path / "yelmox" / "fesm-utils"
    dest.mkdir(parents=True)
    (dest / ".git").mkdir()  # looks like a real checkout
    runner = install.Runner(dry_run=False)
    node = install.Node("fesm-utils", "fesmc", "fesm-utils", "fesm-utils",
                        "build.py", "", [], False)
    with pytest.raises(install.InstallError, match="already exists"):
        runner.link_external(node, dest, target)


def test_link_external_overwrite_moves_to_outdated(tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    dest = tmp_path / "yelmox" / "fesm-utils"
    dest.mkdir(parents=True)
    (dest / "marker").write_text("real-checkout")
    runner = install.Runner(dry_run=False, overwrite=True)
    node = install.Node("fesm-utils", "fesmc", "fesm-utils", "fesm-utils",
                        "build.py", "", [], False)
    st = runner.link_external(node, dest, target)
    assert st == "linked"
    assert dest.is_symlink()
    assert (dest.parent / "outdated-repos" / "fesm-utils" / "marker").exists()


def test_link_external_dry_run(tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    dest = tmp_path / "yelmox" / "fesm-utils"
    runner = install.Runner(dry_run=True)
    node = install.Node("fesm-utils", "fesmc", "fesm-utils", "fesm-utils",
                        "build.py", "", [], False)
    st = runner.link_external(node, dest, target)
    assert st == "dry"
    assert not dest.exists()
    # Emitted to the install.sh log
    assert any("ln -s" in line and "fesm-utils" in line for line in runner.log)


# ---------------------------------------------------------- _resolve_links integration

def test_resolve_links_cli_not_prompted(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "home"))
    target = tmp_path / "external" / "fesm-utils"
    target.mkdir(parents=True)
    plan = install.build_plan("FastHydrology")
    asked = []
    def confirm(prompt, default):
        asked.append(prompt)
        return True
    out = install._resolve_links(plan, tmp_path / "project",
                                 [f"fesm-utils={target}"], confirm)
    assert out == {"fesm-utils": Path(target)}
    assert asked == []  # CLI links bypass prompts


def test_resolve_links_unknown_package_install_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "home"))
    target = tmp_path / "thing"
    target.mkdir()
    plan = install.build_plan("FastHydrology")
    with pytest.raises(install.InstallError, match="not a known package"):
        install._resolve_links(plan, tmp_path / "project",
                               [f"not-a-package={target}"], None)


def test_resolve_links_missing_path_install_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "home"))
    plan = install.build_plan("FastHydrology")
    with pytest.raises(install.InstallError, match="does not exist"):
        install._resolve_links(plan, tmp_path / "project",
                               [f"fesm-utils={tmp_path / 'ghost'}"], None)


def test_resolve_links_file_links_prompted_per_link(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CONFIGME_HOME", str(home))
    target = tmp_path / "external" / "fesm-utils"
    target.mkdir(parents=True)
    _write_links_toml(home / "links.toml", {"fesm-utils": str(target)})

    plan = install.build_plan("FastHydrology")
    prompts = []
    def confirm(prompt, default):
        prompts.append(prompt)
        return True
    out = install._resolve_links(plan, tmp_path / "project", [], confirm)
    assert out == {"fesm-utils": target}
    assert any("fesm-utils" in p and "global" in p for p in prompts)


def test_resolve_links_project_overrides_global_in_prompt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CONFIGME_HOME", str(home))
    proj = tmp_path / "project"
    g_target = tmp_path / "g_fesm"
    p_target = tmp_path / "p_fesm"
    g_target.mkdir()
    p_target.mkdir()
    _write_links_toml(home / "links.toml", {"fesm-utils": str(g_target)})
    _write_links_toml(proj / ".configme" / "links.toml",
                      {"fesm-utils": str(p_target)})

    plan = install.build_plan("FastHydrology")
    prompts = []
    def confirm(prompt, default):
        prompts.append(prompt)
        return True
    out = install._resolve_links(plan, proj, [], confirm)
    assert out == {"fesm-utils": p_target}
    # The single prompt names the project source (not global)
    assert any("project" in p for p in prompts)
    assert not any("global" in p for p in prompts)


def test_resolve_links_user_declines_prompt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CONFIGME_HOME", str(home))
    target = tmp_path / "external" / "fesm-utils"
    target.mkdir(parents=True)
    _write_links_toml(home / "links.toml", {"fesm-utils": str(target)})

    plan = install.build_plan("FastHydrology")
    out = install._resolve_links(plan, tmp_path / "project", [],
                                 confirm_fn=lambda prompt, default: False)
    assert out == {}
