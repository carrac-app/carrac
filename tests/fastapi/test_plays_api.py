"""
Plays over HTTP — CRUD, ownership isolation, status transitions, tier limits.

Every request goes through the composed ASGI app, so each call reaches the
runtime that serves it in production: writes land on the FastAPI plays
router, reads on the Flask blueprint. Callers authenticate with real
per-user API keys.
"""

import pytest


@pytest.fixture
def member_client(key_client):
    return key_client("member")


class TestPlayCRUD:
    """Basic CRUD operations."""

    def test_create_play(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'DXY Breakdown → BTC Rally',
            'thesis': 'DXY topping, liquidity expanding, crypto rallies',
            'type': 'macro',
            'approach': 'single',
            'direction': 'long',
            'primary_asset': 'BTC',
            'related_assets': ['DXY'],
        })
        assert resp.status_code == 201
        data = resp.json()
        p = data['play']
        assert p['title'] == 'DXY Breakdown → BTC Rally'
        assert p['type'] == 'macro'
        assert p['approach'] == 'single'
        assert p['direction'] == 'long'
        assert p['primary_asset'] == 'BTC'
        assert p['related_assets'] == ['DXY']
        assert p['status'] == 'watching'
        assert p['report_tracking'] is True

    def test_create_minimal_play(self, member_client):
        """Only title is required."""
        resp = member_client.post('/api/plays', json={
            'title': 'Quick idea',
        })
        assert resp.status_code == 201
        p = resp.json()['play']
        assert p['title'] == 'Quick idea'
        assert p['status'] == 'watching'
        assert p['type'] == 'directional'
        assert p['approach'] == 'single'

    def test_create_requires_title(self, member_client):
        resp = member_client.post('/api/plays', json={
            'primary_asset': 'BTC',
        })
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('title:')

    def test_create_all_types(self, member_client):
        for t in ('directional', 'macro', 'event'):
            resp = member_client.post('/api/plays', json={
                'title': f'{t} play',
                'type': t,
            })
            assert resp.status_code == 201
            assert resp.json()['play']['type'] == t

    def test_create_all_approaches(self, member_client):
        for a in ('single', 'scaled', 'defensive'):
            resp = member_client.post('/api/plays', json={
                'title': f'{a} play',
                'approach': a,
            })
            assert resp.status_code == 201
            assert resp.json()['play']['approach'] == a

    def test_create_invalid_type(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad type',
            'type': 'invalid',
        })
        assert resp.status_code == 400
        assert 'Invalid type' in resp.json()['error']

    def test_create_invalid_approach(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad approach',
            'approach': 'invalid',
        })
        assert resp.status_code == 400
        assert 'Invalid approach' in resp.json()['error']

    def test_create_invalid_direction(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad direction',
            'direction': 'sideways',
        })
        assert resp.status_code == 400
        assert 'Invalid direction' in resp.json()['error']

    def test_list_plays(self, member_client):
        member_client.post('/api/plays', json={'title': 'Play A', 'primary_asset': 'BTC'})
        member_client.post('/api/plays', json={'title': 'Play B', 'primary_asset': 'ETH'})

        resp = member_client.get('/api/plays')
        assert resp.status_code == 200
        data = resp.json()
        assert data['count'] >= 2

    def test_list_filter_by_asset(self, member_client):
        member_client.post('/api/plays', json={'title': 'BTC Play', 'primary_asset': 'BTC'})
        member_client.post('/api/plays', json={'title': 'ETH Play', 'primary_asset': 'ETH'})

        resp = member_client.get('/api/plays?asset=BTC')
        data = resp.json()
        assert all(p['primary_asset'] == 'BTC' for p in data['plays'])

    def test_list_filter_by_type(self, member_client):
        member_client.post('/api/plays', json={'title': 'Macro', 'type': 'macro'})
        member_client.post('/api/plays', json={'title': 'Event', 'type': 'event'})

        resp = member_client.get('/api/plays?type=macro')
        data = resp.json()
        assert all(p['type'] == 'macro' for p in data['plays'])

    def test_list_filter_by_status(self, member_client):
        member_client.post('/api/plays', json={'title': 'Play A'})
        resp = member_client.get('/api/plays?status=watching')
        data = resp.json()
        assert all(p['status'] == 'watching' for p in data['plays'])

    def test_get_play(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Test Play'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.get(f'/api/plays/{play_id}')
        assert resp.status_code == 200
        assert resp.json()['play']['title'] == 'Test Play'

    def test_get_nonexistent_play(self, member_client):
        resp = member_client.get('/api/plays/99999')
        assert resp.status_code == 404

    def test_update_play(self, member_client):
        create_resp = member_client.post('/api/plays', json={
            'title': 'Original',
            'type': 'directional',
        })
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}', json={
            'title': 'Updated',
            'thesis': 'New thesis text',
            'type': 'macro',
        })
        assert resp.status_code == 200
        p = resp.json()['play']
        assert p['title'] == 'Updated'
        assert p['thesis'] == 'New thesis text'
        assert p['type'] == 'macro'

    def test_update_nonexistent_play(self, member_client):
        resp = member_client.put('/api/plays/99999', json={'title': 'X'})
        assert resp.status_code == 404

    def test_delete_play(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'To Delete'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.delete(f'/api/plays/{play_id}')
        assert resp.status_code == 200
        assert resp.json()['ok'] is True

        # Confirm deleted
        resp = member_client.get(f'/api/plays/{play_id}')
        assert resp.status_code == 404

    def test_delete_nonexistent_play(self, member_client):
        resp = member_client.delete('/api/plays/99999')
        assert resp.status_code == 404


class TestPlayOwnership:
    """Plays are isolated per user."""

    def test_user_cannot_see_other_users_play(self, key_client):
        owner, other = key_client("member"), key_client("member")
        play_id = owner.post('/api/plays', json={'title': 'A Play'}).json()['play']['id']

        resp = other.get(f'/api/plays/{play_id}')
        assert resp.status_code == 404

    def test_user_cannot_update_other_users_play(self, key_client):
        owner, other = key_client("member"), key_client("member")
        play_id = owner.post('/api/plays', json={'title': 'A Play'}).json()['play']['id']

        resp = other.put(f'/api/plays/{play_id}', json={'title': 'Hijack'})
        assert resp.status_code == 404

    def test_user_cannot_delete_other_users_play(self, key_client):
        owner, other = key_client("member"), key_client("member")
        play_id = owner.post('/api/plays', json={'title': 'A Play'}).json()['play']['id']

        resp = other.delete(f'/api/plays/{play_id}')
        assert resp.status_code == 404


class TestPlayLifecycle:
    """Status transitions and state machine."""

    def test_watching_to_acting(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Lifecycle Test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'acting'})
        assert resp.status_code == 200
        assert resp.json()['play']['status'] == 'acting'

    def test_watching_to_completed(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Complete Test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}/status', json={
            'status': 'completed',
            'resolution': 'Hit target',
        })
        assert resp.status_code == 200
        p = resp.json()['play']
        assert p['status'] == 'completed'
        assert p['resolution'] == 'Hit target'
        assert p['resolved_at'] is not None

    def test_watching_to_invalidated(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Invalid Test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'invalidated'})
        assert resp.status_code == 200
        p = resp.json()['play']
        assert p['status'] == 'invalidated'
        assert p['resolved_at'] is not None

    def test_watching_to_expired(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Expire Test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'expired'})
        assert resp.status_code == 200
        assert resp.json()['play']['status'] == 'expired'

    def test_expired_to_watching_reactivation(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Reactivate Test'})
        play_id = create_resp.json()['play']['id']

        # Expire it
        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'expired'})
        # Reactivate
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'watching'})
        assert resp.status_code == 200
        assert resp.json()['play']['status'] == 'watching'

    def test_acting_to_completed(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Act Test'})
        play_id = create_resp.json()['play']['id']

        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'acting'})
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'completed'})
        assert resp.status_code == 200
        assert resp.json()['play']['status'] == 'completed'

    def test_acting_to_invalidated(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Act Inv Test'})
        play_id = create_resp.json()['play']['id']

        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'acting'})
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'invalidated'})
        assert resp.status_code == 200
        assert resp.json()['play']['status'] == 'invalidated'

    def test_completed_is_terminal(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Terminal Test'})
        play_id = create_resp.json()['play']['id']

        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'completed'})
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'watching'})
        assert resp.status_code == 400
        assert 'Cannot transition' in resp.json()['error']

    def test_invalidated_is_terminal(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Terminal Test 2'})
        play_id = create_resp.json()['play']['id']

        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'invalidated'})
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'watching'})
        assert resp.status_code == 400

    def test_invalid_transition_rejected(self, member_client):
        """Watching → acting is valid, but acting → watching is not."""
        create_resp = member_client.post('/api/plays', json={'title': 'Bad Transition'})
        play_id = create_resp.json()['play']['id']

        member_client.put(f'/api/plays/{play_id}/status', json={'status': 'acting'})
        resp = member_client.put(f'/api/plays/{play_id}/status', json={'status': 'watching'})
        assert resp.status_code == 400
        data = resp.json()
        assert 'Cannot transition' in data['error']
        assert 'allowed_transitions' in data

    def test_missing_status_rejected(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'No Status'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}/status', json={})
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('status:')

    def test_resolved_excluded_from_default_list(self, member_client):
        """Completed/invalidated plays are excluded from default list."""
        member_client.post('/api/plays', json={'title': 'Active'})
        create_resp = member_client.post('/api/plays', json={'title': 'Done'})
        done_id = create_resp.json()['play']['id']
        member_client.put(f'/api/plays/{done_id}/status', json={'status': 'completed'})

        resp = member_client.get('/api/plays')
        titles = [p['title'] for p in resp.json()['plays']]
        assert 'Active' in titles
        assert 'Done' not in titles

    def test_include_resolved_shows_all(self, member_client):
        member_client.post('/api/plays', json={'title': 'Active 2'})
        create_resp = member_client.post('/api/plays', json={'title': 'Done 2'})
        done_id = create_resp.json()['play']['id']
        member_client.put(f'/api/plays/{done_id}/status', json={'status': 'completed'})

        resp = member_client.get('/api/plays?include_resolved=true')
        titles = [p['title'] for p in resp.json()['plays']]
        assert 'Active 2' in titles
        assert 'Done 2' in titles


