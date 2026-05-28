"""Project context and fragment resolution (see docs/DESIGN.md sec. 3, 6, 7, 10).

This module knows about the *.configme* contract and the layered configuration
that lets one machine/compiler choice drive every component of an orchestrator:

- discovering the orchestrator a directory belongs to;
- reading / writing its ``.configme/{manifest,config}.toml``;
- the user-level ``~/.configme/`` (override ``CONFIGME_HOME`` for testing);
- the three-tier fragment lookup  orchestrator > user > shipped;
- hostname-based machine auto-detection, with an OS-platform fallback;
- resolving the machine + compiler selection with full precedence.
"""

from __future__ import annotations

import fnmatch
import os
import platform
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


def _manifest_primary(cwd: Path) -> Optional[str]:
    """Return the ``package`` (primary) named by ``cwd``'s manifest, or None when
    there is no manifest. A manifest with no ``package`` key is an error.

    The manifest is the authoritative, directory-name-independent record of what
    a checkout is (written by ``configme install``/``configme init``). ``package``
    is resolved by name: a known orchestrator yields an orchestrator context, a
    known package a standalone-package context."""
    mf = cwd / ".configme" / "manifest.toml"
    if not mf.is_file():
        return None
    name = _load_toml(mf).get("package")
    if not name:
        raise ProjectError(f"{mf}: missing top-level `package = \"...\"`")
    if name not in data.orchestrators() and name not in data.packages():
        raise ProjectError(
            f"{mf}: package '{name}' is not supported by configme. Known: "
            f"{', '.join(sorted(set(data.orchestrators()) | set(data.packages())))}")
    return name


def find_project(cwd: Path) -> Optional[Project]:
    """Identify the orchestrator rooted at ``cwd``.

    Prefers a local ``.configme/manifest.toml`` (whose ``package`` names the
    primary); otherwise matches the directory name against a known orchestrator.
    Returns None when ``cwd`` is a standalone package rather than an orchestrator.
    """
    orchs = data.orchestrators()

    name = _manifest_primary(cwd)
    if name is not None:
        return Project(orchs[name], cwd) if name in orchs else None

    for orch in orchs.values():
        if cwd.name == orch.dir:
            return Project(orch, cwd)
    return None


def find_package(cwd: Path) -> Optional[str]:
    """Identify a standalone package rooted at ``cwd``.

    Prefers the manifest's ``package`` (when it names a package, not an
    orchestrator); otherwise matches the directory name against a known package.
    Used by the bare/config-only form: run inside a single package's directory,
    ``configme`` configures just that package."""
    name = _manifest_primary(cwd)
    if name is not None:
        return name if name in data.packages() and name not in data.orchestrators() else None

    for nm, pkg in data.packages().items():
        if cwd.name == pkg.dir:
            return nm
    return None


def manifest_packages(project: Project) -> List[str]:
    """Dependency packages for the project: the manifest's ``deps`` if present,
    else the orchestrator's shipped default set. Validates every entry is a
    supported package."""
    pkgs = data.packages()
    if project.manifest_path.is_file():
        manifest = _load_toml(project.manifest_path)
        # Entries may carry a ``:ref`` pin (e.g. "yelmo:climber-x"); the bare
        # name is the package, the ref is read separately (see manifest_refs).
        names = [data.split_ref(d)[0] for d in manifest.get("deps", [])]
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


def manifest_refs(project: Project) -> Dict[str, str]:
    """Component git refs the checkout's own manifest pins, via ``name:ref``
    entries in ``deps`` (e.g. ``"yelmo:climber-x"``).

    The manifest is the source of truth a package owner can edit (including to
    point a component at a development branch); these pins override the
    orchestrator's shipped defaults. Returns ``{name: ref}`` for pinned entries
    only — a bare name carries no ref here and so falls back to the orchestrator
    default at resolution time. An absent manifest yields no pins (``{}``)."""
    if not project.manifest_path.is_file():
        return {}
    manifest = _load_toml(project.manifest_path)
    refs: Dict[str, str] = {}
    for dep in manifest.get("deps", []):
        name, ref = data.split_ref(dep)
        if ref:
            refs[name] = ref
    return refs


# ----------------------------------------------------------- manifest authoring

_INSTALL_GITIGNORE = (
    "# Written by configme. config.toml is install-local — it records this\n"
    "# checkout's machine/compiler and install choices, so it is kept out of\n"
    "# the package repo. manifest.toml is portable (package + deps only) and\n"
    "# may be committed.\n"
    "config.toml\n"
)


def render_manifest(package: Optional[str], deps: List[str], *,
                    generic: bool = False) -> str:
    """Render a ``manifest.toml`` body: ``package`` names the checkout's primary
    (an orchestrator or a package), ``deps`` lists the packages it pulls in."""
    deps_block = "".join(f'    "{d}",\n' for d in deps)
    if generic:
        known = ", ".join(sorted(set(data.orchestrators()) | set(data.packages())))
        return (
            "# configme manifest. Set `package` to this checkout's primary\n"
            "# (an orchestrator or a package); list the packages it depends on\n"
            "# in `deps`.\n"
            f"# Known: {known}\n"
            '# package = "yelmox"\n'
            "deps = [\n" + deps_block + "]\n"
        )
    return (
        f"# configme manifest for {package}.\n"
        f'package = "{package}"\n'
        "deps = [\n" + deps_block + "]\n"
    )


def ensure_install_gitignore(configme_dir: Path) -> None:
    """Write ``.configme/.gitignore`` (if absent) so the install-local
    ``config.toml`` never gets committed to a package repository. ``manifest.toml``
    (portable: package + deps) and project-tier fragments (machines/, compilers/)
    are left trackable."""
    gi = configme_dir / ".gitignore"
    if not gi.is_file():
        gi.write_text(_INSTALL_GITIGNORE)


