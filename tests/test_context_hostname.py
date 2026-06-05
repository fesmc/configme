"""Tests for ``context.hostname_machine`` — the short-hostname fast path and
the bounded-FQDN fallback. Regression: ``socket.getfqdn()`` used to be called
unconditionally, which hangs ~30 s on macOS / corporate networks with no PTR
record for the local IP."""

from configme import context


def test_hostname_machine_short_match(monkeypatch):
    monkeypatch.setattr(context.socket, "gethostname", lambda: "levante1")
    monkeypatch.setattr(context, "_hostname_map",
                        lambda: {"levante*": "dkrz_levante"})
    # If we ever fall through to FQDN, the test fails loudly: the bare hostname
    # already matched, so this path should not be touched.
    def _no_fqdn(_timeout):
        raise AssertionError("FQDN lookup should be skipped on a bare-hostname match")
    monkeypatch.setattr(context, "_fqdn_with_timeout", _no_fqdn)
    assert context.hostname_machine() == "dkrz_levante"


def test_hostname_machine_skips_fqdn_when_no_domain_patterns(monkeypatch):
    monkeypatch.setattr(context.socket, "gethostname", lambda: "Mac.fritz.box")
    monkeypatch.setattr(context, "_hostname_map",
                        lambda: {"levante*": "dkrz_levante"})

    def _no_fqdn(_timeout):
        raise AssertionError("no domain pattern in the map, no FQDN call expected")
    monkeypatch.setattr(context, "_fqdn_with_timeout", _no_fqdn)
    assert context.hostname_machine() is None


def test_hostname_machine_fqdn_fallback(monkeypatch):
    monkeypatch.setattr(context.socket, "gethostname", lambda: "login01")
    monkeypatch.setattr(context, "_hostname_map",
                        lambda: {"*.lvt.dkrz.de": "dkrz_levante"})
    monkeypatch.setattr(context, "_fqdn_with_timeout",
                        lambda _t: "login01.lvt.dkrz.de")
    assert context.hostname_machine() == "dkrz_levante"


def test_hostname_machine_fqdn_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(context.socket, "gethostname", lambda: "Mac.fritz.box")
    monkeypatch.setattr(context, "_hostname_map",
                        lambda: {"*.lvt.dkrz.de": "dkrz_levante"})
    monkeypatch.setattr(context, "_fqdn_with_timeout", lambda _t: None)
    assert context.hostname_machine() is None


def test_hostname_machine_empty_map(monkeypatch):
    monkeypatch.setattr(context, "_hostname_map", lambda: {})
    assert context.hostname_machine() is None
