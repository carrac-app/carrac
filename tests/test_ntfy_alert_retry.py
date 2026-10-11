"""A failed send must not consume the alert transition.

The integrity monitor alerts on transitions: a finding pages once, then goes
quiet so a persistent problem does not spam. Correct — but the state was
persisted even if the send FAILED, an alert raised while the endpoint was
unreachable would be destroyed rather than delayed.

For example, a real GOLD/OIL/SILVER row collapse or genuine session holes would
be filed as "already known" while ntfy was timing out, and never page. The modes are correlated — the
outage that stops the send is often the thing worth alerting about
(a host with no route to ntfy.sh at all is the extreme case).

These drive main() end to end against a temp DATA_DIR so the state file is the
real artifact, not a mock.
"""
import datetime
import importlib.util
import json
import pathlib


_SPEC = importlib.util.spec_from_file_location(
    "cache_integrity_check",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cache_integrity_check.py",
)


def _load(tmp_path, monkeypatch):
    """Fresh module instance bound to a temp data dir + state files."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("CARRAC_DATA_DIR", str(data))
    monkeypatch.setenv("CARRAC_CACHE_BASELINE", str(tmp_path / "rows.json"))
    monkeypatch.setenv("CARRAC_CACHE_STATE", str(tmp_path / "state.json"))
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod, data


def _write_cache(data_dir, symbol, rows):
    p = data_dir / f"{symbol}_daily.csv"
    p.write_text(",open,high,low,close,volume\n" + "".join(
        f"{d},1,1,1,1,1\n" for d in rows))
    return p


def _dates(n, end="2026-08-06"):
    e = datetime.date.fromisoformat(end)
    out, d = [], e
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return sorted(out)


def _make_collapse(mod, data, tmp_path):
    """A symbol whose row count collapses vs its recorded high-water mark."""
    _write_cache(data, "GOLD", _dates(50))
    (tmp_path / "rows.json").write_text(json.dumps({"GOLD": 500}))


def test_failed_send_leaves_the_finding_unrecorded(tmp_path, monkeypatch):
    mod, data = _load(tmp_path, monkeypatch)
    _make_collapse(mod, data, tmp_path)
    monkeypatch.setattr(mod, "notify", lambda *a, **k: False)   # send fails

    assert mod.main() == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state == [], f"unannounced finding was recorded as known: {state}"


def test_successful_send_records_it_so_it_does_not_repeat(tmp_path, monkeypatch):
    mod, data = _load(tmp_path, monkeypatch)
    _make_collapse(mod, data, tmp_path)
    monkeypatch.setattr(mod, "notify", lambda *a, **k: True)

    assert mod.main() == 1
    assert json.loads((tmp_path / "state.json").read_text()) == ["GOLD:ROWS"]


def test_the_alert_actually_retries_on_the_next_run(tmp_path, monkeypatch):
    """The whole point: run 1 fails to send, run 2 must treat it as NEW."""
    mod, data = _load(tmp_path, monkeypatch)
    _make_collapse(mod, data, tmp_path)

    sent = []
    monkeypatch.setattr(mod, "notify",
                        lambda title, *a, **k: (sent.append(title), False)[1])
    mod.main()
    assert len(sent) == 1

    # second run, endpoint now reachable
    monkeypatch.setattr(mod, "notify",
                        lambda title, *a, **k: (sent.append(title), True)[1])
    mod.main()
    assert len(sent) == 2, "the lost alert was never retried"
    assert json.loads((tmp_path / "state.json").read_text()) == ["GOLD:ROWS"]


def test_failed_recovery_notice_also_retries(tmp_path, monkeypatch):
    """A 'cleared' notice is an alert too — losing it strands the operator
    believing a problem is still open."""
    mod, data = _load(tmp_path, monkeypatch)
    _write_cache(data, "GOLD", _dates(50))
    (tmp_path / "rows.json").write_text(json.dumps({"GOLD": 50}))   # no collapse
    (tmp_path / "state.json").write_text(json.dumps(["GOLD:ROWS"]))  # was degraded

    monkeypatch.setattr(mod, "notify", lambda *a, **k: False)
    assert mod.main() == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state == ["GOLD:ROWS"], "unsent recovery dropped the prior state"

    monkeypatch.setattr(mod, "notify", lambda *a, **k: True)
    mod.main()
    assert json.loads((tmp_path / "state.json").read_text()) == []


def test_notify_returns_false_when_the_endpoint_is_unreachable(tmp_path, monkeypatch):
    """Exercise the real function, not a stub — it must report failure, not
    swallow it. This is what the caller's contract rests on."""
    mod, _ = _load(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise OSError("timed out")
    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    assert mod.notify("t", "b", "high", "warning") is False


def test_notify_targets_the_configured_base_url(tmp_path, monkeypatch):
    """The endpoint must be overridable, not hardcoded."""
    monkeypatch.setenv("NTFY_BASE", "http://example.invalid:9999")
    mod, _ = _load(tmp_path, monkeypatch)
    assert mod.NTFY_BASE == "http://example.invalid:9999"

    seen = {}
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.setdefault("url", req.full_url) and None
                        or type("R", (), {"read": lambda s: b""})())
    mod.notify("t", "b", "default", "x")
    assert seen["url"].startswith("http://example.invalid:9999/")
