"""Tests for the `runme_config` extra (see extras._runme_config).

The handler delegates `.runme/config.toml` creation to `runme config init` and
then regex-patches `hpc`/`account` in place. Tests mock `subprocess.run` so they
never touch the real `runme` tool: the mock writes a fixture `config.toml` with
CHANGEME placeholders that the patch step rewrites.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from configme import extras, install

if sys.version_info >= (3, 11):
    import tomllib
else:                                        # pragma: no cover - py3.8-3.10
    import tomli as tomllib


# A `.runme/config.toml` as freshly created by `runme config init` —
# placeholders are present so the regex patch has something to substitute.
_FRESH_CONFIG = '''\
hpc = "CHANGEME"
account = "CHANGEME"
queue = "short"
'''


def _ask_factory(answers=None):
    """Build an `ask` callable that returns canned answers in order, recording
    each call so tests can assert the prompt label/default."""
    answers = list(answers or [])
    calls = []

    def ask(label, default=None, *, complete_paths=False):
        calls.append((label, default, complete_paths))
        return answers.pop(0) if answers else None

    ask.calls = calls
    return ask


def _writing_subprocess_run(dst: Path, payload: str = _FRESH_CONFIG):
    """Return a fake `subprocess.run` that writes `payload` to `dst` and records
    the invocation — mimics a successful `runme config init`."""
    seen = {}

    def fake(cmd, cwd=None, check=False, **kw):
        seen["cmd"] = list(cmd)
        seen["cwd"] = cwd
        seen["check"] = check
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(payload)

    fake.seen = seen
    return fake


# --------------------------------------------------------------- dry-run

def test_dry_run_emits_command_without_shelling_out(tmp_path, monkeypatch):
    """Dry-run prints the `runme config init` line to the runner log and does
    NOT invoke subprocess; no file is created."""
    called = []
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)))
    runner = install.Runner(dry_run=True)
    ask = _ask_factory(["mymachine", "myaccount"])

    out = extras._runme_config(True, runner, tmp_path, cfg={}, ask=ask)

    assert out == "dry"
    assert called == []                                  # no subprocess
    assert not (tmp_path / ".runme" / "config.toml").exists()
    assert "runme config init" in "\n".join(runner.log)


def test_value_false_is_a_noop(tmp_path):
    """A falsy `runme_config = false` short-circuits before any prompt or run."""
    runner = install.Runner(dry_run=False)

    def ask(label, default=None, *, complete_paths=False):
        raise AssertionError("must not prompt when the extra is disabled")

    assert extras._runme_config(False, runner, tmp_path, cfg={}, ask=ask) == ""


# --------------------------------------------------------------- happy path

def test_happy_path_runs_init_and_patches_placeholders(tmp_path, monkeypatch):
    """`runme config init` is invoked in `cwd=root` and the resulting TOML has
    its CHANGEME hpc/account patched to the prompted values."""
    dst = tmp_path / ".runme" / "config.toml"
    fake = _writing_subprocess_run(dst)
    monkeypatch.setattr(extras.subprocess, "run", fake)
    runner = install.Runner(dry_run=False)
    ask = _ask_factory(["mymachine", "myaccount"])

    out = extras._runme_config(True, runner, tmp_path, cfg={}, ask=ask,
                               machine=None)

    assert out == "ok"
    assert fake.seen["cmd"] == ["runme", "config", "init"]
    assert fake.seen["cwd"] == tmp_path
    assert fake.seen["check"] is True
    cfg = tomllib.loads(dst.read_text())
    assert cfg["hpc"] == "mymachine"
    assert cfg["account"] == "myaccount"
    assert cfg["queue"] == "short"                # untouched


def test_machine_default_used_for_hpc_prompt(tmp_path, monkeypatch):
    """The resolved machine name is offered as the default for the hpc prompt
    so a user who is configuring for the current machine need not retype it."""
    dst = tmp_path / ".runme" / "config.toml"
    monkeypatch.setattr(extras.subprocess, "run",
                        _writing_subprocess_run(dst))
    runner = install.Runner(dry_run=False)
    ask = _ask_factory(["accepted-default", "acct"])

    extras._runme_config(True, runner, tmp_path, cfg={}, ask=ask,
                        machine="brigit")

    assert ask.calls[0][0] == "hpc name for .runme/config.toml"
    assert ask.calls[0][1] == "brigit"            # default = machine


def test_cfg_values_bypass_prompts(tmp_path, monkeypatch):
    """When cfg already carries hpc/account the handler must not prompt."""
    dst = tmp_path / ".runme" / "config.toml"
    monkeypatch.setattr(extras.subprocess, "run",
                        _writing_subprocess_run(dst))
    runner = install.Runner(dry_run=False)

    def ask(label, default=None, *, complete_paths=False):
        raise AssertionError(f"unexpected prompt: {label!r}")

    out = extras._runme_config(True, runner, tmp_path,
                               cfg={"hpc": "cfgmach", "account": "cfgacct"},
                               ask=ask)

    assert out == "ok"
    cfg = tomllib.loads(dst.read_text())
    assert cfg["hpc"] == "cfgmach"
    assert cfg["account"] == "cfgacct"


def test_patch_anchored_to_line_start(tmp_path, monkeypatch):
    """A stray `hpc = "x"` substring inside another value must not be matched —
    only a real line-start `hpc =` is patched. This guards against the regex
    accidentally rewriting comments or nested values."""
    payload = '''\
hpc = "CHANGEME"
account = "CHANGEME"
note = "do not rewrite: hpc = \\"trap\\""
'''
    dst = tmp_path / ".runme" / "config.toml"
    monkeypatch.setattr(extras.subprocess, "run",
                        _writing_subprocess_run(dst, payload=payload))
    runner = install.Runner(dry_run=False)

    extras._runme_config(True, runner, tmp_path,
                        cfg={"hpc": "real", "account": "acct"},
                        ask=_ask_factory())

    text = dst.read_text()
    assert 'hpc = "real"' in text
    assert 'account = "acct"' in text
    # The decoy inside `note` must be untouched.
    assert 'hpc = \\"trap\\"' in text


# --------------------------------------------------------------- idempotence

def test_existing_config_is_not_reinitialized_but_is_repatched(
        tmp_path, monkeypatch):
    """An existing `.runme/config.toml` is not regenerated (no `runme config
    init` call), but the patch step still runs so hpc/account stay current."""
    dst = tmp_path / ".runme" / "config.toml"
    dst.parent.mkdir()
    dst.write_text('hpc = "old"\naccount = "old"\n')

    called = []
    monkeypatch.setattr(extras.subprocess, "run",
                        lambda *a, **kw: called.append(a))
    runner = install.Runner(dry_run=False)

    extras._runme_config(True, runner, tmp_path,
                        cfg={"hpc": "new", "account": "newacct"},
                        ask=_ask_factory())

    assert called == []                          # init was skipped
    cfg = tomllib.loads(dst.read_text())
    assert cfg == {"hpc": "new", "account": "newacct"}


# --------------------------------------------------------------- failure modes

def test_init_failure_returns_failed(tmp_path, monkeypatch):
    """A non-zero `runme config init` is reported as 'failed', not raised — the
    install summary should surface it, but the broader install must continue."""
    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(extras.subprocess, "run", boom)
    runner = install.Runner(dry_run=False)

    out = extras._runme_config(True, runner, tmp_path,
                               cfg={"hpc": "h", "account": "a"},
                               ask=_ask_factory())
    assert out == "failed"
    assert not (tmp_path / ".runme" / "config.toml").exists()


def test_init_produced_no_file_returns_skipped(tmp_path, monkeypatch):
    """If `runme config init` returns 0 but somehow does not create the config
    file, we don't try to patch a non-existent file — return 'skipped'."""
    monkeypatch.setattr(extras.subprocess, "run", lambda *a, **kw: None)
    runner = install.Runner(dry_run=False)

    out = extras._runme_config(True, runner, tmp_path,
                               cfg={"hpc": "h", "account": "a"},
                               ask=_ask_factory())
    assert out == "skipped"


