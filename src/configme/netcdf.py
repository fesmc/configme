"""netCDF discovery (see docs/DESIGN.md sec. 5).

The build needs three pieces of netCDF information:

    NC_FROOT   netcdf-fortran install prefix
    NC_CROOT   netcdf-c install prefix
    INC_NC     include flags  (-I...)
    LIB_NC     link flags     (-L... -lnetcdff -lnetcdf ...)

These are resolved fresh on every call, with this precedence (the
machine-fragment override is layered on top by the Makefile generator, not
here):

    1. nf-config / nc-config detection
    2. NC_FROOT / NC_CROOT from the environment
    3. NC_FROOT / NC_CROOT parsed from ~/.bashrc / ~/.zshrc

If none yields a usable result, raise NetcdfError with an actionable message.

Note: ``nc-config --static`` is intentionally NOT used. On some installs it
emits CMake target names (e.g. ``-lHDF5::HDF5``, ``-lCURL::libcurl``) that are
not valid linker flags, so it cannot be trusted as a source of link flags.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class NetcdfError(Exception):
    """No usable netCDF configuration could be resolved."""


@dataclass
class NetcdfInfo:
    nc_froot: Optional[str]  # netcdf-fortran prefix
    nc_croot: Optional[str]  # netcdf-c prefix
    inc_nc: str              # include flags for the Makefile
    lib_nc: str              # link flags for the Makefile
    source: str              # human-readable description of where this came from


# ------------------------------------------------------------------ helpers

def _run(tool: str, *args: str) -> Optional[str]:
    """Run `tool args...`, returning stripped stdout, or None on any failure."""
    if shutil.which(tool) is None:
        return None
    try:
        out = subprocess.run(
            [tool, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.stdout.strip()


def _dedupe(tokens: List[str]) -> List[str]:
    """Drop duplicate tokens, preserving first-seen order."""
    seen = set()
    out = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _add_rpath(tokens: List[str]) -> List[str]:
    """Append a runtime search path (-Wl,-rpath,<dir>) for every -L<dir> link
    directory, so the resulting binary finds the shared netCDF libraries at
    runtime without relying on LD_LIBRARY_PATH. Directories are deduped and the
    rpath flags are placed after the existing link tokens."""
    libdirs = _dedupe([t[2:] for t in tokens if t.startswith("-L") and len(t) > 2])
    return tokens + [f"-Wl,-rpath,{d}" for d in libdirs]


def _include_flags(*flag_strings: Optional[str]) -> List[str]:
    """Extract only the -I include tokens from one or more compiler-flag strings
    (so optimisation/warning flags like -O2/-g do not leak into INC_NC)."""
    out: List[str] = []
    for s in flag_strings:
        if not s:
            continue
        for tok in s.split():
            if tok.startswith("-I"):
                out.append(tok)
    return out


# ------------------------------------------------------------------ detection

def _detect_from_config_tools() -> Optional[NetcdfInfo]:
    """Resolve netCDF from nf-config / nc-config, or None if unavailable."""
    nf_prefix = _run("nf-config", "--prefix")
    nc_prefix = _run("nc-config", "--prefix")
    nf_fflags = _run("nf-config", "--fflags")
    nf_flibs = _run("nf-config", "--flibs")
    nc_cflags = _run("nc-config", "--cflags")
    nc_libs = _run("nc-config", "--libs")

    # We need at least the fortran link line to build anything usable.
    if not nf_flibs and not (nf_prefix or nc_prefix):
        return None

    inc = _dedupe(_include_flags(nf_fflags, nc_cflags))
    # Fall back to <prefix>/include if the tools gave no include flags.
    if not inc:
        for prefix in (nf_prefix, nc_prefix):
            if prefix:
                inc.append(f"-I{prefix}/include")
        inc = _dedupe(inc)

    libs: List[str] = []
    if nf_flibs:
        libs.extend(nf_flibs.split())
    if nc_libs:
        libs.extend(nc_libs.split())
    libs = _dedupe(libs)
    if not libs:
        # Last-resort synthesis from prefixes.
        if nf_prefix:
            libs += [f"-L{nf_prefix}/lib", "-lnetcdff"]
        if nc_prefix:
            libs += [f"-L{nc_prefix}/lib", "-lnetcdf"]
        libs = _dedupe(libs)
    if not libs:
        return None
    libs = _add_rpath(libs)

    return NetcdfInfo(
        nc_froot=nf_prefix,
        nc_croot=nc_prefix,
        inc_nc=" ".join(inc),
        lib_nc=" ".join(libs),
        source="nf-config/nc-config",
    )


def _info_from_roots(froot: Optional[str], croot: Optional[str],
                     source: str) -> Optional[NetcdfInfo]:
    """Build a NetcdfInfo from explicit roots, using the conventional template
    form (matches the legacy ${NC_FROOT}/${NC_CROOT} fragments)."""
    if not froot and not croot:
        return None
    # A single root often serves for both fortran and C (e.g. a combined build).
    froot = froot or croot
    croot = croot or froot
    inc = _dedupe([f"-I{froot}/include", f"-I{croot}/include"])
    lib = _add_rpath(
        _dedupe([f"-L{froot}/lib", "-lnetcdff", f"-L{croot}/lib", "-lnetcdf"])
    )
    return NetcdfInfo(
        nc_froot=froot,
        nc_croot=croot,
        inc_nc=" ".join(inc),
        lib_nc=" ".join(lib),
        source=source,
    )


def _detect_from_env() -> Optional[NetcdfInfo]:
    return _info_from_roots(
        os.environ.get("NC_FROOT"),
        os.environ.get("NC_CROOT"),
        source="environment (NC_FROOT/NC_CROOT)",
    )


def _parse_shell_var(text: str, name: str) -> Optional[str]:
    """Find the last `NAME=value` (optionally `export NAME=value`) assignment in
    a shell rc file, returning the unquoted value."""
    value = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() != name:
            continue
        val = val.split("#", 1)[0].strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        value = os.path.expandvars(os.path.expanduser(val))
    return value


def _detect_from_rc() -> Optional[NetcdfInfo]:
    home = Path.home()
    for rc in (home / ".zshrc", home / ".bashrc", home / ".bash_profile"):
        if not rc.is_file():
            continue
        try:
            text = rc.read_text()
        except OSError:
            continue
        froot = _parse_shell_var(text, "NC_FROOT")
        croot = _parse_shell_var(text, "NC_CROOT")
        info = _info_from_roots(froot, croot, source=f"{rc.name} (NC_FROOT/NC_CROOT)")
        if info is not None:
            return info
    return None


def detect() -> NetcdfInfo:
    """Resolve netCDF, applying the detection -> env -> rc precedence.

    Raises NetcdfError with an actionable message if nothing is found.
    """
    for resolver in (_detect_from_config_tools, _detect_from_env, _detect_from_rc):
        info = resolver()
        if info is not None:
            return info
    raise NetcdfError(
        "could not determine netCDF location.\n"
        "  Tried: nf-config/nc-config, $NC_FROOT/$NC_CROOT, and ~/.zshrc/~/.bashrc.\n"
        "  Fix one of:\n"
        "    - load your netCDF module so `nf-config`/`nc-config` are on PATH, or\n"
        "    - export NC_FROOT (netcdf-fortran prefix) and NC_CROOT (netcdf-c prefix)."
    )
