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


@dataclass
class Plan:
    primary: Node
    nodes: List[Node]          # everything to install, deps before dependents
    explicit: bool             # True for a '+'-list (no auto-resolution)
    orchestrator: Optional[data.Orchestrator]


# --------------------------------------------------------------- plan building

def _node_for(name: str) -> Node:
    orchs = data.orchestrators()
    pkgs = data.packages()
    if name in orchs:
        o = orchs[name]
        return Node(o.name, o.org, o.repo, o.dir, o.config_style, "", [], True)
    if name in pkgs:
        p = pkgs[name]
        return Node(p.name, p.org, p.repo, p.dir, p.config_style,
                    p.config_subdir, p.links, False)
    known = sorted(set(orchs) | set(pkgs))
    raise InstallError(f"unknown target '{name}'. Supported: {', '.join(known)}")


def _resolve_deps(name: str, pkgs: Dict[str, data.Package],
                  seen: List[str]) -> None:
    """Post-order DFS over package link deps (deps before dependents)."""
    if name in seen:
        return
    for link in pkgs[name].links if name in pkgs else []:
        _resolve_deps(link.dep, pkgs, seen)
    if name not in seen:
        seen.append(name)


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
        return Plan(primary, nodes, explicit=True, orchestrator=orch)

    primary = _node_for(target)

    if only:
        # Exactly the named target: no subpackages, no deps. Marked explicit so
        # the link phase treats any deps as pending rather than expecting them.
        orch = orchs.get(primary.name) if primary.is_orchestrator else None
        return Plan(primary, [primary], explicit=True, orchestrator=orch)

    if primary.is_orchestrator:
        orch = orchs[primary.name]
        order = []
        # components (resolve each component's own deps too), then orchestrator
        for comp in orch.default_packages:
            _resolve_deps(comp, pkgs, order)
        nodes = [_node_for(n) for n in order] + [primary]
        return Plan(primary, nodes, explicit=False, orchestrator=orch)

    # single package: its deps (auto) then itself
    order: List[str] = []
    _resolve_deps(primary.name, pkgs, order)
    nodes = [_node_for(n) for n in order]
    return Plan(primary, nodes, explicit=False, orchestrator=None)


# --------------------------------------------------------------- runner

@dataclass
class Runner:
    download: str               # clone-ssh | clone-https | no
    dry_run: bool
    overwrite: bool
    log: List[str] = field(default_factory=list)

    def emit(self, line: str) -> None:
        self.log.append(line)

    def clone_url(self, node: Node) -> str:
        if self.download == "clone-https":
            return f"https://github.com/{node.org}/{node.repo}.git"
        return f"git@github.com:{node.org}/{node.repo}.git"

    def clone(self, node: Node, dest: Path) -> str:
        """Returns a status string: cloned | exists | present(no) | dry."""
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
        if self.dry_run:
            return "dry"
        if dest.exists() and not self.overwrite:
            return "exists"
        subprocess.run(["git", "clone", url, str(dest)], check=True)
        return "cloned"

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


def root_for(plan: Plan, install_dir: Optional[str], cwd: Path) -> Tuple[Path, bool]:
    """Return (root, primary_present). root holds the primary; components are
    sub-dirs of it. Default root is within the orchestrator (``yelmox/``) or the
    named package (``yelmo/``, with its deps as ``yelmo/fesm-utils``)."""
    if install_dir:
        return Path(install_dir).expanduser().resolve(), False
    # Inside an existing primary checkout?
    if cwd.name == plan.primary.dir or (cwd / ".configme").is_dir():
        return cwd, True
    return (cwd / plan.primary.dir).resolve(), False


def dest_of(node: Node, plan: Plan, root: Path) -> Path:
    if node.name == plan.primary.name:
        return root
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

    results = {"cloned": [], "configured": [], "linked": [],
               "pending": [], "skipped": [], "failed": []}

    # --- clone phase
    runner.emit("\n# --- clone ---")
    if not (primary_present and download != "no"):
        if not (primary_present):
            try:
                st = runner.clone(plan.primary, root)
                print(f"  clone {plan.primary.name}: {st}")
            except (InstallError, subprocess.CalledProcessError) as e:
                print(f"  ! {plan.primary.name}: {e}")
                results["failed"].append(plan.primary.name)
    else:
        runner.emit(f"# {plan.primary.name}: present at {root}")
        print(f"  {plan.primary.name}: using existing {root}")

    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        dest = dest_of(node, plan, root)
        try:
            st = runner.clone(node, dest)
            print(f"  clone {node.name}: {st}")
        except (InstallError, subprocess.CalledProcessError) as e:
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
        if node.config_style == "build.py":
            _emit_build_py(runner, node, dest, machine, compiler, build_deps,
                           info, confirm_fn)
            # A build.py package may also have a makefile-template subcomponent
            # (e.g. fesm-utils/utils, with its own template + common.mk) that
            # configme generates directly.
            if node.config_subdir:
                label = f"{node.name}/{node.config_subdir}"
                runner.emit(f"# configure {label} Makefile (configme)")
                configure_makefile(label=label, pkg_name=node.name, dest=dest,
                                    config_subdir=node.config_subdir,
                                    is_orchestrator=False, **common_kw)
            else:
                results["skipped" if not build_deps else "configured"].append(node.name)
            continue
        runner.emit(f"(cd {dest} && configme {node.name} -m {machine} -c {compiler})")
        configure_makefile(label=node.name, pkg_name=node.name, dest=dest,
                            config_subdir=node.config_subdir,
                            is_orchestrator=node.is_orchestrator, **common_kw)

    # --- extras (orchestrator post-config steps)
    if plan.orchestrator is not None and plan.orchestrator.extras:
        proj = context.find_project(root) if root.exists() else None
        cfg = context.load_config(proj) if proj else {}
        ask = ask_fn or (lambda label: None)
        extras_mod.run_extras(plan.orchestrator, runner, root, cfg, ask)

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
    for key in ("configured", "linked", "skipped", "pending", "failed"):
        if results[key]:
            print(f"  {key}: {', '.join(results[key])}")
    return 1 if results["failed"] else 0


def _emit_build_py(runner, node, dest, machine, compiler, build_deps, info,
                   confirm_fn=None):
    """fesm-utils-style package: print/run its build.py (see issue #7).

    The build runs autotools and is slow (~10-30 min), so it is not run
    unconditionally. ``--build-deps`` forces it; otherwise, on an interactive
    run, the user is asked (default yes). Dry runs and non-interactive sessions
    without ``--build-deps`` only print the command to run later.
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
        return

    do_build = build_deps
    if not do_build and confirm_fn is not None:
        print(f"  {node.name} needs an autotools build (slow, ~10-30 min):")
        print(f"      (cd {dest} && {cmd})")
        do_build = confirm_fn(f"Build {node.name} now?", True)

    if not do_build:
        print(f"  - {node.name}: build.py-style; run when ready:")
        print(f"      (cd {dest} && {cmd})")
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
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"  ! {node.name} build failed: {e}")
