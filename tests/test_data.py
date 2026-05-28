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