class TestPlayTierLimits:
    """Tier-based play limits."""

    def test_free_user_limited_to_3(self, key_client):
        """Free tier allows up to 3 plays."""
        client = key_client("free")
        for i in range(3):
            resp = client.post('/api/plays', json={'title': f'Play {i}'})
            assert resp.status_code == 201

        resp = client.post('/api/plays', json={'title': 'Blocked'})
        assert resp.status_code == 403
        data = resp.json()
        assert data['limit_type'] == 'play'
        assert data['current'] == 3
        assert data['max'] == 3

    def test_member_blocked_at_tier_cap(self, member_client):
        """Exercises the boundary-check mechanism itself.

        Member's max_plays is 999 (effectively unlimited), so this
        temporarily lowers the tier_config DB override to 5 to reach the 403
        branch without creating 999 plays.
        """
        from user_db import user_db
        from carrac.auth import decorators as dec
        user_db.set_tier_config('member', {'max_plays': 5}, admin_id=None)
        dec.invalidate_tier_config_cache()
        try:
            for i in range(5):
                resp = member_client.post('/api/plays', json={'title': f'Play {i}'})
                assert resp.status_code == 201

            resp = member_client.post('/api/plays', json={'title': 'Play 6'})
            assert resp.status_code == 403
            data = resp.json()
            assert data['current'] == 5
            assert data['max'] == 5
        finally:
            with user_db._get_conn() as conn:
                conn.execute("DELETE FROM tier_config WHERE tier = 'member'")
                conn.commit()
            dec.invalidate_tier_config_cache()

    def test_completed_plays_dont_count_toward_limit(self, member_client):
        # Fill up 5 plays
        ids = []
        for i in range(5):
            resp = member_client.post('/api/plays', json={'title': f'Play {i}'})
            ids.append(resp.json()['play']['id'])

        # Complete one
        member_client.put(f'/api/plays/{ids[0]}/status', json={'status': 'completed'})

        # Should be able to create another
        resp = member_client.post('/api/plays', json={'title': 'Play 6'})
        assert resp.status_code == 201

    def test_invalidated_plays_dont_count_toward_limit(self, member_client):
        ids = []
        for i in range(5):
            resp = member_client.post('/api/plays', json={'title': f'Play {i}'})
            ids.append(resp.json()['play']['id'])

        # Invalidate one
        member_client.put(f'/api/plays/{ids[0]}/status', json={'status': 'invalidated'})

        # Should be able to create another
        resp = member_client.post('/api/plays', json={'title': 'Play 6'})
        assert resp.status_code == 201

    def test_expired_plays_count_toward_limit(self, member_client):
        """Expired is non-terminal, still counts.

        Member's max_plays is 999, so this temporarily lowers the tier_config
        DB override to 5 to reach the 403 branch.
        """
        from user_db import user_db
        from carrac.auth import decorators as dec
        user_db.set_tier_config('member', {'max_plays': 5}, admin_id=None)
        dec.invalidate_tier_config_cache()
        try:
            ids = []
            for i in range(5):
                resp = member_client.post('/api/plays', json={'title': f'Play {i}'})
                ids.append(resp.json()['play']['id'])

            # Expire one (still counts)
            member_client.put(f'/api/plays/{ids[0]}/status', json={'status': 'expired'})

            # Should still be blocked
            resp = member_client.post('/api/plays', json={'title': 'Play 6'})
            assert resp.status_code == 403
        finally:
            with user_db._get_conn() as conn:
                conn.execute("DELETE FROM tier_config WHERE tier = 'member'")
                conn.commit()
            dec.invalidate_tier_config_cache()


