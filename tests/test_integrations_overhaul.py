"""Analytics + CRM integration backend tests.

Covers carrac/integrations.py, carrac/auth/routes.py's signup-field wiring,
carrac/billing/routes.py's Paddle handlers, and carrac/waitlist_release.py.

All HTTP is mocked at `_espocrm_request` / `_posthog_capture` / `_posthog_identify`
(carrac.integrations boundaries) — no live PostHog/Espo network calls.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sync_fire_and_forget(monkeypatch):
    """Run integrations.py's fire-and-forget calls synchronously so tests can
    assert on side effects without waiting on the background ThreadPoolExecutor.

    Every hook in carrac.integrations calls the *module-level* name
    `_fire_and_forget(...)`, so patching the module attribute affects every
    caller inside the module.
    """
    import carrac.integrations as integ

    def _sync(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr(integ, '_fire_and_forget', _sync)
    return _sync


@pytest.fixture(autouse=True)
def _reset_posthog_config_cache():
    """_posthog_api_key / _posthog_host are cached module globals ('once per
    worker') — reset around every test so one test's cache can't leak into
    another's assertions."""
    import carrac.integrations as integ
    orig_key, orig_host = integ._posthog_api_key, integ._posthog_host
    integ._posthog_api_key = None
    integ._posthog_host = None
    yield
    integ._posthog_api_key = orig_key
    integ._posthog_host = orig_host


# ── UTM + alias through signup ──────────────────────────────────────

class TestUTMAndAlias:
    def test_signup_with_ph_anon_id_fires_two_aliases(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        captures = []
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: captures.append(a))
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'id': 'c1'})

        integ.on_user_signup(42, 'new@test.com', ph_anon_id='ph_abc123')

        alias_calls = [c for c in captures if c[1] == '$create_alias']
        assert len(alias_calls) == 2
        assert alias_calls[0] == ('42', '$create_alias', {'alias': 'ph_abc123', 'distinct_id': '42'})
        assert alias_calls[1] == ('42', '$create_alias', {'alias': 'new@test.com', 'distinct_id': '42'})

    def test_signup_without_ph_anon_id_fires_one_alias(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        captures = []
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: captures.append(a))
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'id': 'c1'})

        integ.on_user_signup(43, 'noanon@test.com')

        alias_calls = [c for c in captures if c[1] == '$create_alias']
        assert len(alias_calls) == 1
        assert alias_calls[0] == ('43', '$create_alias', {'alias': 'noanon@test.com', 'distinct_id': '43'})

    def test_set_payload_contains_utm_fields_and_signup_date(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        identify_calls = []
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: identify_calls.append(a))
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'id': 'c1'})

        integ.on_user_signup(7, 'u@test.com', signup_campaign='spring_sale',
                              signup_content='banner', signup_term='trading')

        assert identify_calls, "expected _posthog_identify to be called"
        _uid, props = identify_calls[0]
        assert props['utm_campaign'] == 'spring_sale'
        assert props['utm_content'] == 'banner'
        assert props['utm_term'] == 'trading'
        assert 'signup_date' in props

    def test_set_payload_omits_absent_utm_fields(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        identify_calls = []
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: identify_calls.append(a))
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'id': 'c1'})

        integ.on_user_signup(8, 'bare@test.com')

        _uid, props = identify_calls[0]
        assert 'utm_campaign' not in props
        assert 'utm_content' not in props
        assert 'utm_term' not in props
        assert 'signup_date' in props

    def test_espo_payload_contains_lead_source_and_lifecycle_stage(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        espo_calls = []
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))

        def fake_request(method, endpoint, data=None):
            espo_calls.append((method, endpoint, data))
            return {'id': 'c1'}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_user_signup(9, 'lead@test.com', signup_source='twitter')

        post_calls = [c for c in espo_calls if c[0] == 'POST' and c[1] == 'Contact']
        assert len(post_calls) == 1
        payload = post_calls[0][2]
        assert payload['cLeadSource'] == 'Social'
        assert payload['cLifecycleStage'] == 'registered'

    @pytest.mark.parametrize('source,medium,expected', [
        ('waitlist', None, 'Waitlist'),
        ('invite', None, 'Invite'),
        ('invite_code', None, 'Invite'),
        ('referral', None, 'Referral'),
        ('twitter', None, 'Social'),
        ('x', None, 'Social'),
        ('reddit', None, 'Social'),
        ('facebook', None, 'Social'),
        ('linkedin', None, 'Social'),
        ('google', None, 'Search'),
        ('bing', None, 'Search'),
        ('search', None, 'Search'),
        ('newsletter', None, 'Newsletter'),
        ('email', None, 'Newsletter'),
        (None, None, 'Web Site'),
        ('direct', None, 'Web Site'),
        ('web', None, 'Web Site'),
        ('some_unknown_thing', None, 'Other'),
        (None, 'search', 'Search'),  # falls back to medium when source is empty
    ])
    def test_map_lead_source_table(self, source, medium, expected):
        from carrac.integrations import _map_lead_source
        assert _map_lead_source(source, medium) == expected

    def test_waitlist_join_sets_lifecycle_and_lead_source(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        calls = []

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            return {'id': 'wc1'}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_waitlist_join('lead2@test.com', source='newsletter')

        post_calls = [c for c in calls if c[0] == 'POST' and c[1] == 'Contact']
        assert len(post_calls) == 1
        assert post_calls[0][2]['cLifecycleStage'] == 'waitlist'
        assert post_calls[0][2]['cLeadSource'] == 'Newsletter'


# ── sync failure visibility + duplicate-mint fix ────────────────────

class TestFindContactReturnShape:
    def test_lookup_ok_true_when_found(self, monkeypatch):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'list': [{'id': 'c1'}]})
        contact, ok = integ._espocrm_find_contact_by_email('a@test.com')
        assert ok is True
        assert contact == {'id': 'c1'}

    def test_lookup_ok_true_when_not_found(self, monkeypatch):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: {'list': []})
        contact, ok = integ._espocrm_find_contact_by_email('a@test.com')
        assert contact is None
        assert ok is True

    def test_lookup_ok_false_on_exception(self, monkeypatch):
        import carrac.integrations as integ

        def _raise(*a, **k):
            raise RuntimeError("espo down")
        monkeypatch.setattr(integ, '_espocrm_request', _raise)
        contact, ok = integ._espocrm_find_contact_by_email('a@test.com')
        assert contact is None
        assert ok is False


