"""Typed orchestrator extras (see docs/DESIGN.md sec. 12).

A small, closed vocabulary of post-config steps with built-in handlers, declared
per orchestrator in its ``[extras]`` table — never arbitrary shell. User/machine
specific values (hpc/account, data paths) come from ``.configme/config.toml`` or
an interactive prompt; nothing site-specific is shipped.

Handlers:
    pip_package = ["runme", ...]   pip install -U (install or update via pip)
    runme_config = true            `runme config init` + patch .runme/config.toml
                                   (hpc/account)
    data_link = ["ice_data", ...]  symlink runtime data dirs
    git_repo = [{dir, org, repo, host?, ref?}, ...]
                                   clone an auxiliary repo (any host) into a dir
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Protocol


def _slurm_accounts() -> List[str]:
    """Best-effort list of Slurm accounts the current user may submit under, for
    use as a prompt hint only. Queries the association table via ``sacctmgr``
    (``sacct`` reports past jobs, not entitlements). Returns ``[]`` whenever
    ``sacctmgr`` is absent or the query fails/times out — e.g. on a non-Slurm
    machine — so this never blocks or breaks the prompt."""
    if shutil.which("sacctmgr") is None:
        return []
    cmd = ["sacctmgr", "-nP", "show", "associations"]
    user = os.environ.get("USER")
    if user:
        cmd.append(f"user={user}")
    cmd.append("format=account")
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                             timeout=10)
    except (subprocess.SubprocessError, OSError):
        return []
    accounts = {line.strip() for line in out.stdout.splitlines() if line.strip()}
    return sorted(accounts)


class _Ask(Protocol):
    def __call__(self, label: str, default: Optional[str] = None, *,
                 complete_paths: bool = False) -> Optional[str]: ...


def _pip_package(value, runner, root: Path, cfg: dict, ask,
                 confirm=None, followups=None, machine: Optional[str] = None) -> str:
    names = value if isinstance(value, list) else [value]
    status = []
    for name in names:
        url = f"git+https://github.com/fesmc/{name}"
        # Always `pip install -U`: pip decides whether the package is missing or
        # out of date and installs/upgrades as needed. We don't pre-check with
        # `command -v`/`shutil.which` — that only tells us a command exists, not
        # whether it's current, and would skip available updates.
        runner.emit(f"pip install -U {url}")
        if runner.dry_run:
            print(f"  pip_package {name}: (dry) pip install -U ({url})")
            continue
        print(f"  pip_package {name}: installing/updating ({url})")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", url], check=True)
            status.append(f"{name}=ok")
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"  ! pip install -U {name} failed: {e}")
            status.append(f"{name}=failed")
    return ", ".join(status)


def _runme_config(value, runner, root: Path, cfg: dict, ask,
                  confirm=None, followups=None, machine: Optional[str] = None) -> str:
    if not value:
        return ""
    dst = root / ".runme" / "config.toml"
    # The runme ``hpc`` is the machine name; default it to the machine this
    # install/config already resolved, so it need not be retyped when correct.
    hpc = cfg.get("hpc") or ask("hpc name for .runme/config.toml", default=machine)
    # The account is too case-specific to default, but list the accounts the
    # user may submit under (best-effort, Slurm-only) as a hint before asking.
    account = cfg.get("account")
    if not account:
        accounts = _slurm_accounts()
        if accounts:
            print(f"  available hpc accounts: {', '.join(accounts)}")
        account = ask("hpc account for .runme/config.toml")
    # `runme config init` is the canonical way to seed `.runme/config.toml`
    # (it copies `.runme/config.default.toml` and stamps in defaults); we then
    # patch hpc/account in place. We don't carry our own template anymore.
    init_cmd = ["runme", "config", "init"]
    runner.emit("# runme_config: seed .runme/config.toml and set hpc/account")
    runner.emit(" ".join(init_cmd))
    if runner.dry_run:
        print(f"  runme_config: (dry) would run `{' '.join(init_cmd)}` to "
              f"create {dst} (hpc={hpc}, account={account})")
        return "dry"
    if dst.exists():
        print(f"  runme_config: {dst} exists; leaving as-is")
    else:
        print(f"  runme_config: running `{' '.join(init_cmd)}` in {root}")
        try:
            subprocess.run(init_cmd, cwd=root, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"  ! runme config init failed: {e}")
            return "failed"
        if not dst.exists():
            print(f"  runme_config: `{' '.join(init_cmd)}` did not produce {dst}; "
                  f"skipping patch")
            return "skipped"
        print(f"  runme_config: created {dst}")
    if dst.exists() and (hpc or account):
        text = dst.read_text()
        # TOML form: ``hpc = "value"`` / ``account = "value"``. Anchored on
        # line start (re.MULTILINE) so a stray `hpc` substring inside another
        # value cannot be matched.
        if hpc:
            text = re.sub(r'^(\s*hpc\s*=\s*")[^"]*(")', rf'\g<1>{hpc}\g<2>',
                          text, flags=re.MULTILINE)
        if account:
            text = re.sub(r'^(\s*account\s*=\s*")[^"]*(")',
                          rf'\g<1>{account}\g<2>', text, flags=re.MULTILINE)
        dst.write_text(text)
        print(f"  runme_config: set hpc={hpc or '(unset)'}, "
              f"account={account or '(unset)'}")
    return "ok"


def _data_link(value, runner, root: Path, cfg: dict, ask,
               confirm=None, followups=None, machine: Optional[str] = None) -> str:
    labels = value if isinstance(value, list) else [value]
    done = []
    for label in labels:
        link_path = root / label
        # Already set up? Keep it and don't prompt — re-running an install on a
        # configured tree should leave existing links alone. A dangling symlink
        # still counts as "set up" (the user pointed it somewhere); just note the
        # missing target rather than re-asking.
        if link_path.is_symlink() or link_path.exists():
            note = "" if link_path.exists() else " (target missing)"
            runner.emit(f"# data_link {label}: {link_path} exists; keeping{note}")
            print(f"  data_link {label}: {link_path} exists; keeping{note}")
            done.append(f"{label}=exists")
            continue
        path = cfg.get(label) or ask(f"path to {label}", complete_paths=True)
        runner.emit(f"# data_link {label}: ln -s <path> {label}")
        if not path:
            print(f"  data_link {label}: no path given; pending (link later)")
            done.append(f"{label}=pending")
            continue
        # Data links are stored as absolute, fully expanded paths (~ and $VARS
        # resolved, made absolute from the filesystem root) so they stay valid
        # regardless of where the link is later read from.
        target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
        # Refuse a self-loop (target's inode is link_path's own parent). Uses
        # samefile() so a case-insensitive APFS/HFS+ match (`/x/Foo` vs
        # `/x/foo`) is caught even though the path strings differ.
        try:
            self_loop = link_path.parent.exists() and target.samefile(
                link_path.parent)
        except OSError:
            self_loop = False
        if self_loop:
            print(f"  ! data_link {label}: refusing self-referential link "
                  f"{link_path} -> {target} (target is its parent directory); "
                  f"skipping")
            done.append(f"{label}=skipped (self-loop)")
            continue
        runner.emit(f"ln -s {target} {label}")
        if runner.dry_run:
            print(f"  data_link {label}: (dry) {link_path} -> {target}")
            continue
        link_path.symlink_to(target)
        msg = "" if target.exists() else " (target missing)"
        print(f"  data_link {label}: {link_path} -> {target}{msg}")
        done.append(f"{label}=linked")
    return ", ".join(done)


def _git_repo(value, runner, root: Path, cfg: dict, ask,
              confirm=None, followups=None, machine: Optional[str] = None) -> str:
    """Clone one or more auxiliary git repos into named dirs under the
    orchestrator root — e.g. climber-x's ``input`` from GitLab. Each entry is a
    table ``{dir, org, repo, host?, ref?, protocol?}``; ``host`` defaults to
    GitHub and ``ref`` (branch/tag/commit) is checked out after cloning.

    Honors the install download mode: ``ssh``/``https`` pick the clone
    transport, ``no`` uses whatever is already on disk. A per-entry
    ``protocol`` (``"https"``/``"ssh"``) overrides the transport for that repo
    (handy when a host only has HTTPS login configured); ``-d no`` is never
    overridden. An existing dir is left untouched (idempotent re-runs).

    These repos can be large/slow, so before each fresh clone the user is asked
    (``confirm``, default **no**); declining defers it — the clone command is
    printed and appended to ``followups`` so it surfaces in the install summary.
    Non-interactive runs take the default (skip)."""
    # Imported lazily to avoid an install <-> extras import cycle.
    from configme.install import build_clone_url

    entries = value if isinstance(value, list) else [value]
    done = []
    for e in entries:
        if not isinstance(e, dict):
            print(f"  ! git_repo: entry is not a table: {e!r}; skipping")
            continue
        name = e.get("dir")
        org = e.get("org")
        repo = e.get("repo")
        host = e.get("host", "github.com")
        ref = e.get("ref")
        if not (name and org and repo):
            print(f"  ! git_repo: entry missing dir/org/repo: {e}; skipping")
            done.append("?=invalid")
            continue
        dest = root / name
        # An entry may pin its clone transport with `protocol = "https"` / "ssh"
        # (e.g. a repo on a host where only HTTPS login is set up), overriding
        # the install download mode. `-d no` (use existing) is never overridden.
        transport = runner.download
        protocol = e.get("protocol")
        if protocol and transport != "no":
            transport = protocol
        url = build_clone_url(host, org, repo, transport)
        runner.emit(f"git clone {url} {dest}")
        if ref:
            runner.emit(f"(cd {dest} && git checkout {ref})")

        if runner.download == "no":
            if dest.exists() or dest.is_symlink():
                print(f"  git_repo {name}: -d no; using existing {dest}")
                done.append(f"{name}=present")
            else:
                print(f"  ! git_repo {name}: -d no but {dest} missing; pending")
                done.append(f"{name}=pending")
            continue
        if runner.dry_run:
            print(f"  git_repo {name}: (dry) clone {url} -> {dest}"
                  f"{f' @ {ref}' if ref else ''}")
            done.append(f"{name}=dry")
            continue
        if dest.exists() or dest.is_symlink():
            print(f"  git_repo {name}: {dest} exists; skipping")
            done.append(f"{name}=exists")
            continue
        # A fresh clone of a (potentially large) repo: ask first, default skip.
        # Declining defers it — show the command now and add it to the summary.
        if confirm is not None and not confirm(
                f"Download {name} now? (large repo, can take a while)", False):
            cmds = [f"git clone {url} {dest}"]
            if ref:
                cmds.append(f"(cd {dest} && git checkout {ref})")
            print(f"  - git_repo {name}: skipped; clone it later with:")
            for c in cmds:
                print(f"      {c}")
            if followups is not None:
                followups.extend(cmds)
            done.append(f"{name}=skipped")
            continue
        print(f"  git_repo {name}: cloning {url} -> {dest}")
        try:
            subprocess.run(["git", "clone", url, str(dest)], check=True)
            if ref:
                subprocess.run(["git", "checkout", ref], cwd=dest, check=True)
            done.append(f"{name}=cloned")
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"  ! git_repo {name} clone failed: {exc}")
            done.append(f"{name}=failed")
    return ", ".join(done)


_HANDLERS: dict = {
    "pip_package": _pip_package,
    "runme_config": _runme_config,
    "data_link": _data_link,
    "git_repo": _git_repo,
}


def run_extras(orchestrator, runner, root: Path, cfg: dict,
               ask: _Ask, confirm=None, machine: Optional[str] = None) -> list:
    """Run the orchestrator's typed extras. Returns a list of follow-up shell
    commands for steps the user deferred (e.g. a declined ``git_repo`` clone),
    so the caller can echo them in the install summary.

    ``machine`` is the machine name already resolved for this install/config;
    handlers may use it as a default (e.g. ``runme_config``'s ``hpc``)."""
    followups: list = []
    extras = orchestrator.extras or {}
    if not extras:
        return followups
    runner.emit("\n# --- extras ---")
    print("Extras:")
    for name, value in extras.items():
        handler = _HANDLERS.get(name)
        if handler is None:
            print(f"  ! unknown extra '{name}' (skipping)")
            continue
        handler(value, runner, root, cfg, ask, confirm, followups, machine=machine)
    return followups