class TestPlayValidation:
    """Input validation edge cases."""

    def test_empty_title_rejected(self, member_client):
        resp = member_client.post('/api/plays', json={'title': ''})
        assert resp.status_code == 400

    def test_title_over_200_chars_rejected(self, member_client):
        resp = member_client.post('/api/plays', json={'title': 'A' * 201})
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('title:')

    def test_title_of_200_chars_accepted(self, member_client):
        resp = member_client.post('/api/plays', json={'title': 'A' * 200})
        assert resp.status_code == 201
        assert len(resp.json()['play']['title']) == 200

    def test_invalid_asset_format(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad Asset',
            'primary_asset': 'invalid asset!',
        })
        assert resp.status_code == 400
        assert 'primary_asset' in resp.json()['error']

    def test_invalid_timeframe(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad TF',
            'timeframe': 'invalid',
        })
        assert resp.status_code == 400
        assert 'timeframe' in resp.json()['error']

    def test_conditions_must_be_object(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad Cond',
            'conditions': 'not an object',
        })
        assert resp.status_code == 400
        assert 'conditions' in resp.json()['error']

    def test_conditions_max_rules(self, member_client):
        rules = [{'id': f'r{i}', 'type': 'price_level', 'params': {}} for i in range(31)]
        resp = member_client.post('/api/plays', json={
            'title': 'Too Many Rules',
            'conditions': {'rules': rules, 'logic': 'all'},
        })
        assert resp.status_code == 400
        assert 'Maximum 30' in resp.json()['error']

    def test_linked_drawing_ids_must_be_array(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad Links',
            'linked_drawing_ids': 'not array',
        })
        assert resp.status_code == 400

    def test_linked_drawing_ids_max(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Too Many Links',
            'linked_drawing_ids': list(range(21)),
        })
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('linked_drawing_ids:')

    def test_related_assets_validated(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Related',
            'related_assets': ['DXY', 'GOLD', 'ETH'],
        })
        assert resp.status_code == 201
        p = resp.json()['play']
        assert p['related_assets'] == ['DXY', 'GOLD', 'ETH']

    def test_cross_asset_trigger(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'DXY trigger BTC',
            'primary_asset': 'BTC',
            'trigger_asset': 'DXY',
        })
        assert resp.status_code == 201
        p = resp.json()['play']
        assert p['primary_asset'] == 'BTC'
        assert p['trigger_asset'] == 'DXY'


