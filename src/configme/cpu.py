"""CPU microarchitecture detection for `configme check machine`.

Answers one question: what ``-march`` does the CPU running this command want,
and does it agree with what a machine fragment pins? Two detection backends, in
precedence order:

    1. ``gcc -march=native -Q --help=target`` — gcc resolves ``native`` to the
       concrete uarch (e.g. ``znver3``). Authoritative when gcc is present.
    2. ``/proc/cpuinfo`` model name matched against the ``[[cpu]]`` rules in
       ``data/uarch.toml`` — a best-effort fallback when gcc is unavailable.

The ranks/rules live in ``data/uarch.toml`` so a new CPU is a data edit, not a
code change. Nothing here runs a compiler on cluster *compute* nodes for you:
detection reflects whatever node the command runs on, so on an HPC login node
the caller should run it under ``srun``/``salloc`` for a compute-node answer.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from configme import data


@dataclass
class CpuInfo:
    """Detected CPU picture. ``march`` is the resolved tuning flag (e.g.
    ``znver3``) or None when nothing could be resolved; ``model`` is the raw CPU
    model string when known; ``source`` names how ``march`` was obtained; ``note``
    carries a caveat when the two backends disagreed (e.g. an old gcc)."""

    march: Optional[str]
    model: Optional[str]
    source: str
    note: Optional[str] = None


# ------------------------------------------------------------------ data table

def _uarch_data() -> dict:
    path = data.DATA_DIR / "uarch.toml"
    return data._load_toml(path) if path.is_file() else {}


def march_ranks() -> Dict[str, int]:
    """Map of known ``-march`` flag -> rank (higher = newer/more capable ISA)."""
    table = _uarch_data().get("march", {})
    return {name: int(v.get("rank", 0)) for name, v in table.items()}


def march_desc(march: Optional[str]) -> Optional[str]:
    """Human description for a ``-march`` flag, or None if not in the table."""
    if not march:
        return None
    entry = _uarch_data().get("march", {}).get(march)
    return entry.get("desc") if entry else None


def _cpu_rules() -> List[Tuple[str, str]]:
    """(regex, uarch) model-name fallback rules, in file order."""
    return [(r["match"], r["uarch"]) for r in _uarch_data().get("cpu", [])
            if r.get("match") and r.get("uarch")]


# ------------------------------------------------------------------ detection

def _run(tool: str, *args: str) -> Optional[str]:
    """Run ``tool args...``, returning stripped stdout, or None on any failure."""
    if shutil.which(tool) is None:
        return None
    try:
        out = subprocess.run([tool, *args], check=True, capture_output=True,
                             text=True, timeout=10)
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip()


def _gcc_native_march(cc: str = "gcc") -> Optional[str]:
    """Resolve ``-march=native`` to a concrete uarch via ``gcc -Q --help=target``.
    Returns the uarch token (e.g. ``znver3``) or None if gcc is absent, too old
    to know its own CPU (``native``), or produced no usable line."""
    out = _run(cc, "-march=native", "-Q", "--help=target")
    if not out:
        return None
    for line in out.splitlines():
        # The line looks like: "  -march=  \t\t znver3"
        if "-march=" in line:
            token = line.split("-march=", 1)[1].split()[0] if line.split("-march=", 1)[1].split() else ""
            # gcc too old to identify the CPU echoes "native" — treat as unknown.
            if token and token != "native":
                return token
    return None


def _cpu_model() -> Optional[str]:
    """Best-effort CPU model string, cross-platform: ``/proc/cpuinfo`` on Linux,
    ``sysctl`` on macOS, else ``platform.processor()``."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    if platform.system() == "Darwin":
        brand = _run("sysctl", "-n", "machdep.cpu.brand_string")
        if brand:
            return brand
    return platform.processor() or None


def _march_from_model(model: Optional[str]) -> Optional[str]:
    """Map a CPU model string to a uarch via the ``[[cpu]]`` regex rules."""
    if not model:
        return None
    for pattern, uarch in _cpu_rules():
        if re.search(pattern, model, re.IGNORECASE):
            return uarch
    return None


def detect(cc: str = "gcc") -> CpuInfo:
    """Detect the running CPU's ``-march`` from both backends and return the
    higher-ranked (more capable) result. gcc ``-march=native`` never *over*-reports
    — an old gcc simply falls back to the newest uarch it knows (e.g. a Zen 3
    EPYC 7763 resolves to ``znver1`` under a pre-Zen3 gcc) — so when the
    ``/proc/cpuinfo`` model maps to a newer uarch, that model is trusted and the
    gcc under-report is recorded in ``note``. ``march`` is None only when neither
    backend resolves anything."""
    model = _cpu_model()
    gcc_march = _gcc_native_march(cc)
    model_march = _march_from_model(model)
    ranks = march_ranks()

    def _rank(m: Optional[str]) -> int:
        return ranks.get(m, -1) if m else -2  # None sorts below any known flag

    # Prefer the higher-ranked flag; on a tie or when only one resolved, keep it.
    if _rank(model_march) > _rank(gcc_march):
        march, source = model_march, "/proc/cpuinfo model match"
    else:
        march, source = gcc_march, f"{cc} -march=native"

    if march is None:
        return CpuInfo(march=None, model=model, source="undetected")

    note = None
    if gcc_march and model_march and _rank(model_march) > _rank(gcc_march):
        note = (f"{cc} -march=native reports {gcc_march} (too old to name this "
                f"CPU); using {model_march} from the CPU model instead.")
    return CpuInfo(march=march, model=model, source=source, note=note)


# ------------------------------------------------------------------ comparison

def march_of_fragment(text: str) -> Optional[str]:
    """Extract the ``-march=<x>`` value from a machine fragment's flags, or None
    when the fragment pins no ``-march`` (it inherits the compiler default).
    Comment lines are skipped so a ``-march=native`` mentioned in a note is not
    mistaken for the configured flag."""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"-march=(\S+)", line)
        if m:
            return m.group(1)
    return None


def compare(detected: Optional[str], configured: Optional[str]) -> Tuple[str, str]:
    """Compare a detected vs configured ``-march`` and return (level, message),
    where level is one of ``ok`` / ``warn`` / ``info``. Ranking is best-effort:
    unknown flags degrade to an un-ranked mismatch note rather than a false OK."""
    if detected is None:
        return "info", "could not detect the CPU's march — cannot compare."
    if configured is None:
        return ("info", "machine fragment pins no -march (inherits the compiler "
                "default); detected CPU wants "
                f"-march={detected}.")
    if configured == detected:
        return "ok", f"configured -march matches the detected CPU ({detected})."
    ranks = march_ranks()
    rc, rd = ranks.get(configured), ranks.get(detected)
    if rc is None or rd is None:
        return "warn", (f"configured -march={configured} differs from detected "
                        f"-march={detected} (cannot rank one of them).")
    if rc < rd:
        return "warn", (f"configured -march={configured} is more conservative than "
                        f"the CPU supports (detected {detected}) — leaving "
                        "performance on the table.")
    return "warn", (f"configured -march={configured} is newer than the detected "
                    f"CPU ({detected}) — risk of illegal-instruction faults.")
