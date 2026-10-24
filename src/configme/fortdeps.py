"""Fortran module dependencies: scan sources, emit object-to-object make rules.

A Fortran object must be compiled after the objects whose modules it ``use``s,
so hand-written Makefiles list those edges per object — easy to break (see
docs/DESIGN.md sec. 14). ``configme deps`` instead scans a package's source
directories for ``module``, ``submodule``, ``use`` and ``include`` statements and
writes the edges as make rules (``$(objdir)/a.o: $(objdir)/b.o``) to a file the
package's Makefile includes (default ``config/deps.mk``).

What the generated file contains:

* ``vpath`` lines for every source directory, so one pattern rule
  ``$(objdir)/%.o: %.f90`` in the Makefile finds each source.
* ``deps_objs``: every library object (files with a ``program`` are left out).
* One rule per object listing the objects it depends on, plus any resolved
  ``include`` files.

Modules used but not defined in the scanned sources (intrinsic modules, or
modules of external libraries such as ``yelmo``) are external: they produce no
rule and are listed in a comment. A module defined in more than one file (a
backend and its same-interface stub) is an *alternative*: its users depend on
``$(<module>_obj)``, which the Makefile must set to the selected object before
including the file; ``deps_objs`` carries the variable instead of the files.

The scanner is line-based, not a Fortran parser: comments and string contents
are ignored, ``&`` continuations and ``;`` separators are honoured,
preprocessor conditionals are not evaluated (every branch counts), and
fixed-form files (``.f``, ``.F``, ``.for``, ``.ftn``) are read by column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from configme import generate

FREE_SUFFIXES = (".f90", ".F90", ".f95", ".F95", ".f03", ".F03", ".f08", ".F08")
FIXED_SUFFIXES = (".f", ".F", ".for", ".ftn")
SUFFIXES = FREE_SUFFIXES + FIXED_SUFFIXES

# Not reported as external: the compiler provides them.
INTRINSIC_MODULES = frozenset({
    "iso_fortran_env", "iso_c_binding", "ieee_arithmetic", "ieee_exceptions",
    "ieee_features", "omp_lib", "omp_lib_kinds", "openacc",
})

DEFAULT_OBJDIR = "$(objdir)"
DEFAULT_OUT = "config/deps.mk"

_NAME = r"[a-z_]\w*"
_MODULE = re.compile(rf"^module\s+({_NAME})$")
_SUBMODULE = re.compile(
    rf"^submodule\s*\(\s*({_NAME})\s*(?::\s*({_NAME})\s*)?\)\s*({_NAME})$")
_USE = re.compile(
    rf"^use(?:\s*,\s*(intrinsic|non_intrinsic)\s*::|\s*::|\s+)\s*({_NAME})\s*(?:,.*)?$")
_PROGRAM = re.compile(rf"^program\s+({_NAME})$")
_INCLUDE = re.compile(r"""^include\s*(['"])(.+?)\1$""", re.I)
_CPP_INCLUDE = re.compile(r"""^#\s*include\s*["<](.+?)[">]""")


class DepsError(generate.GenerateError):
    """Dependencies could not be generated (bad input, ambiguous layout)."""


@dataclass
class Unit:
    """What one source file defines and needs."""
    path: Path
    modules: Set[str] = field(default_factory=set)      # modules it defines
    submodules: Set[str] = field(default_factory=set)   # "ancestor:name" ids it defines
    uses: Set[str] = field(default_factory=set)         # non-intrinsic modules it uses
    intrinsic_uses: Set[str] = field(default_factory=set)
    parents: Set[str] = field(default_factory=set)      # submodule parents: "mod" or "mod:sub"
    includes: List[str] = field(default_factory=list)
    is_program: bool = False

    @property
    def obj(self) -> str:
        return self.path.stem + ".o"


# ------------------------------------------------------------------ scanning

def _strip_comment(line: str) -> str:
    """Drop a trailing ``!`` comment that is not inside a string literal."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "!":
            return line[:i]
    return line


def _split_statements(code: str) -> Iterator[str]:
    """Split a logical line on ``;`` outside string literals."""
    quote, start = None, 0
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            yield code[start:i].strip()
            start = i + 1
    yield code[start:].strip()


def _free_statements(lines: Iterable[str]) -> Iterator[str]:
    buf = ""
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#"):          # preprocessor directive
            yield stripped
            continue
        code = _strip_comment(raw).strip()
        if not code:                          # blank/comment line: keeps a continuation open
            continue
        if buf and code.startswith("&"):
            code = code[1:]
        if code.endswith("&"):
            buf += code[:-1]
            continue
        yield from _split_statements(buf + code)
        buf = ""
    if buf:
        yield from _split_statements(buf)


def _fixed_statements(lines: Iterable[str]) -> Iterator[str]:
    buf: Optional[str] = None
    for raw in lines:
        if raw.startswith("#"):
            yield raw.strip()
            continue
        if not raw.strip() or raw[:1] in "cC*!":
            continue
        is_cont = len(raw) > 5 and raw[5] not in " 0" and buf is not None
        code = _strip_comment(raw[6:72])
        if is_cont:
            buf += code
            continue
        if buf is not None:
            yield from _split_statements(buf)
        buf = code
    if buf is not None:
        yield from _split_statements(buf)


def scan_file(path: Path) -> Unit:
    """Read one source file and record what it defines and needs."""
    text = Path(path).read_text(errors="replace")
    fixed = Path(path).suffix in FIXED_SUFFIXES
    stmts = _fixed_statements(text.splitlines()) if fixed else _free_statements(text.splitlines())
    unit = Unit(path=Path(path))
    for stmt in stmts:
        if not stmt:
            continue
        if stmt.startswith("#"):
            m = _CPP_INCLUDE.match(stmt)
            if m:
                unit.includes.append(m.group(1))
            continue
        low = " ".join(stmt.lower().split())
        if (m := _MODULE.match(low)):
            unit.modules.add(m.group(1))
        elif (m := _SUBMODULE.match(low)):
            ancestor, parent, name = m.groups()
            unit.parents.add(f"{ancestor}:{parent}" if parent else ancestor)
            unit.submodules.add(f"{ancestor}:{name}")
        elif (m := _USE.match(low)):
            nature, name = m.groups()
            if nature == "intrinsic":
                unit.intrinsic_uses.add(name)
            else:
                unit.uses.add(name)
        elif (m := _PROGRAM.match(low)):
            unit.is_program = True
        elif (m := _INCLUDE.match(stmt)):
            unit.includes.append(m.group(2))
    return unit


def find_sources(root: Path, src_dirs: Sequence[str]) -> List[Path]:
    """All Fortran sources under ``root/<src>`` for each src dir, recursively."""
    out: List[Path] = []
    for src in src_dirs:
        base = root / src
        if not base.is_dir():
            raise DepsError(f"source directory not found: {base}")
        out.extend(p for p in base.rglob("*") if p.is_file() and p.suffix in SUFFIXES)
    return sorted(set(out))


# ------------------------------------------------------------------ rendering

@dataclass
class Deps:
    """The resolved dependency graph, ready to render as make rules."""
    objdir: str
    root: Path
    units: List[Unit]
    rules: Dict[str, List[str]]          # object -> prerequisites (objects/files)
    alternatives: Dict[str, List[str]]   # module -> provider objects
    externals: Set[str]
    lib_objs: List[str]
    src_dirs: List[str]                  # dirs holding sources, relative to root

    def render(self, command: str) -> str:
        od = self.objdir
        lines = [
            f"# Fortran module dependencies. Generated by configme ({command});",
            "# do not edit. Rerun `configme config` (or `configme deps`) after adding",
            "# or removing a source file or a `use`/`module`/`include` statement.",
            "",
        ]
        if self.externals:
            lines += ["# External modules (defined outside the scanned sources; no rule):",
                      *_wrap_comment(sorted(self.externals)), ""]
        lines += ["# Source directories, for the pattern rule $(objdir)/%.o: %.f90"]
        for suffix in sorted({u.path.suffix for u in self.units}):
            lines.append(f"vpath %{suffix} {' '.join(self.src_dirs)}")
        lines.append("")
        if self.alternatives:
            lines += ["# Modules defined in more than one file (a backend and its stub). The",
                      "# Makefile selects the provider by setting <module>_obj before including",
                      "# this file."]
            for mod, objs in sorted(self.alternatives.items()):
                choices = " ".join(f"{od}/{o}" for o in objs)
                lines += [f"ifndef {mod}_obj",
                          f"$(error {mod}_obj is not set: select one of {choices})",
                          "endif"]
            lines.append("")
        lines += ["# Library objects (files with a `program` are left out)."]
        lines += _wrap_var("deps_objs", self.lib_objs)
        lines.append("")
        lines += ["# Object dependencies."]
        for obj in sorted(self.rules):
            prereqs = self.rules[obj]
            if prereqs:
                lines.append(f"{od}/{obj}: {' '.join(prereqs)}")
        return "\n".join(lines) + "\n"


def _wrap_comment(names: List[str], width: int = 76) -> List[str]:
    out, cur = [], "#  "
    for n in names:
        if len(cur) + len(n) + 1 > width and cur.strip("# "):
            out.append(cur.rstrip())
            cur = "#  "
        cur += f" {n}"
    out.append(cur.rstrip())
    return out


def _wrap_var(name: str, items: List[str]) -> List[str]:
    if not items:
        return [f"{name} ="]
    out = [f"{name} = \\"]
    out += [f"    {it} \\" for it in items[:-1]]
    out.append(f"    {items[-1]}")
    return out


def resolve(root: Path, src_dirs: Sequence[str], objdir: str = DEFAULT_OBJDIR) -> Deps:
    """Scan ``src_dirs`` under ``root`` and resolve the object dependencies."""
    root = Path(root)
    units = [scan_file(p) for p in find_sources(root, src_dirs)]

    by_obj: Dict[str, Unit] = {}
    for u in units:
        if u.obj in by_obj:
            raise DepsError(
                f"two sources compile to the same object {u.obj}: "
                f"{by_obj[u.obj].path.relative_to(root)} and {u.path.relative_to(root)} "
                f"(object names must be unique across source directories)")
        by_obj[u.obj] = u

    providers: Dict[str, List[str]] = {}   # module or "anc:sub" -> objects
    for u in units:
        for key in u.modules | u.submodules:
            providers.setdefault(key, []).append(u.obj)
    alternatives = {m: sorted(objs) for m, objs in providers.items() if len(objs) > 1}
    if any(":" in m for m in alternatives):
        raise DepsError("a submodule is defined in more than one file: "
                        f"{', '.join(m for m in alternatives if ':' in m)}")
    alt_objs = {o for objs in alternatives.values() for o in objs}

    def provider(key: str, me: str) -> Optional[str]:
        objs = providers.get(key)
        if not objs or me in objs:
            return None
        if key in alternatives:
            return f"$({key}_obj)"
        return f"{objdir}/{objs[0]}"

    rules: Dict[str, List[str]] = {}
    externals: Set[str] = set()
    for u in units:
        prereqs: Set[str] = set()
        for mod in u.uses | u.parents:
            if mod in providers:
                p = provider(mod, u.obj)
                if p:
                    prereqs.add(p)
            elif mod not in INTRINSIC_MODULES:
                externals.add(mod)
        for inc in u.includes:
            found = _resolve_include(inc, u.path.parent, root, src_dirs)
            if found is not None:
                prereqs.add(found)
        rules[u.obj] = sorted(prereqs)

    lib_objs = [f"{objdir}/{u.obj}" for u in units
                if not u.is_program and u.obj not in alt_objs]
    lib_objs += [f"$({m}_obj)" for m in sorted(alternatives)]
    src_dirs_found = sorted({str(u.path.parent.relative_to(root)) for u in units})
    return Deps(objdir=objdir, root=root, units=units, rules=rules,
                alternatives=alternatives, externals=externals,
                lib_objs=lib_objs, src_dirs=src_dirs_found)


def _resolve_include(name: str, here: Path, root: Path,
                     src_dirs: Sequence[str]) -> Optional[str]:
    for base in [here] + [root / s for s in src_dirs]:
        p = base / name
        if p.is_file():
            return str(p.relative_to(root))
    return None  # system/external include (e.g. mpif.h): no rule


# ------------------------------------------------------------------ entry points

def write_deps(root: Path, src_dirs: Sequence[str], *, objdir: str = DEFAULT_OBJDIR,
               out: str = DEFAULT_OUT, command: str = "configme deps") -> Tuple[Path, Deps]:
    """Generate ``root/out`` from the sources in ``src_dirs``. Returns the path
    written and the resolved graph."""
    root = Path(root)
    deps = resolve(root, src_dirs, objdir)
    out_path = root / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    generate._atomic_write(out_path, deps.render(command))
    return out_path, deps


def _includes_file(template: str, out: str) -> bool:
    pat = re.compile(rf"^\s*-?include\s+{re.escape(out)}\s*$", re.M)
    return bool(pat.search(template))


def configure_deps(cfgroot: Path, spec) -> Tuple[Path, Deps]:
    """Run the package's ``fortran_deps`` spec during configure. Fails loudly if
    the Makefile template does not include the output file (it would be
    generated but never used)."""
    template = generate.find_repo_file(cfgroot, "Makefile")
    if template is None or not _includes_file(template.read_text(), spec.out):
        raise DepsError(
            f"fortran_deps is enabled but the Makefile template in {cfgroot} does "
            f"not `include {spec.out}`; add it after the object lists.")
    return write_deps(cfgroot, spec.src, objdir=spec.objdir, out=spec.out,
                      command="configme config")
