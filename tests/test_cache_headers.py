"""Tests for carrac.cache_headers — per-endpoint Cache-Control policies."""

from carrac.cache_headers import get_api_cache_header, apply_cache_headers


class TestGetApiCacheHeader:
    """Unit tests for the policy lookup function."""

    # -- Shared reads: public with max-age --

    def test_scanner_get_is_private(self):
        # Scanner is tier-gated: `private` prevents shared-cache cross-serving.
        h = get_api_cache_header('/api/scanner', 'GET')
        assert 'private' in h
        assert 'public' not in h
        assert 'max-age=300' in h

    def test_sentiment_get_is_public(self):
        h = get_api_cache_header('/api/market-sentiment', 'GET')
        assert 'public' in h
        assert 'max-age=300' in h

    def test_stream_get_is_private_short(self):
        h = get_api_cache_header('/api/stream', 'GET')
        assert 'private' in h
        assert 'max-age=60' in h

    def test_morning_report_latest_is_private(self):
        # Report responses vary by auth (tier gating + personal sections):
        # `public` let the nginx proxy cache cross-serve one caller's report
        # to every other caller. Must be `private`.
        h = get_api_cache_header('/api/report/morning/latest', 'GET')
        assert 'private' in h
        assert 'public' not in h

    def test_all_report_reads_are_private(self):
        for p in ('/api/report/morning/latest', '/api/report/morning/recent',
                  '/api/report/weekly/latest', '/api/report/afternoon/latest'):
            h = get_api_cache_header(p, 'GET')
            assert 'private' in h, p
            assert 'public' not in h, p

    def test_quant_endpoints_are_private(self):
        # Gated by the quant_engine tier feature: a shared cache must not
        # hand one plan's response to a caller on another.
        h = get_api_cache_header('/api/quant/health', 'GET')
        assert 'private' in h
        assert 'public' not in h
        assert 'max-age=3600' in h

    def test_tier_or_user_varying_reads_are_never_public(self):
        # Each of these answers differently per caller: tier-gated, tier-shaped
        # (volume-profile timeframes, news custom feeds) or per-user (the
        # stream is filtered to the caller's watchlist).
        for p in ('/api/quant/snapshot', '/api/stream', '/api/transitions',
                  '/api/volume-profile/BTC', '/api/market-news'):
            h = get_api_cache_header(p, 'GET')
            assert 'private' in h, p
            assert 'public' not in h, p

    def test_symbol_lookup_is_long_cache(self):
        h = get_api_cache_header('/api/symbol-lookup', 'GET')
        assert 'public' in h
        assert 'max-age=86400' in h

    def test_stale_while_revalidate_present(self):
        h = get_api_cache_header('/api/scanner', 'GET')
        assert 'stale-while-revalidate' in h

    # -- User-specific reads: private --

    def test_alerts_get_is_private(self):
        h = get_api_cache_header('/api/alerts', 'GET')
        assert 'private' in h
        assert 'no-cache' not in h

    def test_plays_get_is_private(self):
        h = get_api_cache_header('/api/plays', 'GET')
        assert 'private' in h

    def test_journal_get_is_private(self):
        h = get_api_cache_header('/api/journal', 'GET')
        assert 'private' in h

    def test_watchlist_get_is_private(self):
        h = get_api_cache_header('/api/watchlist', 'GET')
        assert 'private' in h

    # -- Writes: no-cache --

    def test_alerts_post_is_no_cache(self):
        h = get_api_cache_header('/api/alerts', 'POST')
        assert 'no-cache' in h
        assert 'no-store' in h

    def test_plays_post_is_no_cache(self):
        h = get_api_cache_header('/api/plays', 'POST')
        assert 'no-cache' in h

    def test_journal_patch_is_no_cache(self):
        h = get_api_cache_header('/api/journal/trades/42', 'PATCH')
        assert 'no-cache' in h

    def test_ai_any_method_is_no_cache(self):
        for method in ('GET', 'POST', 'PUT'):
            h = get_api_cache_header('/api/ai/chat', method)
            assert 'no-cache' in h, f"AI {method} should be no-cache"

    def test_admin_any_method_is_no_cache(self):
        h = get_api_cache_header('/api/admin/users', 'GET')
        assert 'no-cache' in h

    # -- Default fallback --

    def test_unknown_endpoint_defaults_to_no_cache(self):
        h = get_api_cache_header('/api/unknown-future-endpoint', 'GET')
        assert 'no-cache' in h

    # -- Report POST is no-cache (generation) --

    def test_report_post_is_no_cache(self):
        h = get_api_cache_header('/api/report/morning', 'POST')
        assert 'no-cache' in h


