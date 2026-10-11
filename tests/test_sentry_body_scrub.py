"""The error tracker could capture plaintext secrets from request bodies.

_sentry_before_send scrubbed headers, cookies and the query string, but never
event['request']['data'] — the JSON/form body. And sentry_sdk.init() did not
set max_request_body_size, which defaults to "medium" (bodies up to ~10KB).
Crucially, send_default_pii=False does NOT disable body capture; it only gates
cookies and IP, so the one flag that looked like it covered this did not.

Concrete path: POST /api/journal/exchange-sync/connections takes
{api_key, api_secret} for Binance in the body. Any unhandled exception in that
request captured the event with the plaintext secret attached — the same secret
that is Fernet-encrypted one line later on its way into Postgres.

Two independent guards are asserted here, because either alone is one config
change away from being useless:
  1. the transport never asks for bodies at all (max_request_body_size), and
  2. if it ever does, before_send redacts the secret-bearing keys.
"""
import json


from carrac import _sentry_before_send, _sentry_init_kwargs


def _event(data):
    return {'request': {'data': data, 'headers': {}, 'url': 'https://x/y'}}


class TestBodyScrubbing:
    def test_exchange_api_secret_is_redacted(self):
        """The finding's headline case."""
        ev = _sentry_before_send(
            _event({'api_key': 'AK_live_1234', 'api_secret': 'SS_live_5678',
                    'exchange': 'binance', 'label': 'main'}),
            None,
        )
        body = ev['request']['data']
        assert body['api_key'] == '[Filtered]'
        assert body['api_secret'] == '[Filtered]'
        # Non-sensitive fields must survive — a scrubber that nukes the whole
        # body makes the error report useless and gets turned off.
        assert body['exchange'] == 'binance'
        assert body['label'] == 'main'

    def test_password_is_redacted(self):
        ev = _sentry_before_send(
            _event({'email': 'a@b.c', 'password': 'hunter2'}), None)
        assert ev['request']['data']['password'] == '[Filtered]'

    def test_telegram_bot_token_is_redacted(self):
        ev = _sentry_before_send(
            _event({'bot_token': '123456:AAXXXX', 'chat_id': '999'}), None)
        assert ev['request']['data']['bot_token'] == '[Filtered]'
        assert ev['request']['data']['chat_id'] == '999'

    def test_key_matching_is_case_insensitive(self):
        ev = _sentry_before_send(
            _event({'API_Secret': 'x', 'Password': 'y'}), None)
        vals = set(ev['request']['data'].values())
        assert vals == {'[Filtered]'}

    def test_nested_body_is_scrubbed(self):
        """Secrets arrive nested on some routes — the walk must reach them."""
        ev = _sentry_before_send(
            _event({'payload': {'inner': {'api_secret': 'deep', 'sym': 'BTC'}}}),
            None,
        )
        inner = ev['request']['data']['payload']['inner']
        assert inner['api_secret'] == '[Filtered]'
        assert inner['sym'] == 'BTC'

    def test_a_container_named_credentials_is_redacted_wholesale(self):
        """A key that is itself secret-shaped takes its whole subtree with it —
        we do not need to know which inner field is the sensitive one."""
        ev = _sentry_before_send(
            _event({'connection': {'credentials': {'api_secret': 'deep'}}}), None)
        assert ev['request']['data']['connection']['credentials'] == '[Filtered]'

    def test_secrets_in_a_list_are_scrubbed(self):
        ev = _sentry_before_send(
            _event({'items': [{'api_secret': 'a'}, {'sym': 'ETH'}]}), None)
        items = ev['request']['data']['items']
        assert items[0]['api_secret'] == '[Filtered]'
        assert items[1]['sym'] == 'ETH'

    def test_json_string_body_is_handled(self):
        """Sentry sometimes hands the body over as a raw string."""
        ev = _sentry_before_send(
            _event(json.dumps({'api_secret': 'strsecret', 'ok': 1})), None)
        body = ev['request']['data']
        assert 'strsecret' not in json.dumps(body)

    def test_unparseable_string_body_is_dropped_not_passed_through(self):
        """If we cannot inspect it, we must not ship it."""
        ev = _sentry_before_send(_event('api_secret=SS_live_5678&x=1'), None)
        assert 'SS_live_5678' not in json.dumps(ev['request']['data'])

    def test_event_without_request_is_untouched(self):
        ev = _sentry_before_send({'message': 'hi'}, None)
        assert ev == {'message': 'hi'}

    def test_existing_header_scrubbing_still_works(self):
        """Authorization and API-key headers are still scrubbed."""
        ev = _sentry_before_send(
            {'request': {'headers': {'Authorization': 'Bearer abc',
                                     'X-API-Key': 'cvx_k_secret',
                                     'User-Agent': 'curl/8'},
                         'cookies': 'session=abc',
                         'query_string': 'token=leaky&sym=BTC'}},
            None,
        )
        req = ev['request']
        assert req['headers']['Authorization'] == '[Filtered]'
        assert req['headers']['X-API-Key'] == '[Filtered]'
        assert req['headers']['User-Agent'] == 'curl/8'
        assert req['cookies'] == '[Filtered]'
        assert 'leaky' not in req['query_string']


