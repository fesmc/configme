"""`configme install`: clone, configure, and link a stack (see docs/DESIGN.md
sec. 4, 11). This replaces the bespoke per-orchestrator install.py.

Layout: components are cloned as sub-packages directly under the orchestrator
(or under the primary package) — e.g. ``yelmox/yelmo``, ``yelmox/fesm-utils``.
The primary therefore references them directly; only non-primary packages need
inter-component links (e.g. ``yelmox/yelmo/fesm-utils -> ../fesm-utils``), taken
from each package's ``[[package.links]]``.

Selection semantics:
  * an orchestrator name      -> orchestrator + its full default set
  * a single package name     -> that package + the sub-packages it needs (auto)
  * a '+'-joined literal list -> exactly those, no auto-resolution
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from configme import context, data, extras as extras_mod, generate, netcdf


class InstallError(Exception):
    pass


@dataclass
class Node:
    """A thing to install: a package or the orchestrator."""
    name: str
    org: str
    repo: str
    dir: str
    config_style: str
    config_subdir: str
    links: list
    is_orchestrator: bool
    clone: bool = True
    subpackages: list = field(default_factory=list)
    build: "Optional[data.BuildSpec]" = None
    # Git host to clone from (default GitHub) and an optional ref (branch/tag/
    # commit) to check out right after cloning. ``ref`` is set for orchestrator
    # components that pin one (e.g. climber-x's yelmo -> ``climber-x`` branch).
    host: str = "github.com"
    ref: Optional[str] = None
    # Optional component: a clone failure is a soft skip (recorded as
    # "unavailable"), not a hard install failure. ``submodules`` runs
    # `git submodule update --init --recursive` after cloning.
    optional: bool = False
    submodules: bool = False
    # For a subpackage node: the parent package name and the path relative to the
    # parent's dest (so its location follows the parent under any orchestrator).
    parent: Optional[str] = None
    subdir: str = ""


@dataclass
class Plan:
    primary: Node
    nodes: List[Node]          # everything to install, deps before dependents
    explicit: bool             # True for a '+'-list (no auto-resolution)
    orchestrator: Optional[data.Orchestrator]


# --------------------------------------------------------------- plan building

def build_clone_url(host: str, org: str, repo: str, download: str) -> str:
    """Build a git clone URL for ``org/repo`` on ``host``. ``download`` selects
    the transport: ``https`` -> ``https://<host>/<org>/<repo>.git``, anything
    else (ssh) -> ``git@<host>:<org>/<repo>.git``. Host-agnostic so GitHub,
    GitLab, etc. all work from the same registry data."""
    if download == "https":
        return f"https://{host}/{org}/{repo}.git"
    return f"git@{host}:{org}/{repo}.git"


def _node_for(name: str) -> Node:
    orchs = data.orchestrators()
    pkgs = data.packages()
    if name in orchs:
        o = orchs[name]
        return Node(o.name, o.org, o.repo, o.dir, o.config_style, "", [], True,
                    host=o.host)
    if name in pkgs:
        p = pkgs[name]
        return Node(p.name, p.org, p.repo, p.dir, p.config_style,
                    p.config_subdir, p.links, False,
                    clone=p.clone, subpackages=p.subpackages, build=p.build,
                    host=p.host, optional=p.optional, submodules=p.submodules)
    known = sorted(set(orchs) | set(pkgs))
    raise InstallError(f"unknown target '{name}'. Supported: {', '.join(known)}")


def _with_subpackages(nodes: List[Node]) -> List[Node]:
    """Expand each node's contained subpackages, inserted right after it. A
    subpackage shares its parent's checkout (not cloned separately), so its dest
    is anchored to the parent via `parent`/`subdir`. Deduped by name."""
    out: List[Node] = []
    seen = set()
    for n in nodes:
        if n.name not in seen:
            out.append(n)
            seen.add(n.name)
        for sub_name in n.subpackages:
            if sub_name in seen:
                continue
            sub = _node_for(sub_name)
            sub.parent = n.name
            sub.subdir = str(Path(sub.dir).relative_to(n.dir))
            out.append(sub)
            seen.add(sub_name)
    return out


def _parent_of(name: str) -> Optional[str]:
    """The package that contains `name` as a subpackage, if any."""
    for pkg in data.packages().values():
        if name in pkg.subpackages:
            return pkg.name
    return None


def _resolve_deps(name: str, pkgs: Dict[str, data.Package],
                  seen: List[str]) -> None:
    """Post-order DFS over package link deps (deps before dependents)."""
    if name in seen:
        return
    for link in pkgs[name].links if name in pkgs else []:
        _resolve_deps(link.dep, pkgs, seen)
    if name not in seen:
        seen.append(name)


def _apply_component_refs(nodes: List[Node],
                          orch: "Optional[data.Orchestrator]") -> None:
    """Stamp each node with the git ref its orchestrator pins for it (if any),
    so the clone phase checks it out. A ref attaches to the component's own
    checkout, never to a subpackage (which rides its parent's checkout)."""
    if orch is None or not orch.component_refs:
        return
    for n in nodes:
        ref = orch.component_refs.get(n.name)
        if ref and n.clone:
            n.ref = ref


def build_plan(target: str, *, only: bool = False) -> Plan:
    """Resolve a target string into an ordered install/config Plan.

    Grammar (shared by ``configme install`` and ``configme config``):
      * ``yelmox``        orchestrator + its full default set (expanded)
      * ``yelmo``         a package + the sub-packages it needs (auto-resolved)
      * ``yelmox+yelmo``  exactly those, no expansion / auto-resolution
      * ``only=True``     exactly the named target — no orchestrator expansion
                          and no dependency resolution (the ``--only`` flag)
    """
    orchs = data.orchestrators()
    pkgs = data.packages()

    if "+" in target:
        names = [t for t in target.split("+") if t]
        nodes = [_node_for(n) for n in names]
        primary = next((n for n in nodes if n.is_orchestrator), nodes[0])
        orch = orchs.get(primary.name) if primary.is_orchestrator else None
        expanded = _with_subpackages(nodes)
        _apply_component_refs(expanded, orch)
        return Plan(primary, expanded, explicit=True, orchestrator=orch)

    primary = _node_for(target)

    if only:
        # Exactly the named target: no deps. Subpackages still ride along (they
        # are part of the same checkout, not separate dependencies). Marked
        # explicit so the link phase treats any deps as pending.
        orch = orchs.get(primary.name) if primary.is_orchestrator else None
        return Plan(primary, _with_subpackages([primary]), explicit=True,
                    orchestrator=orch)

    if primary.is_orchestrator:
        orch = orchs[primary.name]
        order = []
        # components (resolve each component's own deps too), then orchestrator
        for comp in orch.default_packages:
            _resolve_deps(comp, pkgs, order)
        # Optional components (e.g. private bgc/vilma): resolved after the
        # required set, deduped against it, and flagged so a clone failure is a
        # soft skip rather than a hard failure.
        opt_order: List[str] = []
        for comp in orch.optional_packages:
            _resolve_deps(comp, pkgs, opt_order)
        opt_only = [n for n in opt_order if n not in order]
        comp_nodes = [_node_for(n) for n in order]
        opt_names = set(orch.optional_packages)
        for n in opt_only:
            node = _node_for(n)
            if n in opt_names:
                node.optional = True
            comp_nodes.append(node)
        nodes = comp_nodes + [primary]
        expanded = _with_subpackages(nodes)
        _apply_component_refs(expanded, orch)
        return Plan(primary, expanded, explicit=False, orchestrator=orch)

    # single package: its deps (auto) then itself
    order: List[str] = []
    _resolve_deps(primary.name, pkgs, order)
    nodes = [_node_for(n) for n in order]
    return Plan(primary, _with_subpackages(nodes), explicit=False,
                orchestrator=None)


# --------------------------------------------------------------- runner

def _git_head(dest: Path) -> Optional[str]:
    """Current commit of the checkout at ``dest`` (None if not a git repo)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest,
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _git_branch(dest: Path) -> Optional[str]:
    """Current branch name of the checkout (None if not a git repo or if HEAD is
    detached). Used to tell whether a checkout already sits on a pinned ref."""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=dest, check=True, capture_output=True, text=True)
        name = out.stdout.strip()
        return name if name and name != "HEAD" else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _git_dirty(dest: Path) -> bool:
    """True if the checkout has uncommitted changes to *tracked* files. Untracked
    files are ignored on purpose: configme writes generated Makefiles into each
    checkout, and those should not block an otherwise-clean upgrade."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=dest, check=True, capture_output=True, text=True)
        return bool(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return False


@dataclass
class Runner:
    download: str = "ssh"           # ssh | https | no
    dry_run: bool = False
    overwrite: bool = False
    log: List[str] = field(default_factory=list)

    def emit(self, line: str) -> None:
        self.log.append(line)

    def clone_url(self, node: Node) -> str:
        return build_clone_url(node.host, node.org, node.repo, self.download)

    def clone(self, node: Node, dest: Path) -> str:
        """Clone ``node`` to ``dest`` and, if it pins a ref, check it out.

        When ``dest`` already exists and the node pins a ref, the ref is still
        enforced (see ``_ensure_ref``) so the pin is authoritative on every
        install, not just the first clone.

        Returns a status string:
        cloned | exists | switched | dirty | present(no) | dry."""
        if self.download == "no":
            if not (dest.exists() or dest.is_symlink()):
                raise InstallError(
                    f"{node.name}: -d no requires {dest} to exist already")
            self.emit(f"# {node.name}: using existing {dest}")
            return "present(no)"

        url = self.clone_url(node)
        if self.overwrite and (dest.exists() or dest.is_symlink()) and not self.dry_run:
            outdated = dest.parent / "outdated-repos"
            outdated.mkdir(exist_ok=True)
            shutil.move(str(dest), str(outdated / dest.name))

        self.emit(f"git clone {url} {dest}")
        if node.ref:
            self.emit(f"(cd {dest} && git checkout {node.ref})")
        if node.submodules:
            self.emit(f"(cd {dest} && git submodule update --init --recursive)")
        if dest.exists() and not self.overwrite:
            # Existing checkout: enforce a pinned ref (read-only probing in
            # dry-run; _ensure_ref skips the actual checkout when dry_run).
            return self._ensure_ref(node, dest) if node.ref else "exists"
        if self.dry_run:
            return "dry"
        subprocess.run(["git", "clone", url, str(dest)], check=True)
        if node.ref:
            subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True)
        if node.submodules:
            subprocess.run(["git", "submodule", "update", "--init", "--recursive"],
                           cwd=dest, check=True)
        return "cloned"

    def _ensure_ref(self, node: "Node", dest: Path) -> str:
        """Bring an existing checkout onto the ref its orchestrator pins (e.g.
        yelmo's ``climber-x`` branch). Returns: exists | switched | dirty.

        Already on the ref -> ``exists``. Uncommitted tracked changes -> the
        switch is skipped and reported (never clobbered), returning ``dirty``.
        Otherwise the checkout is switched to the ref; a plain ``git checkout``
        is tried first (offline-friendly when the ref is already fetched) and,
        only if that fails, a ``git fetch`` + retry covers a ref created after
        the original clone."""
        if _git_branch(dest) == node.ref:
            return "exists"
        if _git_dirty(dest):
            self.emit(f"# {node.name}: uncommitted changes at {dest}; "
                      f"cannot switch to {node.ref} (skipped)")
            return "dirty"
        self.emit(f"(cd {dest} && git checkout {node.ref})")
        if self.dry_run:
            return "switched"
        try:
            subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True,
                           capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError):
            self.emit(f"(cd {dest} && git fetch && git checkout {node.ref})")
            try:
                subprocess.run(["git", "fetch"], cwd=dest, check=True)
                subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True)
            except (subprocess.CalledProcessError, OSError) as e:
                raise InstallError(
                    f"{node.name}: could not switch {dest} to ref {node.ref} "
                    f"({e}); switch it by hand or re-clone with --overwrite.")
        return "switched"

    def pull(self, node: "Node", dest: Path) -> Tuple[str, bool]:
        """Update an existing checkout in place. Returns ``(status, updated)``
        where status is one of: missing | dirty | dry | up-to-date | updated, and
        ``updated`` is True only when the pull advanced HEAD (new commits).

        Uses ``git pull --ff-only`` on whatever branch is checked out: a clean
        fast-forward succeeds, anything else (diverged branch, no upstream) is
        reported for the user to resolve by hand — never merged or clobbered.
        A checkout with uncommitted tracked changes is skipped, not touched."""
        if not dest.is_dir():
            self.emit(f"# {node.name}: not present at {dest} (skipped)")
            return "missing", False
        if _git_dirty(dest):
            self.emit(f"# {node.name}: uncommitted changes at {dest} (skipped)")
            return "dirty", False
        self.emit(f"(cd {dest} && git pull --ff-only)")
        if self.dry_run:
            return "dry", False
        before = _git_head(dest)
        try:
            subprocess.run(["git", "pull", "--ff-only"], cwd=dest, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            raise InstallError(
                f"git pull --ff-only failed in {dest} ({e}); resolve manually "
                f"(diverged branch, missing upstream, or network).")
        return ("updated", True) if _git_head(dest) != before else ("up-to-date", False)

    def link(self, link_path: Path, target: Path) -> str:
        rel = os.path.relpath(target, link_path.parent)
        self.emit(f"ln -s {rel} {link_path}")
        if self.dry_run:
            return "dry"
        if link_path.is_symlink() or link_path.exists():
            return "exists"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(rel)
        return "linked"


# --------------------------------------------------------------- main entry

def configure_makefile(*, label, pkg_name, dest, config_subdir, is_orchestrator,
                       machine, compiler, machine_path, compiler_path,
                       dry_run, results) -> None:
    """Generate one Makefile (modern common.mk, overlay, or legacy fallback),
    printing progress and recording the outcome in `results`. `label` is the
    display name (e.g. 'yelmo' or 'fesm-utils/utils'); `pkg_name` keys overlay
    lookup.

    Shared by `configme install` and `configme config` — the single Makefile
    generation code path."""
    cfgroot = dest / config_subdir if config_subdir else dest
    if dry_run:
        print(f"  configure {label}: (dry) would generate Makefile")
        return
    if not cfgroot.is_dir():
        print(f"  - {label}: not present; skipping configure")
        results["skipped"].append(label)
        return
    try:
        copied = generate.ensure_common(pkg_name, cfgroot)
        if copied is not None:
            print(f"  ~ {label}: copied configme-provided common.mk -> {copied}")
        if generate.has_common(cfgroot):
            out = generate.generate_makefile(dest, machine, compiler,
                                             machine_path, compiler_path, config_subdir)
            print(f"  + configure {label}: {out}")
            results["configured"].append(label)
        elif generate.legacy_flat_config(cfgroot, machine, compiler):
            out = generate.legacy_makefile(dest, machine, compiler, config_subdir)
            print(f"  + configure {label}: {out} (LEGACY flat config)")
            results["configured"].append(label)
        elif is_orchestrator:
            out = generate.generate_makefile(dest, machine, compiler,
                                             machine_path, compiler_path, config_subdir)
            print(f"  + configure {label}: {out} (no common.mk)")
            results["configured"].append(label)
        else:
            print(f"  - {label}: no common.mk and no legacy config "
                  f"'{machine}_{compiler}'; skipping")
            results["skipped"].append(label)
    except (generate.GenerateError, netcdf.NetcdfError) as e:
        print(f"  ! {label}: {e}")
        results["failed"].append(label)


def _primary_present(root: Path, plan: Plan) -> bool:
    """True if ``root`` holds a real checkout of the primary, judged by the
    presence of its Makefile template — the one file configme must get from the
    checkout (see generate.generate_makefile / legacy_makefile).

    A matching directory name or a bare ``.configme/`` dir is deliberately NOT
    enough: configme writes ``.configme/`` itself, so a stub from an aborted run
    — or an incomplete download with subdirs but no template — would otherwise be
    mistaken for a finished checkout and silently skip the clone."""
    if not root.is_dir():
        return False
    sub = plan.primary.config_subdir
    cfgroot = root / sub if sub else root
    return generate.find_repo_file(cfgroot, "Makefile") is not None


def root_for(plan: Plan, install_dir: Optional[str], cwd: Path) -> Tuple[Path, bool]:
    """Return (root, primary_present). root holds the primary; components are
    sub-dirs of it. Default root is within the orchestrator (``yelmox/``) or the
    named package (``yelmo/``, with its deps as ``yelmo/fesm-utils``).

    ``primary_present`` reflects whether a real checkout already lives at root
    (see _primary_present) — not merely whether the directory exists or is named
    after the primary."""
    if install_dir:
        root = Path(install_dir).expanduser().resolve()
        return root, _primary_present(root, plan)
    # In or pointing at an existing primary checkout? (dir named after the
    # primary, or one carrying configme metadata) — but only "present" if it
    # actually holds a checkout.
    if cwd.name == plan.primary.dir or (cwd / ".configme").is_dir():
        return cwd, _primary_present(cwd, plan)
    root = (cwd / plan.primary.dir).resolve()
    return root, _primary_present(root, plan)


def dest_of(node: Node, plan: Plan, root: Path) -> Path:
    if node.name == plan.primary.name:
        return root
    # A subpackage lives inside its parent's checkout, so anchor it to the
    # parent's dest (which already accounts for orchestrator component_paths).
    if node.parent is not None:
        parent = next((n for n in plan.nodes if n.name == node.parent), None)
        if parent is not None:
            return dest_of(parent, plan, root) / node.subdir
    if plan.orchestrator is not None:
        return root / plan.orchestrator.component_paths.get(node.name, node.dir)
    return root / node.dir


def run_install(target: str, *, download: str, install_dir: Optional[str],
                machine: Optional[str], compiler: Optional[str],
                overwrite: bool, build_deps: bool, dry_run: bool,
                only: bool = False,
                select_fn=None, ask_fn=None, confirm_fn=None) -> int:
    cwd = Path.cwd()
    plan = build_plan(target, only=only)           # fail-fast: unknown names
    if not plan.primary.clone:
        parent = _parent_of(plan.primary.name)
        hint = (f"run `configme install {parent}`" if parent
                else "install its parent package")
        raise InstallError(
            f"{plan.primary.name} is a component of another package's checkout "
            f"and cannot be installed on its own; {hint}. (To (re)generate its "
            f"Makefile use `configme config {plan.primary.name}`.)")
    root, primary_present = root_for(plan, install_dir, cwd)

    # Resolve selection early (fail-fast on missing machine/compiler) using the
    # project at root if it already exists.
    project = context.find_project(root) if root.exists() else None
    machine, compiler = context.resolve_selection(machine, compiler, project, select_fn)
    machine_path, machine_tier = context.resolve_fragment("machine", machine, project)
    compiler_path, compiler_tier = context.resolve_fragment("compiler", compiler, project)

    runner = Runner(download=download, dry_run=dry_run, overwrite=overwrite)
    runner.emit("#!/usr/bin/env bash")
    runner.emit("# Generated by `configme install` — reproduces this install.")
    runner.emit("set -euo pipefail")
    runner.emit(f"# target={target}  machine={machine}  compiler={compiler}")

    header = "DRY RUN — no changes will be made.\n" if dry_run else ""
    print(f"{header}configme install {target}")
    print(f"  root: {root}")
    print(f"  machine={machine}  compiler={compiler}  download={download}")
    print(f"  packages: {', '.join(n.name for n in plan.nodes)}"
          f"{' (literal, no auto-resolve)' if plan.explicit else ''}")

    results = {"cloned": [], "configured": [], "built": [], "linked": [],
               "pending": [], "deferred": [], "skipped": [],
               "unavailable": [], "failed": []}

    # --- clone phase
    runner.emit("\n# --- clone ---")
    if primary_present and not overwrite:
        runner.emit(f"# {plan.primary.name}: present at {root}")
        print(f"  {plan.primary.name}: using existing {root}")
    elif download == "no":
        # -d no means "configure what's already on disk" — but there is no real
        # checkout here (no Makefile template found). Fail fast rather than write
        # a misleading .configme stub over an empty/partial directory.
        print(f"  ! {plan.primary.name}: no checkout found at {root} "
              f"(no Makefile template under config/ or .configme/); "
              f"-d no cannot configure it. Clone it (drop -d no) or point "
              f"--dir at an existing checkout.")
        results["failed"].append(plan.primary.name)
        print("\nSummary:\n  failed: " + plan.primary.name)
        return 1
    else:
        try:
            st = runner.clone(plan.primary, root)
            ref_note = f" (ref {plan.primary.ref})" if plan.primary.ref else ""
            print(f"  clone {plan.primary.name}: {st}{ref_note}")
        except (InstallError, subprocess.CalledProcessError) as e:
            print(f"  ! {plan.primary.name}: {e}")
            results["failed"].append(plan.primary.name)

    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        if not node.clone:
            # A contained component (e.g. fesm-utils/utils): it arrives with its
            # parent's checkout, so there is nothing to clone.
            runner.emit(f"# {node.name}: component of {node.parent} (not cloned)")
            continue
        dest = dest_of(node, plan, root)
        try:
            st = runner.clone(node, dest)
            ref_note = f" (ref {node.ref})" if node.ref else ""
            print(f"  clone {node.name}: {st}{ref_note}")
        except (InstallError, subprocess.CalledProcessError) as e:
            if node.optional:
                # Optional component (often a private repo): no access just means
                # this build variant is unavailable — note it and carry on.
                print(f"  - {node.name}: optional; not cloned ({e}); skipping")
                results["unavailable"].append(node.name)
            else:
                print(f"  ! {node.name}: {e}")
                results["failed"].append(node.name)

    # --- manifest (make the checkout self-describing, independent of its dir
    # name so e.g. `--dir check` is still recognised by a later bare `configme`)
    deps = [n.name for n in plan.nodes if n.name != plan.primary.name]
    runner.emit("\n# --- manifest ---")
    runner.emit(f"mkdir -p {root}/.configme  # write .configme/manifest.toml")
    if dry_run:
        print(f"  manifest: (dry) would write {root}/.configme/manifest.toml "
              f"(package={plan.primary.name}, deps={deps})")
    elif root.exists():
        mf, created = context.write_manifest(root, plan.primary.name, deps)
        print(f"  + wrote {mf}" if created else f"  manifest: using existing {mf}")

    # --- link phase (non-primary packages only)
    runner.emit("\n# --- links ---")
    plan_names = {n.name for n in plan.nodes}
    pkgs_all = data.packages()
    present_dirs = {n.name: dest_of(n, plan, root) for n in plan.nodes}
    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        node_root = dest_of(node, plan, root)
        # Don't fabricate links (or a stray package dir) for a package that
        # isn't actually present. In dry-run nothing is on disk, so still show
        # the intended links.
        if not dry_run and not node_root.exists():
            continue
        for link in node.links:
            dep_dest = present_dirs.get(link.dep)
            if dep_dest is None and link.dep in pkgs_all:
                sub = (plan.orchestrator.component_paths.get(
                       link.dep, pkgs_all[link.dep].dir)
                       if plan.orchestrator else pkgs_all[link.dep].dir)
                dep_dest = root / sub
            link_path = node_root / link.path
            dep_present = dep_dest is not None and dep_dest.exists()
            # A dependency not in this install set and not already on disk is
            # "pending": create the link anyway (so a later `configme install
            # <dep>` just works), but warn. Common in '+'-literal mode.
            if link.dep not in plan_names and not dep_present:
                if dep_dest is not None:
                    runner.link(link_path, dep_dest)
                print(f"  ! {node.name}: dependency '{link.dep}' not in this "
                      f"install; link {link.path} created but pending")
                results["pending"].append(f"{node.name}->{link.dep}")
                continue
            st = runner.link(link_path, dep_dest)
            print(f"  link {node.name}/{link.path} -> {link.dep}: {st}")
            if st == "linked":
                results["linked"].append(f"{node.name}/{link.path}")

    # --- configure phase
    runner.emit("\n# --- configure (per package) ---")
    info = None
    try:
        info = netcdf.detect()
    except netcdf.NetcdfError:
        info = None  # configure step will surface this per-package if needed
    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    for node in plan.nodes:
        dest = dest_of(node, plan, root)
        if node.config_style == "none":
            # Clone-only component (e.g. vilma, bgc): configme places it but does
            # not generate a Makefile for it (it is built by the orchestrator, or
            # ships a prebuilt library).
            runner.emit(f"# {node.name}: clone-only (configme does not configure it)")
            if not dry_run and dest.is_dir():
                print(f"  - {node.name}: clone-only; no configuration")
            continue
        if node.config_style == "build.py":
            # _emit_build_py records its own outcome (built / deferred / failed)
            # in results, so the parent node's status reflects what actually
            # happened rather than the --build-deps flag.
            _emit_build_py(runner, node, dest, machine, compiler, build_deps,
                           info, results, confirm_fn)
            # A build.py package may also expose a makefile-template config
            # subdir that configme generates directly. (fesm-utils instead now
            # carries its utils component as a separate subpackage.)
            if node.config_subdir:
                label = f"{node.name}/{node.config_subdir}"
                runner.emit(f"# configure {label} Makefile (configme)")
                configure_makefile(label=label, pkg_name=node.name, dest=dest,
                                    config_subdir=node.config_subdir,
                                    is_orchestrator=False, **common_kw)
            continue
        runner.emit(f"(cd {dest} && configme config {node.name} "
                    f"-m {machine} -c {compiler})")
        configure_makefile(label=node.name, pkg_name=node.name, dest=dest,
                            config_subdir=node.config_subdir,
                            is_orchestrator=node.is_orchestrator, **common_kw)
        # A package configme owns the build of (e.g. fesm-utils/utils) is
        # compiled here, after its Makefile exists. Gated like the build.py step.
        if node.build is not None:
            _build_make_package(runner, node, dest, build_deps,
                                confirm_fn, results)

    # --- extras (orchestrator post-config steps)
    followups: List[str] = []
    if plan.orchestrator is not None and plan.orchestrator.extras:
        proj = context.find_project(root) if root.exists() else None
        cfg = context.load_config(proj) if proj else {}
        ask = ask_fn or (lambda label, default=None, *, complete_paths=False: default)
        followups = extras_mod.run_extras(plan.orchestrator, runner, root, cfg,
                                          ask, confirm_fn)

    # --- reproducibility log
    install_sh = root / ".install.sh"
    if dry_run:
        print(f"\n--- .install.sh (dry-run preview; would write {install_sh}) ---")
        print("\n".join(runner.log))
    elif root.exists():
        install_sh.write_text("\n".join(runner.log) + "\n")
        try:
            install_sh.chmod(0o755)
        except OSError:
            pass
        print(f"\n  + wrote {install_sh}")

    # --- summary
    print("\nSummary:")
    for key in ("configured", "built", "linked", "pending",
                "deferred", "skipped", "unavailable", "failed"):
        if results[key]:
            print(f"  {key}: {', '.join(results[key])}")
    if followups:
        print("\nDeferred — run these when ready:")
        for cmd in followups:
            print(f"  {cmd}")
    return 1 if results["failed"] else 0


def run_upgrade(target: str, *, install_dir: Optional[str],
                machine: Optional[str], compiler: Optional[str],
                build_deps: bool, dry_run: bool, only: bool = False,
                select_fn=None, confirm_fn=None) -> int:
    """`configme upgrade`: ``git pull`` existing checkouts in place and
    reconfigure them with the same machine/compiler as the original install
    (read from ``.configme/config.toml``, overridable with ``-m``/``-c``).

    Mirrors ``install``'s target grammar (orchestrator + deps, a package + its
    auto-resolved deps, a '+'-literal list, or ``--only``) but never clones: a
    missing checkout, or one with uncommitted tracked changes, is skipped with a
    warning (run ``configme install`` / resolve it by hand instead). Only a
    package whose pull advanced HEAD is rebuilt, and only when it is a build.py /
    make-built package — gated like ``install`` (``--build-deps`` forces the
    build, otherwise an interactive prompt asks)."""
    cwd = Path.cwd()
    plan = build_plan(target, only=only)           # fail-fast: unknown names
    if not plan.primary.clone:
        parent = _parent_of(plan.primary.name)
        hint = (f"run `configme upgrade {parent}`" if parent
                else "upgrade its parent package")
        raise InstallError(
            f"{plan.primary.name} is a component of another package's checkout "
            f"and cannot be upgraded on its own; {hint}. (To (re)generate its "
            f"Makefile use `configme config {plan.primary.name}`.)")
    root, _ = root_for(plan, install_dir, cwd)
    if not root.exists():
        raise InstallError(
            f"{root} does not exist — nothing to upgrade. Run "
            f"`configme install {target}` first.")

    # Same selection the install used, reused from config.toml unless -m/-c
    # override it; persisted back so a retarget sticks for later runs.
    project = context.find_project(root)
    machine, compiler = context.resolve_selection(machine, compiler, project, select_fn)
    machine_path, _ = context.resolve_fragment("machine", machine, project)
    compiler_path, _ = context.resolve_fragment("compiler", compiler, project)

    runner = Runner(dry_run=dry_run)
    runner.emit("#!/usr/bin/env bash")
    runner.emit("# Generated by `configme upgrade` — reproduces this upgrade.")
    runner.emit("set -euo pipefail")
    runner.emit(f"# target={target}  machine={machine}  compiler={compiler}")

    header = "DRY RUN — no changes will be made.\n" if dry_run else ""
    print(f"{header}configme upgrade {target}")
    print(f"  root: {root}")
    print(f"  machine={machine}  compiler={compiler}")
    print(f"  packages: {', '.join(n.name for n in plan.nodes)}"
          f"{' (literal, no auto-resolve)' if plan.explicit else ''}")

    results = {"updated": [], "configured": [], "built": [], "missing": [],
               "deferred": [], "skipped": [], "failed": []}

    # --- pull phase (each checkout in place; a subpackage rides its parent's
    # checkout, so it has no separate pull but inherits the parent's updated flag)
    runner.emit("\n# --- pull ---")
    updated: Dict[str, bool] = {}
    for node in plan.nodes:
        if not node.clone:
            runner.emit(f"# {node.name}: component of {node.parent} (no separate pull)")
            continue
        dest = dest_of(node, plan, root)
        try:
            status, did_update = runner.pull(node, dest)
        except InstallError as e:
            print(f"  ! {node.name}: {e}")
            results["failed"].append(node.name)
            updated[node.name] = False
            continue
        updated[node.name] = did_update
        print(f"  pull {node.name}: {status}")
        if status == "missing":
            results["missing"].append(node.name)
        elif status == "dirty":
            results["skipped"].append(node.name)
        elif status == "updated":
            results["updated"].append(node.name)

    # --- reconfigure (always) + rebuild (only packages the pull advanced)
    runner.emit("\n# --- reconfigure (per package) ---")
    try:
        info = netcdf.detect()
    except netcdf.NetcdfError:
        info = None
    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    for node in plan.nodes:
        dest = dest_of(node, plan, root)
        # A subpackage shares its parent's git checkout, so its "was it updated?"
        # is the parent's pull result.
        owner = node.name if node.clone else (node.parent or node.name)
        was_updated = updated.get(owner, False)

        if node.config_style == "none":
            runner.emit(f"# {node.name}: clone-only (configme does not configure it)")
            continue

        if node.config_style == "build.py":
            if node.config_subdir:
                label = f"{node.name}/{node.config_subdir}"
                runner.emit(f"# configure {label} Makefile (configme)")
                configure_makefile(label=label, pkg_name=node.name, dest=dest,
                                    config_subdir=node.config_subdir,
                                    is_orchestrator=False, **common_kw)
            if was_updated:
                _emit_build_py(runner, node, dest, machine, compiler, build_deps,
                               info, results, confirm_fn)
            elif dest.is_dir():
                print(f"  - {node.name}: unchanged; skipping rebuild")
            continue

        runner.emit(f"(cd {dest} && configme config {node.name} "
                    f"-m {machine} -c {compiler})")
        configure_makefile(label=node.name, pkg_name=node.name, dest=dest,
                            config_subdir=node.config_subdir,
                            is_orchestrator=node.is_orchestrator, **common_kw)
        if node.build is not None:
            if was_updated:
                _build_make_package(runner, node, dest, build_deps,
                                    confirm_fn, results)
            elif dest.is_dir():
                print(f"  - {node.name}: unchanged; skipping rebuild")

    # --- reproducibility log
    upgrade_sh = root / ".upgrade.sh"
    if dry_run:
        print(f"\n--- .upgrade.sh (dry-run preview; would write {upgrade_sh}) ---")
        print("\n".join(runner.log))
    else:
        upgrade_sh.write_text("\n".join(runner.log) + "\n")
        try:
            upgrade_sh.chmod(0o755)
        except OSError:
            pass
        print(f"\n  + wrote {upgrade_sh}")

    # --- summary
    print("\nSummary:")
    for key in ("updated", "configured", "built", "missing",
                "deferred", "skipped", "failed"):
        if results[key]:
            print(f"  {key}: {', '.join(results[key])}")
    return 1 if results["failed"] else 0


def _emit_build_py(runner, node, dest, machine, compiler, build_deps, info,
                   results, confirm_fn=None):
    """fesm-utils-style package: print/run its build.py (see issue #7).

    The build runs autotools and is slow (~10-30 min), so it is not run
    unconditionally. ``--build-deps`` forces it; otherwise, on an interactive
    run, the user is asked (default yes). Dry runs and non-interactive sessions
    without ``--build-deps`` only print the command to run later.

    Records the actual outcome in ``results``: ``deferred`` when the build is
    not run (dry run, missing checkout, or the user declined), ``built`` on
    success, ``failed`` on a build error.
    """
    nc_env = ""
    if info is not None and info.nc_froot and info.nc_croot:
        nc_env = f"NC_FROOT={info.nc_froot} NC_CROOT={info.nc_croot} "
    cmd = f"{nc_env}./build.py --variant both -m {machine} -c {compiler}"
    runner.emit(f"# {node.name}: build with autotools (slow, ~10-30 min):")
    runner.emit(f"(cd {dest} && {cmd})")

    if runner.dry_run or not dest.is_dir():
        print(f"  - {node.name}: build.py-style; run when ready:")
        print(f"      (cd {dest} && {cmd})")
        results["deferred"].append(node.name)
        return

    do_build = build_deps
    if not do_build and confirm_fn is not None:
        print(f"  {node.name} needs an autotools build (slow, ~10-30 min):")
        print(f"      (cd {dest} && {cmd})")
        do_build = confirm_fn(f"Build {node.name} now?", True)

    if not do_build:
        print(f"  - {node.name}: build.py-style; run when ready:")
        print(f"      (cd {dest} && {cmd})")
        results["deferred"].append(node.name)
        return

    print(f"  building {node.name}: {cmd}")
    try:
        env = dict(os.environ)
        if info is not None and info.nc_froot:
            env["NC_FROOT"] = info.nc_froot
            env["NC_CROOT"] = info.nc_croot or ""
        subprocess.run(["./build.py", "--variant", "both",
                        "-m", machine, "-c", compiler],
                       cwd=dest, check=True, env=env)
        results["built"].append(node.name)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"  ! {node.name} build failed: {e}")
        results["failed"].append(node.name)


