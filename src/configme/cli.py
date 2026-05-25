"""Command-line interface for configme.

Command surface (see docs/DESIGN.md sec. 4)::

    configme install <target> [options]   clone/use-existing + configure + link
    configme [pkgs...]                     config-only: (re)generate Makefile(s)
    configme netcdf                        detect & print NC_FROOT / NC_CROOT
    configme init                          scaffold/validate a .configme/ folder
    configme list                          supported packages / machines / compilers

Only `list` is implemented in this first slice (issue #1). The other verbs are
present so the surface and help are stable, but exit with a clear pointer to the
issue that implements them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from configme import __version__, data, generate, netcdf

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
        raise generate.GenerateError(
            f"no {label} given; pass --{label[:7]} "
            f"(available: {', '.join(options) or 'none'})"
        )
    print(f"Available {label}s: {', '.join(options) or '(none)'}")
    while True:
        ans = input(f"  {label}: ").strip()
        if ans:
            return ans


def _resolve_selection(machine, compiler):
    """Resolve machine + compiler from flags, prompting on a TTY otherwise.

    (The richer precedence — orchestrator/user config, hostname detection —
    arrives with the .configme contract in issue #5.)"""
    if not machine:
        machine = _prompt_choice("machine", data.machines())
    if not compiler:
        compiler = _prompt_choice("compiler", data.compilers())
    return machine, compiler


def _resolve_target_root(name: str, cwd: Path):
    """Map a target name to (root_path, config_subdir).

    The root is the target's directory if it sits under cwd, or cwd itself when
    cwd already *is* that directory (so configme works both from a parent and
    from inside the repo)."""
    orchestrators = data.orchestrators()
    packages = data.packages()
    if name in orchestrators:
        dirname, subdir = orchestrators[name].dir, ""
    elif name in packages:
        dirname, subdir = packages[name].dir, packages[name].config_subdir
    else:
        known = sorted(set(orchestrators) | set(packages))
        raise generate.GenerateError(
            f"unknown target '{name}'. Supported: {', '.join(known)}"
        )
    if (cwd / dirname).is_dir():
        return cwd / dirname, subdir
    if cwd.name == dirname:
        return cwd, subdir
    raise generate.GenerateError(
        f"could not find '{name}' at {cwd / dirname} or as the current directory. "
        f"Run from its parent directory or from inside it."
    )


def cmd_config(targets, machine, compiler) -> int:
    cwd = Path.cwd()

    # With no explicit target, configure the orchestrator at the current dir.
    # (Iterating the full manifest of packages is issue #5.)
    if not targets:
        orchestrators = data.orchestrators()
        match = next((o for o in orchestrators.values() if o.dir == cwd.name), None)
        if match is None:
            raise generate.GenerateError(
                "not inside a known orchestrator; name a target "
                f"(e.g. `configme yelmox`) or run from one of: "
                f"{', '.join(sorted(o.dir for o in orchestrators.values()))}"
            )
        targets = [match.name]

    machine, compiler = _resolve_selection(machine, compiler)

    for name in targets:
        root, subdir = _resolve_target_root(name, cwd)
        out = generate.generate_makefile(root, machine, compiler, subdir)
        print(f"  + {name}: wrote {out}  (machine={machine}, compiler={compiler})")
    return 0


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
    _pending("`configme init` scaffolding", issue=5)


def cmd_install(args: argparse.Namespace) -> int:
    _pending(f"`configme install {args.target}`", issue=6)


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
    except (data.DataError, generate.GenerateError, netcdf.NetcdfError) as e:
        print(f"configme: {e}", file=sys.stderr)
        return 1


def _report_pending(e: NotImplementedYet) -> None:
    print(f"configme: {e.what} is not implemented yet.", file=sys.stderr)
    print(f"  Tracked in issue #{e.issue} "
          f"(https://github.com/fesmc/configme/issues/{e.issue}).",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
