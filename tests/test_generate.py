"""Makefile generation failsafe: a package that ships a common.mk but whose
template forgot to `include config/common.mk` must fail loudly, not emit a
Makefile with its dependency wiring silently dropped (the yelmo climber-x-branch
bug). See generate.generate_makefile.
"""

import pytest

from configme import generate


def _setup(tmp_path, template_body, *, with_common):
    """Lay out a minimal repo: config/Makefile (template), a netCDF-providing
    machine fragment (so generation never reaches netcdf.detect), a compiler
    fragment, and optionally a config/common.mk. Returns generate kwargs."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "Makefile").write_text(template_body)
    if with_common:
        (cfg / "common.mk").write_text("# Shared build configuration (wiring).\n")
    machine = tmp_path / "machine.mk"
    machine.write_text("DFLAGS_NODEBUG = -O2\nINC_NC = -I/nc\nLIB_NC = -L/nc\n")
    compiler = tmp_path / "compiler.mk"
    compiler.write_text("FC = gfortran\n")
    return dict(root=tmp_path, machine="m", compiler="c",
                machine_path=machine, compiler_path=compiler)


def test_raises_when_common_present_but_not_included(tmp_path):
    kw = _setup(tmp_path, "FC=x\n<COMPILER_CONFIGURATION>\n\nall:\n\techo hi\n",
                with_common=True)
    with pytest.raises(generate.GenerateError, match="would be dropped"):
        generate.generate_makefile(**kw)
    assert not (tmp_path / "Makefile").exists()  # nothing emitted on failure


def test_ok_when_common_present_and_included(tmp_path):
    kw = _setup(
        tmp_path,
        "FC=x\n<COMPILER_CONFIGURATION>\n\ninclude config/common.mk\n\nall:\n\techo hi\n",
        with_common=True)
    out = generate.generate_makefile(**kw)
    text = out.read_text()
    assert "include config/common.mk" in text          # preserved
    assert "<COMPILER_CONFIGURATION>" not in text       # placeholder filled


def test_ok_when_no_common_and_no_include(tmp_path):
    # A package/orchestrator without a common.mk needs no include — must not trip.
    kw = _setup(tmp_path, "FC=x\n<COMPILER_CONFIGURATION>\n\nall:\n\techo hi\n",
                with_common=False)
    out = generate.generate_makefile(**kw)
    assert out.is_file()


def test_dash_include_is_accepted(tmp_path):
    # GNU make's `-include` (ignore-if-missing) also counts as pulling it in.
    kw = _setup(
        tmp_path,
        "FC=x\n<COMPILER_CONFIGURATION>\n\n-include config/common.mk\n",
        with_common=True)
    assert generate.generate_makefile(**kw).is_file()