def _build_make_package(runner, node, dest, build_deps, confirm_fn, results):
    """Compile a package whose build configme owns (`[package.build]`), once per
    variant: ``make openmp=<0|1> <make_target>``. Runs in the inherited shell
    environment (the Makefile already carries the configme-resolved compiler /
    netCDF, so no module loading is needed here). Gated like the build.py step:
    ``--build-deps`` forces it, an interactive run asks (default yes), and dry /
    non-interactive runs only print the commands."""
    spec = node.build
    cmds = [(v, f"make openmp={spec.openmp_flag(v)} {spec.make_target}")
            for v in spec.variants]
    runner.emit(f"# {node.name}: build ({', '.join(spec.variants)}):")
    for _, cmd in cmds:
        runner.emit(f"(cd {dest} && {cmd})")

    def _defer(reason: str) -> None:
        print(f"  - {node.name}: {reason}; build when ready:")
        for _, cmd in cmds:
            print(f"      (cd {dest} && {cmd})")
        results["deferred"].append(node.name)

    if runner.dry_run or not dest.is_dir():
        _defer("configured")
        return

    do_build = build_deps
    if not do_build and confirm_fn is not None:
        print(f"  {node.name} needs a make build ({', '.join(spec.variants)}):")
        do_build = confirm_fn(f"Build {node.name} now?", True)
    if not do_build:
        _defer("build deferred")
        return

    for variant, cmd in cmds:
        print(f"  building {node.name} ({variant}): {cmd}")
        try:
            subprocess.run(["make", f"openmp={spec.openmp_flag(variant)}",
                            spec.make_target], cwd=dest, check=True)
            results["built"].append(f"{node.name} ({variant})")
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"  ! {node.name} build failed: {e}")
            results["failed"].append(f"{node.name} ({variant})")
