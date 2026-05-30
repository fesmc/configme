"""Command-line interface for configme.

Command surface (see docs/DESIGN.md sec. 4)::

    configme install <target> [options]   clone/use-existing + configure + link + build
    configme update [options]              self-update: pip install -U configme
    configme upgrade [<target>] [options] git pull + reconfigure (+ rebuild if changed)
    configme config [<target>] [options]  config-only: (re)generate Makefile(s)
    configme status [<target>] [options]  read-only: what is present / still pending
    configme git <git args...> [options]  fan-out a git command across each managed repo
    configme [-m M] [-c C]                alias for `configme config` on the current dir
    configme show <name>                   print a machine/compiler fragment to stdout
    configme new machine|compiler <name>   scaffold a fragment from an existing one
    configme netcdf                        detect & print NC_FROOT / NC_CROOT
    configme init                          scaffold/validate a .configme/ folder
    configme list                          supported packages / machines / compilers

`install` and `config` share one target grammar (`install.build_plan`) and one
Makefile-generation path (`install.configure_makefile`); `config` is the
`install` configure step without clone / link / extras / build.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import os
import subprocess
import sys
from pathlib import Path

from configme import (__version__, context, data, generate, install, netcdf,
                      status)
from configme import git as git_mod

# Verbs handled by subparsers. With no verb, bare `configme [-m M] [-c C]`
# configures the current orchestrator; name packages with `configme config`.
VERBS = {"install", "update", "upgrade", "config", "show", "new", "netcdf",
         "init", "list", "status", "git"}

# Standalone tools `configme install <name>` shortcuts to a `pip install -U` of,
# rather than treating as a managed model package (no clone/configure/link/
# build). These are convenience helpers — `runme` in particular is reused by
# the `pip_package` orchestrator extra, so this just gives users a direct verb
# for the same install when they want it standalone. Kept deliberately tiny.
_PIP_TOOLS = ("runme",)


# ----------------------------------------------------------------- not-yet-impl

class NotImplementedYet(Exception):
    """A verb whose behaviour is specified but not yet built."""

    def __init__(self, what: str, issue: int):
        super().__init__(what)
        self.what = what
        self.issue = issue


def _pending(what: str, issue: int):
    raise NotImplementedYet(what, issue)


# ----------------------------------------------------------------------- list

def cmd_list(args: argparse.Namespace) -> int:
    orchestrators = data.orchestrators()
    packages = data.packages()
    machines = data.machines()
    compilers = data.compilers()

    print("Orchestrators:")
    if orchestrators:
        for name, o in orchestrators.items():
            pkgs = ", ".join(o.default_packages) if o.default_packages else "(none yet)"
            print(f"  {name:12s} {o.org}/{o.repo}")
            print(f"               default set: {pkgs}")
    else:
        print("  (none)")

    print("\nPackages:")
    if packages:
        for name, p in packages.items():
            print(f"  {name:14s} {p.org}/{p.repo}  [{', '.join(p.config_styles)}]")
    else:
        print("  (none)")

    print("\nTools:")
    if _PIP_TOOLS:
        for name in _PIP_TOOLS:
            print(f"  {name:14s} fesmc/{name}")
    else:
        print("  (none)")

    print("\nMachines: " + (", ".join(machines) if machines else "(none)"))
    print("Compilers: " + (", ".join(compilers) if compilers else "(none)"))
    return 0


def _detect_kind(name: str, kind, project):
    """Resolve the fragment kind for `show`: honour an explicit --machine/
    --compiler, else auto-detect (error if the name exists in both)."""
    if kind is not None:
        return kind
    in_m = name in context.available_fragments("machine", project)
    in_c = name in context.available_fragments("compiler", project)
    if in_m and in_c:
        raise context.ProjectError(
            f"'{name}' is both a machine and a compiler; disambiguate with "
            f"--machine or --compiler.")
    if in_m:
        return "machine"
    if in_c:
        return "compiler"
    raise context.ProjectError(
        f"no machine or compiler named '{name}'. "
        f"machines: {', '.join(context.available_fragments('machine', project)) or '(none)'}; "
        f"compilers: {', '.join(context.available_fragments('compiler', project)) or '(none)'}")


def cmd_show(args: argparse.Namespace) -> int:
    """Print a machine/compiler fragment to stdout (with an origin header) so it
    can be read or copied into a local .configme/."""
    project = context.find_project(Path.cwd())
    kind = _detect_kind(args.name, args.kind, project)
    path, tier, text = context.read_fragment(kind, args.name, project)
    print(f"# {kind} '{args.name}' — from {tier} tier: {path}")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Scaffold a new machine/compiler fragment, seeded from an existing one,
    into the project (if any) and user tiers for the user to edit."""
    project = context.find_project(Path.cwd())
    src = args.src or ("linux" if args.kind == "machine" else "gfortran")
    written = context.create_fragment(args.kind, args.name, src=src,
                                      project=project, force=args.force)
    for p in written:
        print(f"  + wrote {p}")
    if project is None:
        print("  note: not inside a project; wrote the user-tier copy only.")
    print(f"Edit the file(s) above for '{args.name}', then use "
          f"-{args.kind[0]} {args.name} with `configme config`/`install`.")
    return 0


