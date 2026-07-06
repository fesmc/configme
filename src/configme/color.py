"""ANSI colorization for configme's human-facing output.

configme prints a running log of what it is doing (clone / configure / link /
build) using a small, consistent *marker grammar* at the head of each line::

    +  created / success        green
    ~  modified / copied         yellow
    >  running / active          cyan
    -  skipped / deferred        dim
    !  error / warning           red
    $  command echo              dim

This module is the single place that turns those markers (and the ``status``
report's state labels) into color, so the ~180 ``print`` call sites elsewhere
stay as plain marker-prefixed strings. :func:`cprint` is a drop-in for ``print``
that auto-colors by leading marker; :func:`paint` is the same transform without
the printing; the semantic helpers (:func:`ok`, :func:`warn`, ...) are for the
few call sites that emphasize a span explicitly (headers, summary counts).

Coloring is **safe by default**: it is emitted only when the target stream is a
real terminal and the environment does not forbid it, so redirected or piped
output (logs, ``configme show`` fragments, generated ``install.sh``) never gets
polluted with escape sequences. The behaviour follows the widely-adopted
conventions:

  * ``NO_COLOR`` set (to anything)   -> never color (https://no-color.org)
  * ``FORCE_COLOR`` set (non-empty,   -> always color, even when not a TTY
    not "0")
  * ``TERM=dumb``                     -> never color
  * otherwise                         -> color iff the stream ``isatty()``

The CLI ``--color {auto,always,never}`` flag overrides the auto-detection via
:func:`set_mode` (``always``/``never`` short-circuit the environment probes;
``auto`` is the default described above).
"""

from __future__ import annotations

import os
import sys

# --------------------------------------------------------------- raw ANSI codes

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}

# ------------------------------------------------------------------- mode / gate

# One of "auto" | "always" | "never"; set once from the CLI. "auto" (the default)
# probes the environment and the stream per call — see module docstring.
_mode = "auto"


def set_mode(mode: str) -> None:
    """Set the global color mode from the CLI ``--color`` flag. Unknown values
    fall back to ``auto`` rather than raising — coloring is never worth a crash."""
    global _mode
    _mode = mode if mode in ("auto", "always", "never") else "auto"


def get_mode() -> str:
    return _mode


def enabled(stream=None) -> bool:
    """Whether color should be emitted to ``stream`` (default: stdout) right now,
    honoring the current mode and the ``NO_COLOR``/``FORCE_COLOR``/``TERM`` env
    conventions. Any non-tty / unknown stream degrades to no color."""
    if _mode == "never":
        return False
    if _mode == "always":
        return True
    # auto: environment vetoes first (NO_COLOR wins over FORCE_COLOR per spec).
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # closed / non-stream object
        return False


# ----------------------------------------------------------------- styling core

def style(text: str, *names: str, stream=None) -> str:
    """Wrap ``text`` in the named ANSI attributes (e.g. ``style(x, "red",
    "bold")``) when color is enabled for ``stream``; otherwise return it
    unchanged. Empty text is returned as-is so blank lines never grow codes."""
    if not text or not enabled(stream):
        return text
    prefix = "".join(_CODES[n] for n in names if n in _CODES)
    return f"{prefix}{text}{_RESET}" if prefix else text


# Semantic helpers — for call sites that emphasize a span explicitly rather than
# relying on the leading-marker auto-detection in `paint`.
def ok(text: str, stream=None) -> str:
    return style(text, "green", stream=stream)


def warn(text: str, stream=None) -> str:
    return style(text, "yellow", stream=stream)


def err(text: str, stream=None) -> str:
    return style(text, "red", stream=stream)


def skip(text: str, stream=None) -> str:
    return style(text, "dim", stream=stream)


def run(text: str, stream=None) -> str:
    return style(text, "cyan", stream=stream)


def header(text: str, stream=None) -> str:
    return style(text, "bold", stream=stream)


def hint(text: str, stream=None) -> str:
    return style(text, "dim", stream=stream)


# --------------------------------------------------------- marker auto-detection

# Leading marker (first non-space glyph of a line) -> ANSI attributes. A line
# only counts as marked when the marker is followed by a space (or is the whole
# line), so tokens like "-march" or "$HOME" in prose are never mistaken for one.
_MARK_STYLE = {
    "+": ("green",),
    "~": ("yellow",),
    ">": ("cyan",),
    "-": ("dim",),
    "!": ("red",),
    "$": ("dim",),
}


def paint(line: str, stream=None) -> str:
    """Return ``line`` with color applied according to its leading marker (see
    :data:`_MARK_STYLE`). Unmarked lines are returned unchanged. Multi-line input
    is painted line-by-line, so an embedded ``\\n`` block is handled correctly."""
    if not enabled(stream):
        return line
    if "\n" in line:
        return "\n".join(paint(part, stream=stream) for part in line.split("\n"))
    stripped = line.lstrip(" ")
    if not stripped:
        return line
    names = _MARK_STYLE.get(stripped[0])
    if names and (len(stripped) == 1 or stripped[1] == " "):
        indent = line[: len(line) - len(stripped)]
        return indent + style(stripped, *names, stream=stream)
    return line


def cprint(line: str = "", *, file=None) -> None:
    """``print`` replacement that auto-colors ``line`` by its leading marker.
    Kept signature-compatible with the plain single-argument ``print`` calls it
    replaces (plus an optional ``file`` for stderr); color is gated on that
    stream, so ``cprint(..., file=sys.stderr)`` colors iff stderr is a TTY."""
    stream = file if file is not None else sys.stdout
    print(paint(line, stream=stream), file=stream)
