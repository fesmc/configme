"""`configme git`: fan-out a git command across each managed repository.

Sibling of `install` / `upgrade` — reuses the same plan + root + ``dest_of``
machinery so the repo set matches what those verbs see. The git command itself
is opaque: anything ``git`` accepts is forwarded verbatim. The safety story is
the per-repo Y/n prompt that wraps every invocation, plus a final summary that
lists each repo's command (skipped ones commented out) so the user can copy a
declined or failed step and run it by hand.

Repo selection mirrors ``install``/``upgrade``: every plan node whose ``clone``
flag is set and whose checkout exists on disk. Component subpackages that ride
a parent's checkout (e.g. ``yelmo/fesm-utils`` under a yelmox install) have no
git history of their own and are skipped — they share their parent's tree.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from configme import color, install


class GitError(Exception):
    pass


# Git subcommands that never mutate the working tree and (with the exception of
# ``fetch``, which writes remote-tracking refs in ``.git/``) don't change the
# repository state in a way that needs reviewing per-repo. Invocations whose
# first token is in this set skip the Y/n prompt by default; ``--yes`` extends
# the same skip to any verb. Classification is by subcommand name only — args
# are NOT inspected, so this stays predictable across forms like
# ``log --since=...`` or ``diff HEAD~3``.
READ_ONLY_VERBS = frozenset({
    "status", "log", "diff", "show", "fetch", "blame", "grep", "shortlog",
    "describe", "rev-parse", "ls-files", "ls-tree",
})


def _enumerate_repos(plan, root: Path) -> List[Tuple[str, Path]]:
    """Return ``[(name, dest), ...]`` for every plan node that has its own
    on-disk checkout. Order follows ``plan.nodes`` (deterministic and matches
    the order the user sees in ``install``/``upgrade``)."""
    out: List[Tuple[str, Path]] = []
    for node in plan.nodes:
        if not node.clone:
            continue
        dest = install.dest_of(node, plan, root)
        if dest.is_dir() and (dest / ".git").exists():
            out.append((node.name, dest))
    return out


def _shell(cmd: Iterable[str]) -> str:
    """Render an argv list as a copy-pasteable shell command."""
    return " ".join(shlex.quote(a) for a in cmd)


def _current_ref(dest: Path) -> str:
    """Best-effort name of the currently checked-out ref for ``dest``: the
    branch name on a normal checkout, ``detached@<short-sha>`` when HEAD is
    detached, or ``?`` if git can't be queried. Used purely to label output so
    the user always sees which version of a repo a command is hitting."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dest, capture_output=True, text=True)
    except OSError:
        return "?"
    if res.returncode != 0:
        return "?"
    ref = res.stdout.strip()
    if ref and ref != "HEAD":
        return ref
    # Detached HEAD (or empty): fall back to the short commit SHA.
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=dest, capture_output=True, text=True)
    except OSError:
        return "detached"
    short = sha.stdout.strip() if sha.returncode == 0 else ""
    return f"detached@{short}" if short else "detached"


def _parse_repos(spec: Optional[str]) -> Optional[List[str]]:
    if spec is None:
        return None
    names = [s.strip() for s in spec.split(",") if s.strip()]
    return names or None


def run_git(target: Optional[str], git_args: List[str], *,
            repos: Optional[str], yes: bool, confirm_fn) -> int:
    """Drive ``configme git``: build the plan, enumerate repos, print the
    command once, then prompt Y/n per repo and run sequentially. Continues past
    failures and exits 1 if any repo's git call returned non-zero.

    ``target`` may be None (caller defaults to the current orchestrator/package
    via ``_bare_target``). ``git_args`` is the verbatim argv that follows
    ``configme git`` — first element is the git subcommand, the rest are its
    own flags/arguments. No validation, no allowlist: per-repo confirmation is
    the safety net.

    ``repos`` is an optional comma-separated filter (``--repos pkg1,pkg2``)
    that narrows the run to a subset of the managed repo set; unknown names
    fail fast with the available list.

    ``yes`` skips the per-repo confirmation prompt (one-way override). The
    prompt is also skipped automatically when the git subcommand is in
    ``READ_ONLY_VERBS`` — the common read-only fan-outs (``status``, ``log``,
    ``diff``, ``fetch``, ...) run unattended unless the user is in the loop
    for some other reason."""
    if not git_args:
        raise GitError(
            "missing git subcommand. Example: `configme git status` "
            "or `configme git pull --ff-only`.")

    cwd = Path.cwd()
    plan = install.build_plan(target)
    root, _ = install.root_for(plan, None, cwd)
    if not root.exists():
        raise GitError(
            f"{root} does not exist — nothing to operate on. Run "
            f"`configme install {plan.primary.name}` first.")

    available = _enumerate_repos(plan, root)
    if not available:
        color.cprint(f"configme git: no managed git checkouts under {root}.")
        return 0

    wanted = _parse_repos(repos)
    if wanted is not None:
        # Each --repos entry is a package name or a path to where the checkout
        # lives; resolve against the present checkouts (the repos git can act
        # on). Unknown entries raise (InstallError, caught alongside GitError).
        selected_names = install.resolve_repo_selectors(wanted, available)
        selected = [(n, d) for n, d in available if n in selected_names]
    else:
        selected = available

    cmd = ["git"] + list(git_args)
    auto_yes = yes or git_args[0] in READ_ONLY_VERBS
    reason = ("--yes" if yes
              else f"{git_args[0]!r} is read-only" if auto_yes else None)
    color.cprint(color.header(f"configme git — {len(selected)} repo(s) under {root}:"))
    color.cprint(f"  $ {_shell(cmd)}")
    if auto_yes:
        color.cprint(f"  (auto-confirm: {reason})")
    color.cprint()

    ran: List[Tuple[str, Path]] = []
    skipped: List[Tuple[str, Path]] = []
    failed: List[Tuple[str, Path, int]] = []

    for name, dest in selected:
        # Stamp the current ref onto the label (e.g. ``fesm-utils:main``) so it
        # is always clear which checked-out version the command is hitting —
        # read just before running, so it reflects the tree the command acts on.
        disp = f"{name}:{_current_ref(dest)}"
        try:
            rel = dest.relative_to(root)
            label = f"{disp} ({rel})" if str(rel) != "." else disp
        except ValueError:
            label = f"{disp} ({dest})"
        if not auto_yes and not confirm_fn(f"run in {label}?", True):
            color.cprint(f"  - {disp}: skipped")
            skipped.append((disp, dest))
            continue
        color.cprint(f"  > {disp}: (cd {dest} && {_shell(cmd)})")
        try:
            res = subprocess.run(cmd, cwd=dest)
        except OSError as e:
            color.cprint(f"  ! {disp}: failed to launch git ({e})")
            failed.append((disp, dest, -1))
            continue
        if res.returncode != 0:
            color.cprint(f"  ! {disp}: git exited {res.returncode}")
            failed.append((disp, dest, res.returncode))
        else:
            ran.append((disp, dest))

    color.cprint()
    color.cprint(f"Summary: ran={len(ran)}  skipped={len(skipped)}  failed={len(failed)}")
    if ran or skipped or failed:
        color.cprint("Commands:")
        for disp, dest in ran:
            color.cprint(f"  (cd {dest} && {_shell(cmd)})")
        for disp, dest in skipped:
            color.cprint(f"  # (cd {dest} && {_shell(cmd)})    # skipped: {disp}")
        for disp, dest, rc in failed:
            color.cprint(f"  (cd {dest} && {_shell(cmd)})    # FAILED ({rc}): {disp}")
    return 1 if failed else 0