# ----------------------------------------------------------- stubbed verbs

def _select(*, project, machine: "str | None" = None,
            compiler: "str | None" = None, default_compiler: "str | None" = None):
    """Resolve the machine+compiler selection, prompting as needed, and return
    the final complete ``(machine, compiler)`` pair.

    ``machine``/``compiler`` are whatever was already resolved (flag, config, or
    hostname); either may be None. ``default_compiler`` is the machine's table
    default. When both a machine and a compiler can be proposed (the machine is
    known and the compiler is set or defaulted), offer one-key confirmation
    (`Keep this configuration? [Y/n]`); declining picks both by hand. When a
    complete pair cannot be proposed, prompt for both from the start.

    An unknown *machine* triggers the escape valve: offer to scaffold `<name>.mk`
    from `linux` so a new machine is usable immediately; an unknown *compiler*
    re-prompts with a hint to `configme new compiler`.
    """
    cand_machine = machine
    cand_compiler = compiler or default_compiler

    if not sys.stdin.isatty():
        # No prompt possible: use a complete proposed config if we have one,
        # otherwise fail with the actionable hint.
        if cand_machine and cand_compiler:
            return cand_machine, cand_compiler
        raise context.ProjectError(
            "no machine/compiler resolved; pass -m/--machine and -c/--compiler "
            "(or set them in .configme/config.toml). "
            f"machines: {', '.join(context.available_fragments('machine', project)) or '(none)'}; "
            f"compilers: {', '.join(context.available_fragments('compiler', project)) or '(none)'}")

    # Only the kinds still missing are prompted; an explicit/config value is kept.
    need_machine = machine is None
    need_compiler = compiler is None

    if cand_machine and cand_compiler:
        print(f"Detected configuration: machine={cand_machine}, compiler={cand_compiler}")
        if _confirm("Keep this configuration?", True):
            return cand_machine, cand_compiler
        need_machine = need_compiler = True  # declined: choose both by hand

    m_opts = context.available_fragments("machine", project)
    c_opts = context.available_fragments("compiler", project)
    if need_machine:
        print("Available machines:  "
              + ", ".join(m_opts + ["<new_machine>"]))
    if need_compiler:
        print("Available compilers: " + (", ".join(c_opts) or "(none)"))

    while True:
        if need_machine and need_compiler:
            parts = input("  machine compiler: ").split()
            if len(parts) != 2:
                print("  enter exactly two words: <machine> <compiler>")
                continue
            machine, compiler = parts
        elif need_machine:
            machine = input("  machine: ").strip()
            if not machine:
                continue
        else:
            compiler = input("  compiler: ").strip()
            if not compiler:
                continue

        if need_machine and machine not in m_opts and not _ensure_machine(machine, project):
            machine = None
            continue
        if need_compiler and compiler not in c_opts:
            print(f"  compiler '{compiler}' is not known (available: "
                  f"{', '.join(c_opts) or '(none)'}). To add one, run "
                  f"`configme new compiler {compiler}` and edit it.")
            compiler = None
            continue
        return machine, compiler