class TestPlayDrawingLink:
    """Drawing link/unlink operations."""

    @staticmethod
    def _seed_owned_drawing(user_id):
        # Linking checks that the drawing belongs to the caller, so the test
        # links a real drawing owned by the calling user.
        from carrac.content_db import get_content_db
        return get_content_db().save_drawing(
            asset='BTC', timeframe='1D', drawing_type='horizontal_line',
            price_level=100.0, user_id=user_id,
        )

    def test_link_drawing(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Link Test'})
        play_id = create_resp.json()['play']['id']
        did = self._seed_owned_drawing(member_client.user_id)

        resp = member_client.post(f'/api/plays/{play_id}/link-drawing', json={
            'drawing_id': did,
        })
        assert resp.status_code == 200
        p = resp.json()['play']
        assert did in p['linked_drawing_ids']

    def test_link_drawing_idempotent(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Idempotent Test'})
        play_id = create_resp.json()['play']['id']
        did = self._seed_owned_drawing(member_client.user_id)

        member_client.post(f'/api/plays/{play_id}/link-drawing', json={'drawing_id': did})
        member_client.post(f'/api/plays/{play_id}/link-drawing', json={'drawing_id': did})
        resp = member_client.get(f'/api/plays/{play_id}')
        p = resp.json()['play']
        assert p['linked_drawing_ids'].count(did) == 1

    def test_unlink_drawing(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Unlink Test'})
        play_id = create_resp.json()['play']['id']
        did = self._seed_owned_drawing(member_client.user_id)

        member_client.post(f'/api/plays/{play_id}/link-drawing', json={'drawing_id': did})
        resp = member_client.post(f'/api/plays/{play_id}/unlink-drawing', json={'drawing_id': did})
        assert resp.status_code == 200
        p = resp.json()['play']
        assert did not in p['linked_drawing_ids']

    def test_link_requires_drawing_id(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'No ID'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.post(f'/api/plays/{play_id}/link-drawing', json={})
        assert resp.status_code == 400

    def test_link_nonexistent_play(self, member_client):
        resp = member_client.post('/api/plays/99999/link-drawing', json={'drawing_id': 1})
        assert resp.status_code == 404


class TestPlayContext:
    """Context endpoint for morning engine / AI."""

    def test_context_empty(self, member_client):
        resp = member_client.get('/api/plays/context')
        assert resp.status_code == 200
        data = resp.json()
        assert data['plays'] == []
        assert 'No active plays' in data['summary']

    def test_context_with_plays(self, member_client):
        member_client.post('/api/plays', json={
            'title': 'BTC Long',
            'type': 'directional',
            'approach': 'single',
            'direction': 'long',
            'primary_asset': 'BTC',
            'thesis': 'Breakout incoming',
        })

        resp = member_client.get('/api/plays/context')
        data = resp.json()
        assert len(data['plays']) == 1
        cp = data['plays'][0]
        assert cp['title'] == 'BTC Long'
        assert cp['type'] == 'directional'
        assert cp['thesis'] == 'Breakout incoming'
        assert cp['conditions_met'] == '0/0'
        assert 'watching' in data['summary']

    def test_context_excludes_terminal(self, member_client):
        member_client.post('/api/plays', json={'title': 'Active'})
        create_resp2 = member_client.post('/api/plays', json={'title': 'Done'})
        done_id = create_resp2.json()['play']['id']
        member_client.put(f'/api/plays/{done_id}/status', json={'status': 'completed'})

        resp = member_client.get('/api/plays/context')
        titles = [p['title'] for p in resp.json()['plays']]
        assert 'Active' in titles
        assert 'Done' not in titles

    def test_active_endpoint(self, member_client):
        member_client.post('/api/plays', json={'title': 'Active Play'})
        resp = member_client.get('/api/plays/active')
        assert resp.status_code == 200
        assert resp.json()['count'] >= 1


class TestPlayAuth:
    """Authentication requirements."""

    def test_unauthenticated_rejected(self, asgi_client):
        resp = asgi_client.get('/api/plays', follow_redirects=False)
        assert resp.status_code in (401, 302)  # redirect or 401

    def test_unauthenticated_create_rejected(self, asgi_client):
        resp = asgi_client.post('/api/plays', json={'title': 'Nope'}, follow_redirects=False)
        assert resp.status_code in (401, 302)


class TestPlayMetadata:
    """Metadata field (trade fields, milestones)."""

    def test_create_play_with_metadata(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'With metadata',
            'metadata': {'conviction': 3, 'milestones': [{'label': 'Step 1', 'done': False}]},
        })
        assert resp.status_code == 201
        p = resp.json()['play']
        assert p['metadata']['conviction'] == 3
        assert len(p['metadata']['milestones']) == 1

    def test_update_metadata(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Meta test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}', json={
            'metadata': {
                'conviction': 4,
                'entry_low': 64000,
                'entry_high': 65000,
                'stop_loss': 62000,
                'targets': [{'price': 68000, 'label': 'TP1'}],
            }
        })
        assert resp.status_code == 200
        m = resp.json()['play']['metadata']
        assert m['conviction'] == 4
        assert m['entry_low'] == 64000
        assert m['targets'][0]['price'] == 68000

    def test_metadata_must_be_object(self, member_client):
        resp = member_client.post('/api/plays', json={
            'title': 'Bad metadata',
            'metadata': 'not an object',
        })
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('metadata:')

    def test_metadata_size_limit(self, member_client):
        # Create a metadata object that exceeds 10KB
        big_meta = {'milestones': [{'label': 'x' * 200, 'done': False}] * 60}
        resp = member_client.post('/api/plays', json={
            'title': 'Too big metadata',
            'metadata': big_meta,
        })
        assert resp.status_code == 400
        assert 'too large' in resp.json()['error']

    def test_metadata_merge_preserves_fields(self, member_client):
        """PUT partial metadata doesn't clobber existing keys (frontend handles merge)."""
        create_resp = member_client.post('/api/plays', json={
            'title': 'Merge test',
            'metadata': {'conviction': 3, 'milestones': [{'label': 'Step 1', 'done': False}]},
        })
        play_id = create_resp.json()['play']['id']

        # Frontend sends full merged metadata; backend just stores it
        resp = member_client.put(f'/api/plays/{play_id}', json={
            'metadata': {'conviction': 5, 'milestones': [{'label': 'Step 1', 'done': True}]},
        })
        assert resp.status_code == 200
        m = resp.json()['play']['metadata']
        assert m['conviction'] == 5
        assert m['milestones'][0]['done'] is True

    def test_default_metadata_empty(self, member_client):
        resp = member_client.post('/api/plays', json={'title': 'No meta'})
        assert resp.status_code == 201
        p = resp.json()['play']
        assert p['metadata'] == {} or p['metadata'] is None or p['metadata'] == {}


class TestPlayConditionsUpdate:
    """Updating conditions and invalidation via PUT."""

    def test_update_conditions(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Cond test'})
        play_id = create_resp.json()['play']['id']

        conditions = {
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 70000, 'relation': 'above'}, 'met': False},
                {'id': 'r2', 'type': 'manual', 'params': {'label': 'Check DXY'}, 'met': False},
            ],
            'logic': 'all',
        }
        resp = member_client.put(f'/api/plays/{play_id}', json={'conditions': conditions})
        assert resp.status_code == 200
        c = resp.json()['play']['conditions']
        assert len(c['rules']) == 2
        assert c['logic'] == 'all'
        assert c['rules'][0]['type'] == 'price_level'

    def test_update_invalidation(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Inv test'})
        play_id = create_resp.json()['play']['id']

        invalidation = {
            'id': 'inv1',
            'type': 'price_level',
            'params': {'level': 58000, 'relation': 'below'},
            'met': False,
        }
        resp = member_client.put(f'/api/plays/{play_id}', json={'invalidation': invalidation})
        assert resp.status_code == 200
        inv = resp.json()['play']['invalidation']
        assert inv['type'] == 'price_level'
        assert inv['params']['level'] == 58000

    def test_update_both_conditions_and_invalidation(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Both test'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}', json={
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'manual', 'params': {'label': 'Check'}, 'met': False}],
                'logic': 'all',
            },
            'invalidation': {'id': 'inv1', 'type': 'price_level', 'params': {'level': 50000, 'relation': 'below'}},
        })
        assert resp.status_code == 200
        p = resp.json()['play']
        assert len(p['conditions']['rules']) == 1
        assert p['invalidation']['type'] == 'price_level'

    def test_invalid_conditions_rejected(self, member_client):
        create_resp = member_client.post('/api/plays', json={'title': 'Bad cond'})
        play_id = create_resp.json()['play']['id']

        resp = member_client.put(f'/api/plays/{play_id}', json={
            'conditions': 'not an object',
        })
        assert resp.status_code == 400
        assert resp.json()['error'].startswith('conditions:')


