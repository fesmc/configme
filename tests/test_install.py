"""Tests for install-side behavior that `configme status` does not cover:
the artifact-presence probe used to skip already-built packages, and the
idempotent `data_link` extra (an existing link is kept, not re-prompted).
"""

import sys
from pathlib import Path

from configme import cli, context, data, extras, install


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


# --------------------------------------------------- _stage_build_machine_file
#
# configme carries a user-authored fesm-utils machine TOML into the checkout's
# machines/ so build.py's `-m <name>` resolves a configme-defined machine. The
# user authors it in the user tier (~/.configme/machines/, via CONFIGME_HOME).


def _author_user_tier_machine(monkeypatch, tmp_path, name, body="x=1\n"):
    home = tmp_path / "home"
    monkeypatch.setenv("CONFIGME_HOME", str(home))
    src = home / "machines" / f"{name}.toml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body)
    return src


def _checkout(tmp_path):
    dest = tmp_path / "fesm-utils"
    (dest / "machines").mkdir(parents=True)
    return dest


def test_stage_copies_when_target_absent(monkeypatch, tmp_path):
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook", "machine='chinook'\n")
    dest = _checkout(tmp_path)
    install._stage_build_machine_file(dest, "chinook", None, False, None)
    assert (dest / "machines" / "chinook.toml").read_text() == "machine='chinook'\n"


def test_stage_noop_when_no_authored_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "empty-home"))
    dest = _checkout(tmp_path)
    install._stage_build_machine_file(dest, "chinook", None, False, None)
    # build.py falls back to its own bundled machines/ — configme writes nothing.
    assert not (dest / "machines" / "chinook.toml").exists()


def test_stage_dry_run_does_not_write(monkeypatch, tmp_path):
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook")
    dest = _checkout(tmp_path)
    install._stage_build_machine_file(dest, "chinook", None, True, None)
    assert not (dest / "machines" / "chinook.toml").exists()


def test_stage_identical_target_is_noop(monkeypatch, tmp_path):
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook", "same\n")
    dest = _checkout(tmp_path)
    target = dest / "machines" / "chinook.toml"
    target.write_text("same\n")
    # A confirm_fn that fails the test if called — identical content must not prompt.
    def _no_prompt(_q, _d):
        raise AssertionError("must not prompt when target is identical")
    install._stage_build_machine_file(dest, "chinook", None, False, _no_prompt)
    assert target.read_text() == "same\n"


def test_stage_differing_target_prompts_and_keeps_on_no(monkeypatch, tmp_path):
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook", "new\n")
    dest = _checkout(tmp_path)
    target = dest / "machines" / "chinook.toml"
    target.write_text("committed\n")
    install._stage_build_machine_file(dest, "chinook", None, False,
                                      lambda _q, _d: False)
    assert target.read_text() == "committed\n"  # checkout's copy preserved


def test_stage_differing_target_overwrites_on_yes(monkeypatch, tmp_path):
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook", "new\n")
    dest = _checkout(tmp_path)
    target = dest / "machines" / "chinook.toml"
    target.write_text("committed\n")
    install._stage_build_machine_file(dest, "chinook", None, False,
                                      lambda _q, _d: True)
    assert target.read_text() == "new\n"


def test_stage_differing_target_keeps_when_noninteractive(monkeypatch, tmp_path):
    # confirm_fn is None (non-interactive): never clobber an existing checkout file.
    _author_user_tier_machine(monkeypatch, tmp_path, "chinook", "new\n")
    dest = _checkout(tmp_path)
    target = dest / "machines" / "chinook.toml"
    target.write_text("committed\n")
    install._stage_build_machine_file(dest, "chinook", None, False, None)
    assert target.read_text() == "committed\n"


# ------------------------- new machine scaffolds the fesm-utils build.py TOML


def test_new_machine_scaffolds_build_toml(monkeypatch, tmp_path):
    # `configme new machine` (project=None: user tier only) drops both the .mk
    # fragment and a fesm-utils build.py .toml stub, with the placeholder name
    # substituted, ready to feed _stage_build_machine_file.
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "home"))
    written = context.create_fragment("machine", "chinook", src="linux", project=None)
    mk = tmp_path / "home" / "machines" / "chinook.mk"
    toml = tmp_path / "home" / "machines" / "chinook.toml"
    assert mk in written and toml in written
    body = toml.read_text()
    assert 'name = "chinook"' in body        # placeholder substituted
    assert "my_cluster" not in body
    assert "[compilers.gfortran]" in body    # seeded from the bundled template


