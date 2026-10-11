"""LANDING_HOSTS: hostnames that serve only the public landing site. Sign-in
pages, and signed-in visitors, arriving on one of them are sent to the app's
own address (SITE_URL). Unset, nothing is redirected — whatever the Host."""

from __future__ import annotations

import pytest


@pytest.fixture
def landing(monkeypatch):
    monkeypatch.setenv("LANDING_HOSTS", "landing.example.test, www.landing.example.test")
    monkeypatch.setenv("SITE_URL", "https://app.example.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


@pytest.mark.parametrize("host", ["carrac.cc", "landing.example.test"])
def test_unset_redirects_nothing(client, monkeypatch, host):
    monkeypatch.delenv("LANDING_HOSTS", raising=False)
    resp = client.get("/login", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["landing.example.test", "www.landing.example.test"])
def test_sign_in_page_on_a_landing_host_goes_to_the_app(client, landing, host):
    resp = client.get("/login?next=/journal", headers={"Host": host})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://app.example.test/login?next=/journal"


def test_signed_in_visitor_on_a_landing_host_goes_to_the_app(client, landing):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
    # The test client scopes cookies to "localhost"; present the same session
    # cookie to the landing host.
    sid = client.get_cookie("session").value
    client.set_cookie("session", sid, domain="landing.example.test")
    resp = client.get("/mcp", headers={"Host": "landing.example.test"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://app.example.test/mcp"


def test_anonymous_visitor_on_a_landing_host_stays(client, landing):
    resp = client.get("/mcp", headers={"Host": "landing.example.test"})
    assert resp.status_code != 302


def test_the_app_host_itself_is_never_redirected(client, landing):
    resp = client.get("/login", headers={"Host": "app.example.test"})
    assert resp.status_code == 200
