"""Tests for the data registry — focused on the [package.artifacts] schema."""

from pathlib import Path

import pytest

from configme import data


def test_fesm_utils_artifacts_loaded():
    pkgs = data.packages()
    art = pkgs["fesm-utils"].artifacts
    assert set(art) == {"serial", "omp"}
    assert "lis-serial/lib/liblis.a" in art["serial"]
    assert "fftw-omp/lib/libfftw3.a" in art["omp"]


def test_utils_artifacts_loaded():
    art = data.packages()["fesm-utils/utils"].artifacts
    assert art["serial"] == ["include-serial/libfesmutils.a"]
    assert art["omp"] == ["include-omp/libfesmutils.a"]


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