def test_new_compiler_does_not_scaffold_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIGME_HOME", str(tmp_path / "home"))
    written = context.create_fragment("compiler", "myc", src="gfortran", project=None)
    assert written and all(p.suffix == ".mk" for p in written)


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


# ---------------------------------------------------------------------- root_for

def test_root_for_uses_subdir_when_cwd_manifest_names_other_project(tmp_path):
    # Running `configme install fesm-utils` from inside an existing yelmox
    # checkout must install fesm-utils as `yelmox/fesm-utils/`, not treat the
    # yelmox root itself as the fesm-utils checkout. A bare `.configme/` on
    # cwd is not enough to claim cwd is the primary — the manifest's `package`
    # must match.
    (tmp_path / ".configme").mkdir()
    (tmp_path / ".configme" / "manifest.toml").write_text('package = "yelmox"\n')
    plan = install.build_plan("fesm-utils")
    root, present = install.root_for(plan, None, tmp_path)
    assert root == (tmp_path / "fesm-utils").resolve()
    assert present is False


def test_root_for_uses_cwd_when_manifest_names_primary(tmp_path):
    # Inverse of the above: cwd IS the fesm-utils checkout (manifest says so),
    # so root_for must return cwd itself rather than `cwd/fesm-utils`.
    (tmp_path / ".configme").mkdir()
    (tmp_path / ".configme" / "manifest.toml").write_text('package = "fesm-utils"\n')
    plan = install.build_plan("fesm-utils")
    root, _ = install.root_for(plan, None, tmp_path)
    assert root == tmp_path


# ------------------------------------------------- nested-dependency placement

def _order(plan):
    return [n.name for n in plan.nodes]


def _dest_under(tgt, name, root=Path("/r")):
    plan = install.build_plan(tgt)
    node = next(n for n in plan.nodes if n.name == name)
    return plan, install.dest_of(node, plan, root)


def test_nest_link_places_dependency_inside_its_consumer_in_every_orchestrator():
    # yelmo's `nest = true` link makes FastHydrology clone inside yelmo's
    # checkout (yelmo/FastHydrology), and it must hold in EVERY orchestrator that
    # pulls yelmo in — not just the one that happens to list FastHydrology.
    for orch in ("yelmox", "climber-x"):
        plan, fh = _dest_under(orch, "FastHydrology")
        yelmo = next(n for n in plan.nodes if n.name == "yelmo")
        assert fh == install.dest_of(yelmo, plan, Path("/r")) / "FastHydrology"
        # ...and the dep must be ordered AFTER its container, else cloning it
        # first creates a non-empty yelmo/ that blocks yelmo's own clone.
        order = _order(plan)
        assert order.index("yelmo") < order.index("FastHydrology")


def test_nest_link_standalone_install_clones_dependency_at_root():
    # `configme install FastHydrology` on its own: it is the primary (no yelmo to
    # nest under), so it clones at the install root, not under a phantom yelmo/.
    plan, fh = _dest_under("FastHydrology", "FastHydrology")
    assert plan.primary.name == "FastHydrology"
    assert fh == Path("/r")


def test_nest_link_resolves_via_orchestrator_when_consumer_absent_from_plan():
    # Installing only the nested dependency into an existing orchestrator
    # (the `+`-list the "install as a component" prompt replans to): yelmo is not
    # a node, but FastHydrology must still land under it via the orchestrator.
    _, fh = _dest_under("yelmox+FastHydrology", "FastHydrology")
    assert fh == Path("/r/yelmo/FastHydrology")


def test_nest_link_emits_no_self_referential_symlink(tmp_path, capsys):
    # A `nest = true` link clones the dependency *inside* its consumer's
    # checkout (yelmo/FastHydrology), so the link phase must NOT also emit a
    # symlink for it — `dest_of` resolves the dep to the link path itself, so a
    # link would be the pointless self-loop `ln -s FastHydrology yelmo/FastHydrology`.
    rc = install.run_install(
        "yelmox", download="ssh", install_dir=str(tmp_path),
        machine="macbook", compiler="gfortran",
        overwrite=False, build_deps=False, dry_run=True, only=False,
        link_args=None, select_fn=None, ask_fn=None, confirm_fn=lambda q, d: d,
    )
    assert rc == 0
    script = capsys.readouterr().out
    # The pointless self-link must be absent...
    assert f"ln -s FastHydrology {tmp_path}/yelmo/FastHydrology" not in script
    # ...while FastHydrology's own (legitimate) fesm-utils link is still emitted.
    assert f"{tmp_path}/yelmo/FastHydrology/fesm-utils" in script