class TestCreateIfMissingGating:
    """on_user_signup / on_waitlist_join / on_login must NOT create a
    duplicate contact when the Espo lookup itself failed (outage)."""

    @pytest.mark.parametrize('hook_name,call', [
        ('on_user_signup', lambda integ: integ.on_user_signup(1, 'sig@test.com')),
        ('on_waitlist_join', lambda integ: integ.on_waitlist_join('wl@test.com')),
        ('on_login', lambda integ: integ.on_login(2, 'log@test.com')),
    ])
    def test_no_create_when_lookup_failed(self, monkeypatch, sync_fire_and_forget, hook_name, call, caplog):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, False))
        post_calls = []
        monkeypatch.setattr(
            integ, '_espocrm_request',
            lambda method, endpoint, data=None: post_calls.append((method, endpoint)),
        )
        with caplog.at_level('ERROR', logger='carrac.integrations'):
            call(integ)
        assert not any(m == 'POST' for m, _ in post_calls), f"{hook_name} issued a create despite lookup_ok=False"
        assert any('skipping' in r.message.lower() for r in caplog.records)

    @pytest.mark.parametrize('call', [
        lambda integ: integ.on_user_signup(3, 'sig2@test.com'),
        lambda integ: integ.on_waitlist_join('wl2@test.com'),
        lambda integ: integ.on_login(4, 'log2@test.com'),
    ])
    def test_creates_when_lookup_ok(self, monkeypatch, sync_fire_and_forget, call):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        post_calls = []

        def fake_request(method, endpoint, data=None):
            post_calls.append((method, endpoint, data))
            return {'id': 'new1'}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        call(integ)
        assert any(m == 'POST' and e == 'Contact' for m, e, _ in post_calls)