def write_manifest(root: Path, package: str, deps: List[str]) -> Tuple[Path, bool]:
    """Ensure ``<root>/.configme/manifest.toml`` exists (plus the install-local
    ``.gitignore``), making the checkout self-describing regardless of its
    directory name. An existing manifest — e.g. one committed to the package
    repo — is left untouched. Returns ``(path, created)``."""
    configme_dir = root / ".configme"
    configme_dir.mkdir(parents=True, exist_ok=True)
    ensure_install_gitignore(configme_dir)
    mf = configme_dir / "manifest.toml"
    if mf.is_file():
        return mf, False
    mf.write_text(render_manifest(package, deps))
    return mf, True


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


def read_fragment(kind: str, name: str,
                  project: Optional[Project]) -> Tuple[Path, str, str]:
    """Return (path, tier, text) for a resolved fragment — used by `show`."""
    path, tier = resolve_fragment(kind, name, project)
    return path, tier, path.read_text()


def _seeded_fragment_text(kind: str, name: str, src: str, body: str) -> str:
    """Header naming the new fragment + the seed's body (minus its own first
    description line). The user edits this; it is a stub, not authoritative."""
    label = "Machine" if kind == "machine" else "Compiler"
    header = (
        f"# {label} configuration: {name} — user-created stub, seeded from '{src}'.\n"
        f"# Edit as needed for this {kind}; consider contributing it to configme\n"
        f"# (https://github.com/fesmc/configme) so others can reuse it.\n"
        "#\n"
    )
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]  # drop the seed's own "<Label> configuration: <src>" line
    rest = "\n".join(lines).lstrip("\n")
    return header + rest + ("\n" if not rest.endswith("\n") else "")


def create_fragment(kind: str, name: str, *, src: str,
                    project: Optional[Project], force: bool = False) -> List[Path]:
    """Scaffold a new machine/compiler fragment ``<name>.mk`` seeded from ``src``.

    Writes the project tier (``<root>/.configme/<kind>s/``) when a project is
    given, and always the user tier (``~/.configme/<kind>s/``) as a durable
    backup. Refuses to overwrite existing files unless ``force``. Returns the
    paths written."""
    if kind not in ("machine", "compiler"):
        raise ProjectError(f"unknown fragment kind '{kind}'")
    _src_path, _tier = resolve_fragment(kind, src, project)  # fail-fast on bad seed
    content = _seeded_fragment_text(kind, name, src, _src_path.read_text())

    targets: List[Path] = []
    if project is not None:
        targets.append(_tier_dir(project.configme_dir, kind) / f"{name}.mk")
    targets.append(_tier_dir(user_dir(), kind) / f"{name}.mk")

    existing = [t for t in targets if t.is_file()]
    if existing and not force:
        raise ProjectError(
            f"{kind} fragment '{name}' already exists: "
            f"{', '.join(str(p) for p in existing)} (use --force to overwrite)")

    written: List[Path] = []
    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(content)
        written.append(t)
    return written


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


def platform_machine() -> Optional[str]:
    """Best-effort generic machine from the host OS, used only when no hostname
    pattern matched. macOS maps to ``macbook`` and Linux to the generic
    ``linux`` base; everything else stays unresolved so configme prompts.

    This sits below ``hostname_machine`` in precedence: a recognised cluster
    login node always wins over the OS fallback."""
    system = platform.system()
    if system == "Darwin":
        return "macbook"
    if system == "Linux":
        return "linux"
    return None


def _compiler_defaults() -> Dict[str, str]:
    path = data.DATA_DIR / "compiler_defaults.toml"
    if not path.is_file():
        return {}
    return _load_toml(path).get("default_compiler", {})


def default_compiler(machine: Optional[str]) -> Optional[str]:
    """The default compiler for ``machine`` from the shipped per-machine table
    (``data/compiler_defaults.toml``), or None when the machine is unknown or
    has no default. Used only to *propose* a selection — never to override an
    explicit ``-c``/config choice."""
    if not machine:
        return None
    return _compiler_defaults().get(machine)


# --------------------------------------------------------------- selection

def resolve_selection(machine: Optional[str], compiler: Optional[str],
                      project: Optional[Project], select_fn) -> Tuple[str, str]:
    """Resolve machine + compiler with precedence:

        explicit flag > orchestrator config.toml > user config.toml
                      > hostname auto-detect > OS-platform fallback
                      (machine only) > prompt

    When anything is still unresolved, ``select_fn(project, machine, compiler,
    default_compiler)`` (see cli._select) returns the final, complete pair: it
    proposes the partially-resolved machine plus the machine's default compiler
    (from the per-machine table) for one-key confirmation, or prompts for both.
    Persists the resolved pair into the project's config.toml so later runs
    inside the same orchestrator do not prompt again.
    """
    proj_cfg = load_config(project)
    user_cfg = load_user_config()

    machine = (machine or proj_cfg.get("machine") or user_cfg.get("machine")
               or hostname_machine() or platform_machine())
    compiler = (compiler or proj_cfg.get("compiler") or user_cfg.get("compiler"))

    if not machine or not compiler:
        machine, compiler = select_fn(project=project, machine=machine,
                                      compiler=compiler,
                                      default_compiler=default_compiler(machine))

    # Persist into the orchestrator config so the choice is inherited/reused.
    if project is not None and (proj_cfg.get("machine") != machine
                                or proj_cfg.get("compiler") != compiler):
        save_config(project, {"machine": machine, "compiler": compiler})

    return machine, compiler
