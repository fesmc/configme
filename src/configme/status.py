"""`configme status`: read-only inspection of an installed stack.

Where ``install`` reports what a run just *did*, ``status`` answers what is true
on disk *now* — independent of any run. It reconstructs the expected world the
same way ``install`` does (the checkout's manifest names the primary ->
``build_plan`` -> ``root_for``) and probes the disk, reporting per-component
state across four categories:

  repo    each cloned component (incl. data_packages) is a real git checkout
  link    each inter-component build symlink resolves
  build   each declared ``[package.artifacts]`` file exists (per variant)
  extra   each orchestrator extra (data_link / runme_config) is present

Generality comes from driving every check off the same registry metadata that
drives ``install`` — not a separate hand-maintained checklist — so a new
package/orchestrator/extra is covered automatically.

The inspection is **pure**: no clones, no Makefile regeneration, no config
writes, no prompts. The same checks are appended as a compact "still pending"
block to the ``install`` and ``config`` summaries (see ``pending_block``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from configme import color, data, install

# "ok" is the only good state; everything else is reported.
#   missing  a required component/link that should be present is not
#   broken   a symlink that dangles (its target does not exist)
#   partial  some but not all artifacts of a build variant exist (half-built)
#   pending  a soft/deferred absence: an optional or data repo not cloned, a
#            build not started, an extra not yet set up — intended, not an error
_OK = "ok"
# States that signal a genuine problem (non-zero exit). ``pending`` is excluded:
# it is an intended/deferred gap, not a failure.
_HARD = {"missing", "broken", "partial"}

_CATEGORY_TITLES = [
    ("repo", "Repositories"),
    ("link", "Links"),
    ("build", "Builds"),
    ("extra", "Extras"),
]

_MARK = {
    "ok": "ok     ",
    "missing": "MISSING",
    "broken": "broken ",
    "partial": "partial",
    "pending": "pending",
}

# ANSI attributes for each state's ``[label]`` in a rendered row. ``ok`` is
# green, the hard problems red (``missing`` also bold — it's the loudest), the
# half-states yellow, and the deferred ``pending`` a calm cyan.
_STATE_STYLE = {
    "ok": ("green",),
    "missing": ("red", "bold"),
    "broken": ("red",),
    "partial": ("yellow",),
    "pending": ("cyan",),
}


@dataclass
class Check:
    """One inspected fact about the stack on disk."""
    category: str   # "repo" | "link" | "build" | "extra"
    name: str       # component / link / artifact label
    state: str      # see the state vocabulary above
    detail: str = ""  # short human note (e.g. "optional; not cloned")
    hint: str = ""    # a command the user can run to resolve a non-ok state


# --------------------------------------------------------------- probing

def _is_checkout(dest: Path) -> bool:
    """True if ``dest`` holds a git checkout. ``.git`` is a directory for a
    normal clone and a file for a submodule/worktree, so probe its existence
    rather than its type."""
    return (dest / ".git").exists()


def inspect(plan, root: Path) -> List[Check]:
    """Probe the disk for the state of every component in ``plan`` rooted at
    ``root``. Read-only. Returns checks grouped by category in a stable order:
    repos, links, builds, extras."""
    pkgs = data.packages()
    checks: List[Check] = []
    checks += _inspect_repos(plan, root)
    checks += _inspect_links(plan, root, pkgs)
    checks += _inspect_builds(plan, root, pkgs)
    checks += _inspect_extras(plan, root)
    return checks


def _inspect_repos(plan, root: Path) -> List[Check]:
    """Each cloned component is present as a real git checkout. A subpackage
    rides its parent's checkout, so it is not a repo of its own and is skipped
    here (its build is still probed in ``_inspect_builds``)."""
    out: List[Check] = []
    for node in plan.nodes:
        if not node.clone:
            continue
        dest = install.dest_of(node, plan, root)
        if _is_checkout(dest):
            out.append(Check("repo", node.name, _OK))
        elif node.clone_policy in ("optional", "prompt"):
            detail = ("optional; not cloned" if node.clone_policy == "optional"
                      else "not cloned (opt-in)")
            out.append(Check("repo", node.name, "pending",
                             detail=detail,
                             hint=f"configme install {node.name}"))
        else:
            out.append(Check("repo", node.name, "missing",
                             hint=f"configme install {node.name}"))
    return out


def _inspect_links(plan, root: Path, pkgs) -> List[Check]:
    """Each non-primary package's build symlinks resolve. Mirrors the resolution
    in ``install``'s link phase, but only reads: a present symlink whose target
    exists is ``ok``; a dangling one is ``broken``; an absent one is ``missing``
    when its dependency is on disk (re-link), else ``pending`` (install the dep
    first)."""
    out: List[Check] = []
    relink = f"configme install {plan.primary.name}"
    present_dirs = {n.name: install.dest_of(n, plan, root) for n in plan.nodes}
    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        node_root = install.dest_of(node, plan, root)
        if not node_root.exists():
            continue  # package not present; its links are not relevant yet
        for link in node.links:
            dep_dest = present_dirs.get(link.dep)
            if dep_dest is None and link.dep in pkgs:
                sub = (plan.orchestrator.component_paths.get(
                       link.dep, pkgs[link.dep].dir)
                       if plan.orchestrator else pkgs[link.dep].dir)
                dep_dest = root / sub
            link_path = node_root / link.path
            dep_present = dep_dest is not None and dep_dest.exists()
            label = f"{node.name}/{link.path} -> {link.dep}"
            if link_path.is_symlink():
                if link_path.resolve().exists():
                    out.append(Check("link", label, _OK))
                else:
                    out.append(Check("link", label, "broken",
                                     detail="symlink target missing",
                                     hint=(relink if dep_present
                                           else f"configme install {link.dep}")))
            elif link_path.exists():
                out.append(Check("link", label, _OK))  # a real dir/file is there
            elif dep_present:
                out.append(Check("link", label, "missing",
                                 detail="dependency present but not linked",
                                 hint=relink))
            else:
                out.append(Check("link", label, "pending",
                                 detail=f"dependency '{link.dep}' not present",
                                 hint=f"configme install {link.dep}"))
    return out


def _inspect_builds(plan, root: Path, pkgs) -> List[Check]:
    """Each declared build artifact exists, per variant. A package with no
    ``[package.artifacts]`` is not reported. A variant with all artifacts present
    is ``ok``; none present is ``pending`` (not built yet — possibly deferred on
    purpose); some present is ``partial`` (a genuinely incomplete build)."""
    out: List[Check] = []
    for node in plan.nodes:
        pkg = pkgs.get(node.name)
        if pkg is None or not pkg.artifacts:
            continue
        dest = install.dest_of(node, plan, root)
        if not dest.exists():
            continue  # a missing checkout is already reported as a repo problem
        # A subpackage's build runs as part of its parent's install.
        owner = node.name if node.clone else (node.parent or node.name)
        hint = f"configme install {owner} --build-deps"
        for variant, paths in pkg.artifacts.items():
            present = [p for p in paths if (dest / p).exists()]
            label = f"{node.name} ({variant})"
            if len(present) == len(paths):
                out.append(Check("build", label, _OK))
            elif not present:
                out.append(Check("build", label, "pending",
                                 detail="not built", hint=hint))
            else:
                missing = [p for p in paths if p not in present]
                out.append(Check("build", label, "partial",
                                 detail=f"missing {', '.join(missing)}",
                                 hint=hint))
    return out


def _inspect_extras(plan, root: Path) -> List[Check]:
    """Each orchestrator extra is present on disk. ``data_link``/``runme_config``
    are probed; ``pip_package`` is skipped because an installed pip package
    cannot be reliably probed from the checkout. (Auxiliary/data repos are not
    extras — they are packages, probed by ``_inspect_repos``.)"""
    out: List[Check] = []
    orch = plan.orchestrator
    if orch is None or not orch.extras:
        return out
    install_hint = f"configme install {orch.name}"
    for name, value in orch.extras.items():
        if name == "data_link":
            for label in (value if isinstance(value, list) else [value]):
                link_path = root / label
                if link_path.is_symlink() or link_path.exists():
                    out.append(Check("extra", f"data_link {label}", _OK))
                else:
                    # A data link points at a site-specific path configme can't
                    # know, so the resolving hint is the bare symlink command to
                    # run by hand, not a `configme install` re-run.
                    out.append(Check("extra", f"data_link {label}", "pending",
                                     detail="not linked",
                                     hint=f"ln -s /path/to/{label} {link_path}"))
        elif name == "runme_config":
            if value:
                if (root / ".runme" / "config.toml").exists():
                    out.append(Check("extra", "runme_config", _OK))
                else:
                    out.append(Check("extra", "runme_config", "pending",
                                     detail=".runme/config.toml not created",
                                     hint=install_hint))
    return out


# --------------------------------------------------------------- reporting

def has_problems(checks: List[Check]) -> bool:
    """True if any check is in a hard-problem state (missing/broken/partial).
    ``pending`` items are deliberately not problems."""
    return any(c.state in _HARD for c in checks)


def _format_row(c: Check) -> str:
    mark = color.style(_MARK.get(c.state, c.state), *_STATE_STYLE.get(c.state, ()))
    s = f"[{mark}] {c.name}"
    if c.detail:
        s += f"  ({c.detail})"
    if c.state != _OK and c.hint:
        s += f"  -> {color.hint(c.hint)}"
    return s


def _commands(checks: List[Check]) -> List[str]:
    """The de-duplicated, order-preserving list of resolving commands from every
    non-ok check — the 'run these by hand' footer."""
    out: List[str] = []
    for c in checks:
        if c.state != _OK and c.hint and c.hint not in out:
            out.append(c.hint)
    return out


def render(checks: List[Check], root: Path, primary: str, *,
           verbose: bool = False) -> str:
    """Full ``configme status`` report. By default only non-ok rows are shown
    (a fully-ok category collapses to a one-line note); ``verbose`` shows every
    row. A footer lists the commands to resolve anything outstanding."""
    lines = [color.header(f"configme status: {primary} at {root}")]
    for cat, title in _CATEGORY_TITLES:
        rows = [c for c in checks if c.category == cat]
        if not rows:
            continue
        shown = rows if verbose else [c for c in rows if c.state != _OK]
        if not shown:
            lines.append(f"\n  {title}: {color.ok(f'all present ({len(rows)})')}")
            continue
        lines.append(f"\n  {title}:")
        lines += ["    " + _format_row(c) for c in shown]

    problems = [c for c in checks if c.state in _HARD]
    pending = [c for c in checks if c.state == "pending"]
    lines.append("")
    if not problems and not pending:
        lines.append(color.ok("Everything present and built."))
    else:
        bits = []
        if problems:
            bits.append(color.err(f"{len(problems)} problem(s)"))
        if pending:
            bits.append(color.warn(f"{len(pending)} pending"))
        lines.append("Summary: " + ", ".join(bits) + ".")

    cmds = _commands(checks)
    if cmds:
        lines.append("\nRun these when ready:")
        lines += [f"  {c}" for c in cmds]
    return "\n".join(lines)


def pending_block(checks: List[Check]) -> str:
    """A compact 'still pending' block for the ``install``/``config`` summary:
    every non-ok check plus the commands to resolve them. Returns an empty
    string when the disk is fully in order (nothing to append)."""
    notok = [c for c in checks if c.state != _OK]
    if not notok:
        return ""
    lines = ["\nCurrent status (configme status):"]
    for cat, title in _CATEGORY_TITLES:
        rows = [c for c in notok if c.category == cat]
        lines += ["  " + _format_row(c) for c in rows]
    cmds = _commands(checks)
    if cmds:
        lines.append("\nRun these when ready:")
        lines += [f"  {c}" for c in cmds]
    return "\n".join(lines)
