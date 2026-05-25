"""Command-line interface for configme.

Command surface (see docs/DESIGN.md sec. 4)::

    configme install <target> [options]   clone/use-existing + configure + link
    configme [pkgs...]                     config-only: (re)generate Makefile(s)
    configme netcdf                        detect & print NC_FROOT / NC_CROOT
    configme init                          scaffold/validate a .configme/ folder
    configme list                          supported packages / machines / compilers

`list`, `netcdf`, `init`, and the config-only form are implemented. `install`
is still stubbed with a pointer to its tracking issue.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from configme import __version__, context, data, generate, install, netcdf

# Verbs handled by subparsers. Anything else in first position is treated as a
# config-only package target (the `configme [pkgs...]` form).
VERBS = {"install", "netcdf", "init", "list"}


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
            print(f"  {name:14s} {p.org}/{p.repo}  [{p.config_style}]")
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


def _nudge(kind: str, name: str, tier: str) -> None:
    """Advisory-only: suggest contributing a fragment that is defined locally
    but absent from the shipped registry. No network/gh coupling."""
    if tier != "shipped" and context.is_locally_defined_only(kind, name):
        print(f"  note: {kind} '{name}' is defined locally ({tier} tier) but is "
              f"not in the central configme registry.")
        print(f"        Consider contributing it: "
              f"https://github.com/fesmc/configme (PR or issue).")


def _config_targets(targets, project, cwd):
    """Resolve the list of (name, root, config_subdir, config_style) to build.

    With explicit targets, resolve each against cwd/project. With none, expand
    the orchestrator: its own Makefile plus every manifest package present."""
    orchestrators = data.orchestrators()
    packages = data.packages()

    def resolve_one(name):
        if name in orchestrators:
            dirname, subdir, style = orchestrators[name].dir, "", orchestrators[name].config_style
        elif name in packages:
            p = packages[name]
            dirname, subdir, style = p.dir, p.config_subdir, p.config_style
        else:
            known = sorted(set(orchestrators) | set(packages))
            raise context.ProjectError(
                f"unknown target '{name}'. Supported: {', '.join(known)}")
        # Prefer the project root when set; else cwd-relative or cwd itself.
        base = project.root if project is not None else cwd
        if (base / dirname).is_dir():
            return name, base / dirname, subdir, style
        if base.name == dirname:
            return name, base, subdir, style
        raise context.ProjectError(
            f"could not find '{name}' at {base / dirname} or as the current "
            f"directory. Clone it (configme install) or run from the right place.")

    if targets:
        return [resolve_one(n) for n in targets]

    if project is None:
        raise context.ProjectError(
            "not inside a known orchestrator; name a target (e.g. `configme "
            "yelmox`), run `configme init`, or cd into one of: "
            f"{', '.join(sorted(o.dir for o in orchestrators.values()))}")

    # Bare form: the orchestrator's own Makefile + each manifest package present.
    items = [(project.orchestrator.name, project.root, "",
              project.orchestrator.config_style)]
    for name in context.manifest_packages(project):
        p = packages[name]
        root = project.root / p.dir
        if not root.exists():
            print(f"  - {name}: not present under {project.root} "
                  f"(clone via `configme install`); skipping")
            continue
        items.append((name, root, p.config_subdir, p.config_style))
    return items


def cmd_config(targets, machine, compiler) -> int:
    cwd = Path.cwd()
    project = context.find_project(cwd)

    items = _config_targets(targets, project, cwd)
    machine, compiler = context.resolve_selection(machine, compiler, project,
                                                  prompt_fn=_prompt_choice)

    machine_path, machine_tier = context.resolve_fragment("machine", machine, project)
    compiler_path, compiler_tier = context.resolve_fragment("compiler", compiler, project)
    _nudge("machine", machine, machine_tier)
    _nudge("compiler", compiler, compiler_tier)

    print(f"Configuring (machine={machine}, compiler={compiler}):")
    n_ok = 0
    for name, root, subdir, style in items:
        if style == "build.py":
            print(f"  - {name}: build.py-style package; not a Makefile target "
                  f"(use `configme install --build-deps`); skipping")
            continue
        cfgroot = root / subdir if subdir else root
        if generate.find_repo_file(cfgroot, "common.mk") is None and name != project_orch_name(project):
            # makefile-template package not yet migrated to a common.mk.
            print(f"  - {name}: no common.mk found (not migrated yet); skipping "
                  f"(legacy fallback tracked in #9)")
            continue
        out = generate.generate_makefile(root, machine, compiler,
                                         machine_path, compiler_path, subdir)
        print(f"  + {name}: wrote {out}")
        n_ok += 1
    print(f"Done: {n_ok} Makefile(s) generated.")
    return 0


def project_orch_name(project):
    return project.orchestrator.name if project is not None else None


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
        prompt_fn=_prompt_choice,
    )


# ----------------------------------------------------------------- parser

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configme",
        description="Configure the build of the yelmox / climber-x model stacks "
        "from one source of machine/compiler truth.",
        epilog="With no verb, `configme [pkgs...]` (re)generates Makefiles for the "
        "named packages (or all packages in the current orchestrator).",
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
    p_install.add_argument("--overwrite", action="store_true")
    p_install.add_argument("--build-deps", action="store_true")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=cmd_install)

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # Config-only form: `configme [targets...] [-m M] [-c C]`. It is selected
    # unless the first token is a known verb or a top-level flag (-h/-V). This
    # way both `configme yelmox ...` and `configme -m macbook ...` route here,
    # while `configme list` / `configme --help` go to the verb parser.
    top_flags = {"-h", "--help", "-V", "--version"}
    is_config_only = not (argv and (argv[0] in VERBS or argv[0] in top_flags))
    try:
        if is_config_only:
            cfg = argparse.ArgumentParser(prog="configme")
            cfg.add_argument("targets", nargs="*")
            cfg.add_argument("-m", "--machine", default=None)
            cfg.add_argument("-c", "--compiler", default=None)
            a = cfg.parse_args(argv)
            return cmd_config(a.targets, a.machine, a.compiler)

        args = parser.parse_args(argv)
        if getattr(args, "verb", None) is None:
            return cmd_config([], None, None)
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
