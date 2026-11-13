"""Fortran module dependency generation (fortdeps.py, `configme deps`): a small
synthetic source tree covering modules, submodules, `use ..., only:`, intrinsic
and external modules, comments/strings, continuations, includes, a program, and
a backend/stub pair defining the same module.
"""

import pytest

from configme import cli, data, fortdeps, install

SOURCES = {
    "src/core/base.f90": """\
module base
    use, intrinsic :: iso_fortran_env, only: real64
    use precision, only: dp          ! external library (fesm-utils)
    implicit none
    character(len=*), parameter :: msg = "use fake_in_string; module nope"
    include 'base_params.inc'
contains
    subroutine say()
        print *, 'use also_fake'   ! use not_a_dep
    end subroutine
end module base
""",
    "src/core/base_params.inc": "integer, parameter :: n = 3\n",
    "src/core/comp.F90": """\
MODULE Comp
    USE base, ONLY: &
        say
    implicit none
    interface
        module subroutine comp_make()
        end subroutine
        module procedure say
    end interface
end module
""",
    "src/core/comp_impl.f90": """\
submodule (comp) comp_impl
    use be_x
contains
    module subroutine comp_make()
    end subroutine
end submodule comp_impl
""",
    "src/backends/be_x.f90": "module be_x\n  use comp\n  use yelmo\nend module be_x\n",
    "src/backends/be_x_stub.f90": "module be_x\n  use comp\nend module be_x\n",
    "src/core/twomods.f90": "module m1\nend module\nmodule m2\n  use m1; use base\nend module\n",
    "src/legacy/old.f": """\
c     a fixed-form comment: use nothing
      subroutine old()
      use base
      end
""",
    "src/prog.f90": "program p\n  use m2\nend program p\n",
}


@pytest.fixture
def tree(tmp_path):
    for rel, text in SOURCES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_scan_ignores_comments_strings_and_procedure_statements(tree):
    u = fortdeps.scan_file(tree / "src/core/base.f90")
    assert u.modules == {"base"}
    assert u.uses == {"precision"}
    assert u.intrinsic_uses == {"iso_fortran_env"}
    assert u.includes == ["base_params.inc"]
    c = fortdeps.scan_file(tree / "src/core/comp.F90")
    assert c.modules == {"comp"}                 # not `subroutine`/`procedure`
    assert c.uses == {"base"}                    # continued `use ..., only: &`


def test_rules(tree):
    deps = fortdeps.resolve(tree, ["src"])
    r = deps.rules
    od = "$(objdir)"
    assert r["base.o"] == ["src/core/base_params.inc"]
    assert r["comp.o"] == [f"{od}/base.o"]
    assert r["comp_impl.o"] == ["$(be_x_obj)", f"{od}/comp.o"]   # parent + alternative
    assert r["be_x.o"] == [f"{od}/comp.o"] and r["be_x_stub.o"] == [f"{od}/comp.o"]
    assert r["twomods.o"] == [f"{od}/base.o"]    # m1 is in the same file: no self-dep
    assert r["old.o"] == [f"{od}/base.o"]        # fixed form
    assert r["prog.o"] == [f"{od}/twomods.o"]
    assert deps.externals == {"precision", "yelmo"}     # intrinsics not listed
    assert deps.alternatives == {"be_x": ["be_x.o", "be_x_stub.o"]}
    # Library objects: no program, alternatives through their variable.
    assert f"{od}/prog.o" not in deps.lib_objs
    assert f"{od}/be_x.o" not in deps.lib_objs
    assert "$(be_x_obj)" in deps.lib_objs and f"{od}/comp_impl.o" in deps.lib_objs


def test_write_deps_and_cli(tree):
    out, _ = fortdeps.write_deps(tree, ["src"], objdir="obj")
    text = out.read_text()
    assert out == tree / "config/deps.mk"
    assert "obj/comp.o: obj/base.o\n" in text
    assert "ifndef be_x_obj\n$(error be_x_obj is not set" in text
    assert "vpath %.f90 src src/backends src/core src/legacy\n" in text
    assert "vpath %.F90 " in text and "vpath %.f " in text
    # The CLI writes the same rules (default objdir is the make variable).
    assert cli.main(["deps", str(tree), "--out", "deps2.mk"]) == 0
    assert "$(objdir)/comp.o: $(objdir)/base.o\n" in (tree / "deps2.mk").read_text()


def test_duplicate_object_names_fail(tree):
    (tree / "src/legacy/base.f90").write_text("module other\nend module\n")
    with pytest.raises(fortdeps.DepsError, match="same object base.o"):
        fortdeps.resolve(tree, ["src"])


def test_configure_requires_template_include(tree):
    spec = data.FortranDeps(src=["src"])
    (tree / "config").mkdir()
    (tree / "config/Makefile").write_text("<COMPILER_CONFIGURATION>\n")
    with pytest.raises(fortdeps.DepsError, match="does not `include config/deps.mk`"):
        fortdeps.configure_deps(tree, spec)
    (tree / "config/Makefile").write_text("<COMPILER_CONFIGURATION>\ninclude config/deps.mk\n")
    out, _ = fortdeps.configure_deps(tree, spec)
    assert out.is_file()


def test_configure_makefile_generates_deps(tree):
    (tree / "config").mkdir()
    (tree / "config/Makefile").write_text(
        "<COMPILER_CONFIGURATION>\ninclude config/common.mk\ninclude config/deps.mk\n")
    (tree / "config/common.mk").write_text("# wiring\n")
    machine = tree / "machine.mk"
    machine.write_text("INC_NC = -I/nc\nLIB_NC = -L/nc\n")
    compiler = tree / "compiler.mk"
    compiler.write_text("FC = gfortran\n")
    results = {"configured": [], "skipped": [], "failed": []}
    install.configure_makefile(
        label="demo", pkg_name="demo", dest=tree, config_subdir="",
        is_orchestrator=True, machine="m", compiler="c", machine_path=machine,
        compiler_path=compiler, dry_run=False, results=results,
        fortran_deps=data.FortranDeps(src=["src"]))
    assert results["configured"] == ["demo"]
    assert (tree / "Makefile").is_file() and (tree / "config/deps.mk").is_file()


def test_kryos_registered_with_fortran_deps():
    orch = data.orchestrators()["kryos"]
    assert orch.fortran_deps is not None and orch.fortran_deps.src == ["src"]
    assert orch.fortran_deps.out == "config/deps.mk"
    for dep in ("fesm-utils", "yelmo", "FastIsostasy", "chion", "insol", "vilma2", "PlasticIceSheet"):
        assert dep in orch.default_packages
    assert install.build_plan("kryos").primary.fortran_deps == orch.fortran_deps
