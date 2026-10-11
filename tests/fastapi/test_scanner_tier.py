"""Scanner tier-gating tests.

Free tier sees top N ranked results; paid tiers see everything.
Pricing page sells this as "Top 5 / Full / Full".
"""

from unittest.mock import patch


def _fake_scanner_results(n):
    return {
        'results': [{'symbol': f'SYM{i}', 'best_verdict': 'GOOD',
                     'long': {'total': 100 - i, 'verdict': 'GOOD'},
                     'short': {'total': 50, 'verdict': 'AVOID'}}
                    for i in range(n)],
        'macro': {},
        'computed_at': '2026-04-17T00:00:00Z',
    }


class TestScannerTierGate:
    def test_free_tier_capped_at_five(self, key_client):
        free_client = key_client('free')
        with patch('carrac.caches.get_cached_scanner',
                   return_value=_fake_scanner_results(12)):
            resp = free_client.get('/api/scanner')
            assert resp.status_code == 200
            data = resp.json()
            assert len(data['results']) == 5
            assert data.get('total_available') == 12
            assert data.get('returned') == 5
            assert data.get('upgrade_to_see_all') is True

    def test_member_sees_all(self, key_client):
        member_client = key_client('member')
        with patch('carrac.caches.get_cached_scanner',
                   return_value=_fake_scanner_results(12)):
            resp = member_client.get('/api/scanner')
            assert resp.status_code == 200
            data = resp.json()
            assert len(data['results']) == 12
            # No truncation metadata on paid tiers
            assert 'upgrade_to_see_all' not in data

    def test_free_below_cap_no_metadata(self, key_client):
        free_client = key_client('free')
        with patch('carrac.caches.get_cached_scanner',
                   return_value=_fake_scanner_results(3)):
            resp = free_client.get('/api/scanner')
            assert resp.status_code == 200
            data = resp.json()
            assert len(data['results']) == 3
            assert 'upgrade_to_see_all' not in data