def _ensure_machine(name: str, project) -> bool:
    """Escape valve for an unknown machine: offer to scaffold it from `linux`
    into the project (if any) and user tiers. Returns True if the machine is now
    available (created), False to re-prompt."""
    if not _confirm(f"machine '{name}' is not known — create "
                    f".configme/machines/{name}.mk (and the ~/.configme copy) "
                    f"from 'linux' to edit?", True):
        return False
    written = context.create_fragment("machine", name, src="linux", project=project)
    for p in written:
        print(f"  + wrote {p}")
    print(f"  edit the above to match '{name}'; using it for this run.")
    return True


@contextlib.contextmanager
def _path_completion():
    """Enable bash-like Tab completion of filesystem paths for the duration of
    one ``input()`` call, restoring readline's prior state on exit.

    The completer preserves whatever prefix the user typed (``~/``, ``../``,
    ``$VAR``) and only appends the matched remainder, adding a trailing
    separator on directories. If ``readline`` is unavailable (e.g. the build of
    Python lacks it), this is a no-op and ``input()`` behaves normally."""
    try:
        import readline
    except ImportError:
        yield
        return

    def complete(text, state):
        expanded = os.path.expanduser(os.path.expandvars(text))
        matches = []
        for m in sorted(glob.glob(expanded + "*")):
            comp = text + m[len(expanded):]
            if os.path.isdir(m) and not comp.endswith(os.sep):
                comp += os.sep
            matches.append(comp)
        return matches[state] if state < len(matches) else None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()
    # Treat the whole path (slashes included) as one token to complete.
    readline.set_completer_delims(" \t\n")
    readline.set_completer(complete)
    # macOS ships libedit under the readline name; it needs a different binding.
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        yield
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def _ask(label: str, default: "str | None" = None, *,
         complete_paths: bool = False) -> "str | None":
    """Free-text prompt for extras values; returns None when non-interactive
    (so extras degrade to 'pending' rather than blocking). The default that
    applies when the user just presses Enter is always shown — '(None)' when
    there is no default. With ``complete_paths`` the prompt offers bash-like
    Tab completion of filesystem paths."""
    if not sys.stdin.isatty():
        return default
    shown = default if default is not None else "None"
    ctx = _path_completion() if complete_paths else contextlib.nullcontext()
    with ctx:
        ans = input(f"  {label} ({shown}): ").strip()
    return ans or default


def _confirm(question: str, default: bool = True) -> bool:
    """Yes/no prompt. Returns ``default`` when non-interactive (no TTY), so
    scripted/CI runs are not blocked. The default is shown as the capitalised
    choice in ``[Y/n]`` / ``[y/N]``."""
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"  {question} {suffix}: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def _nudge(kind: str, name: str, tier: str) -> None:
    """Advisory-only: suggest contributing a fragment that is defined locally
    but absent from the shipped registry. No network/gh coupling."""
    if tier != "shipped" and context.is_locally_defined_only(kind, name):
        print(f"  note: {kind} '{name}' is defined locally ({tier} tier) but is "
              f"not in the central configme registry.")
        print(f"        Consider contributing it: "
              f"https://github.com/fesmc/configme (PR or issue).")


def _bare_target(cwd: Path) -> str:
    """Map the current directory to a config target: the orchestrator it roots,
    else the package it roots. Errors if it is neither."""
    project = context.find_project(cwd)
    if project is not None:
        return project.orchestrator.name
    pkg = context.find_package(cwd)
    if pkg is not None:
        return pkg
    raise context.ProjectError(
        "not inside a known orchestrator or package; name a target (e.g. "
        "`configme config yelmox`), run `configme init`, or cd into a package "
        "or one of: "
        f"{', '.join(sorted(o.dir for o in data.orchestrators().values()))}")


