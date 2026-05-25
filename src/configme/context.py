"""Project context and fragment resolution (see docs/DESIGN.md sec. 3, 6, 7, 10).

This module knows about the *.configme* contract and the layered configuration
that lets one machine/compiler choice drive every component of an orchestrator:

- discovering the orchestrator a directory belongs to;
- reading / writing its ``.configme/{manifest,config}.toml``;
- the user-level ``~/.configme/`` (override ``CONFIGME_HOME`` for testing);
- the three-tier fragment lookup  orchestrator > user > shipped;
- hostname-based machine auto-detection;
- resolving the machine + compiler selection with full precedence.
"""

from __future__ import annotations

import fnmatch
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from configme import data

try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    import tomli as _toml  # type: ignore


def user_dir() -> Path:
    """User-level configme directory (``~/.configme``; ``CONFIGME_HOME`` wins)."""
    return Path(os.environ.get("CONFIGME_HOME", str(Path.home() / ".configme")))


class ProjectError(Exception):
    """A project/context problem (bad manifest, unknown orchestrator, etc.)."""


# --------------------------------------------------------------------- project

@dataclass
class Project:
    orchestrator: data.Orchestrator
    root: Path

    @property
    def configme_dir(self) -> Path:
        return self.root / ".configme"

    @property
    def manifest_path(self) -> Path:
        return self.configme_dir / "manifest.toml"

    @property
    def config_path(self) -> Path:
        return self.configme_dir / "config.toml"


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return _toml.load(f)
    except FileNotFoundError:
        return {}
    except _toml.TOMLDecodeError as e:
        raise ProjectError(f"invalid TOML in {path}: {e}") from e


def find_project(cwd: Path) -> Optional[Project]:
    """Identify the orchestrator rooted at ``cwd``.

    Prefers a local ``.configme/manifest.toml`` (which names its orchestrator);
    otherwise matches the directory name against a known orchestrator.
    """
    orchs = data.orchestrators()

    mf = cwd / ".configme" / "manifest.toml"
    if mf.is_file():
        manifest = _load_toml(mf)
        name = manifest.get("orchestrator")
        if not name:
            raise ProjectError(f"{mf}: missing top-level `orchestrator = \"...\"`")
        if name not in orchs:
            raise ProjectError(
                f"{mf}: orchestrator '{name}' is not supported by configme. "
                f"Known: {', '.join(sorted(orchs))}"
            )
        return Project(orchs[name], cwd)

    for orch in orchs.values():
        if cwd.name == orch.dir:
            return Project(orch, cwd)
    return None


def manifest_packages(project: Project) -> List[str]:
    """Package list for the project: the local manifest if present, else the
    orchestrator's shipped default set. Validates every entry is supported."""
    pkgs = data.packages()
    if project.manifest_path.is_file():
        manifest = _load_toml(project.manifest_path)
        names = list(manifest.get("packages", []))
        source = str(project.manifest_path)
    else:
        names = list(project.orchestrator.default_packages)
        source = "shipped seed manifest"
    unknown = [n for n in names if n not in pkgs]
    if unknown:
        raise ProjectError(
            f"{source}: unsupported package(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(pkgs))}"
        )
    return names


# ------------------------------------------------------------------ config.toml

def load_config(project: Optional[Project]) -> dict:
    if project is None:
        return {}
    return _load_toml(project.config_path)


def load_user_config() -> dict:
    return _load_toml(user_dir() / "config.toml")


def _dump_simple_toml(values: Dict[str, str], comment: str = "") -> str:
    """Serialise a flat string-keyed table to TOML (tomllib is read-only)."""
    lines = []
    if comment:
        lines.extend(f"# {c}" for c in comment.splitlines())
    for k, v in values.items():
        lines.append(f'{k} = "{v}"')
    return "\n".join(lines) + "\n"