def test_general_nesting_invariant_no_node_precedes_its_container():
    # The reorder is a general rule: for every orchestrator, a node nested inside
    # another node's checkout must be ordered after it.
    root = Path("/r")
    for orch in data.orchestrators():
        plan = install.build_plan(orch)
        order = _order(plan)
        dests = {n.name: install.dest_of(n, plan, root) for n in plan.nodes}
        for inner in plan.nodes:
            for outer in plan.nodes:
                if outer is inner or dests[outer.name] == root:
                    continue
                if dests[outer.name] in dests[inner.name].parents:
                    assert order.index(outer.name) < order.index(inner.name), (
                        f"{orch}: {inner.name} (in {outer.name}) ordered before it")


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
    rc = cli._install_pip_tool("runme", None, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert called == []                          # dry-run: no subprocess
    assert "DRY RUN" in out
    expected = (f"{sys.executable} -m pip install -U "
                "git+https://github.com/fesmc/runme")
    assert expected in out


def test_install_runme_pinned_ref_builds_at_ref_url(capsys, monkeypatch):
    """`configme install runme:v0.3.1 --dry-run` pins the git URL with `@ref`."""
    monkeypatch.setattr("configme.cli.subprocess.run",
                        lambda *a, **kw: None)
    # Not installed at that version → no skip, proceeds to (dry) install.
    monkeypatch.setattr("configme.extras.pip_tool_satisfied",
                        lambda name, ref: False)
    rc = cli._install_pip_tool("runme", "v0.3.1", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("git+https://github.com/fesmc/runme@v0.3.1" in out)


def test_install_runme_pinned_already_installed_skips(capsys, monkeypatch):
    """A pinned version already installed is a no-op — no pip shellout."""
    called = []
    monkeypatch.setattr("configme.cli.subprocess.run",
                        lambda *a, **kw: called.append(a) or None)
    monkeypatch.setattr("configme.extras.pip_tool_satisfied",
                        lambda name, ref: True)
    rc = cli._install_pip_tool("runme", "v0.3.1", dry_run=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert called == []                          # satisfied: nothing run
    assert "already installed" in out


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


# ----------------------------------------------- _host_orchestrator_for


def _write_manifest(root: Path, package: str) -> None:
    (root / ".configme").mkdir(parents=True, exist_ok=True)
    (root / ".configme" / "manifest.toml").write_text(f'package = "{package}"\n')


def test_host_orchestrator_for_returns_orch_when_target_is_component(tmp_path):
    _write_manifest(tmp_path, "yelmox")
    host = install._host_orchestrator_for("FastHydrology", tmp_path)
    assert host is not None and host.name == "yelmox"


def test_host_orchestrator_for_returns_orch_for_optional_component(tmp_path):
    # climber-x's bgc/vilma are optional_packages — still components of the
    # orchestrator, so the prompt path should still trigger.
    _write_manifest(tmp_path, "climber-x")
    host = install._host_orchestrator_for("vilma", tmp_path)
    assert host is not None and host.name == "climber-x"


def test_host_orchestrator_for_none_when_no_manifest(tmp_path):
    assert install._host_orchestrator_for("FastHydrology", tmp_path) is None


def test_host_orchestrator_for_none_when_manifest_names_package(tmp_path):
    _write_manifest(tmp_path, "fesm-utils")
    assert install._host_orchestrator_for("FastHydrology", tmp_path) is None


def test_host_orchestrator_for_none_when_target_is_orch_itself(tmp_path):
    # Inside yelmox, `configme install yelmox` has nothing to graft — return
    # None so the existing "primary already present" path handles it.
    _write_manifest(tmp_path, "yelmox")
    assert install._host_orchestrator_for("yelmox", tmp_path) is None


def test_host_orchestrator_for_none_when_target_unrelated(tmp_path):
    # FastHydrology is not a climber-x component, so being inside climber-x
    # does not trigger the prompt.
    _write_manifest(tmp_path, "climber-x")
    assert install._host_orchestrator_for("FastHydrology", tmp_path) is None


def test_host_orchestrator_for_none_on_corrupt_manifest(tmp_path):
    # A manifest with no `package` key raises ProjectError in
    # _manifest_primary; the helper swallows it so the prompt simply does not
    # fire (the surrounding install proceeds with the default plan).
    (tmp_path / ".configme").mkdir()
    (tmp_path / ".configme" / "manifest.toml").write_text("deps = []\n")
    assert install._host_orchestrator_for("FastHydrology", tmp_path) is None


# ------------------------------------------ run_install: orchestrator prompt


def _stop_at_root_for(monkeypatch):
    """Halt run_install right after the (re)plan, before clone / link / build.
    Used to inspect what target build_plan was called with after the prompt."""

    class _Bail(Exception):
        pass

    def stop(*a, **kw):
        raise _Bail

    monkeypatch.setattr(install, "root_for", stop)
    return _Bail


def _spy_build_plan(monkeypatch, calls):
    real = install.build_plan

    def spy(target, *, only=False):
        calls.append(target)
        return real(target, only=only)

    monkeypatch.setattr(install, "build_plan", spy)


def test_run_install_prompts_inside_orchestrator_and_replans(tmp_path, monkeypatch):
    # Inside yelmox, `configme install FastHydrology` offers to install
    # FastHydrology as a yelmox component (sharing existing deps) instead of
    # side-by-side. When the user accepts, the plan is rebuilt as
    # "yelmox+FastHydrology" before the rest of the install runs.
    _write_manifest(tmp_path, "yelmox")
    monkeypatch.chdir(tmp_path)
    Bail = _stop_at_root_for(monkeypatch)
    calls: list = []
    _spy_build_plan(monkeypatch, calls)
    asked: list = []

    def confirm(question, default):
        asked.append(question)
        return True

    try:
        install.run_install(
            "FastHydrology", download="ssh", install_dir=None,
            machine="macbook", compiler="gfortran",
            overwrite=False, build_deps=False, dry_run=True, only=False,
            link_args=None, select_fn=None, ask_fn=None, confirm_fn=confirm,
        )
    except Bail:
        pass

    assert calls == ["FastHydrology", "yelmox+FastHydrology"]
    assert asked and "yelmox" in asked[0] and "FastHydrology" in asked[0]


def test_run_install_keeps_standalone_when_user_declines(tmp_path, monkeypatch):
    # Same setup as above but the user declines the prompt — the plan stays on
    # FastHydrology-as-primary so the standalone install path runs.
    _write_manifest(tmp_path, "yelmox")
    monkeypatch.chdir(tmp_path)
    Bail = _stop_at_root_for(monkeypatch)
    calls: list = []
    _spy_build_plan(monkeypatch, calls)

    try:
        install.run_install(
            "FastHydrology", download="ssh", install_dir=None,
            machine="macbook", compiler="gfortran",
            overwrite=False, build_deps=False, dry_run=True, only=False,
            link_args=None, select_fn=None, ask_fn=None,
            confirm_fn=lambda q, d: False,
        )
    except Bail:
        pass

    assert calls == ["FastHydrology"]


def test_run_install_skips_prompt_with_only_flag(tmp_path, monkeypatch):
    # --only is the user being explicit about scope; the prompt must not fire.
    _write_manifest(tmp_path, "yelmox")
    monkeypatch.chdir(tmp_path)
    Bail = _stop_at_root_for(monkeypatch)
    asked: list = []

    try:
        install.run_install(
            "FastHydrology", download="ssh", install_dir=None,
            machine="macbook", compiler="gfortran",
            overwrite=False, build_deps=False, dry_run=True, only=True,
            link_args=None, select_fn=None, ask_fn=None,
            confirm_fn=lambda q, d: asked.append(q) or True,
        )
    except Bail:
        pass

    assert asked == []


def test_run_install_skips_prompt_with_install_dir(tmp_path, monkeypatch):
    # --dir is the user being explicit about location; do not graft.
    _write_manifest(tmp_path, "yelmox")
    monkeypatch.chdir(tmp_path)
    Bail = _stop_at_root_for(monkeypatch)
    asked: list = []

    try:
        install.run_install(
            "FastHydrology", download="ssh",
            install_dir=str(tmp_path / "somewhere-else"),
            machine="macbook", compiler="gfortran",
            overwrite=False, build_deps=False, dry_run=True, only=False,
            link_args=None, select_fn=None, ask_fn=None,
            confirm_fn=lambda q, d: asked.append(q) or True,
        )
    except Bail:
        pass

    assert asked == []
