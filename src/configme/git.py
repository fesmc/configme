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

from configme import install


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
        print(f"configme git: no managed git checkouts under {root}.")
        return 0

    wanted = _parse_repos(repos)
    if wanted is not None:
        unknown = [n for n in wanted if n not in {name for name, _ in available}]
        if unknown:
            raise GitError(
                f"unknown repo(s): {', '.join(unknown)}. "
                f"available: {', '.join(n for n, _ in available)}")
        selected = [(n, d) for n, d in available if n in wanted]
    else:
        selected = available

    cmd = ["git"] + list(git_args)
    auto_yes = yes or git_args[0] in READ_ONLY_VERBS
    reason = ("--yes" if yes
              else f"{git_args[0]!r} is read-only" if auto_yes else None)
    print(f"configme git — {len(selected)} repo(s) under {root}:")
    print(f"  $ {_shell(cmd)}")
    if auto_yes:
        print(f"  (auto-confirm: {reason})")
    print()

    ran: List[Tuple[str, Path]] = []
    skipped: List[Tuple[str, Path]] = []
    failed: List[Tuple[str, Path, int]] = []

    for name, dest in selected:
        try:
            rel = dest.relative_to(root)
            label = f"{name} ({rel})" if str(rel) != "." else name
        except ValueError:
            label = f"{name} ({dest})"
        if not auto_yes and not confirm_fn(f"run in {label}?", True):
            print(f"  - {name}: skipped")
            skipped.append((name, dest))
            continue
        print(f"  > {name}: (cd {dest} && {_shell(cmd)})")
        try:
            res = subprocess.run(cmd, cwd=dest)
        except OSError as e:
            print(f"  ! {name}: failed to launch git ({e})")
            failed.append((name, dest, -1))
            continue
        if res.returncode != 0:
            print(f"  ! {name}: git exited {res.returncode}")
            failed.append((name, dest, res.returncode))
        else:
            ran.append((name, dest))

    print()
    print(f"Summary: ran={len(ran)}  skipped={len(skipped)}  failed={len(failed)}")
    if ran or skipped or failed:
        print("Commands:")
        for name, dest in ran:
            print(f"  (cd {dest} && {_shell(cmd)})")
        for name, dest in skipped:
            print(f"  # (cd {dest} && {_shell(cmd)})    # skipped: {name}")
        for name, dest, rc in failed:
            print(f"  (cd {dest} && {_shell(cmd)})    # FAILED ({rc}): {name}")
    return 1 if failed else 0