def save_config(project: Project, updates: Dict[str, str]) -> None:
    """Merge updates into .configme/config.toml, preserving existing keys."""
    current = _load_toml(project.config_path)
    merged = {k: v for k, v in current.items() if isinstance(v, str)}
    merged.update(updates)
    project.configme_dir.mkdir(parents=True, exist_ok=True)
    project.config_path.write_text(
        _dump_simple_toml(
            merged,
            comment="configme local settings. Edit machine/compiler to retarget.",
        )
    )


# ------------------------------------------------------------- fragment lookup

def _tier_dir(base: Path, kind: str) -> Path:
    return base / (kind + "s")  # machines / compilers


def _shipped_dir(kind: str) -> Path:
    return data.MACHINES_DIR if kind == "machine" else data.COMPILERS_DIR


def available_fragments(kind: str, project: Optional[Project]) -> List[str]:
    """All fragment names of a kind visible across the three tiers."""
    names = set(data.machines() if kind == "machine" else data.compilers())
    for base in (_maybe(project), user_dir()):
        if base is None:
            continue
        d = _tier_dir(base, kind)
        if d.is_dir():
            names |= {p.stem for p in d.glob("*.mk")}
    return sorted(names)


def _maybe(project: Optional[Project]) -> Optional[Path]:
    return project.configme_dir if project else None


def resolve_fragment(kind: str, name: str,
                     project: Optional[Project]) -> Tuple[Path, str]:
    """Resolve a fragment to (path, tier) with precedence orchestrator > user >
    shipped. Raises ProjectError listing options if not found."""
    candidates: List[Tuple[str, Path]] = []
    if project is not None:
        candidates.append(
            ("orchestrator", _tier_dir(project.configme_dir, kind) / f"{name}.mk"))
    candidates.append(("user", _tier_dir(user_dir(), kind) / f"{name}.mk"))
    candidates.append(("shipped", _shipped_dir(kind) / f"{name}.mk"))
    for tier, path in candidates:
        if path.is_file():
            return path, tier
    raise ProjectError(
        f"{kind} fragment '{name}' not found in any tier. "
        f"Available {kind}s: {', '.join(available_fragments(kind, project)) or '(none)'}"
    )


def is_locally_defined_only(kind: str, name: str) -> bool:
    """True if a fragment name is NOT in the shipped registry — used to nudge the
    user to contribute it centrally."""
    shipped = data.machines() if kind == "machine" else data.compilers()
    return name not in shipped


# --------------------------------------------------------------- hostname map

def _hostname_map() -> Dict[str, str]:
    path = data.DATA_DIR / "hostnames.toml"
    if not path.is_file():
        return {}
    return _load_toml(path).get("hostnames", {})


def hostname_machine() -> Optional[str]:
    """Best-effort machine from the current hostname via the shipped glob map."""
    names = {socket.gethostname(), socket.getfqdn()}
    mapping = _hostname_map()
    for host in names:
        for pattern, machine in mapping.items():
            if fnmatch.fnmatch(host, pattern):
                return machine
    return None


# --------------------------------------------------------------- selection

def resolve_selection(machine: Optional[str], compiler: Optional[str],
                      project: Optional[Project], prompt_fn) -> Tuple[str, str]:
    """Resolve machine + compiler with precedence:

        explicit flag > orchestrator config.toml > user config.toml
                      > hostname auto-detect (machine only) > prompt

    Persists the resolved pair into the project's config.toml so later runs
    inside the same orchestrator do not prompt again.
    """
    proj_cfg = load_config(project)
    user_cfg = load_user_config()

    machine = (machine or proj_cfg.get("machine") or user_cfg.get("machine")
               or hostname_machine())
    compiler = (compiler or proj_cfg.get("compiler") or user_cfg.get("compiler"))

    if not machine:
        machine = prompt_fn("machine", available_fragments("machine", project))
    if not compiler:
        compiler = prompt_fn("compiler", available_fragments("compiler", project))

    # Persist into the orchestrator config so the choice is inherited/reused.
    if project is not None and (proj_cfg.get("machine") != machine
                                or proj_cfg.get("compiler") != compiler):
        save_config(project, {"machine": machine, "compiler": compiler})

    return machine, compiler