class TestWaitlistInvitedDistinctId:
    def test_waitlist_invited_uses_email_as_distinct_id(self, monkeypatch):
        from user_db import user_db as _udb
        from carrac import waitlist_release as wr
        import carrac.email as em
        import carrac.integrations as integ

        monkeypatch.setattr(_udb, 'pick_next_waitlist', lambda n: [{'email': 'invitee@test.com'}])
        monkeypatch.setattr(_udb, 'generate_invite_code', lambda *a, **k: 'CODE1234')
        monkeypatch.setattr(_udb, 'mark_waitlisted_invited', lambda *a, **k: True)
        monkeypatch.setattr(em, 'send_invite_email', lambda *a, **k: True)
        monkeypatch.setattr(wr, '_espo_mark_invited', lambda *a, **k: None)

        calls = []
        monkeypatch.setattr(integ, 'on_funnel_event', lambda *a, **k: calls.append(a))

        rc = wr.run(count=1, created_by=1, dry_run=False)

        assert rc == 0
        assert calls, "on_funnel_event was not called"
        assert calls[0][0] == 'invitee@test.com'
        assert calls[0][1] == 'waitlist_invited'

    def test_espo_mark_invited_sets_lifecycle_stage(self, monkeypatch):
        from carrac import waitlist_release as wr
        import carrac.integrations as integ

        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c9'}, True))
        put_calls = []

        def fake_request(method, endpoint, data=None):
            put_calls.append((method, endpoint, data))
            return {}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        wr._espo_mark_invited('invited@test.com', 'wave1')

        assert len(put_calls) == 1
        assert put_calls[0][2]['cLifecycleStage'] == 'invited'

    def test_espo_mark_invited_logs_on_exception(self, monkeypatch, caplog):
        from carrac import waitlist_release as wr
        import carrac.integrations as integ

        def _raise(*a, **k):
            raise RuntimeError("espo down")
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', _raise)

        with caplog.at_level('ERROR', logger='carrac.waitlist_release'):
            wr._espo_mark_invited('boom@test.com', None)  # must not raise

        assert any('failed' in r.message.lower() for r in caplog.records)


# ── billing events + tier-change context ────────────────────────────

class TestTierChangeOpportunitySync:
    def test_upgrade_creates_opportunity_exactly_once(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))

        calls = []
        state = {'has_opp': False}

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if method == 'GET' and endpoint.startswith('Opportunity'):
                return {'list': [{'id': 'opp1'}]} if state['has_opp'] else {'list': []}
            if method == 'POST' and endpoint == 'Opportunity':
                state['has_opp'] = True
                return {'id': 'opp1'}
            return {}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_tier_change(1, 'up@test.com', 'free', 'premium')
        integ.on_tier_change(1, 'up@test.com', 'free', 'premium')  # second call — no duplicate

        post_opp_calls = [c for c in calls if c[0] == 'POST' and c[1] == 'Opportunity']
        assert len(post_opp_calls) == 1
        assert post_opp_calls[0][2]['stage'] == 'Closed Won'
        assert post_opp_calls[0][2]['amount'] == 29
        assert post_opp_calls[0][2]['contactId'] == 'c1'

    def test_downgrade_flips_opportunity_to_closed_lost(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))

        calls = []

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if method == 'GET' and endpoint.startswith('Opportunity'):
                return {'list': [{'id': 'opp1'}]}
            return {}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_tier_change(1, 'down@test.com', 'premium', 'free')

        put_calls = [c for c in calls if c[0] == 'PUT' and c[1] == 'Opportunity/opp1']
        assert len(put_calls) == 1
        assert put_calls[0][2] == {'stage': 'Closed Lost'}

    def test_lifecycle_stage_paying_on_upgrade(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))

        calls = []

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if method == 'GET' and endpoint.startswith('Opportunity'):
                return {'list': []}
            return {'id': 'opp1'}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_tier_change(1, 'up2@test.com', 'free', 'premium')

        contact_put = [c for c in calls if c[0] == 'PUT' and c[1] == 'Contact/c1']
        assert contact_put and contact_put[0][2]['cLifecycleStage'] == 'paying'

    def test_lifecycle_stage_activated_on_downgrade_to_free(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))

        calls = []

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if method == 'GET' and endpoint.startswith('Opportunity'):
                return {'list': [{'id': 'opp1'}]}
            return {}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_tier_change(1, 'down2@test.com', 'premium', 'free')

        contact_put = [c for c in calls if c[0] == 'PUT' and c[1] == 'Contact/c1']
        assert contact_put and contact_put[0][2]['cLifecycleStage'] == 'activated'

    def test_tier_change_creates_contact_when_missing(self, monkeypatch, sync_fire_and_forget):
        """A tier change on a contact-less user creates the contact (it must not silently no-op)."""
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))

        calls = []

        def fake_request(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if method == 'GET' and endpoint.startswith('Opportunity'):
                return {'list': []}
            if method == 'POST' and endpoint == 'Contact':
                return {'id': 'new_c1'}
            return {}
        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        integ.on_tier_change(5, 'nocontact@test.com', 'free', 'premium')

        post_contact = [c for c in calls if c[0] == 'POST' and c[1] == 'Contact']
        assert len(post_contact) == 1
        post_opp = [c for c in calls if c[0] == 'POST' and c[1] == 'Opportunity']
        assert len(post_opp) == 1, "Opportunity should be created against the newly-created contact"


class TestOpportunitySyncConcurrency:
    """Two near-simultaneous on_tier_change calls run in the 4-worker
    fire-and-forget executor (carrac/integrations.py's `_executor`). Before
    the fix, both could observe "no Opportunity" via the GET-check and then
    both POST-create, doubling the Opportunity. _sync_opportunity_for_tier_change
    now holds _OPPORTUNITY_SYNC_LOCK across the GET-check + POST-create."""

    def test_concurrent_sync_creates_exactly_one_opportunity(self, monkeypatch):
        import threading
        import time
        import carrac.integrations as integ

        state = {'opps': []}
        state_lock = threading.Lock()

        def fake_request(method, endpoint, data=None):
            if method == 'GET' and endpoint.startswith('Opportunity'):
                # Artificial delay between the check and the create widens
                # the race window — without _OPPORTUNITY_SYNC_LOCK
                # serializing callers, both threads would pass this check
                # before either one creates.
                time.sleep(0.05)
                with state_lock:
                    has_opp = len(state['opps']) > 0
                return {'list': [{'id': 'opp1'}]} if has_opp else {'list': []}
            if method == 'POST' and endpoint == 'Opportunity':
                with state_lock:
                    state['opps'].append(data)
                return {'id': 'opp1'}
            return {}

        monkeypatch.setattr(integ, '_espocrm_request', fake_request)

        barrier = threading.Barrier(2)

        def _run():
            barrier.wait()
            integ._sync_opportunity_for_tier_change('c1', 'race@test.com', 'free', 'premium')

        t1 = threading.Thread(target=_run)
        t2 = threading.Thread(target=_run)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not t1.is_alive() and not t2.is_alive()
        assert len(state['opps']) == 1


class TestBillingEvent:
    def test_on_billing_event_fires_capture_and_set(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        from user_db import user_db as _udb

        monkeypatch.setattr(_udb, 'get_ai_usage_stats', lambda uid: {
            'total_spent_cents': 100, 'total_deposited_cents': 2500, 'balance_cents': 2400,
        })
        identify_calls = []
        capture_calls = []
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: identify_calls.append(a))
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: capture_calls.append(a))

        integ.on_billing_event(5, 'billing@test.com', 'topup', 2500)

        assert identify_calls and identify_calls[0][1]['ai_total_deposited_cents'] == 2500
        assert capture_calls and capture_calls[0][1] == 'billing_topup'
        assert capture_calls[0][2]['amount_cents'] == 2500


