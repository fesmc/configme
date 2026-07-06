"""Tests for the ANSI colorization helper (`configme.color`).

Coloring must be *opt-in-safe*: emitted only when the mode/stream/env allow it,
and always faithfully round-tripping the underlying text when disabled — so
redirected output (logs, generated scripts, `configme show` fragments) is never
polluted with escape sequences. These tests pin the mode gating, the env-var
conventions, and the leading-marker auto-detection.
"""

import io

import pytest

from configme import color

RESET = "\033[0m"


@pytest.fixture(autouse=True)
def _restore_mode():
    """Each test twiddles the global mode; restore the default afterwards."""
    saved = color.get_mode()
    yield
    color.set_mode(saved)


# --------------------------------------------------------------------- gating

def test_never_mode_emits_no_codes():
    color.set_mode("never")
    assert color.paint("  + wrote foo") == "  + wrote foo"
    assert color.style("x", "red") == "x"
    assert color.enabled() is False


def test_always_mode_colors_a_marker_line():
    color.set_mode("always")
    out = color.paint("  + wrote foo")
    assert out == "  \033[32m+ wrote foo" + RESET


def test_unknown_mode_falls_back_to_auto():
    color.set_mode("technicolor")
    assert color.get_mode() == "auto"


def test_auto_mode_off_for_non_tty_stream():
    color.set_mode("auto")
    buf = io.StringIO()  # StringIO.isatty() is False
    assert color.enabled(buf) is False
    assert color.paint("  ! boom", stream=buf) == "  ! boom"


def test_no_color_env_beats_force_color(monkeypatch):
    color.set_mode("auto")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color.enabled() is False


def test_force_color_enables_non_tty(monkeypatch):
    color.set_mode("auto")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color.enabled(io.StringIO()) is True


def test_term_dumb_disables(monkeypatch):
    color.set_mode("auto")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert color.enabled(io.StringIO()) is False


# ----------------------------------------------------------- marker detection

@pytest.mark.parametrize("line,code", [
    ("  + created", "\033[32m"),   # green
    ("  ~ copied", "\033[33m"),    # yellow
    ("  > running", "\033[36m"),   # cyan
    ("  - skipped", "\033[2m"),    # dim
    ("  ! failed", "\033[31m"),    # red
    ("  $ git status", "\033[2m"),  # dim
])
def test_each_marker_maps_to_its_color(line, code):
    color.set_mode("always")
    out = color.paint(line)
    assert code in out and out.endswith(RESET)


def test_unmarked_line_untouched():
    color.set_mode("always")
    assert color.paint("  root: /x") == "  root: /x"
    assert color.paint("configme install yelmox") == "configme install yelmox"


def test_marker_needs_trailing_space():
    """`-march` / `$HOME` in prose must not be mistaken for a marker."""
    color.set_mode("always")
    assert color.paint("-march=native") == "-march=native"
    assert color.paint("$HOME/bin") == "$HOME/bin"


def test_blank_line_stays_blank():
    color.set_mode("always")
    assert color.paint("") == ""
    assert color.style("", "red") == ""


def test_multiline_painted_per_line():
    color.set_mode("always")
    out = color.paint("  + a\n  ! b\n  plain")
    lines = out.split("\n")
    assert "\033[32m" in lines[0]
    assert "\033[31m" in lines[1]
    assert lines[2] == "  plain"


def test_style_composes_attributes():
    color.set_mode("always")
    out = color.style("MISSING", "red", "bold")
    assert out == "\033[31m\033[1mMISSING" + RESET


def test_cprint_uses_stream_gate(capsys):
    """cprint colors per the *target* stream: stdout capture is non-tty -> plain
    even in 'always'? No — 'always' forces on regardless of tty. Assert codes."""
    color.set_mode("always")
    color.cprint("  + done")
    captured = capsys.readouterr()
    assert "\033[32m" in captured.out
