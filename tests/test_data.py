"""Tests for the data registry — focused on the [package.artifacts] schema."""

from pathlib import Path

import pytest

from configme import data


def test_fesm_utils_artifacts_loaded():
    pkgs = data.packages()
    art = pkgs["fesm-utils"].artifacts
    assert set(art) == {"serial", "omp"}
    # utils static lib + the vendored external libs, at the flattened paths.
    assert "include-serial/libfesmutils.a" in art["serial"]
    assert "lis/lis-serial/lib/liblis.a" in art["serial"]
    assert "include-omp/libfesmutils.a" in art["omp"]
    assert "fftw/fftw-omp/lib/libfftw3.a" in art["omp"]


def test_fesm_utils_is_makefile_template_with_build():
    pkg = data.packages()["fesm-utils"]
    assert pkg.config_style == "makefile-template"
    assert pkg.build is not None
    assert pkg.build.make_target == "all"
    assert pkg.build.variants == ["serial", "omp"]


def test_package_without_artifacts_is_empty():
    assert data.packages()["yelmo"].artifacts == {}


def test_parse_artifacts_rejects_non_table():
    with pytest.raises(data.DataError):
        data._parse_artifacts(["not", "a", "table"], Path("x.toml"))


def test_parse_artifacts_rejects_non_string_paths():
    with pytest.raises(data.DataError):
        data._parse_artifacts({"serial": [1, 2]}, Path("x.toml"))


def test_parse_artifacts_rejects_non_list_value():
    with pytest.raises(data.DataError):
        data._parse_artifacts({"serial": "lib.a"}, Path("x.toml"))


def test_parse_artifacts_normalises():
    out = data._parse_artifacts({"serial": ["a.a", "b.a"]}, Path("x.toml"))
    assert out == {"serial": ["a.a", "b.a"]}


# ------------------------------------------------------------- machine_refs

def test_parse_machine_refs_normalises():
    out = data._parse_machine_refs({"dkrz_levante": "dkrz_levante", "*": "main"},
                                   Path("x.toml"))
    assert out == {"dkrz_levante": "dkrz_levante", "*": "main"}


def test_parse_machine_refs_rejects_non_table():
    with pytest.raises(data.DataError):
        data._parse_machine_refs(["not", "a", "table"], Path("x.toml"))


def test_parse_machine_refs_rejects_non_string_ref():
    with pytest.raises(data.DataError):
        data._parse_machine_refs({"m": 3}, Path("x.toml"))


def test_parse_machine_refs_rejects_empty_ref():
    with pytest.raises(data.DataError):
        data._parse_machine_refs({"m": ""}, Path("x.toml"))


def test_package_without_machine_refs_is_empty():
    assert data.packages()["yelmo"].machine_refs == {}


def test_vilma_declares_per_machine_branches():
    mrefs = data.packages()["vilma"].machine_refs
    assert mrefs["dkrz_levante"] == "dkrz_levante"
    assert mrefs["pik_hpc2024"] == "main"


# --------------------------------------------------------------- clone_policy

def test_clone_policy_defaults_to_required():
    assert data._parse_clone_policy({}, Path("x.toml")) == "required"


def test_clone_policy_explicit_values():
    for p in ("required", "optional", "prompt"):
        assert data._parse_clone_policy({"clone_policy": p}, Path("x.toml")) == p


def test_clone_policy_legacy_optional_alias():
    # `optional = true` is accepted as an alias for clone_policy = "optional".
    assert data._parse_clone_policy({"optional": True}, Path("x.toml")) == "optional"


def test_clone_policy_rejects_unknown_value():
    with pytest.raises(data.DataError):
        data._parse_clone_policy({"clone_policy": "maybe"}, Path("x.toml"))


def test_clone_policy_rejects_conflicting_legacy_and_new():
    # Setting both legacy optional=true and a non-optional clone_policy is a
    # contradiction, not a silent precedence rule.
    with pytest.raises(data.DataError):
        data._parse_clone_policy(
            {"optional": True, "clone_policy": "prompt"}, Path("x.toml"))


def test_shipped_packages_carry_expected_clone_policy():
    pkgs = data.packages()
    assert pkgs["yelmo"].clone_policy == "required"
    assert pkgs["vilma"].clone_policy == "optional"
    assert pkgs["bgc"].clone_policy == "optional"


# --------------------------------------------------------------- data_packages

def test_orchestrator_parses_data_packages_with_refs(tmp_path):
    # data_packages parses like the other component lists, including name:ref.
    toml = tmp_path / "demo.toml"
    toml.write_text(
        '[orchestrator]\n'
        'name = "demo"\norg = "x"\nrepo = "demo"\ndir = "demo"\n'
        'config_style = "makefile-template"\n'
        'default_packages = ["yelmo"]\n'
        'data_packages = ["input", "obs:v2"]\n')
    orch = data.Orchestrator.from_file(toml)
    assert orch.data_packages == ["input", "obs"]
    assert orch.component_refs["obs"] == "v2"


def test_orchestrator_data_packages_defaults_empty():
    # An orchestrator without the key has no data repos.
    assert data.orchestrators()["yelmox"].data_packages == []