def cmd_config(target, machine, compiler, *, only: bool = False,
               dry_run: bool = False) -> int:
    """(Re)generate Makefiles for the planned packages — config-only: no clone,
    no link, no extras, no build. Shares `install`'s target grammar
    (`build_plan`) and Makefile generation (`configure_makefile`); it is exactly
    the `install` configure step without the surrounding setup."""
    cwd = Path.cwd()
    if not target:
        target = _bare_target(cwd)

    plan = install.build_plan(target, only=only)
    root, _ = install.root_for(plan, None, cwd)
    project = context.find_project(root) if root.exists() else None

    machine, compiler = context.resolve_selection(machine, compiler, project,
                                                  select_fn=_select)
    machine_path, machine_tier = context.resolve_fragment("machine", machine, project)
    compiler_path, compiler_tier = context.resolve_fragment("compiler", compiler, project)
    _nudge("machine", machine, machine_tier)
    _nudge("compiler", compiler, compiler_tier)

    header = "DRY RUN — no changes will be made.\n" if dry_run else ""
    print(f"{header}configme config {target}")
    print(f"  root: {root}")
    print(f"  machine={machine}  compiler={compiler}")
    print(f"  packages: {', '.join(n.name for n in plan.nodes)}"
          f"{' (literal, no auto-resolve)' if plan.explicit else ''}")

    results = {"configured": [], "skipped": [], "failed": []}

    # --- ref reconcile: resolve each component's ref (manifest pin wins over the
    # orchestrator default) and switch its checkout *before* regenerating any
    # Makefile — the template lives inside the checkout and can differ between
    # refs. A clean branch mismatch is confirmed (default yes); a dirty or
    # declined checkout is left untouched and reported.
    install._apply_manifest_refs(plan.nodes, project)
    reconciler = install.Runner(dry_run=dry_run)
    for node in plan.nodes:
        if not node.ref or not node.clone:
            continue
        dest = install.dest_of(node, plan, root)
        if not dest.is_dir():
            continue
        try:
            st = reconciler.ensure_ref(node, dest, confirm_fn=_confirm)
        except install.InstallError as e:
            print(f"  ! {node.name}: {e}")
            results["failed"].append(node.name)
            continue
        if st == "switched":
            print(f"  ref {node.name}: switched to {node.ref}")
        elif st == "dirty":
            print(f"  ref {node.name}: dirty; not switched to {node.ref} (skipped)")
            results["skipped"].append(node.name)
        elif st == "declined":
            print(f"  ref {node.name}: kept current branch "
                  f"(declined switch to {node.ref})")

    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    for node in plan.nodes:
        dest = install.dest_of(node, plan, root)
        if node.config_style == "none":
            # Clone-only component (e.g. vilma/bgc): nothing to (re)generate.
            continue
        if node.config_style == "build.py":
            # The autotools build is a separate, slow step owned by `install`;
            # `config` only (re)generates the makefile-template subcomponent.
            print(f"  - {node.name}: build.py-style; build is a separate step "
                  f"(`configme install {node.name} --build-deps`)")
            if node.config_subdir:
                install.configure_makefile(
                    label=f"{node.name}/{node.config_subdir}", pkg_name=node.name,
                    dest=dest, config_subdir=node.config_subdir,
                    is_orchestrator=False, **common_kw)
            else:
                results["skipped"].append(node.name)
            continue
        install.configure_makefile(
            label=node.name, pkg_name=node.name, dest=dest,
            config_subdir=node.config_subdir,
            is_orchestrator=node.is_orchestrator, **common_kw)

    print("\nThis run:")
    for key in ("configured", "skipped", "failed"):
        if results[key]:
            print(f"  {key}: {', '.join(results[key])}")
    # Read-only "still pending" picture of the whole checkout — config only
    # (re)generates Makefiles, so this surfaces repos/links/builds/extras the
    # user still needs to address (see `configme status`). Skipped on a dry run.
    if not dry_run:
        block = status.pending_block(status.inspect(plan, root))
        if block:
            print(block)
    return 1 if results["failed"] else 0


