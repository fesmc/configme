"""Typed orchestrator extras (see docs/DESIGN.md sec. 12).

A small, closed vocabulary of post-config steps with built-in handlers, declared
per orchestrator in its ``[extras]`` table — never arbitrary shell. User/machine
specific values (hpc/account, data paths) come from ``.configme/config.toml`` or
an interactive prompt; nothing site-specific is shipped.

Handlers:
    pip_package = ["runme", ...]   install a command via pip if missing
    runme_config = true            create/patch .runme_config (hpc/account)
    data_link = ["ice_data", ...]  symlink runtime data dirs
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Protocol


class _Ask(Protocol):
    def __call__(self, label: str, default: Optional[str] = None, *,
                 complete_paths: bool = False) -> Optional[str]: ...


def _pip_package(value, runner, root: Path, cfg: dict, ask) -> str:
    names = value if isinstance(value, list) else [value]
    status = []
    for name in names:
        url = f"git+https://github.com/fesmc/{name}"
        runner.emit(f"command -v {name} >/dev/null 2>&1 || pip install {url}")
        if runner.dry_run:
            print(f"  pip_package {name}: (dry) install if missing ({url})")
            continue
        if shutil.which(name):
            print(f"  pip_package {name}: already on PATH; skipping")
            status.append(f"{name}=present")
            continue
        print(f"  pip_package {name}: installing ({url})")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", url], check=True)
            status.append(f"{name}=installed")
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"  ! pip install {name} failed: {e}")
            status.append(f"{name}=failed")
    return ", ".join(status)


def _runme_config(value, runner, root: Path, cfg: dict, ask) -> str:
    if not value:
        return ""
    src = root / ".runme" / "runme_config"
    dst = root / ".runme_config"
    hpc = cfg.get("hpc") or ask("hpc name for .runme_config")
    account = cfg.get("account") or ask("hpc account for .runme_config")
    runner.emit("# runme_config: create .runme_config and set hpc/account")
    runner.emit("runme --config  # or: cp .runme/runme_config .runme_config")
    if runner.dry_run:
        print(f"  runme_config: (dry) would create {dst} (hpc={hpc}, account={account})")
        return "dry"
    if dst.exists():
        print(f"  runme_config: {dst} exists; leaving as-is")
    elif src.is_file():
        dst.write_bytes(src.read_bytes())
        print(f"  runme_config: created {dst} from template")
    else:
        print(f"  runme_config: no {src} template found; skipping")
        return "skipped"
    if dst.exists() and (hpc or account):
        text = dst.read_text()
        if hpc:
            text = re.sub(r'("hpc"\s*:\s*")[^"]*(")', rf'\g<1>{hpc}\g<2>', text)
        if account:
            text = re.sub(r'("account"\s*:\s*")[^"]*(")',
                          rf'\g<1>{account}\g<2>', text)
        dst.write_text(text)
        print(f"  runme_config: set hpc={hpc or '(unset)'}, "
              f"account={account or '(unset)'}")
    return "ok"


def _data_link(value, runner, root: Path, cfg: dict, ask) -> str:
    labels = value if isinstance(value, list) else [value]
    done = []
    for label in labels:
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
        link_path = root / label
        runner.emit(f"ln -s {target} {label}")
        if runner.dry_run:
            print(f"  data_link {label}: (dry) {link_path} -> {target}")
            continue
        if link_path.is_symlink() or link_path.exists():
            print(f"  data_link {label}: {link_path} exists; skipping")
            done.append(f"{label}=exists")
            continue
        link_path.symlink_to(target)
        msg = "" if target.exists() else " (target missing)"
        print(f"  data_link {label}: {link_path} -> {target}{msg}")
        done.append(f"{label}=linked")
    return ", ".join(done)


_HANDLERS: dict = {
    "pip_package": _pip_package,
    "runme_config": _runme_config,
    "data_link": _data_link,
}


def run_extras(orchestrator, runner, root: Path, cfg: dict,
               ask: _Ask) -> None:
    extras = orchestrator.extras or {}
    if not extras:
        return
    runner.emit("\n# --- extras ---")
    print("Extras:")
    for name, value in extras.items():
        handler = _HANDLERS.get(name)
        if handler is None:
            print(f"  ! unknown extra '{name}' (skipping)")
            continue
        handler(value, runner, root, cfg, ask)