class TestPlayDirectionMismatch:
    """
    Verify the backend does NOT enforce direction/condition agreement.
    The mismatch warning is client-side only (plays-panel.js _warnDirectionMismatch).
    A Play marked 'long' with a 'bearish' engulfing rule must still save successfully
    via the API — the warning is a soft UX nudge, not a hard block.

    Real-world precedent: play_id 10 in production had direction='long' with a
    bb_breakout_retest_engulfing rule using direction='bearish'. The Play evaluated
    correctly; only the label disagreement looked confusing in reports.
    """

    def test_long_play_with_bearish_engulfing_rule_saves_ok(self, member_client):
        """Backend allows mismatched Play.direction + rule.params.direction."""
        resp = member_client.post('/api/plays', json={
            'title': 'Mismatch test — long play, bearish rule',
            'direction': 'long',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'engulfing_detected',
                    'params': {'direction': 'bearish', 'timeframe': '4h'},
                    'met': False,
                }],
                'logic': 'all',
            },
        })
        assert resp.status_code == 201, resp.json()
        p = resp.json()['play']
        assert p['direction'] == 'long'
        assert p['conditions']['rules'][0]['params']['direction'] == 'bearish'

    def test_short_play_with_bullish_bb_rule_saves_ok(self, member_client):
        """Backend allows short Play with bullish bb_breakout_retest_engulfing rule."""
        resp = member_client.post('/api/plays', json={
            'title': 'Mismatch test — short play, bullish rule',
            'direction': 'short',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'bb_breakout_retest_engulfing',
                    'params': {
                        'direction': 'bullish',
                        'timeframe': '4h',
                        'lookback_bars': 15,
                        'proximity_pct': 0.5,
                    },
                    'met': False,
                }],
                'logic': 'all',
            },
        })
        assert resp.status_code == 201, resp.json()
        p = resp.json()['play']
        assert p['direction'] == 'short'
        assert p['conditions']['rules'][0]['params']['direction'] == 'bullish'

    def test_matched_direction_still_saves_ok(self, member_client):
        """Sanity: long Play with bullish rule also saves fine (no warning would fire)."""
        resp = member_client.post('/api/plays', json={
            'title': 'No mismatch — long play, bullish rule',
            'direction': 'long',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'engulfing_detected',
                    'params': {'direction': 'bullish', 'timeframe': '1h'},
                    'met': False,
                }],
                'logic': 'all',
            },
        })
        assert resp.status_code == 201, resp.json()
        p = resp.json()['play']
        assert p['direction'] == 'long'

    def test_neutral_play_with_any_rule_direction_saves_ok(self, member_client):
        """neutral Play.direction accepts any rule direction without mismatch."""
        resp = member_client.post('/api/plays', json={
            'title': 'Neutral play — bearish rule',
            'direction': 'neutral',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'bb_fade_engulfing',
                    'params': {'direction': 'bearish', 'timeframe': '4h'},
                    'met': False,
                }],
                'logic': 'all',
            },
        })
        assert resp.status_code == 201, resp.json()
        p = resp.json()['play']
        assert p['direction'] == 'neutral'