# ------------------------------------------- prompt_hpc_account source precedence

def _write_runme(root, body):
    d = root / ".runme"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(body)
    return d / "config.toml"


def test_prompt_reads_account_from_existing_runme_config(tmp_path, monkeypatch, capsys):
    """An account already in the repo's .runme/config.toml is reused (printed,
    not asked) — the prompt is never shown."""
    monkeypatch.setattr(extras, "_slurm_accounts", lambda: [])
    _write_runme(tmp_path, 'hpc = "awi_albedo"\naccount = "envi.p_forclima"\n')
    ask = _ask_factory()

    hpc, account = extras.prompt_hpc_account(
        {}, ask, machine="awi_albedo", root=tmp_path)

    assert (hpc, account) == ("awi_albedo", "envi.p_forclima")
    assert ask.calls == []                        # never prompted
    assert "envi.p_forclima" in capsys.readouterr().out


def test_prompt_configme_cfg_beats_runme_config(tmp_path, monkeypatch):
    """.configme/config.toml wins over the repo's .runme/config.toml."""
    monkeypatch.setattr(extras, "_slurm_accounts", lambda: [])
    _write_runme(tmp_path, 'account = "from_runme"\n')
    ask = _ask_factory()

    _, account = extras.prompt_hpc_account(
        {"account": "from_configme"}, ask, machine="m", root=tmp_path)

    assert account == "from_configme"
    assert ask.calls == []


def test_prompt_asks_when_account_absent_everywhere(tmp_path, monkeypatch):
    """No recorded account anywhere → fall through to the interactive prompt."""
    monkeypatch.setattr(extras, "_slurm_accounts", lambda: [])
    _write_runme(tmp_path, 'hpc = "awi_albedo"\naccount = ""\n')   # blank == unset
    ask = _ask_factory(["typed_acct"])

    _, account = extras.prompt_hpc_account(
        {}, ask, machine="awi_albedo", root=tmp_path)

    assert account == "typed_acct"
    assert len(ask.calls) == 1


def test_prompt_warns_on_hpc_machine_mismatch(tmp_path, monkeypatch, capsys):
    """A file hpc that disagrees with the selected machine is honoured but
    flagged with a warning pointing at the file."""
    monkeypatch.setattr(extras, "_slurm_accounts", lambda: [])
    _write_runme(tmp_path, 'hpc = "other_machine"\naccount = "a"\n')

    hpc, _ = extras.prompt_hpc_account(
        {}, _ask_factory(), machine="awi_albedo", root=tmp_path)

    out = capsys.readouterr().out
    assert hpc == "other_machine"                 # file value still wins
    assert "warning" in out and "does not match" in out
