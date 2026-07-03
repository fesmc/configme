"""Tests for ``configme.cpu`` — CPU -march detection and the fragment
comparison behind ``configme check machine``. The two detection backends
(``gcc -march=native`` and the ``/proc/cpuinfo`` model-name fallback) and the
rank-based comparison are exercised without touching the real host."""

import textwrap

from configme import cpu


# ------------------------------------------------------------- model -> uarch

def test_march_from_model_epyc_generations():
    assert cpu._march_from_model("AMD EPYC 7702 64-Core Processor") == "znver2"
    assert cpu._march_from_model("AMD EPYC 7763 64-Core Processor") == "znver3"
    assert cpu._march_from_model("AMD EPYC 9654 96-Core Processor") == "znver4"


def test_march_from_model_unknown_is_none():
    assert cpu._march_from_model("Intel(R) Xeon(R) Platinum 8480+") is None
    assert cpu._march_from_model(None) is None


# --------------------------------------------------------------- detect order

def test_detect_uses_gcc_when_it_agrees(monkeypatch):
    monkeypatch.setattr(cpu, "_gcc_native_march", lambda cc="gcc": "znver2")
    monkeypatch.setattr(cpu, "_cpu_model",
                        lambda: "AMD EPYC 7702 64-Core Processor")
    info = cpu.detect()
    assert info.march == "znver2"
    assert "native" in info.source
    assert info.note is None


def test_detect_prefers_model_when_gcc_underreports(monkeypatch):
    # Levante regression: a Zen 3 EPYC 7763 under a pre-Zen3 gcc resolves
    # -march=native to znver1. The 7763 model is the trustworthy signal.
    monkeypatch.setattr(cpu, "_gcc_native_march", lambda cc="gcc": "znver1")
    monkeypatch.setattr(cpu, "_cpu_model",
                        lambda: "AMD EPYC 7763 64-Core Processor")
    info = cpu.detect()
    assert info.march == "znver3"
    assert "cpuinfo" in info.source
    assert info.note and "znver1" in info.note


def test_detect_falls_back_to_model(monkeypatch):
    monkeypatch.setattr(cpu, "_gcc_native_march", lambda cc="gcc": None)
    monkeypatch.setattr(cpu, "_cpu_model",
                        lambda: "AMD EPYC 7702 64-Core Processor")
    info = cpu.detect()
    assert info.march == "znver2"
    assert "cpuinfo" in info.source


def test_detect_undetected(monkeypatch):
    monkeypatch.setattr(cpu, "_gcc_native_march", lambda cc="gcc": None)
    monkeypatch.setattr(cpu, "_cpu_model", lambda: "Some Unknown CPU")
    info = cpu.detect()
    assert info.march is None
    assert info.source == "undetected"


def test_gcc_native_parses_help_target(monkeypatch):
    out = textwrap.dedent("""\
        The following options are target specific:
          -march=                     \t\tznver3
          -mtune=                     \t\tznver3
    """)
    monkeypatch.setattr(cpu, "_run", lambda *a: out)
    assert cpu._gcc_native_march() == "znver3"


def test_gcc_native_old_gcc_echoes_native(monkeypatch):
    # A gcc too old to identify the CPU prints "native"; treat as unknown.
    monkeypatch.setattr(cpu, "_run", lambda *a: "  -march=  \t\tnative")
    assert cpu._gcc_native_march() is None


# --------------------------------------------------------------- fragment march

def test_march_of_fragment():
    frag = "DFLAGS_NODEBUG = -Ofast -march=znver2 -traceback\n"
    assert cpu.march_of_fragment(frag) == "znver2"


def test_march_of_fragment_none_when_unset():
    assert cpu.march_of_fragment("DFLAGS_NODEBUG = -O2 -fp-model precise\n") is None


def test_march_of_fragment_ignores_comment_mentions():
    frag = ("# the login gcc mis-resolves -march=native to znver1\n"
            "DFLAGS_NODEBUG = -Ofast -march=znver3 -traceback\n")
    assert cpu.march_of_fragment(frag) == "znver3"


# --------------------------------------------------------------- comparison

def test_compare_match_is_ok():
    level, _ = cpu.compare("znver2", "znver2")
    assert level == "ok"


def test_compare_conservative_warns():
    level, msg = cpu.compare("znver3", "core-avx2")
    assert level == "warn"
    assert "conservative" in msg


def test_compare_too_new_warns():
    level, msg = cpu.compare("core-avx2", "znver3")
    assert level == "warn"
    assert "illegal-instruction" in msg


def test_compare_no_detection_is_info():
    assert cpu.compare(None, "znver2")[0] == "info"


def test_compare_no_configured_is_info():
    assert cpu.compare("znver3", None)[0] == "info"


def test_compare_unrankable_warns():
    level, msg = cpu.compare("znver3", "znverX")
    assert level == "warn"
    assert "cannot rank" in msg
