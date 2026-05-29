"""Tests for install-side behavior that `configme status` does not cover:
the artifact-presence probe used to skip already-built packages, and the
idempotent `data_link` extra (an existing link is kept, not re-prompted).
"""

from pathlib import Path

from configme import data, extras, install


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
