"""Tests for carrac/crm_reconcile.py (weekly CRM reconciliation).

FakeEspo is a minimal in-memory stand-in for the EspoCRM REST shape
crm_reconcile.py talks to (Contact / TargetList / Opportunity) — no live
network calls. Mocked at the `carrac.integrations._espocrm_request` boundary
(crm_reconcile.py itself does local `from carrac.integrations import
_espocrm_request` inside run_reconciliation(), so patching the module
attribute is picked up on every call).
"""

from __future__ import annotations

import pytest


def _default_target_lists():
    return {
        'Free Tier Users': {'id': 'tl_free', 'members': set()},
        'Premium Users': {'id': 'tl_premium', 'members': set()},
        'Active Users (14d)': {'id': 'tl_active', 'members': set()},
        'Waitlist': {'id': 'tl_wl', 'members': set()},
    }


class FakeEspo:
    """In-memory stand-in for the subset of the EspoCRM API crm_reconcile.py
    exercises: Contact search/create/update, TargetList lookup-by-name +
    relationship link/unlink, Opportunity search-by-contact/create/update."""

    def __init__(self, contacts=None, target_lists=None, opportunities=None):
        self.contacts = {c['id']: dict(c) for c in (contacts or [])}
        self.target_lists = {name: dict(tl) for name, tl in (target_lists or {}).items()}
        self.opportunities = {o['id']: dict(o) for o in (opportunities or [])}
        self._next_id = 1000
        self.calls = []

    def _new_id(self, prefix):
        self._next_id += 1
        return f'{prefix}{self._next_id}'

    def _tl_by_id(self, list_id):
        for tl in self.target_lists.values():
            if tl['id'] == list_id:
                return tl
        return None

    def request(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        path, _, query = endpoint.partition('?')

        if method == 'GET' and path == 'Contact':
            return {'list': list(self.contacts.values())}

        if method == 'POST' and path == 'Contact':
            cid = self._new_id('c')
            c = dict(data or {})
            c['id'] = cid
            self.contacts[cid] = c
            return c

        if method == 'PUT' and path.startswith('Contact/'):
            cid = path.split('/', 1)[1]
            self.contacts.setdefault(cid, {'id': cid}).update(data or {})
            return self.contacts[cid]

        if method == 'GET' and path == 'TargetList':
            name = query.split('where[0][value]=')[-1]
            tl = self.target_lists.get(name)
            return {'list': [{'id': tl['id']}]} if tl else {'list': []}

        if method == 'GET' and path.startswith('TargetList/') and path.endswith('/contacts'):
            list_id = path.split('/')[1]
            tl = self._tl_by_id(list_id)
            return {'list': [{'id': cid} for cid in sorted(tl['members'])]} if tl else {'list': []}

        if method == 'POST' and path.startswith('TargetList/') and path.endswith('/contacts'):
            list_id = path.split('/')[1]
            tl = self._tl_by_id(list_id)
            if tl:
                tl['members'].update((data or {}).get('ids', []))
            return {}

        if method == 'DELETE' and path.startswith('TargetList/') and path.endswith('/contacts'):
            list_id = path.split('/')[1]
            tl = self._tl_by_id(list_id)
            if tl:
                tl['members'].difference_update((data or {}).get('ids', []))
            return {}

        if method == 'GET' and path == 'Opportunity':
            contact_id = query.split('where[0][value]=')[-1]
            matches = [o for o in self.opportunities.values() if o.get('contactId') == contact_id]
            return {'list': matches}

        if method == 'POST' and path == 'Opportunity':
            oid = self._new_id('opp')
            o = dict(data or {})
            o['id'] = oid
            self.opportunities[oid] = o
            return o

        if method == 'PUT' and path.startswith('Opportunity/'):
            oid = path.split('/', 1)[1]
            self.opportunities.setdefault(oid, {'id': oid}).update(data or {})
            return self.opportunities[oid]

        return {}


@pytest.fixture
def patch_espo(monkeypatch):
    """Wire a FakeEspo into carrac.integrations._espocrm_request and return it."""
    import carrac.integrations as integ

    def _apply(fake):
        monkeypatch.setattr(integ, '_espocrm_request', fake.request)
        return fake
    return _apply


class TestMissingUserCreatesContact:
    def test_missing_user_creates_contact(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(contacts=[], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 5, 'email': 'missing@test.com', 'display_name': 'Missing User',
             'is_admin': False, 'tier': 'free', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['contacts_created'] == 1
        assert summary['errors'] == 0
        created = [c for c in fake.contacts.values() if c.get('emailAddress') == 'missing@test.com']
        assert len(created) == 1
        assert created[0]['cTier'] == 'free'
        assert created[0]['cConvexityUserId'] == 5
        # Never-active free user -> 'registered' (not 'activated').
        assert created[0]['cLifecycleStage'] == 'registered'

    def test_missing_active_free_user_gets_activated_stage(self, monkeypatch, patch_espo):
        """Lifecycle stage must match on_tier_change's semantics
        (carrac/integrations.py): a free user who has ever been active is
        'activated', not lumped in with never-logged-in 'registered' users."""
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb
        from datetime import datetime, timezone

        fake = patch_espo(FakeEspo(contacts=[], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 6, 'email': 'active-free@test.com', 'display_name': 'Active Free',
             'is_admin': False, 'tier': 'free', 'last_active': datetime.now(timezone.utc).isoformat()},
        ])

        summary = recon.run_reconciliation()

        assert summary['contacts_created'] == 1
        created = [c for c in fake.contacts.values() if c.get('emailAddress') == 'active-free@test.com']
        assert created[0]['cLifecycleStage'] == 'activated'

    def test_missing_premium_user_gets_paying_stage(self, monkeypatch, patch_espo):
        """Premium users must be stamped 'paying', not 'activated' — matching
        on_tier_change's lifecycle semantics for paid tiers."""
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(contacts=[], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 7, 'email': 'new-premium@test.com', 'display_name': 'New Premium',
             'is_admin': False, 'tier': 'premium', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['contacts_created'] == 1
        created = [c for c in fake.contacts.values() if c.get('emailAddress') == 'new-premium@test.com']
        assert created[0]['cLifecycleStage'] == 'paying'


class TestTierMismatchPatches:
    def test_tier_mismatch_patches(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(
            contacts=[{'id': 'c1', 'emailAddress': 'p@test.com', 'cConvexityUserId': 7,
                       'cTier': 'free', 'cLifecycleStage': 'registered'}],
            target_lists=_default_target_lists(),
        ))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 7, 'email': 'p@test.com', 'display_name': 'P',
             'is_admin': False, 'tier': 'premium', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['tier_patched'] == 1
        assert summary['contacts_created'] == 0
        assert fake.contacts['c1']['cTier'] == 'premium'

    def test_matching_tier_is_left_alone(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        patch_espo(FakeEspo(
            contacts=[{'id': 'c1', 'emailAddress': 'ok@test.com', 'cConvexityUserId': 8,
                       'cTier': 'free', 'cLifecycleStage': 'registered'}],
            target_lists=_default_target_lists(),
        ))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 8, 'email': 'ok@test.com', 'display_name': 'OK',
             'is_admin': False, 'tier': 'free', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['tier_patched'] == 0
        assert summary['contacts_created'] == 0


class TestTargetListMembershipSync:
    def test_add_and_remove_computed_correctly(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon

        fake = patch_espo(FakeEspo(target_lists={'Free Tier Users': {'id': 'tl1', 'members': {'a', 'b'}}}))

        added, removed = recon._sync_targetlist_membership('tl1', ['b', 'c'])

        assert added == 1
        assert removed == 1
        assert fake.target_lists['Free Tier Users']['members'] == {'b', 'c'}

    def test_no_op_when_membership_already_matches(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon

        fake = patch_espo(FakeEspo(target_lists={'Premium Users': {'id': 'tl2', 'members': {'x'}}}))

        added, removed = recon._sync_targetlist_membership('tl2', ['x'])

        assert (added, removed) == (0, 0)
        # No link/unlink relationship calls should have been issued.
        rel_calls = [c for c in fake.calls if c[0] in ('POST', 'DELETE') and c[1].endswith('/contacts')]
        assert rel_calls == []


class TestMissingTargetListSkipped:
    def test_missing_list_logs_error_and_skips_without_creating(self, monkeypatch, patch_espo, caplog):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        target_lists = _default_target_lists()
        del target_lists['Free Tier Users']  # simulate D not having created it yet
        fake = patch_espo(FakeEspo(contacts=[], target_lists=target_lists))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 9, 'email': 'x@test.com', 'display_name': 'X',
             'is_admin': False, 'tier': 'free', 'last_active': None},
        ])

        with caplog.at_level('ERROR', logger='carrac.crm_reconcile'):
            summary = recon.run_reconciliation()

        assert summary['targetlists_skipped_missing'] == 1
        # The user-repair step (step 3/4) still ran despite the missing list.
        assert summary['contacts_created'] == 1
        assert not any(c[0] == 'POST' and c[1] == 'TargetList' for c in fake.calls)
        assert any('not found' in r.message for r in caplog.records)


class TestFailureIsolation:
    def test_one_user_repair_failure_does_not_abort_the_rest(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(contacts=[], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': None, 'email': 'bad-row', 'display_name': None,
             'is_admin': False, 'tier': 'free', 'last_active': None},  # id=None -> str(u['id']) still works but forces an odd path
            {'id': 11, 'email': 'good@test.com', 'display_name': 'Good',
             'is_admin': False, 'tier': 'free', 'last_active': None},
        ])

        # Make the FIRST contact-create attempt raise, second succeed.
        real_request = fake.request
        state = {'n': 0}

        def flaky_request(method, endpoint, data=None):
            if method == 'POST' and endpoint == 'Contact':
                state['n'] += 1
                if state['n'] == 1:
                    raise RuntimeError("simulated Espo 500")
            return real_request(method, endpoint, data)

        import carrac.integrations as integ
        monkeypatch.setattr(integ, '_espocrm_request', flaky_request)

        summary = recon.run_reconciliation()

        assert summary['errors'] >= 1
        # The second (good) user still got repaired despite the first failing.
        assert summary['contacts_created'] == 1
        created = [c for c in fake.contacts.values() if c.get('emailAddress') == 'good@test.com']
        assert len(created) == 1

    def test_top_level_failure_does_not_raise(self, monkeypatch):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        def _raise():
            raise RuntimeError("db down")
        monkeypatch.setattr(_udb, 'list_users', _raise)

        summary = recon.run_reconciliation()  # must not raise

        assert summary['errors'] >= 1


class TestEmptyUserListFailOpenGuard:
    """user_db.list_users() catches its own exceptions and returns [] rather
    than raising (user_db.py::list_users). Without a guard, run_reconciliation()
    would compute every membership set as empty and unlink EVERY existing
    TargetList member while reporting errors == 0 — a silent fail-open wipe.
    An empty user table is not a realistic prod state (the admin account
    always exists), so empty must be treated as an outage: abort before any
    create/patch/link/unlink call."""

    def test_empty_user_list_aborts_without_touching_existing_members(self, monkeypatch, patch_espo, caplog):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        target_lists = _default_target_lists()
        target_lists['Free Tier Users']['members'] = {'c1', 'c2'}
        target_lists['Premium Users']['members'] = {'c3'}
        target_lists['Active Users (14d)']['members'] = {'c1', 'c3'}
        fake = patch_espo(FakeEspo(
            contacts=[
                {'id': 'c1', 'emailAddress': 'a@test.com', 'cConvexityUserId': 1, 'cTier': 'free'},
                {'id': 'c2', 'emailAddress': 'b@test.com', 'cConvexityUserId': 2, 'cTier': 'free'},
                {'id': 'c3', 'emailAddress': 'c@test.com', 'cConvexityUserId': 3, 'cTier': 'premium'},
            ],
            target_lists=target_lists,
        ))
        # NOT a raise — this mirrors user_db's actual fail-open behavior.
        monkeypatch.setattr(_udb, 'list_users', lambda: [])

        with caplog.at_level('ERROR', logger='carrac.crm_reconcile'):
            summary = recon.run_reconciliation()

        assert summary['errors'] >= 1
        assert summary['users_scanned'] == 0
        # No Espo calls at all — reconciliation must bail out before even
        # fetching contacts, let alone syncing TargetList membership.
        assert fake.calls == []
        rel_calls = [c for c in fake.calls if c[0] in ('POST', 'DELETE') and c[1].endswith('/contacts')]
        assert rel_calls == []
        # Existing members are untouched.
        assert fake.target_lists['Free Tier Users']['members'] == {'c1', 'c2'}
        assert fake.target_lists['Premium Users']['members'] == {'c3'}
        assert fake.target_lists['Active Users (14d)']['members'] == {'c1', 'c3'}
        assert any('refusing fail-open sync' in r.message for r in caplog.records)


class TestOpportunityBackfill:
    def test_premium_contact_without_opportunity_gets_one(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(
            contacts=[{'id': 'c1', 'emailAddress': 'prem@test.com', 'cConvexityUserId': 20,
                       'cTier': 'premium', 'cLifecycleStage': 'paying'}],
            target_lists=_default_target_lists(),
        ))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 20, 'email': 'prem@test.com', 'display_name': 'Prem',
             'is_admin': False, 'tier': 'premium', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['opportunities_created'] == 1
        opps = [o for o in fake.opportunities.values() if o.get('contactId') == 'c1']
        assert len(opps) == 1
        assert opps[0]['stage'] == 'Closed Won'

    def test_premium_contact_with_existing_opportunity_not_duplicated(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        fake = patch_espo(FakeEspo(
            contacts=[{'id': 'c1', 'emailAddress': 'prem2@test.com', 'cConvexityUserId': 21,
                       'cTier': 'premium', 'cLifecycleStage': 'paying'}],
            target_lists=_default_target_lists(),
            opportunities=[{'id': 'opp_existing', 'contactId': 'c1', 'stage': 'Closed Won'}],
        ))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 21, 'email': 'prem2@test.com', 'display_name': 'Prem2',
             'is_admin': False, 'tier': 'premium', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['opportunities_created'] == 0
        assert len(fake.opportunities) == 1


class TestSummaryCountsAccurate:
    def test_combined_scenario_counts(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        fake = patch_espo(FakeEspo(
            contacts=[
                {'id': 'c1', 'emailAddress': 'premium@test.com', 'cConvexityUserId': 1,
                 'cTier': 'premium', 'cLifecycleStage': 'paying', 'cLastActive': now_iso},
            ],
            target_lists=_default_target_lists(),
        ))
        monkeypatch.setattr(_udb, 'list_users', lambda: [
            {'id': 1, 'email': 'premium@test.com', 'display_name': 'Premium',
             'is_admin': False, 'tier': 'premium', 'last_active': now_iso},
            {'id': 2, 'email': 'new@test.com', 'display_name': 'New',
             'is_admin': False, 'tier': 'free', 'last_active': None},
        ])

        summary = recon.run_reconciliation()

        assert summary['users_scanned'] == 2
        assert summary['contacts_scanned'] == 1
        assert summary['contacts_created'] == 1     # user 2 missing from Espo
        assert summary['tier_patched'] == 0         # user 1 already matches
        assert summary['opportunities_created'] == 1  # premium contact has no Opportunity yet
        assert summary['errors'] == 0
        # Both contacts land in at least one TargetList (premium + free/new).
        assert summary['targetlist_added'] >= 2
        assert fake.target_lists['Premium Users']['members'] == {'c1'}
        assert 'c1' not in fake.target_lists['Free Tier Users']['members']


class TestNameCollisionOnCreate:
    """Espo's duplicate check is on the contact NAME, not the
    email. One user's display name is "carrac", so the create POST carried
    firstName "carrac" + the placeholder last name and collided with the
    existing "Carrac —" contact. Every weekly run hit 409, was
    swallowed by the per-user except, and that user stayed unsynced for three
    weeks. A 409 on create must retry with Espo's skipDuplicateCheck — the
    record is distinct by email and by user id — and count as created."""

    class _CollidingEspo(FakeEspo):
        def request(self, method, endpoint, data=None, extra_headers=None):
            path, _, query = endpoint.partition('?')
            skip = (extra_headers or {}).get('X-Skip-Duplicate-Check') == 'true'
            if method == 'POST' and path == 'Contact' and not skip:
                self.calls.append((method, endpoint, data))
                import requests
                resp = requests.Response()
                resp.status_code = 409
                resp.reason = 'Conflict'
                resp.url = 'https://crm.example/api/v1/Contact'
                raise requests.HTTPError('409 Client Error: Conflict', response=resp)
            return super().request(method, endpoint, data)

    def _user14(self):
        return [{'id': 14, 'email': 'carrac@example.org', 'display_name': 'carrac',
                 'is_admin': False, 'tier': 'free', 'last_active': None}]

    def test_409_on_create_retries_with_skip_duplicate_check(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb
        existing = {'id': 'c10', 'name': 'Carrac —', 'emailAddress': 'owner@example.org',
                    'cConvexityUserId': 10, 'cTier': 'free'}
        fake = patch_espo(self._CollidingEspo(contacts=[existing], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: self._user14())
        summary = recon.run_reconciliation()
        assert summary['errors'] == 0
        assert summary['contacts_created'] == 1
        posts = [e for m, e, _ in fake.calls if m == 'POST' and e.startswith('Contact')]
        assert posts == ['Contact', 'Contact'], 'one plain create, then one retry'
        created = [c for c in fake.contacts.values() if c.get('cConvexityUserId') == 14]
        assert len(created) == 1 and created[0]['emailAddress'] == 'carrac@example.org'

    def test_non_409_create_failure_is_still_an_error(self, monkeypatch, patch_espo):
        import carrac.crm_reconcile as recon
        from user_db import user_db as _udb

        class _Down(FakeEspo):
            def request(self, method, endpoint, data=None, extra_headers=None):
                self.calls.append((method, endpoint, extra_headers))
                if method == 'POST' and endpoint.startswith('Contact'):
                    import requests
                    resp = requests.Response(); resp.status_code = 500
                    raise requests.HTTPError('500 Server Error', response=resp)
                return super().request(method, endpoint, data)

        fake = patch_espo(_Down(contacts=[], target_lists=_default_target_lists()))
        monkeypatch.setattr(_udb, 'list_users', lambda: self._user14())
        summary = recon.run_reconciliation()
        assert summary['errors'] == 1 and summary['contacts_created'] == 0
        assert not any(isinstance(h, dict) and h.get('X-Skip-Duplicate-Check') for _, _, h in fake.calls)
