"""Makefile generation: assemble fragments into a package's Makefile.

The generated Makefile = the package's ``config/Makefile`` template with its
``<COMPILER_CONFIGURATION>`` placeholder replaced by, in order:

    compiler.mk   (central, shipped by configme)
    machine.mk    (central, shipped by configme)
    netCDF block  (auto-detected, unless the machine fragment overrides it)

The package's ``common.mk`` (repo-owned dependency wiring) is **not** inlined
here: the template itself carries an explicit ``include config/common.mk``
after the placeholder, so the include is visible to anyone reading the
template by hand. configme only ensures ``common.mk`` exists in the package's
``config/`` (copying a shipped overlay when needed — see ``ensure_common``).

Because the include is the template's responsibility, ``generate_makefile``
fails loudly when a ``common.mk`` is present but the template forgot to include
it — otherwise the generated Makefile would silently drop the dependency wiring.

See docs/DESIGN.md sec. 2 and sec. 5. The three-tier fragment lookup
(orchestrator/user/shipped) and the .configme manifest come in later slices;
this module currently resolves fragments from the shipped registry and reads
the template from the repo (.configme/ first, then config/).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from configme import data, netcdf

PLACEHOLDER = "<COMPILER_CONFIGURATION>"

# configme-shipped common.mk overlays for packages whose common.mk is not (yet)
# committed in their own repo. Dropped into the package's config/ at configure
# time so the package can be configured without modifying its upstream.
OVERLAYS_DIR = data.DATA_DIR / "overlays"

# A machine fragment "provides netCDF" (and thus overrides auto-detection) if it
# assigns any of these variables.
_NETCDF_ASSIGN = re.compile(r"^\s*(INC_NC|LIB_NC|NC_FROOT|NC_CROOT)\s*=", re.M)

# A template "pulls in common.mk" if it has a make `include` (or `-include`)
# directive referencing a *common.mk path. Used to fail loudly when a package
# ships a common.mk but its template forgot to include it (see generate_makefile).
_INCLUDES_COMMON = re.compile(r"^\s*-?include\b[^\n]*\bcommon\.mk\b", re.M)


class GenerateError(Exception):
    """A Makefile could not be generated (missing fragment, template, etc.)."""


def find_repo_file(cfgroot: Path, relname: str) -> Optional[Path]:
    """Locate a repo config file, preferring .configme/ over config/."""
    for base in (cfgroot / ".configme", cfgroot / "config"):
        p = base / relname
        if p.is_file():
            return p
    return None


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically (temp file in the same dir + rename), so an
    interrupted run never leaves a corrupt or half-written Makefile."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".configme-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _netcdf_block() -> str:
    info = netcdf.detect()
    return (
        f"# netCDF — auto-detected by configme (source: {info.source}).\n"
        f"NC_FROOT = {info.nc_froot or ''}\n"
        f"NC_CROOT = {info.nc_croot or ''}\n"
        f"INC_NC = {info.inc_nc}\n"
        f"LIB_NC = {info.lib_nc}"
    )


def generate_makefile(root: Path, machine: str, compiler: str,
                      machine_path: Path, compiler_path: Path,
                      config_subdir: str = "") -> Path:
    """Generate <root>/<config_subdir>/Makefile and return its path.

    The caller resolves the machine/compiler fragment paths (three-tier lookup
    lives in context.py). Raises GenerateError on any missing input, NetcdfError
    if netCDF cannot be resolved and the machine fragment does not override it.
    """
    root = Path(root)
    cfgroot = root / config_subdir if config_subdir else root
    if not cfgroot.is_dir():
        raise GenerateError(f"directory not found: {cfgroot}")

    template_path = find_repo_file(cfgroot, "Makefile")
    if template_path is None:
        raise GenerateError(
            f"no Makefile template found in {cfgroot}/.configme/ or {cfgroot}/config/"
        )
    template = template_path.read_text()
    if PLACEHOLDER not in template:
        raise GenerateError(
            f"template {template_path} has no {PLACEHOLDER} placeholder"
        )

    # Failsafe: a package's dependency wiring lives in its common.mk, which
    # configme does NOT inline — the template must pull it in with its own
    # `include config/common.mk` (see module docstring). If a common.mk exists
    # but the template forgot the include (a half-finished migration), the
    # generated Makefile would silently drop all dependency wiring (FESMUTILS,
    # LIS, …) and the build would fail to find its dependencies. Fail loudly,
    # with the fix, rather than emit a quietly-broken Makefile.
    if has_common(cfgroot) and not _INCLUDES_COMMON.search(template):
        raise GenerateError(
            f"{template_path} ships a common.mk but does not `include "
            f"config/common.mk` — the dependency wiring it defines (FESMUTILS, "
            f"LIS, …) would be dropped from the generated Makefile. Add "
            f"`include config/common.mk` right after {PLACEHOLDER} in the "
            f"template (mirror the package's main-branch Makefile)."
        )

    compiler_mk = Path(compiler_path).read_text()
    machine_mk = Path(machine_path).read_text()

    header = (
        f"## Generated by configme — machine={machine}, compiler={compiler}.\n"
        f"## Do not edit this block by hand; rerun `configme` to regenerate."
    )
    parts = [header, compiler_mk, machine_mk]

    # netCDF: auto-detect and inject, unless the machine fragment overrides it.
    if not _NETCDF_ASSIGN.search(machine_mk):
        parts.append(_netcdf_block())

    # common.mk is pulled in by the template's own `include config/common.mk`
    # line (committed to the package's config/Makefile), not inlined here.

    compile_info = "\n\n".join(p.strip("\n") for p in parts)
    makefile = template.replace(PLACEHOLDER, compile_info)

    out_path = cfgroot / "Makefile"
    _atomic_write(out_path, makefile)
    return out_path


def has_common(cfgroot: Path) -> bool:
    return find_repo_file(cfgroot, "common.mk") is not None


def overlay_common(pkg_name: str) -> Optional[Path]:
    """A configme-shipped common.mk overlay for pkg_name, if one exists."""
    p = OVERLAYS_DIR / pkg_name / "common.mk"
    return p if p.is_file() else None


def ensure_common(pkg_name: str, cfgroot: Path) -> Optional[Path]:
    """If the package has no common.mk but configme ships an overlay for it,
    copy the overlay into <cfgroot>/config/common.mk. Returns the destination if
    copied, else None. Lets configme configure a package whose common.mk is not
    yet committed upstream (e.g. FastIsostasy)."""
    if has_common(cfgroot):
        return None
    overlay = overlay_common(pkg_name)
    if overlay is None:
        return None
    dest_dir = cfgroot / "config" if (cfgroot / "config").is_dir() else cfgroot / ".configme"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "common.mk"
    shutil.copyfile(overlay, dest)
    return dest


def legacy_flat_config(cfgroot: Path, machine: str, compiler: str) -> Optional[Path]:
    """Locate a repo's legacy monolithic flat config `<machine>_<compiler>`."""
    return find_repo_file(cfgroot, f"{machine}_{compiler}")