class TestPaddleWebhookOldTier:
    def test_subscription_updated_passes_real_old_tier(self, monkeypatch):
        import carrac.billing.routes as br
        from user_db import user_db as _udb
        from carrac.config import Config
        import carrac.integrations as integ

        monkeypatch.setattr(br, '_resolve_user_from_event', lambda d: (7, 'ctm_y'))

        state = {'n': 0}

        def _get_user_by_id(uid):
            state['n'] += 1
            # 1st call: snapshot BEFORE the flip. 2nd call: AFTER (post-commit sync).
            tier = 'free' if state['n'] == 1 else 'premium'
            return {'id': uid, 'tier': tier, 'email': 'up@test.com'}
        monkeypatch.setattr(_udb, 'get_user_by_id', _get_user_by_id)
        monkeypatch.setattr(_udb, 'update_tier_only', lambda *a, **k: True)
        monkeypatch.setattr(_udb, 'process_paddle_event_atomic', lambda eid, uid, work: (work() or True))
        monkeypatch.setattr(Config, 'paddle_price_to_tier', staticmethod(lambda: {'pri_premium': 'premium'}))

        captured = []
        monkeypatch.setattr(integ, 'on_tier_change', lambda *a: captured.append(a))

        br._handle_subscription_updated({'items': [{'price': {'id': 'pri_premium'}}]}, event_id='evt_2')

        assert captured == [(7, 'up@test.com', 'free', 'premium')]


