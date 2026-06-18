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
from typing import Dict, List, Optional

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


def split_ref(spec: str) -> "tuple[str, Optional[str]]":
    """Split a ``name:ref`` package spec into ``(name, ref)``.

    A component may pin a git ref (branch, tag, or commit) with a trailing
    ``:ref`` — e.g. ``yelmo:climber-x`` clones yelmo and checks out its
    ``climber-x`` branch. ``ref`` is ``None`` when no ``:`` is present (the
    repository's default branch is used)."""
    name, sep, ref = spec.partition(":")
    return (name, ref or None) if sep else (name, None)


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
    """A build symlink a package/orchestrator needs: <dir>/<path> -> <dep>.

    ``nest`` flips the relationship from "symlink to a shared root clone" to
    "clone the dependency *inside* this package's checkout at ``path``". Used for
    a dependency exclusive to one consumer (e.g. yelmo's FastHydrology), so it
    lives under its consumer (``yelmo/FastHydrology``) in every orchestrator
    rather than at the root. A shared dependency (e.g. fesm-utils) keeps the
    default symlink form."""

    dep: str
    path: str  # relative to the package dir; defaults to the dep name
    nest: bool = False

    @classmethod
    def from_dict(cls, d: dict, source: Path) -> "Link":
        dep = d.get("dep")
        if not dep:
            raise DataError(f"{source}: a [[links]] entry is missing 'dep'")
        return cls(dep=dep, path=d.get("path", dep), nest=bool(d.get("nest", False)))


@dataclass
class BuildSpec:
    """How configme compiles a makefile-template package it owns the build of
    (currently only ``fesm-utils/utils``). The Makefile is generated first by
    the normal configure step; configme then runs ``make openmp=<0|1>
    <make_target>`` once per variant, in the inherited shell environment (no
    module loading — that is the user's / build.py's responsibility)."""

    make_target: str
    variants: List[str] = field(default_factory=lambda: ["serial"])

    # openmp= flag value passed to make for each variant name.
    _VARIANT_FLAG = {"serial": 0, "omp": 1}

    @classmethod
    def from_dict(cls, d: dict, source: Path) -> "BuildSpec":
        target = d.get("make_target")
        if not target:
            raise DataError(f"{source}: [package.build] missing 'make_target'")
        variants = list(d.get("variants", ["serial"]))
        bad = [v for v in variants if v not in cls._VARIANT_FLAG]
        if bad:
            raise DataError(
                f"{source}: [package.build] unknown variant(s) {bad}; "
                f"valid: {sorted(cls._VARIANT_FLAG)}")
        return cls(make_target=target, variants=variants)

    def openmp_flag(self, variant: str) -> int:
        return self._VARIANT_FLAG[variant]


def _parse_artifacts(raw: dict, source: Path) -> Dict[str, List[str]]:
    """Validate and normalise a ``[package.artifacts]`` table: a mapping of
    variant name -> list of artifact paths (strings). Anything else is a data
    error rather than a silent skip, since a malformed table would make
    ``configme status`` under-report build completeness."""
    if not isinstance(raw, dict):
        raise DataError(f"{source}: [package.artifacts] must be a table "
                        f"(variant -> list of paths)")
    out: Dict[str, List[str]] = {}
    for variant, paths in raw.items():
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise DataError(
                f"{source}: [package.artifacts] '{variant}' must be a list of "
                f"path strings")
        out[variant] = list(paths)
    return out


