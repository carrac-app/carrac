from carrac.reports.routes import _use_editorial

class Req:
    def __init__(self, args): self.args = args

def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("REPORT_EDITORIAL_ENABLED", raising=False)
    assert _use_editorial(Req({}), is_admin=False) is False

def test_flag_on_via_env(monkeypatch):
    monkeypatch.setenv("REPORT_EDITORIAL_ENABLED", "1")
    assert _use_editorial(Req({}), is_admin=False) is True

def test_admin_preview_override(monkeypatch):
    monkeypatch.delenv("REPORT_EDITORIAL_ENABLED", raising=False)
    assert _use_editorial(Req({"editorial": "1"}), is_admin=True) is True
    assert _use_editorial(Req({"editorial": "1"}), is_admin=False) is False


def test_classic_escape_hatch_beats_flag(monkeypatch):
    monkeypatch.setenv("REPORT_EDITORIAL_ENABLED", "1")
    assert _use_editorial(Req({"classic": "1"}), is_admin=False) is False
    assert _use_editorial(Req({"classic": "1"}), is_admin=True) is False
    assert _use_editorial(Req({}), is_admin=False) is True