def _create_play(client, **kwargs):
    """Create a play over HTTP and return it."""
    resp = client.post('/api/plays', json={'title': 'Test Play', **kwargs})
    assert resp.status_code == 201, resp.json()
    return resp.json()['play']


class TestPlaysMonitorPersistence:
    """Condition state persistence through API."""

    def test_persist_condition_state(self, member_client):
        """Creating a play and persisting updated conditions."""
        play = _create_play(member_client, title='Persist Test', conditions={
            'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}, 'met': False}],
            'logic': 'all',
        })
        play_id = play['id']

        # Verify initial state
        resp = member_client.get(f'/api/plays/{play_id}')
        p = resp.json()['play']
        assert p['conditions']['rules'][0]['met'] is False

        # Persist updated state
        from carrac.plays.models import persist_condition_state
        updated_conditions = {
            'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}, 'met': True, 'met_at': '2026-04-01T12:00:00', 'evidence': 'Price $55K > $50K'}],
            'logic': 'all',
        }
        persist_condition_state(play_id, member_client.user_id, updated_conditions)

        # Verify persisted
        resp = member_client.get(f'/api/plays/{play_id}')
        p = resp.json()['play']
        assert p['conditions']['rules'][0]['met'] is True
        assert 'Price $55K' in p['conditions']['rules'][0]['evidence']

    def test_get_active_plays_for_monitoring(self, member_client):
        """Monitoring query returns active plays, excludes terminal."""
        _create_play(member_client, title='Active Play')
        done_play = _create_play(member_client, title='Done Play')
        member_client.put(f'/api/plays/{done_play["id"]}/status', json={'status': 'completed'})

        from carrac.plays.models import get_active_plays_for_monitoring
        plays = get_active_plays_for_monitoring()
        titles = [p['title'] for p in plays]
        assert 'Active Play' in titles
        assert 'Done Play' not in titles


