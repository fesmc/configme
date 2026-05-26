"""Command-line interface for configme.

Command surface (see docs/DESIGN.md sec. 4)::

    configme install <target> [options]   clone/use-existing + configure + link + build
    configme config [<target>] [options]  config-only: (re)generate Makefile(s)
    configme [-m M] [-c C]                alias for `configme config` on the current dir
    configme netcdf                        detect & print NC_FROOT / NC_CROOT
    configme init                          scaffold/validate a .configme/ folder
    configme list                          supported packages / machines / compilers

`install` and `config` share one target grammar (`install.build_plan`) and one
Makefile-generation path (`install.configure_makefile`); `config` is the
`install` configure step without clone / link / extras / build.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from configme import __version__, context, data, generate, install, netcdf

# Verbs handled by subparsers. With no verb, bare `configme [-m M] [-c C]`
# configures the current orchestrator; name packages with `configme config`.
VERBS = {"install", "config", "netcdf", "init", "list"}


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

    print("\nMachines: " + (", ".join(machines) if machines else "(none)"))
    print("Compilers: " + (", ".join(compilers) if compilers else "(none)"))
    return 0


# ----------------------------------------------------------- stubbed verbs

def _prompt_choice(label: str, options) -> str:
    """Prompt for a value, showing available options. TTY only."""
    if not sys.stdin.isatty():
        raise context.ProjectError(
            f"no {label} given; pass --{label[:1]} / --{label} "
            f"(available: {', '.join(options) or 'none'})"
        )
    print(f"Available {label}s: {', '.join(options) or '(none)'}")
    while True:
        ans = input(f"  {label}: ").strip()
        if ans:
            return ans


def _ask(label: str, default: "str | None" = None) -> "str | None":
    """Free-text prompt for extras values; returns None when non-interactive
    (so extras degrade to 'pending' rather than blocking). The default that
    applies when the user just presses Enter is always shown — '(None)' when
    there is no default."""
    if not sys.stdin.isatty():
        return default
    shown = default if default is not None else "None"
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
                                                  prompt_fn=_prompt_choice)
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
    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    for node in plan.nodes:
        dest = install.dest_of(node, plan, root)
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

    print("\nSummary:")
    for key in ("configured", "skipped", "failed"):
        if results[key]:
            print(f"  {key}: {', '.join(results[key])}")
    return 1 if results["failed"] else 0


def cmd_config_verb(args: argparse.Namespace) -> int:
    """`configme config [target]` — explicit verb form of in-place
    (re)configuration; does not clone, link, or build (use `configme install`
    for that)."""
    return cmd_config(args.target, args.machine, args.compiler,
                      only=args.only, dry_run=args.dry_run)


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


def _render_manifest(orch_name, packages_list, generic: bool) -> str:
    pkgs = "".join(f'    "{p}",\n' for p in packages_list)
    if generic:
        supported = ", ".join(sorted(data.orchestrators()))
        return (
            "# configme manifest. Set the orchestrator and prune the package list.\n"
            f"# Supported orchestrators: {supported}\n"
            '# orchestrator = "yelmox"\n'
            "packages = [\n" + pkgs + "]\n"
        )
    return (
        f"# configme manifest for {orch_name}.\n"
        f'orchestrator = "{orch_name}"\n'
        "packages = [\n" + pkgs + "]\n"
    )


def cmd_init(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    orchestrators = data.orchestrators()
    packages = data.packages()
    orch = next((o for o in orchestrators.values() if o.dir == cwd.name), None)

    configme_dir = cwd / ".configme"
    configme_dir.mkdir(exist_ok=True)
    print(f"configme init in {cwd}")
    if orch is not None:
        print(f"  recognised orchestrator: {orch.name}")
    else:
        print("  not a recognised orchestrator directory — writing a generic "
              "template (set `orchestrator` in manifest.toml).")

    # --- manifest.toml: create if missing, else validate.
    mf = configme_dir / "manifest.toml"
    if not mf.is_file():
        if orch is not None:
            mf.write_text(_render_manifest(orch.name, orch.default_packages, False))
        else:
            mf.write_text(_render_manifest(None, sorted(packages), True))
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


def cmd_install(args: argparse.Namespace) -> int:
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
        prompt_fn=_prompt_choice,
        ask_fn=_ask,
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

    p_install = sub.add_parser(
        "install", help="clone/use-existing + configure + link a stack")
    p_install.add_argument(
        "target", help="orchestrator, package, or '+'-joined literal list "
        "(e.g. yelmox, yelmo, yelmox+yelmo)")
    p_install.add_argument("-d", "--download",
                           choices=["clone-ssh", "clone-https", "no"],
                           default="clone-ssh")
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
                raise context.ProjectError(
                    "to configure specific packages, use `configme config "
                    f"{' '.join(a.targets)}` (bare `configme` configures the "
                    "current directory; `configme install` clones + builds).")
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
            install.InstallError, netcdf.NetcdfError) as e:
        print(f"configme: {e}", file=sys.stderr)
        return 1


def _report_pending(e: NotImplementedYet) -> None:
    print(f"configme: {e.what} is not implemented yet.", file=sys.stderr)
    print(f"  Tracked in issue #{e.issue} "
          f"(https://github.com/fesmc/configme/issues/{e.issue}).",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