class TestPaddleTopupWiresOnBillingEvent:
    def test_topup_fires_on_billing_event(self, monkeypatch):
        import carrac.billing.routes as br
        from user_db import user_db as _udb
        from carrac.config import Config
        import carrac.integrations as integ

        monkeypatch.setattr(br, '_resolve_user_from_event', lambda d: (1, 'ctm_x'))
        monkeypatch.setattr(_udb, 'get_user_by_id',
                             lambda uid: {'id': uid, 'tier': 'premium', 'email': 'topup@test.com'})
        monkeypatch.setattr(_udb, 'set_paddle_customer_id', lambda *a, **k: None)
        monkeypatch.setattr(_udb, 'add_ai_credits', lambda *a, **k: None)
        monkeypatch.setattr(_udb, 'record_ai_deposit', lambda *a, **k: None)
        monkeypatch.setattr(_udb, 'process_paddle_event_atomic', lambda eid, uid, work: (work() or True))
        monkeypatch.setattr(Config, 'paddle_price_to_tier', staticmethod(lambda: {}))
        monkeypatch.setattr(Config, 'paddle_price_to_credits', staticmethod(lambda: {'pri_topup_25': 2500}))
        monkeypatch.setattr(br, '_tier_allows_topup', lambda uid: True)

        calls = []
        monkeypatch.setattr(integ, 'on_billing_event', lambda *a, **k: calls.append(a))

        br._handle_transaction_completed(
            {'items': [{'price': {'id': 'pri_topup_25'}}], 'customer_id': 'ctm_x'},
            event_id='evt_topup_1',
        )

        assert calls == [(1, 'topup@test.com', 'topup', 2500)]


# ── activity heartbeat ───────────────────────────────────────────────

class TestActivityHeartbeat:
    @pytest.fixture(autouse=True)
    def _no_redis(self, monkeypatch):
        """Force the fallback-dict throttle path deterministically, regardless
        of whatever REDIS_URL happens to be set in the ambient dev environment."""
        import carrac.caches as caches_mod
        monkeypatch.setattr(caches_mod, '_get_redis', lambda: None)

    def test_two_calls_within_window_one_espo_write(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_activity_heartbeat_fallback', {})
        put_calls = []
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: put_calls.append(a))
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        from user_db import user_db as _udb
        monkeypatch.setattr(_udb, 'update_last_active', lambda uid: None)

        integ.on_activity(999001, 'heartbeat@test.com')
        integ.on_activity(999001, 'heartbeat@test.com')

        assert len(put_calls) == 1

    def test_throttle_key_is_per_user(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_activity_heartbeat_fallback', {})
        put_calls = []
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: ({'id': 'c1'}, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: put_calls.append(a))
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        from user_db import user_db as _udb
        monkeypatch.setattr(_udb, 'update_last_active', lambda uid: None)

        integ.on_activity(999002, 'a@test.com')
        integ.on_activity(999003, 'b@test.com')

        assert len(put_calls) == 2

    def test_anonymous_request_is_noop(self, monkeypatch):
        import carrac.integrations as integ
        fire_calls = []
        monkeypatch.setattr(integ, '_fire_and_forget', lambda *a, **k: fire_calls.append(a))

        integ.on_activity(None, None)

        assert fire_calls == []

    def test_calls_update_last_active(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_activity_heartbeat_fallback', {})
        monkeypatch.setattr(integ, '_espocrm_find_contact_by_email', lambda email: (None, True))
        monkeypatch.setattr(integ, '_espocrm_request', lambda *a, **k: None)
        monkeypatch.setattr(integ, '_posthog_identify', lambda *a, **k: None)
        from user_db import user_db as _udb
        calls = []
        monkeypatch.setattr(_udb, 'update_last_active', lambda uid: calls.append(uid))

        integ.on_activity(999004, 'active@test.com')

        assert calls == [999004]


# ── hygiene ───────────────────────────────────────────────────────────

class TestHygiene:
    def test_on_funnel_event_defined_exactly_once(self):
        import inspect
        import carrac.integrations as integ
        src = inspect.getsource(integ)
        assert src.count('def on_funnel_event') == 1

    def test_on_funnel_event_anon_fallback_survives(self, monkeypatch, sync_fire_and_forget):
        """The surviving definition (the one that was actually executing, since
        Python's later def wins) falls back to 'anon' rather than no-op'ing —
        callers like waitlist_release rely on this."""
        import carrac.integrations as integ
        captures = []
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: captures.append(a))

        integ.on_funnel_event(None, 'anon_event', {'x': 1})

        assert captures == [('anon', 'anon_event', {'x': 1})]

    def test_on_funnel_event_fires_capture_with_user_id(self, monkeypatch, sync_fire_and_forget):
        import carrac.integrations as integ
        captures = []
        monkeypatch.setattr(integ, '_posthog_capture', lambda *a, **k: captures.append(a))

        integ.on_funnel_event(42, 'some_event', {'y': 2})

        assert captures == [('42', 'some_event', {'y': 2})]

    def test_posthog_host_default_matches_config(self):
        import carrac.integrations as integ
        from carrac.config import Config

        integ._init_posthog_config()

        assert integ._posthog_host == Config.POSTHOG_HOST
        # The module must not hardcode a US default that drifts from
        # config.py's EU default.
        assert integ._posthog_host != 'https://us.i.posthog.com'