@dataclass
class Package:
    name: str
    org: str
    repo: str
    dir: str
    config_style: str  # primary (behavioural): "makefile-template" | "build.py"
    # Git host the repo is cloned from (default GitHub). Lets a component live on
    # another host (e.g. a GitLab-hosted input repo) without special-casing.
    host: str = "github.com"
    config_subdir: str = ""  # e.g. fesm-utils keeps its config under utils/
    links: List[Link] = field(default_factory=list)
    # Optional informational list of all config styles a package exposes, for
    # display only (e.g. fesm-utils is build.py at the top but its utils/
    # subcomponent is makefile-template). Defaults to [config_style].
    config_styles: List[str] = field(default_factory=list)
    # False for a component that lives inside another package's checkout (e.g.
    # fesm-utils/utils): it is never cloned on its own and cannot be an install
    # primary; it appears when its parent is cloned.
    clone: bool = True
    # Components contained in this package's checkout, configured/built in the
    # same plan right after it (no separate clone). Names must be known packages.
    subpackages: List[str] = field(default_factory=list)
    # If set, configme builds this package via `make` after configuring it.
    build: "Optional[BuildSpec]" = None
    # Build artifacts (library files) that prove a build completed, keyed by
    # variant name (serial/omp) -> paths relative to the package's checkout dir.
    # Lets `configme status` report build completeness by probing the disk,
    # without re-running make. Build-style-agnostic: it works the same for a
    # build.py package (fesm-utils' LIS+FFTW) and a `make` package
    # (fesm-utils/utils' libfesmutils). Empty when a package declares none.
    artifacts: Dict[str, List[str]] = field(default_factory=dict)
    # True for an optional component (often private): a clone failure — e.g. no
    # access to a private repo — is a soft skip recorded in the summary, never a
    # hard install failure. (See climber-x's bgc/vilma.)
    optional: bool = False
    # If True, run `git submodule update --init --recursive` after cloning
    # (e.g. bgc carries the M4AGO submodule).
    submodules: bool = False

    @classmethod
    def from_file(cls, path: Path) -> "Package":
        data = _load_toml(path)
        pkg = data.get("package")
        if pkg is None:
            raise DataError(f"{path}: missing [package] table")
        links = [Link.from_dict(d, path) for d in pkg.get("links", [])]
        build = pkg.get("build")
        artifacts = _parse_artifacts(pkg.get("artifacts", {}), path)
        try:
            style = pkg.get("config_style", "makefile-template")
            return cls(
                name=pkg["name"],
                org=pkg["org"],
                repo=pkg["repo"],
                dir=pkg.get("dir", pkg["name"]),
                config_style=style,
                host=pkg.get("host", "github.com"),
                config_subdir=pkg.get("config_subdir", ""),
                links=links,
                config_styles=list(pkg.get("config_styles", [])) or [style],
                clone=bool(pkg.get("clone", True)),
                subpackages=list(pkg.get("subpackages", [])),
                build=BuildSpec.from_dict(build, path) if build else None,
                artifacts=artifacts,
                optional=bool(pkg.get("optional", False)),
                submodules=bool(pkg.get("submodules", False)),
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
    host: str = "github.com"
    # Component names in build order, with any ``:ref`` already stripped off
    # (the ref lives in ``component_refs``). Keeping this a plain list of names
    # means every existing consumer — manifest writing, validation, display —
    # works unchanged.
    default_packages: List[str] = field(default_factory=list)
    # Optional components attempted on install but allowed to fail softly (e.g.
    # private repos a given user may not have access to: climber-x's bgc/vilma).
    # Same ``name:ref`` syntax as default_packages.
    optional_packages: List[str] = field(default_factory=list)
    # name -> git ref (branch/tag/commit) for components that pin one, parsed
    # from ``default_packages`` / ``optional_packages`` entries (``name:ref``).
    component_refs: Dict[str, str] = field(default_factory=dict)
    extras: dict = field(default_factory=dict)
    links: List[Link] = field(default_factory=list)
    # Optional per-orchestrator override of where each component lives, relative
    # to the orchestrator root (default is the package's own `dir`). e.g.
    # climber-x places yelmo at src/yelmo and fesm-utils at src/utils/fesm-utils.
    component_paths: Dict[str, str] = field(default_factory=dict)

    def path_of(self, package: "Package") -> str:
        return self.component_paths.get(package.name, package.dir)

    @classmethod
    def from_file(cls, path: Path) -> "Orchestrator":
        data = _load_toml(path)
        orch = data.get("orchestrator")
        if orch is None:
            raise DataError(f"{path}: missing [orchestrator] table")
        links = [Link.from_dict(d, path) for d in orch.get("links", [])]
        # default_packages entries may carry a ``:ref`` (e.g. "yelmo:climber-x").
        # Split each into a bare name (kept in default_packages) and an optional
        # ref (collected in component_refs).
        refs: Dict[str, str] = {}

        def _split(specs):
            out = []
            for spec in specs:
                name, ref = split_ref(spec)
                out.append(name)
                if ref:
                    refs[name] = ref
            return out

        names = _split(orch.get("default_packages", []))
        optional = _split(orch.get("optional_packages", []))
        try:
            return cls(
                name=orch["name"],
                org=orch["org"],
                repo=orch["repo"],
                dir=orch.get("dir", orch["name"]),
                config_style=orch.get("config_style", "makefile-template"),
                host=orch.get("host", "github.com"),
                default_packages=names,
                optional_packages=optional,
                component_refs=refs,
                extras=dict(orch.get("extras", {})),
                links=links,
                component_paths=dict(orch.get("component_paths", {})),
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
