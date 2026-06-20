"""Typed orchestrator extras (see docs/DESIGN.md sec. 12).

A small, closed vocabulary of post-config steps with built-in handlers, declared
per orchestrator in its ``[extras]`` table — never arbitrary shell. User/machine
specific values (hpc/account, data paths) come from ``.configme/config.toml`` or
an interactive prompt; nothing site-specific is shipped.

Handlers:
    pip_package = ["runme", ...]   pip install -U (install or update via pip).
                                   Pin a version/ref with `name:ref`
                                   (e.g. "runme:v0.3.1"); a pinned version that
                                   is already installed is skipped.
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


def pip_tool_url(name: str, ref: Optional[str] = None) -> str:
    """Git URL for a fesmc pip tool, optionally pinned to a ``ref`` (branch, tag,
    or commit) using pip's ``@ref`` VCS syntax — e.g. ``runme`` →
    ``git+https://github.com/fesmc/runme`` and ``runme`` @ ``v0.3.1`` →
    ``…/runme@v0.3.1``."""
    url = f"git+https://github.com/fesmc/{name}"
    return f"{url}@{ref}" if ref else url


def _installed_version(name: str) -> Optional[str]:
    """Installed distribution version of ``name`` per package metadata, or
    ``None`` if not installed (or metadata is unavailable)."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def pip_tool_satisfied(name: str, ref: Optional[str]) -> bool:
    """Whether a pinned ``ref`` is already the installed version, so the pip
    install can be skipped.

    Only a *version* ref can be confirmed this way: package metadata records a
    PEP 440 version (e.g. ``0.3.1``), not the git branch/commit a build came
    from. So a version tag (``v0.3.1`` / ``0.3.1``) that equals the installed
    version is "satisfied"; a branch or commit ref never matches and always
    (re)installs, and an unpinned ``None`` ref is never satisfied (the caller
    upgrades to latest). A leading ``v`` on the tag is ignored when comparing."""
    if not ref:
        return False
    installed = _installed_version(name)
    if installed is None:
        return False
    norm = lambda s: s[1:] if s.startswith("v") else s
    return norm(installed) == norm(ref)