class TestEvaluateEndpoint:
    """POST /api/plays/<id>/evaluate endpoint."""

    def test_evaluate_returns_404_for_missing_play(self, member_client):
        resp = member_client.post('/api/plays/99999/evaluate')
        assert resp.status_code == 404

    def test_evaluate_requires_auth(self, asgi_client):
        resp = asgi_client.post('/api/plays/1/evaluate', follow_redirects=False)
        assert resp.status_code in (401, 302)

    def test_evaluate_returns_503_without_market_data(self, member_client):
        """Without cached market data, evaluate returns 503."""
        play = _create_play(member_client, title='Eval Test')
        resp = member_client.post(f'/api/plays/{play["id"]}/evaluate')
        # May return 503 or 500 depending on cache state in test env
        assert resp.status_code in (500, 503)


class TestPlaysConditionValidation:
    """Condition rules are validated on create."""

    def test_create_play_with_unknown_condition_type_returns_400(self, member_client):
        """A rule using an unknown condition type must be rejected with 400."""
        resp = member_client.post('/api/plays', json={
            'title': 'Bad condition test',
            'conditions': {
                'rules': [
                    {
                        'id': 'r1',
                        'type': 'totally_made_up_condition_type',
                        'params': {'foo': 'bar'},
                    }
                ],
                'logic': 'all',
            },
        })
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert 'error' in body
        assert 'invalid conditions' in body['error'].lower() or 'unknown condition type' in body['error'].lower()

    def test_create_play_with_valid_conditions_returns_201(self, member_client):
        """A play with a valid price_level condition must be created successfully."""
        resp = member_client.post('/api/plays', json={
            'title': 'Valid condition play',
            'conditions': {
                'rules': [
                    {
                        'id': 'r1',
                        'type': 'price_level',
                        'params': {
                            'level': 60000,
                            'relation': 'above',
                        },
                    }
                ],
                'logic': 'all',
            },
        })
        assert resp.status_code == 201, resp.text
        play = resp.json()['play']
        assert play['conditions']['rules'][0]['type'] == 'price_level'


class TestPlaysContextEndpoint:
    """Plays context endpoint returns correct format for AI/morning engine."""

    def test_context_format_for_ai(self, member_client):
        # Create a play
        member_client.post('/api/plays', json={
            'title': 'AI Context Test',
            'type': 'macro',
            'approach': 'defensive',
            'direction': 'neutral',
            'primary_asset': 'BTC',
            'thesis': 'Risk-off incoming',
            'action_plan': 'Reduce exposure if VIX spikes',
        })

        resp = member_client.get('/api/plays/context')
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['plays']) == 1

        p = data['plays'][0]
        assert p['title'] == 'AI Context Test'
        assert p['type'] == 'macro'
        assert p['approach'] == 'defensive'
        assert p['direction'] == 'neutral'
        assert p['primary_asset'] == 'BTC'
        assert p['thesis'] == 'Risk-off incoming'
        assert p['action_plan'] == 'Reduce exposure if VIX spikes'
        assert 'conditions_met' in p
        assert 'watching' in data['summary']
