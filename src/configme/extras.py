"""Typed orchestrator extras (see docs/DESIGN.md sec. 12).

A small, closed vocabulary of post-config steps with built-in handlers, declared
per orchestrator in its ``[extras]`` table — never arbitrary shell. User/machine
specific values (hpc/account, data paths) come from ``.configme/config.toml`` or
an interactive prompt; nothing site-specific is shipped.

Extras are post-config *actions*, not repositories. An auxiliary/data repo
(e.g. climber-x's `input` from GitLab) is a clone-only package in the
orchestrator's `data_packages` list (see data.py / install.build_plan), not an
extra.

Handlers:
    pip_package = ["runme", ...]   pip install -U (install or update via pip).
                                   Pin a version/ref with `name:ref`
                                   (e.g. "runme:v0.3.1"); a pinned version that
                                   is already installed is skipped.
    runme_config = true            `runme config init` + patch .runme/config.toml
                                   (hpc/account)
    data_link = ["ice_data", ...]  symlink runtime data dirs
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Protocol

from configme import color


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


def _read_runme_hpc_account(runme_path: Optional[Path]) -> tuple:
    """Extract ``(hpc, account)`` from an existing ``.runme/config.toml``,
    mirroring the anchored regex the writer (``_runme_config``) uses so read and
    write stay symmetric regardless of which TOML section the keys sit in. An
    empty string value counts as unset. Returns ``(None, None)`` when the file is
    absent or a key is missing/blank."""
    if runme_path is None or not runme_path.is_file():
        return None, None
    text = runme_path.read_text()

    def grab(key: str) -> Optional[str]:
        m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', text, flags=re.MULTILINE)
        return m.group(1) if (m and m.group(1)) else None

    return grab("hpc"), grab("account")


def prompt_hpc_account(cfg: dict, ask: _Ask, machine: Optional[str] = None, *,
                       root: Optional[Path] = None,
                       configme_path: Optional[Path] = None) -> tuple:
    """Resolve ``(hpc, account)`` for the ``runme_config`` extra, prompting only
    for what is not already recorded.

    Values are resolved with a fixed precedence — ``.configme/config.toml`` (the
    ``cfg`` dict) first, then the repo's ``.runme/config.toml`` (read via
    ``root``). A value found in either file is *reused, not asked*: it is printed
    with a note pointing at the file to edit if it is wrong (mirroring how the
    already-chosen machine is echoed rather than re-prompted). Only a value
    absent from both files is prompted for — the account with a best-effort
    Slurm-account hint.

    ``hpc`` is runme's machine name, so it defaults to the ``machine`` this
    install already resolved. If a file *does* pin an ``hpc`` that disagrees with
    the selected machine, that is almost certainly stale config, so a warning is
    printed (the file value still wins — the user is told where to fix it).

    ``configme install`` calls this up front — alongside machine/compiler
    selection — and seeds the result into the cfg it hands ``run_extras``, so the
    later ``runme_config`` step reuses these values without asking again (see
    ``_runme_config``). ``account`` may come back ``None`` (non-interactive, or
    the user skipped)."""
    runme_path = (root / ".runme" / "config.toml") if root else None
    runme_hpc, runme_account = _read_runme_hpc_account(runme_path)

    def resolve(key: str, runme_val: Optional[str]) -> tuple:
        """(value, source-path) preferring .configme over .runme; (None, None)
        when neither file records the key."""
        if cfg.get(key):
            return cfg[key], configme_path
        if runme_val:
            return runme_val, runme_path
        return None, None

    # hpc — runme's machine name. A file value is authoritative but warned about
    # when it contradicts the machine actually selected for this install.
    hpc, hpc_src = resolve("hpc", runme_hpc)
    if hpc is None:
        hpc = machine
    else:
        if machine and hpc != machine:
            color.cprint(f"  ! warning: hpc '{hpc}' in {hpc_src} does not match "
                         f"selected machine '{machine}' — edit {hpc_src} to fix")
        color.cprint(f"  hpc: {hpc} (from {hpc_src}; edit there to change)")

    # account — reuse a recorded value (printed, not asked); otherwise prompt.
    account, acct_src = resolve("account", runme_account)
    if account is not None:
        color.cprint(f"  hpc account: {account} "
                     f"(from {acct_src}; edit there to change)")
    else:
        accounts = _slurm_accounts()
        if accounts:
            color.cprint(f"  available hpc accounts: {', '.join(accounts)}")
        account = ask("hpc account for .runme/config.toml")
    return hpc, account


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
                 upgrade: bool = False) -> str:
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
            color.cprint(f"  pip_package {name}: {ref} already installed; skipping")
            status.append(f"{name}=present")
            continue
        # On `upgrade`, treat the tool like every other extra: opt-in per package
        # (default no; `-y` forces). Install always runs it unconditionally.
        if upgrade and confirm is not None and not confirm(
                f"Update pip package {name} (pip install -U)?", False):
            color.cprint(f"  - pip_package {name}: skipped")
            status.append(f"{name}=skipped")
            continue
        # `-U` so an unpinned tool tracks latest and a pinned-but-mismatched one
        # is replaced; the `@ref` in the URL fixes exactly what gets fetched.
        runner.emit(f"pip install -U {url}")
        if runner.dry_run:
            color.cprint(f"  pip_package {name}: (dry) pip install -U ({url})")
            continue
        color.cprint(f"  pip_package {name}: installing/updating ({url})")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", url], check=True)
            status.append(f"{name}=ok")
        except (subprocess.CalledProcessError, OSError) as e:
            color.cprint(f"  ! pip install -U {name} failed: {e}")
            status.append(f"{name}=failed")
    return ", ".join(status)


def _runme_config(value, runner, root: Path, cfg: dict, ask,
                  confirm=None, followups=None, machine: Optional[str] = None,
                  upgrade: bool = False) -> str:
    if not value:
        return ""
    # On `upgrade`, gate re-running like any other extra (default no; `-y`
    # forces). Install seeds .runme/config.toml unconditionally.
    if upgrade and confirm is not None and not confirm(
            "Re-run runme config (.runme/config.toml)?", False):
        color.cprint("  - runme_config: skipped")
        return "skipped"
    dst = root / ".runme" / "config.toml"
    # The runme ``hpc`` is the machine name; default it to the machine this
    # install/config already resolved, so it need not be retyped when correct.
    hpc = cfg.get("hpc") or ask("hpc name for .runme/config.toml", default=machine)
    # The account is too case-specific to default, but list the accounts the
    # user may submit under (best-effort, Slurm-only) as a hint before asking.
    # A *present* ``account`` key is authoritative (even if empty): `configme
    # install` collects it up front and seeds it here, so a seeded-but-empty
    # account is a deliberate "skipped", not a reason to re-prompt. Only an
    # absent key (e.g. the upgrade path, or config.toml without one) asks.
    if "account" in cfg:
        account = cfg["account"]
    else:
        accounts = _slurm_accounts()
        if accounts:
            color.cprint(f"  available hpc accounts: {', '.join(accounts)}")
        account = ask("hpc account for .runme/config.toml")
    # `runme config init` is the canonical way to seed `.runme/config.toml`
    # (it copies `.runme/config.default.toml` and stamps in defaults); we then
    # patch hpc/account in place. We don't carry our own template anymore.
    init_cmd = ["runme", "config", "init"]
    runner.emit("# runme_config: seed .runme/config.toml and set hpc/account")
    runner.emit(" ".join(init_cmd))
    if runner.dry_run:
        color.cprint(f"  runme_config: (dry) would run `{' '.join(init_cmd)}` to "
              f"create {dst} (hpc={hpc}, account={account})")
        return "dry"
    if dst.exists():
        color.cprint(f"  runme_config: {dst} exists; leaving as-is")
    else:
        color.cprint(f"  runme_config: running `{' '.join(init_cmd)}` in {root}")
        try:
            subprocess.run(init_cmd, cwd=root, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            color.cprint(f"  ! runme config init failed: {e}")
            return "failed"
        if not dst.exists():
            color.cprint(f"  runme_config: `{' '.join(init_cmd)}` did not produce {dst}; "
                  f"skipping patch")
            return "skipped"
        color.cprint(f"  runme_config: created {dst}")
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
        color.cprint(f"  runme_config: set hpc={hpc or '(unset)'}, "
              f"account={account or '(unset)'}")
    return "ok"


def _data_link(value, runner, root: Path, cfg: dict, ask,
               confirm=None, followups=None, machine: Optional[str] = None,
               upgrade: bool = False) -> str:
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
            color.cprint(f"  data_link {label}: {link_path} exists; keeping{note}")
            done.append(f"{label}=exists")
            continue
        path = cfg.get(label) or ask(f"path to {label}", complete_paths=True)
        runner.emit(f"# data_link {label}: ln -s <path> {label}")
        if not path:
            color.cprint(f"  data_link {label}: no path given; pending (link later)")
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
            color.cprint(f"  ! data_link {label}: refusing self-referential link "
                  f"{link_path} -> {target} (target is its parent directory); "
                  f"skipping")
            done.append(f"{label}=skipped (self-loop)")
            continue
        runner.emit(f"ln -s {target} {label}")
        if runner.dry_run:
            color.cprint(f"  data_link {label}: (dry) {link_path} -> {target}")
            continue
        link_path.symlink_to(target)
        msg = "" if target.exists() else " (target missing)"
        color.cprint(f"  data_link {label}: {link_path} -> {target}{msg}")
        done.append(f"{label}=linked")
    return ", ".join(done)


_HANDLERS: dict = {
    "pip_package": _pip_package,
    "runme_config": _runme_config,
    "data_link": _data_link,
}


def run_extras(orchestrator, runner, root: Path, cfg: dict,
               ask: _Ask, confirm=None, machine: Optional[str] = None,
               upgrade: bool = False) -> list:
    """Run the orchestrator's typed extras (post-config *actions*: pip tools,
    runme config, data links). Returns a list of follow-up shell commands for
    steps the user deferred, so the caller can echo them in the install summary.

    Auxiliary/data repositories are *not* extras — they are clone-only packages
    in the orchestrator's ``data_packages`` and flow through the normal node
    pipeline (see install.build_plan).

    ``machine`` is the machine name already resolved for this install/config;
    handlers may use it as a default (e.g. ``runme_config``'s ``hpc``).

    ``upgrade`` switches the handlers to ``configme upgrade`` semantics: every
    extra becomes opt-in per item (``confirm``, default no; ``-y`` forces yes)."""
    followups: list = []
    extras = orchestrator.extras or {}
    if not extras:
        return followups
    runner.emit("\n# --- extras ---")
    color.cprint("Extras:")
    for name, value in extras.items():
        handler = _HANDLERS.get(name)
        if handler is None:
            color.cprint(f"  ! unknown extra '{name}' (skipping)")
            continue
        handler(value, runner, root, cfg, ask, confirm, followups,
                machine=machine, upgrade=upgrade)
    return followups
