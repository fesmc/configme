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

from configme import __version__, data

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

def cmd_config(targets) -> int:
    scope = ", ".join(targets) if targets else "all packages in the current orchestrator"
    _pending(
        f"config-only Makefile generation ({scope})",
        issue=3,
    )


def cmd_netcdf(args: argparse.Namespace) -> int:
    _pending("netCDF detection (`configme netcdf`)", issue=2)


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

    # Config-only shortcut: a leading token that is neither a flag nor a known
    # verb means `configme [pkgs...]` — (re)configure those packages.
    if argv and not argv[0].startswith("-") and argv[0] not in VERBS:
        try:
            return cmd_config(argv)
        except NotImplementedYet as e:
            _report_pending(e)
            return 2

    args = parser.parse_args(argv)

    try:
        if getattr(args, "verb", None) is None:
            # Bare `configme` => config-only across the current orchestrator.
            return cmd_config([])
        return args.func(args)
    except NotImplementedYet as e:
        _report_pending(e)
        return 2
    except data.DataError as e:
        print(f"configme: {e}", file=sys.stderr)
        return 1


def _report_pending(e: NotImplementedYet) -> None:
    print(f"configme: {e.what} is not implemented yet.", file=sys.stderr)
    print(f"  Tracked in issue #{e.issue} "
          f"(https://github.com/fesmc/configme/issues/{e.issue}).",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