class TestApplyCacheHeaders:
    """Integration tests for the after_request handler."""

    class FakeResponse:
        def __init__(self, content_type='application/json'):
            self.headers = {}
            self.content_type = content_type

    def test_static_gets_immutable(self):
        resp = self.FakeResponse()
        apply_cache_headers(resp, '/static/js/app.js', 'GET', 'application/javascript')
        assert 'immutable' in resp.headers['Cache-Control']

    def test_static_404_is_no_store_not_immutable(self):
        """A 404 static response must NOT be cached immutably —
        else a browser freezes the broken URL for a year (mid-deploy rsync
        --delete window strands real visitors)."""
        resp = self.FakeResponseWithStatus(404, 'text/html')
        apply_cache_headers(resp, '/static/js/vanished.js', 'GET', 'text/html')
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc
        assert 'immutable' not in cc
        assert 'max-age=31536000' not in cc

    def test_static_200_still_immutable(self):
        """Pins that a real 200 static asset keeps immutable."""
        resp = self.FakeResponseWithStatus(200, 'application/javascript')
        apply_cache_headers(resp, '/static/js/app.js', 'GET', 'application/javascript')
        assert 'immutable' in resp.headers['Cache-Control']

    def test_html_gets_no_cache(self):
        resp = self.FakeResponse('text/html; charset=utf-8')
        apply_cache_headers(resp, '/', 'GET', 'text/html; charset=utf-8')
        assert 'no-cache' in resp.headers['Cache-Control']

    def test_api_scanner_gets_private(self):
        # Scanner is tier-gated: must be private to block shared-cache leaks.
        resp = self.FakeResponse()
        apply_cache_headers(resp, '/api/scanner', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'private' in cc
        assert 'public' not in cc

    def test_api_chart_not_touched(self):
        resp = self.FakeResponse()
        apply_cache_headers(resp, '/api/chart/BTC', 'GET', 'application/json')
        assert 'Cache-Control' not in resp.headers  # chart manages its own

    def test_service_worker_no_cache(self):
        resp = self.FakeResponse()
        apply_cache_headers(resp, '/static/service-worker.js', 'GET', 'application/javascript')
        assert 'must-revalidate' in resp.headers['Cache-Control']

    # -- Error-response gate: non-2xx must never get a cacheable policy header --

    class FakeResponseWithStatus:
        def __init__(self, status_code, content_type='application/json'):
            self.headers = {}
            self.content_type = content_type
            self.status_code = status_code

    def test_error_responses_get_no_store_not_policy_header_401(self):
        """A 401 on /api/report/morning/latest must NOT receive the public policy."""
        resp = self.FakeResponseWithStatus(401)
        apply_cache_headers(resp, '/api/report/morning/latest', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc, "401 response must have no-store"
        assert 'public' not in cc, "401 response must NOT have public cache-control"
        assert 'max-age=3600' not in cc, "401 response must NOT have max-age=3600"
        assert resp.headers.get('Pragma') == 'no-cache'
        assert resp.headers.get('Expires') == '0'

    def test_error_responses_get_no_store_not_policy_header_403(self):
        """A 403 on a public API endpoint must NOT receive the policy header."""
        resp = self.FakeResponseWithStatus(403)
        apply_cache_headers(resp, '/api/scanner', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc
        assert 'public' not in cc

    def test_error_responses_get_no_store_not_policy_header_404(self):
        """A 404 on an API endpoint must NOT receive the policy header."""
        resp = self.FakeResponseWithStatus(404)
        apply_cache_headers(resp, '/api/quant/health', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc
        assert 'public' not in cc

    def test_error_responses_get_no_store_not_policy_header_500(self):
        """A 500 on an API endpoint must NOT receive a cacheable policy."""
        resp = self.FakeResponseWithStatus(500)
        apply_cache_headers(resp, '/api/report/morning/latest', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'no-store' in cc
        assert 'public' not in cc

    def test_200_responses_still_get_policy_header(self):
        """200 on /api/report/morning/latest gets the (now private) policy header."""
        resp = self.FakeResponseWithStatus(200)
        apply_cache_headers(resp, '/api/report/morning/latest', 'GET', 'application/json')
        cc = resp.headers['Cache-Control']
        assert 'private' in cc
        assert 'max-age=300' in cc

    def test_201_response_on_write_endpoint_is_no_store(self):
        """A 201 (still 2xx) on a private endpoint stays private (not no-store)."""
        resp = self.FakeResponseWithStatus(201)
        apply_cache_headers(resp, '/api/plays', 'POST', 'application/json')
        cc = resp.headers['Cache-Control']
        # POST /api/plays matches the write no-cache policy, not an error gate
        assert 'no-store' in cc