class TestTransportIsWired:
    """"What calls this?" — a scrubber nobody passes to sentry_sdk.init is
    decoration. These assert the config actually carries the guards."""

    def test_bodies_are_never_requested(self):
        assert _sentry_init_kwargs('https://k@example.invalid/1')[
            'max_request_body_size'] == 'never'

    def test_before_send_is_the_scrubber(self):
        assert _sentry_init_kwargs('https://k@example.invalid/1')[
            'before_send'] is _sentry_before_send

    def test_pii_stays_off(self):
        kw = _sentry_init_kwargs('https://k@example.invalid/1')
        assert kw['send_default_pii'] is False


class TestWebsocketHandshakeGrouping:
    """One condition must be ONE issue.

    websocket-client logs handshake failures through its own `websocket` logger
    at ERROR, so the logging integration turns each into an event — and it
    stringifies the exception with the entire nginx response header dict inline,
    including a `date:` header. The error tracker groups by message, so every event became
    its own permanent issue: 614 issues for a single condition (rate-limited
    Finnhub handshakes), 66% of the tracker. That is what made the error tracker
    unreadable, and an unreadable tracker is why nothing in it was ever triaged.

    Message shape below is copied from a real production event.
    """

    TMPL = ("Handshake status 429 Too Many Requests -+-+- {'server': "
            "'nginx/1.18.0', 'date': '%s', 'content-length': '73', "
            "'x-ratelimit-limit': '5', 'x-ratelimit-remaining': '0'}")

    def _ev(self, date):
        return {'logger': 'websocket', 'level': 'error',
                'logentry': {'message': self.TMPL % date}}

    def test_two_events_differing_only_by_date_group_together(self):
        a = _sentry_before_send(self._ev('Fri, 12 Jun 2026 15:20:40 GMT'), None)
        b = _sentry_before_send(self._ev('Sat, 13 Jun 2026 09:01:02 GMT'), None)
        assert a['logentry']['message'] == b['logentry']['message']
        assert a['logentry']['message'] == 'Handshake status 429 Too Many Requests'

    def test_headers_are_moved_not_dropped(self):
        """A grouping fix, not a mute — the volatile detail still ships."""
        a = _sentry_before_send(self._ev('Fri, 12 Jun 2026 15:20:40 GMT'), None)
        assert 'Fri, 12 Jun 2026' in a['extra']['websocket_response_headers']

    def test_other_loggers_are_untouched(self):
        ev = {'logger': 'carrac.precompute', 'logentry': {'message': 'a -+-+- b'}}
        assert _sentry_before_send(ev, None)['logentry']['message'] == 'a -+-+- b'

    def test_websocket_event_without_the_separator_survives(self):
        ev = {'logger': 'websocket', 'logentry': {'message': 'Connection refused'}}
        assert _sentry_before_send(ev, None)['logentry']['message'] == 'Connection refused'