def legacy_makefile(root: Path, machine: str, compiler: str,
                    config_subdir: str = "") -> Path:
    """Labelled stopgap for not-yet-migrated repos (see docs/DESIGN.md sec. 7).

    Use the repo's existing flat `config/<machine>_<compiler>` file as the whole
    <COMPILER_CONFIGURATION> block, then append the auto-detected netCDF so it
    overrides whatever (often env-based) INC_NC/LIB_NC the flat file set. The
    repo gets no centralisation until properly migrated to a common.mk.
    """
    root = Path(root)
    cfgroot = root / config_subdir if config_subdir else root
    template_path = find_repo_file(cfgroot, "Makefile")
    if template_path is None:
        raise GenerateError(f"no Makefile template in {cfgroot}")
    flat = legacy_flat_config(cfgroot, machine, compiler)
    if flat is None:
        raise GenerateError(
            f"no legacy flat config '{machine}_{compiler}' in {cfgroot}/config/")
    template = template_path.read_text()
    if PLACEHOLDER not in template:
        raise GenerateError(f"template {template_path} has no {PLACEHOLDER}")
    header = (f"## Generated by configme (LEGACY fallback) from "
              f"config/{machine}_{compiler}. Migrate this repo to a common.mk.")
    # netCDF appended last so the detected values override the flat file's.
    compile_info = "\n\n".join([header, flat.read_text().strip("\n"),
                                _netcdf_block()])
    makefile = template.replace(PLACEHOLDER, compile_info)
    out_path = cfgroot / "Makefile"
    _atomic_write(out_path, makefile)
    return out_path
