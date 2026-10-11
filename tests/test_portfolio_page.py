def test_portfolio_page_requires_login(client):
    resp = client.get('/portfolio')
    assert resp.status_code in (302, 401)


def test_portfolio_page_renders_the_dom_contract(auth_client):
    resp = auth_client.get('/portfolio')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for dom_id in ('pf-book-value', 'pf-day-change', 'pf-unrealized',
                   'pf-position-count', 'pf-curve', 'pf-curve-range',
                   'pf-donut', 'pf-split', 'pf-table-body', 'pf-excluded'):
        assert f'id="{dom_id}"' in html, dom_id


def test_portfolio_page_pins_echarts_with_integrity(auth_client):
    html = auth_client.get('/portfolio').get_data(as_text=True)
    assert 'echarts@5.6.0' in html
    assert 'sha384-pPi0zxBAoDu6+JXW/C68UZLvBUUtU+7zonhif43rqj7pxsGyqyqzcian2Rj37Rss' in html
    assert 'crossorigin="anonymous"' in html


def test_portfolio_page_has_responsive_scaffolding(auth_client):
    """Shallow scaffolding checks only — proves the mobile/PWA CSS hooks
    exist in the rendered markup, not that the layout looks right. Visual
    verification is done by hand."""
    html = auth_client.get('/portfolio').get_data(as_text=True)
    assert 'max-width: 768px' in html
    assert 'overflow-x' in html
    assert 'safe-area-inset' in html


def test_portfolio_page_enables_safe_area_insets(auth_client):
    """env(safe-area-inset-*) resolves to its fallback (0) on every device
    without viewport-fit=cover — the previous version of this test only
    checked the CSS token existed, which passed even though the insets
    were permanently inert. Both checks together are the honest pair: the
    mechanism is enabled AND the rules that depend on it exist."""
    html = auth_client.get('/portfolio').get_data(as_text=True)
    assert 'viewport-fit=cover' in html
    assert 'safe-area-inset' in html
