"""`--link` and `links.toml`: reuse an existing on-disk checkout of a package
instead of cloning a fresh one (see docs/DESIGN.md).

When ``configme install <root>`` (or ``configme upgrade``) needs a package
already present somewhere else on disk, the user can point at it:

    configme install yelmox --link fesm-utils=/work/shared/fesm-utils

configme then symlinks ``<root>/fesm-utils -> /work/shared/fesm-utils`` instead
of cloning a duplicate. The same mapping can also live in TOML:

    # ~/.configme/links.toml  (global)
    # <root>/.configme/links.toml  (project — overrides global per-package)
    [links]
    "fesm-utils" = "/work/shared/fesm-utils"

CLI ``--link`` always overrides the file. File-supplied links are confirmed
per-package at install time (the file may carry stale entries); CLI-supplied
links are taken as explicit and applied silently.

Build stamp (``.configme-build.toml`` inside each built package) records the
(machine, compiler, variants) the local build was made for, so an external
package linked into an install with a different (machine, compiler) gets a
warning before its stale artifacts confuse the consumer's link step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    import tomli as _toml  # type: ignore

from configme import context


class LinkError(Exception):
    """Bad ``--link`` argument, malformed ``links.toml``, or missing target."""


# --------------------------------------------------------------- parsing

def parse_link_args(items: List[str]) -> Dict[str, Path]:
    """Parse ``--link pkg=path`` arguments into ``{pkg: Path}``.

    Empty values, missing ``=``, and duplicate packages are hard errors —
    silent fallthrough to a clone would defeat the whole point of the flag."""
    out: Dict[str, Path] = {}
    for raw in items:
        if "=" not in raw:
            raise LinkError(
                f"--link {raw!r}: expected PKG=PATH (e.g. --link fesm-utils=/abs/path)")
        pkg, _, path = raw.partition("=")
        pkg = pkg.strip()
        path = path.strip()
        if not pkg or not path:
            raise LinkError(f"--link {raw!r}: empty package or path")
        if pkg in out:
            raise LinkError(
                f"--link {pkg}=...: package given more than once on the command line")
        out[pkg] = Path(os.path.expandvars(os.path.expanduser(path)))
    return out


def _load_links_file(path: Path) -> Dict[str, Path]:
    """Read a ``links.toml`` file and return ``{pkg: Path}``. Missing file -> {}.

    Schema:
        [links]
        "fesm-utils" = "/abs/path"
    """
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            doc = _toml.load(f)
    except _toml.TOMLDecodeError as e:
        raise LinkError(f"invalid TOML in {path}: {e}") from e
    section = doc.get("links", {})
    if not isinstance(section, dict):
        raise LinkError(f"{path}: [links] must be a table of package = path entries")
    out: Dict[str, Path] = {}
    for pkg, val in section.items():
        if not isinstance(val, str) or not val:
            raise LinkError(
                f"{path}: links.{pkg!r} must be a non-empty path string")
        out[pkg] = Path(os.path.expandvars(os.path.expanduser(val)))
    return out


def load_file_links(project_root: Optional[Path]) -> Tuple[Dict[str, Path],
                                                            Dict[str, Path]]:
    """Load ``links.toml`` from the global and project tiers.

    Returns ``(global_links, project_links)`` separately so callers can show
    each source in prompts/diagnostics. Project entries override global per
    package at merge time (see ``merge_links``)."""
    global_links = _load_links_file(context.user_dir() / "links.toml")
    project_links: Dict[str, Path] = {}
    if project_root is not None:
        project_links = _load_links_file(project_root / ".configme" / "links.toml")
    return global_links, project_links


def merge_links(global_links: Dict[str, Path],
                project_links: Dict[str, Path],
                cli_links: Dict[str, Path]) -> Dict[str, Tuple[Path, str]]:
    """Merge the three tiers into ``{pkg: (path, source)}``.

    Precedence (each tier fully overrides the prior for the same package):
        cli (``--link``)  >  project (.configme/links.toml)  >  global (~/.configme/links.toml)

    ``source`` is ``"cli"`` | ``"project"`` | ``"global"`` so callers can
    decide whether to prompt (file tiers) or apply silently (cli)."""
    merged: Dict[str, Tuple[Path, str]] = {}
    for pkg, path in global_links.items():
        merged[pkg] = (path, "global")
    for pkg, path in project_links.items():
        merged[pkg] = (path, "project")
    for pkg, path in cli_links.items():
        merged[pkg] = (path, "cli")
    return merged


def validate_links(links: Dict[str, Tuple[Path, str]],
                   known_packages: List[str]) -> None:
    """Hard-error on a link pointing at a non-existent path or an unknown
    package name. Validation is upfront so the install loop never half-clones
    and then trips over a bad link."""
    for pkg, (path, source) in links.items():
        if pkg not in known_packages:
            raise LinkError(
                f"--link / links.toml: '{pkg}' is not a known package "
                f"(source: {source})")
        if not path.exists():
            raise LinkError(
                f"--link {pkg}={path}: path does not exist "
                f"(source: {source}). Create it, fix the path, or drop the link.")
        if not path.is_dir():
            raise LinkError(
                f"--link {pkg}={path}: not a directory (source: {source})")


# --------------------------------------------------------------- prompt

def confirm_file_links(links: Dict[str, Tuple[Path, str]],
                       confirm_fn) -> Dict[str, Path]:
    """Per-link prompt for entries that came from a ``links.toml`` (the file
    may carry stale entries — better to confirm than silently apply).

    CLI-supplied entries (``source == "cli"``) are applied unconditionally.
    With no ``confirm_fn`` (non-interactive), file links default to applied
    (the file is itself a signal of intent)."""
    out: Dict[str, Path] = {}
    for pkg, (path, source) in links.items():
        if source == "cli":
            out[pkg] = path
            continue
        prompt = f"Link {pkg} -> {path}? (from {source} links.toml)"
        if confirm_fn is None or confirm_fn(prompt, True):
            out[pkg] = path
    return out


# --------------------------------------------------------------- build stamp

_STAMP_NAME = ".configme-build.toml"


def stamp_path(dest: Path) -> Path:
    """Path of the build stamp file inside a package checkout."""
    return dest / _STAMP_NAME


def write_build_stamp(dest: Path, *, tool: str, machine: str, compiler: str,
                      variants: List[str]) -> Path:
    """Record (tool, machine, compiler, variants) inside the just-built package.

    Tool is ``build.py`` or ``make`` — to let later diagnostics tell apart an
    autotools build of fesm-utils from a Makefile build of fesm-utils/utils."""
    path = stamp_path(dest)
    body = (
        "# Written by configme. Records what this checkout was last built for.\n"
        "[build]\n"
        f'tool = "{tool}"\n'
        f'machine = "{machine}"\n'
        f'compiler = "{compiler}"\n'
        f"variants = {_toml_list(variants)}\n"
    )
    path.write_text(body)
    return path


def read_build_stamp(dest: Path) -> Optional[dict]:
    """Read the build stamp from ``dest`` (None if absent or unreadable).

    Returns the parsed ``[build]`` table (flat dict with ``tool``, ``machine``,
    ``compiler``, ``variants``)."""
    path = stamp_path(dest)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            doc = _toml.load(f)
    except (_toml.TOMLDecodeError, OSError):
        return None
    build = doc.get("build")
    return build if isinstance(build, dict) else None


def stamp_mismatch(stamp: dict, *, machine: str, compiler: str) -> Optional[str]:
    """Return a human-readable note if the stamp disagrees with the current
    (machine, compiler), else None. Used to warn when a linked external was
    built for a different toolchain than the install consuming it."""
    sm = stamp.get("machine")
    sc = stamp.get("compiler")
    if (sm is not None and sm != machine) or (sc is not None and sc != compiler):
        return (f"built for machine={sm}/compiler={sc}, "
                f"but this install uses machine={machine}/compiler={compiler}")
    return None


def _toml_list(items: List[str]) -> str:
    """Render a string list as a TOML inline array (small, no dep on a writer)."""
    inner = ", ".join(f'"{v}"' for v in items)
    return f"[{inner}]"