def cmd_config_verb(args: argparse.Namespace) -> int:
    """`configme config [target]` — explicit verb form of in-place
    (re)configuration; does not clone, link, or build (use `configme install`
    for that)."""
    return cmd_config(args.target, args.machine, args.compiler,
                      only=args.only, dry_run=args.dry_run)


def cmd_status(args: argparse.Namespace) -> int:
    """`configme status [target]` — read-only report of what is present on disk
    and what is still pending: every repository (including optional and data
    ones), every build symlink, each declared build artifact, and each
    orchestrator extra. Reconstructs the same plan as `install`/`config` but
    makes no changes. Exit 1 if anything is a hard problem (missing/broken/
    half-built), else 0; purely-pending (deferred) items do not fail."""
    cwd = Path.cwd()
    target = args.target or _bare_target(cwd)
    plan = install.build_plan(target)
    root, _ = install.root_for(plan, args.install_dir, cwd)
    checks = status.inspect(plan, root)
    print(status.render(checks, root, plan.primary.name, verbose=args.verbose))
    return 1 if status.has_problems(checks) else 0


def cmd_netcdf(args: argparse.Namespace) -> int:
    """Detect and print the netCDF roots. With -v, also show the resolved
    include/link flags and where they came from."""
    info = netcdf.detect()
    print(f"NC_FROOT={info.nc_froot or ''}")
    print(f"NC_CROOT={info.nc_croot or ''}")
    if args.verbose:
        print(f"# source: {info.source}")
        print(f"INC_NC={info.inc_nc}")
        print(f"LIB_NC={info.lib_nc}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    orchestrators = data.orchestrators()
    packages = data.packages()
    orch = next((o for o in orchestrators.values() if o.dir == cwd.name), None)

    configme_dir = cwd / ".configme"
    configme_dir.mkdir(exist_ok=True)
    context.ensure_install_gitignore(configme_dir)
    print(f"configme init in {cwd}")
    if orch is not None:
        print(f"  recognised orchestrator: {orch.name}")
    else:
        print("  not a recognised orchestrator directory — writing a generic "
              "template (set `package` in manifest.toml).")

    # --- manifest.toml: create if missing, else validate.
    mf = configme_dir / "manifest.toml"
    if not mf.is_file():
        if orch is not None:
            mf.write_text(context.render_manifest(orch.name, orch.default_packages))
        else:
            mf.write_text(context.render_manifest(None, sorted(packages),
                                                  generic=True))
        print(f"  + wrote {mf}")
    else:
        print(f"  - {mf} exists; validating")
        try:
            project = context.find_project(cwd)
            names = context.manifest_packages(project) if project else []
            print(f"      ok: {len(names)} supported package(s) listed")
        except context.ProjectError as e:
            print(f"      ! {e}")

    # --- config.toml: create a commented template if missing, else validate.
    cf = configme_dir / "config.toml"
    if not cf.is_file():
        cf.write_text(
            "# configme local settings — uncomment and set for this checkout.\n"
            "# machine and compiler are otherwise resolved from flags, the user\n"
            "# config (~/.configme/config.toml), hostname detection, or a prompt.\n"
            f"# Available machines:  {', '.join(data.machines())}\n"
            f"# Available compilers: {', '.join(data.compilers())}\n"
            '# machine = "macbook"\n'
            '# compiler = "gfortran"\n'
        )
        print(f"  + wrote {cf}")
    else:
        print(f"  - {cf} exists; validating")
        project = context.find_project(cwd)
        cfg = context.load_config(project) if project else {}
        for key, kind in (("machine", "machine"), ("compiler", "compiler")):
            val = cfg.get(key)
            if val and val not in context.available_fragments(kind, project):
                print(f"      ! {key} '{val}' has no fragment in any tier "
                      f"(available: {', '.join(context.available_fragments(kind, project))})")
            elif val:
                print(f"      ok: {key} = {val}")

    print("Done.")
    return 0


def _install_pip_tool(name: str, *, dry_run: bool) -> int:
    """`configme install <tool>` for a tool in ``_PIP_TOOLS``: shortcut to
    ``pip install -U git+https://github.com/fesmc/<tool>``, the same URL the
    ``pip_package`` orchestrator extra uses. Other ``install`` flags
    (``-m``/``-c``/``--dir``/``--build-deps``/``-d``) do not apply and are
    ignored — this is purely a pip shellout, not a managed-package install."""
    url = f"git+https://github.com/fesmc/{name}"
    cmd = [sys.executable, "-m", "pip", "install", "-U", url]
    if dry_run:
        print("DRY RUN — no changes will be made.")
        print(f"  would run: {' '.join(cmd)}")
        return 0
    print(f"configme install {name}")
    print(f"  running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"configme: pip install {name} failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.target in _PIP_TOOLS:
        return _install_pip_tool(args.target, dry_run=args.dry_run)
    return install.run_install(
        args.target,
        download=args.download,
        install_dir=args.install_dir,
        machine=args.machine,
        compiler=args.compiler,
        overwrite=args.overwrite,
        build_deps=args.build_deps,
        dry_run=args.dry_run,
        only=args.only,
        select_fn=_select,
        ask_fn=_ask,
        confirm_fn=_confirm,
    )


def cmd_update(args: argparse.Namespace) -> int:
    """`configme update` — self-update configme by reinstalling the latest from
    its git repo with ``pip install -U``. Lets pip decide whether anything needs
    to change; we don't pre-check the installed version."""
    url = "git+https://github.com/fesmc/configme"
    cmd = [sys.executable, "-m", "pip", "install", "-U", url]
    if args.dry_run:
        print("DRY RUN — no changes will be made.")
        print(f"  would run: {' '.join(cmd)}")
        return 0
    print(f"configme update (currently {__version__})")
    print(f"  running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"configme: self-update failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_git(args: argparse.Namespace) -> int:
    """`configme git <git args...>` — run the same git command in each managed
    repository, one at a time, with a Y/n prompt before each invocation.

    The git argv is fully pass-through (no allowlist): the per-repo
    confirmation is the safety gate. With no ``--target``, operates on the
    current orchestrator/package (same resolution as ``upgrade``/``status``);
    ``--repos`` narrows the run to a comma-separated subset of managed repos."""
    target = args.target or _bare_target(Path.cwd())
    return git_mod.run_git(target, list(args.args or []),
                           repos=args.repos, yes=args.yes, confirm_fn=_confirm)


def cmd_upgrade(args: argparse.Namespace) -> int:
    """`configme upgrade [target]` — git pull existing checkouts + reconfigure
    with the same machine/compiler as before. With no target, upgrades the
    current checkout's primary (orchestrator or package) + its deps."""
    target = args.target or _bare_target(Path.cwd())
    return install.run_upgrade(
        target,
        install_dir=args.install_dir,
        machine=args.machine,
        compiler=args.compiler,
        build_deps=args.build_deps,
        dry_run=args.dry_run,
        only=args.only,
        select_fn=_select,
        confirm_fn=_confirm,
    )


# ----------------------------------------------------------------- parser

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configme",
        description="Configure the build of the yelmox / climber-x model stacks "
        "from one source of machine/compiler truth.",
        epilog="With no verb, `configme` (re)generates Makefiles for every "
        "package in the current orchestrator. Use `configme config <pkgs...>` "
        "to target specific packages, or `configme install` to clone + build.",
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"configme {__version__}")
    sub = parser.add_subparsers(dest="verb")

    p_list = sub.add_parser("list", help="list supported packages, machines, compilers")
    p_list.set_defaults(func=cmd_list)

    p_netcdf = sub.add_parser("netcdf", help="detect & print NC_FROOT / NC_CROOT")
    p_netcdf.add_argument("-v", "--verbose", action="store_true",
                          help="also print INC_NC / LIB_NC and the detection source")
    p_netcdf.set_defaults(func=cmd_netcdf)

    p_init = sub.add_parser("init", help="scaffold/validate a .configme/ folder")
    p_init.set_defaults(func=cmd_init)

    p_show = sub.add_parser(
        "show", help="print a machine/compiler fragment to stdout")
    p_show.add_argument("name", help="machine or compiler name (auto-detected)")
    grp = p_show.add_mutually_exclusive_group()
    grp.add_argument("--machine", dest="kind", action="store_const",
                     const="machine", help="treat NAME as a machine")
    grp.add_argument("--compiler", dest="kind", action="store_const",
                     const="compiler", help="treat NAME as a compiler")
    p_show.set_defaults(func=cmd_show, kind=None)

    p_new = sub.add_parser(
        "new", help="scaffold a new machine/compiler fragment from an existing one")
    p_new.add_argument("kind", choices=["machine", "compiler"])
    p_new.add_argument("name", help="name of the new fragment")
    p_new.add_argument("--from", dest="src", default=None,
                       help="seed fragment to copy (default: linux for machine, "
                       "gfortran for compiler)")
    p_new.add_argument("--force", action="store_true",
                       help="overwrite an existing fragment")
    p_new.set_defaults(func=cmd_new)

    p_config = sub.add_parser(
        "config", help="(re)generate Makefiles for already-present packages "
        "(no clone/link/build)")
    p_config.add_argument(
        "target", nargs="?", default=None,
        help="orchestrator, package, or '+'-joined literal list "
        "(e.g. yelmox, yelmo, yelmox+yelmo). Default: the current directory.")
    p_config.add_argument("-m", "--machine", default=None)
    p_config.add_argument("-c", "--compiler", default=None)
    p_config.add_argument("--only", action="store_true",
                          help="configure only the named target — do not expand "
                          "an orchestrator to its subpackages")
    p_config.add_argument("--dry-run", action="store_true")
    p_config.set_defaults(func=cmd_config_verb)

    p_status = sub.add_parser(
        "status", help="read-only: report what is present and what is still "
        "pending (repos, links, builds, extras)")
    p_status.add_argument(
        "target", nargs="?", default=None,
        help="orchestrator, package, or '+'-joined literal list "
        "(e.g. yelmox, yelmo, yelmox+yelmo). Default: the current directory.")
    p_status.add_argument("--dir", dest="install_dir", default=None)
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="show every check, including ones already ok")
    p_status.set_defaults(func=cmd_status)

    p_install = sub.add_parser(
        "install", help="clone/use-existing + configure + link a stack")
    p_install.add_argument(
        "target", help="orchestrator, package, or '+'-joined literal list "
        "(e.g. yelmox, yelmo, yelmox+yelmo)")
    p_install.add_argument("-d", "--download",
                           choices=["ssh", "https", "no"],
                           default="ssh",
                           help="how to obtain repos: ssh/https clone, or "
                           "'no' to use an existing on-disk checkout")
    p_install.add_argument("--dir", dest="install_dir", default=None)
    p_install.add_argument("-m", "--machine", default=None)
    p_install.add_argument("-c", "--compiler", default=None)
    p_install.add_argument("--only", action="store_true",
                           help="install only the named target — do not expand "
                           "an orchestrator to its subpackages or pull deps")
    p_install.add_argument("--overwrite", action="store_true")
    p_install.add_argument("--build-deps", action="store_true")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser(
        "update", help="self-update configme (pip install -U from its git repo)")
    p_update.add_argument("--dry-run", action="store_true",
                          help="print the pip command without running it")
    p_update.set_defaults(func=cmd_update)

    p_upgrade = sub.add_parser(
        "upgrade", help="git pull existing checkouts + reconfigure "
        "(and rebuild any that changed)")
    p_upgrade.add_argument(
        "target", nargs="?", default=None,
        help="orchestrator, package, or '+'-joined literal list "
        "(e.g. yelmox, yelmo, yelmox+yelmo). Default: the current directory.")
    p_upgrade.add_argument("-m", "--machine", default=None)
    p_upgrade.add_argument("-c", "--compiler", default=None)
    p_upgrade.add_argument("--dir", dest="install_dir", default=None)
    p_upgrade.add_argument("--only", action="store_true",
                           help="upgrade only the named target — do not expand "
                           "an orchestrator to its subpackages or pull deps")
    p_upgrade.add_argument("--build-deps", action="store_true",
                           help="rebuild updated packages without prompting")
    p_upgrade.add_argument("--dry-run", action="store_true")
    p_upgrade.set_defaults(func=cmd_upgrade)

    p_git = sub.add_parser(
        "git", help="run the same git command on each managed repo, with a "
        "Y/n prompt per repo")
    p_git.add_argument("--target", default=None,
                       help="orchestrator or package whose repo set to use "
                       "(default: the current directory's)")
    p_git.add_argument("--repos", default=None,
                       help="comma-separated subset of managed repo names to "
                       "operate on (default: all)")
    p_git.add_argument("-y", "--yes", action="store_true",
                       help="skip the per-repo Y/n prompt (one-way). Read-only "
                       "git verbs (status, log, diff, fetch, ...) auto-confirm "
                       "anyway; this extends the same to any verb.")
    # REMAINDER captures everything after the first non-option token verbatim,
    # so git's own flags (`--ff-only`, `-s`, `--decorate`) reach git intact and
    # don't get eaten by configme's parser.
    p_git.add_argument("args", nargs=argparse.REMAINDER,
                       help="git subcommand and its arguments (e.g. status, "
                       "pull --ff-only, log -1 --decorate)")
    p_git.set_defaults(func=cmd_git)

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # Bare form: `configme [-m M] [-c C]` (no verb, no targets) configures the
    # current orchestrator. It is selected unless the first token is a known verb
    # or a top-level flag (-h/-V), so `configme -m macbook` routes here while
    # `configme list` / `configme --help` go to the verb parser. Naming packages
    # without a verb (`configme yelmox`) is redirected to `configme config`.
    top_flags = {"-h", "--help", "-V", "--version"}
    is_bare = not (argv and (argv[0] in VERBS or argv[0] in top_flags))
    try:
        if is_bare:
            cfg = argparse.ArgumentParser(prog="configme")
            cfg.add_argument("targets", nargs="*")
            cfg.add_argument("-m", "--machine", default=None)
            cfg.add_argument("-c", "--compiler", default=None)
            cfg.add_argument("--only", action="store_true")
            cfg.add_argument("--dry-run", action="store_true")
            a = cfg.parse_args(argv)
            if a.targets:
                target_str = " ".join(a.targets)
                cwd = Path.cwd()
                managed = (context.find_project(cwd) is not None
                           or context.find_package(cwd) is not None)
                if managed:
                    raise context.ProjectError(
                        "to configure specific packages, use `configme config "
                        f"{target_str}` (bare `configme` configures the current "
                        "directory; `configme install` clones + builds).")
                raise context.ProjectError(
                    "current directory is not managed by configme. To clone and "
                    f"build '{target_str}' here, run `configme install {target_str}`.")
            return cmd_config(None, a.machine, a.compiler,
                              only=a.only, dry_run=a.dry_run)

        args = parser.parse_args(argv)
        if getattr(args, "verb", None) is None:
            return cmd_config(None, None, None)
        return args.func(args)
    except NotImplementedYet as e:
        _report_pending(e)
        return 2
    except (data.DataError, context.ProjectError, generate.GenerateError,
            install.InstallError, netcdf.NetcdfError, git_mod.GitError) as e:
        print(f"configme: {e}", file=sys.stderr)
        return 1


def _report_pending(e: NotImplementedYet) -> None:
    print(f"configme: {e.what} is not implemented yet.", file=sys.stderr)
    print(f"  Tracked in issue #{e.issue} "
          f"(https://github.com/fesmc/configme/issues/{e.issue}).",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
