"""Access to configme's shipped data registry.

The registry is data, not code (see docs/DESIGN.md sec. 9): adding a supported
package, orchestrator, machine, or compiler is a file edit under data/, with no
change to the CLI logic. This module loads and validates that data.

Layout (all under ``configme/data/``)::

    packages/<name>.toml        one per supported component package
    orchestrators/<name>.toml   one per supported orchestrator (yelmox, climber-x)
    machines/<name>.mk          shipped machine fragments (CPU flags, link extras)
    compilers/<name>.mk         shipped compiler fragments (FC, FFLAGS, DFLAGS)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

try:  # tomllib is stdlib on Python >= 3.11; tomli is the backport otherwise.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        sys.exit(
            "configme: need Python >= 3.11 (for tomllib) or `pip install tomli`."
        )

# Data ships inside the installed package; resolve it relative to this module.
DATA_DIR = Path(__file__).resolve().parent / "data"
PACKAGES_DIR = DATA_DIR / "packages"
ORCHESTRATORS_DIR = DATA_DIR / "orchestrators"
MACHINES_DIR = DATA_DIR / "machines"
COMPILERS_DIR = DATA_DIR / "compilers"


class DataError(Exception):
    """Raised when a shipped (or local) data file is missing or malformed."""


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return _toml.load(f)
    except FileNotFoundError as e:
        raise DataError(f"data file not found: {path}") from e
    except _toml.TOMLDecodeError as e:
        raise DataError(f"invalid TOML in {path}: {e}") from e


@dataclass
class Link:
    """A build symlink a package/orchestrator needs: <dir>/<path> -> <dep>."""

    dep: str
    path: str  # relative to the package dir; defaults to the dep name

    @classmethod
    def from_dict(cls, d: dict, source: Path) -> "Link":
        dep = d.get("dep")
        if not dep:
            raise DataError(f"{source}: a [[links]] entry is missing 'dep'")
        return cls(dep=dep, path=d.get("path", dep))


@dataclass
class Package:
    name: str
    org: str
    repo: str
    dir: str
    config_style: str  # "makefile-template" | "build.py"
    config_subdir: str = ""  # e.g. fesm-utils keeps its config under utils/
    links: List[Link] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Package":
        data = _load_toml(path)
        pkg = data.get("package")
        if pkg is None:
            raise DataError(f"{path}: missing [package] table")
        links = [Link.from_dict(d, path) for d in pkg.get("links", [])]
        try:
            return cls(
                name=pkg["name"],
                org=pkg["org"],
                repo=pkg["repo"],
                dir=pkg.get("dir", pkg["name"]),
                config_style=pkg.get("config_style", "makefile-template"),
                config_subdir=pkg.get("config_subdir", ""),
                links=links,
            )
        except KeyError as e:
            raise DataError(f"{path}: [package] missing required key {e}") from e


@dataclass
class Orchestrator:
    name: str
    org: str
    repo: str
    dir: str
    config_style: str
    default_packages: List[str] = field(default_factory=list)
    extras: List[str] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Orchestrator":
        data = _load_toml(path)
        orch = data.get("orchestrator")
        if orch is None:
            raise DataError(f"{path}: missing [orchestrator] table")
        links = [Link.from_dict(d, path) for d in orch.get("links", [])]
        try:
            return cls(
                name=orch["name"],
                org=orch["org"],
                repo=orch["repo"],
                dir=orch.get("dir", orch["name"]),
                config_style=orch.get("config_style", "makefile-template"),
                default_packages=list(orch.get("default_packages", [])),
                extras=list(orch.get("extras", [])),
                links=links,
            )
        except KeyError as e:
            raise DataError(
                f"{path}: [orchestrator] missing required key {e}"
            ) from e


def _stems(directory: Path, suffix: str) -> List[str]:
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob(f"*{suffix}"))


def packages() -> Dict[str, Package]:
    """All supported component packages, keyed by name."""
    out: Dict[str, Package] = {}
    for path in sorted(PACKAGES_DIR.glob("*.toml")):
        pkg = Package.from_file(path)
        out[pkg.name] = pkg
    return out


def orchestrators() -> Dict[str, Orchestrator]:
    """All supported orchestrators, keyed by name."""
    out: Dict[str, Orchestrator] = {}
    for path in sorted(ORCHESTRATORS_DIR.glob("*.toml")):
        orch = Orchestrator.from_file(path)
        out[orch.name] = orch
    return out


def machines() -> List[str]:
    """Names of shipped machine fragments."""
    return _stems(MACHINES_DIR, ".mk")


def compilers() -> List[str]:
    """Names of shipped compiler fragments."""
    return _stems(COMPILERS_DIR, ".mk")
