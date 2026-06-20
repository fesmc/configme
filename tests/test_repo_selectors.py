"""Tests for `install.resolve_repo_selectors` — the `--repos` resolver shared by
`configme upgrade` and `configme git`. Each entry is a package name or a path to
where a checkout lives; a path is resolved (~/vars/relative-to-cwd, symlinks
followed) and matched against the candidates' on-disk locations.
"""

import os

import pytest

from configme import install


def test_name_match_returns_name(tmp_path):
    cands = [("climber-x-input", tmp_path / "input"), ("yelmo", tmp_path / "yelmo")]
    assert install.resolve_repo_selectors(["yelmo"], cands) == {"yelmo"}


def test_absolute_path_resolves_to_package_name(tmp_path):
    (tmp_path / "input").mkdir()
    cands = [("climber-x-input", tmp_path / "input"), ("yelmo", tmp_path / "yelmo")]
    got = install.resolve_repo_selectors([str(tmp_path / "input")], cands)
    assert got == {"climber-x-input"}


def test_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    (tmp_path / "input").mkdir()
    cands = [("climber-x-input", tmp_path / "input")]
    monkeypatch.chdir(tmp_path)
    # "input" is not a package name, so it is tried as a path relative to cwd.
    assert install.resolve_repo_selectors(["input"], cands) == {"climber-x-input"}


def test_symlinked_checkout_matches_real_location(tmp_path):
    # A --link target: the checkout's dest is a symlink to an external dir, so
    # pointing at the real location still selects it (both sides are resolved).
    real = tmp_path / "external" / "yelmo"
    real.mkdir(parents=True)
    link = tmp_path / "yelmo"
    link.symlink_to(real)
    cands = [("yelmo", link)]
    assert install.resolve_repo_selectors([str(real)], cands) == {"yelmo"}


def test_name_wins_over_a_path_that_happens_to_exist(tmp_path, monkeypatch):
    # "input" is both a real package name (mapped to dir A) and an existing
    # relative path (dir tmp/input belonging to another package). The name wins.
    (tmp_path / "input").mkdir()
    cands = [("input", tmp_path / "A"), ("other", tmp_path / "input")]
    monkeypatch.chdir(tmp_path)
    assert install.resolve_repo_selectors(["input"], cands) == {"input"}


def test_user_expansion(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "input").mkdir(parents=True)
    cands = [("climber-x-input", home / "input")]
    monkeypatch.setenv("HOME", str(home))
    assert install.resolve_repo_selectors(["~/input"], cands) == {"climber-x-input"}


def test_unknown_name_or_path_raises_with_available(tmp_path):
    cands = [("yelmo", tmp_path / "yelmo")]
    with pytest.raises(install.InstallError) as e:
        install.resolve_repo_selectors(["nope"], cands)
    assert "yelmo" in str(e.value)          # lists the available names
    assert "nope" in str(e.value)


def test_mixed_name_and_path_entries(tmp_path):
    (tmp_path / "input").mkdir()
    cands = [("climber-x-input", tmp_path / "input"), ("yelmo", tmp_path / "yelmo")]
    got = install.resolve_repo_selectors(
        ["yelmo", str(tmp_path / "input")], cands)
    assert got == {"yelmo", "climber-x-input"}
