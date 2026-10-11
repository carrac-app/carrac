"""GA cutover: with the flag on,
a LOGGED-IN NON-ADMIN member gets the editorial page at /report/morning —
complete, disclaimer present, and free of admin-only surfaces."""


class TestEditorialGA:
    def test_member_gets_editorial_when_flag_on(self, auth_client, monkeypatch):
        monkeypatch.setenv("REPORT_EDITORIAL_ENABLED", "1")
        resp = auth_client.get('/report/morning')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Morning Briefing" in html            # editorial masthead
        assert "not financial advice" in html        # disclaimer survives
        assert 'id="ed-desk"' not in html            # desk flag off + non-admin
        assert "PERSONAL_SECTION_START" not in html  # operator sections never leak

    def test_classic_hatch_serves_old_page(self, auth_client, monkeypatch):
        monkeypatch.setenv("REPORT_EDITORIAL_ENABLED", "1")
        resp = auth_client.get('/report/morning?classic=1')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Morning Briefing" not in html or "tab-btn" in html or "Loading voice briefing" in html