def _pip_package(value, runner, root: Path, cfg: dict, ask,
                 confirm=None, followups=None, machine: Optional[str] = None,
                 upgrade: bool = False, repos=None) -> str:
    # Imported lazily to avoid an install <-> extras import cycle.
    from configme.data import split_ref

    specs = value if isinstance(value, list) else [value]
    status = []
    for spec in specs:
        # `name:ref` pins a release version (or any branch/tag/commit), mirroring
        # how git components pin refs (`yelmo:climber-x`). Bare `name` is unpinned.
        name, ref = split_ref(spec)
        url = pip_tool_url(name, ref)
        # Pinned to a version that's already installed? Nothing to do. (Only
        # version refs can be confirmed installed — see `pip_tool_satisfied`.)
        if pip_tool_satisfied(name, ref):
            runner.emit(f"# pip_package {name}: {ref} already installed; skipping")
            print(f"  pip_package {name}: {ref} already installed; skipping")
            status.append(f"{name}=present")
            continue
        # On `upgrade`, treat the tool like every other extra: opt-in per package
        # (default no; `-y` forces). Install always runs it unconditionally.
        if upgrade and confirm is not None and not confirm(
                f"Update pip package {name} (pip install -U)?", False):
            print(f"  - pip_package {name}: skipped")
            status.append(f"{name}=skipped")
            continue
        # `-U` so an unpinned tool tracks latest and a pinned-but-mismatched one
        # is replaced; the `@ref` in the URL fixes exactly what gets fetched.
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
                  confirm=None, followups=None, machine: Optional[str] = None,
                  upgrade: bool = False, repos=None) -> str:
    if not value:
        return ""
    # On `upgrade`, gate re-running like any other extra (default no; `-y`
    # forces). Install seeds .runme/config.toml unconditionally.
    if upgrade and confirm is not None and not confirm(
            "Re-run runme config (.runme/config.toml)?", False):
        print("  - runme_config: skipped")
        return "skipped"
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
               confirm=None, followups=None, machine: Optional[str] = None,
               upgrade: bool = False, repos=None) -> str:
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
              confirm=None, followups=None, machine: Optional[str] = None,
              upgrade: bool = False, repos=None) -> str:
    """Clone (or, on ``upgrade``, refresh) one or more auxiliary git repos into
    named dirs under the orchestrator root — e.g. climber-x's ``input`` from
    GitLab. Each entry is a table ``{dir, org, repo, host?, ref?, protocol?}``;
    ``host`` defaults to GitHub and ``ref`` (branch/tag/commit) is checked out
    after cloning.

    Honors the install download mode: ``ssh``/``https`` pick the clone
    transport, ``no`` uses whatever is already on disk. A per-entry
    ``protocol`` (``"https"``/``"ssh"``) overrides the transport for that repo
    (handy when a host only has HTTPS login configured); ``-d no`` is never
    overridden.

    Each entry is gated by ``confirm`` (default **no**; ``configme upgrade -y``
    forces yes) because these repos can be large/slow:

    * **install** (``upgrade=False``) — a *missing* dir is offered as a fresh
      clone; declining defers it (the clone command is printed and appended to
      ``followups`` for the summary). An existing dir is left untouched
      (idempotent re-runs).
    * **upgrade** (``upgrade=True``) — a *present* dir is offered a
      ``git pull --ff-only`` in place (the install path never refreshes these);
      a *missing* dir is offered a fresh clone, exactly as on install.

    ``repos`` (when not None) narrows the run to entries whose ``dir`` is in the
    set — this is how ``configme upgrade --repos input`` selects exactly which
    auxiliary repos to touch. Non-interactive runs take the default (skip)."""
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
        # `--repos` filter (upgrade): touch only the named auxiliary repos.
        if repos is not None and name not in repos:
            continue
        dest = root / name
        exists = dest.exists() or dest.is_symlink()
        # An entry may pin its clone transport with `protocol = "https"` / "ssh"
        # (e.g. a repo on a host where only HTTPS login is set up), overriding
        # the install download mode. `-d no` (use existing) is never overridden.
        transport = runner.download
        protocol = e.get("protocol")
        if protocol and transport != "no":
            transport = protocol
        url = build_clone_url(host, org, repo, transport)

        # `-d no`: never fetch anything; just report what is (or isn't) on disk.
        if runner.download == "no":
            runner.emit(f"# git_repo {name}: -d no; using existing {dest}")
            if exists:
                print(f"  git_repo {name}: -d no; using existing {dest}")
                done.append(f"{name}=present")
            else:
                print(f"  ! git_repo {name}: -d no but {dest} missing; pending")
                done.append(f"{name}=pending")
            continue

        # On upgrade, a present dir is refreshed in place (a pull, not a clone);
        # everywhere else (install, or a missing dir) the action is a clone.
        if upgrade and exists:
            if confirm is not None and not confirm(
                    f"Pull latest into {name}? ({dest})", False):
                runner.emit(f"# git_repo {name}: {dest} exists; pull declined")
                print(f"  - git_repo {name}: skipped pull of {dest}")
                done.append(f"{name}=skipped")
                continue
            try:
                status, _ = runner.pull_dir(name, dest)
            except Exception as exc:  # InstallError on a non-ff / diverged tree
                print(f"  ! git_repo {name}: pull failed: {exc}")
                done.append(f"{name}=failed")
                continue
            print(f"  git_repo {name}: pull {status}")
            done.append(f"{name}={status}")
            continue

        runner.emit(f"git clone {url} {dest}")
        if ref:
            runner.emit(f"(cd {dest} && git checkout {ref})")
        if runner.dry_run:
            print(f"  git_repo {name}: (dry) clone {url} -> {dest}"
                  f"{f' @ {ref}' if ref else ''}")
            done.append(f"{name}=dry")
            continue
        if exists:
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


# Extras that manage a git checkout addressable by a repo name (its ``dir``).
# Only these are eligible when ``configme upgrade --repos`` narrows the run to a
# named subset — the others (pip tools, runme config, data links) are not repos.
_REPO_EXTRAS = frozenset({"git_repo"})


def run_extras(orchestrator, runner, root: Path, cfg: dict,
               ask: _Ask, confirm=None, machine: Optional[str] = None,
               upgrade: bool = False, repos=None) -> list:
    """Run the orchestrator's typed extras. Returns a list of follow-up shell
    commands for steps the user deferred (e.g. a declined ``git_repo`` clone),
    so the caller can echo them in the install summary.

    ``machine`` is the machine name already resolved for this install/config;
    handlers may use it as a default (e.g. ``runme_config``'s ``hpc``).

    ``upgrade`` switches the handlers to ``configme upgrade`` semantics: every
    extra becomes opt-in per item (``confirm``, default no; ``-y`` forces yes),
    and ``git_repo`` refreshes a present checkout with ``git pull`` rather than
    skipping it. ``repos`` (when not None) narrows the run to that named set of
    repos — only repo-managing extras (see ``_REPO_EXTRAS``) run, and each is
    further filtered to the matching ``dir`` entries."""
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
        # `--repos` selects specific repos: non-repo extras have no place in it.
        if repos is not None and name not in _REPO_EXTRAS:
            continue
        handler(value, runner, root, cfg, ask, confirm, followups,
                machine=machine, upgrade=upgrade, repos=repos)
    return followups
