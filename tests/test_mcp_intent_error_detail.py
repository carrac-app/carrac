"""A failed MCP call must record WHY, not only the exception class.

`error_kind` stored `type(e).__name__` and the message was dropped
at the moment it was known. Consequence: a tool that fails on nearly every
call looks like this in the usage log:

    log_trade   1875 calls   ok=2   ToolError x1873

with every row saying `ToolError`. `trade_log` raises ToolError from eight distinct
validation branches (asset missing, entry_price non-positive, tags not a list,
too many tags, tag too long, multiple src: tags, unknown src: vocabulary,
size not a number). Working out which one fired meant reading the source and
guessing, because the answer had never been stored.

This is the alert dead-letter queue's defect
In a second subsystem, found the same day: a failure record that
proves something broke and cannot say what.

`error_kind` keeps holding just the class — the admin endpoint groups by it and
low cardinality is the point. The message goes beside it.
"""
import pytest


@pytest.fixture
def captured(monkeypatch):
    """Capture what the dispatcher hands the telemetry writer.

    `call_tool` imports `log_intent_dispatch` from carrac.mcp.runtime inside
    its finally block, so patching the attribute on that module intercepts it.
    Tools are resolved out of TOOL_REGISTRY, so a fake handler is registered
    there under a name nothing else uses.
    """
    rows = []
    from carrac.mcp import runtime

    monkeypatch.setattr(runtime, 'log_intent_dispatch',
                        lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def register(monkeypatch):
    """Install a handler under a throwaway tool name."""
    from carrac.mcp.registry import TOOL_REGISTRY

    def _install(handler, name='_intent_probe'):
        monkeypatch.setitem(TOOL_REGISTRY, name, {'handler': handler, 'name': name})
        return name

    return _install


class TestTheMessageSurvives:
    def test_dispatch_records_the_exception_message(self, captured, register):
        from carrac.mcp import registry
        from carrac.mcp.protocol import ToolError

        def boom(arguments):
            raise ToolError('entry_price must be a positive number')

        name = register(boom)
        try:
            registry.call_tool(name, {})
        except Exception:
            pass

        assert captured, 'the telemetry writer was never called'
        row = captured[-1]
        assert row['error_kind'] == 'ToolError', 'class stays low-cardinality'
        assert 'entry_price must be a positive number' in (row.get('error_detail') or ''), (
            'the message is the whole diagnosis — 1,873 prod failures were '
            'unreadable without it'
        )

    def test_a_successful_call_records_no_error(self, captured, register):
        from carrac.mcp import registry

        name = register(lambda a: {'ok': 1})
        registry.call_tool(name, {})
        row = captured[-1]
        assert row['error_kind'] is None
        assert row.get('error_detail') is None


class TestItCannotPoisonTheTelemetry:
    def test_a_huge_message_is_truncated(self, captured, register):
        """An upstream HTML error page must not become the telemetry row."""
        from carrac.mcp import registry

        def boom(arguments):
            raise RuntimeError('<html>' + 'A' * 20000 + '</html>')

        name = register(boom)
        try:
            registry.call_tool(name, {})
        except Exception:
            pass

        detail = captured[-1]['error_detail']
        assert len(detail) <= 500

    def test_newlines_are_flattened(self, captured, register):
        from carrac.mcp import registry

        def boom(arguments):
            raise RuntimeError('line one\nline two\rline three')

        name = register(boom)
        try:
            registry.call_tool(name, {})
        except Exception:
            pass

        detail = captured[-1]['error_detail']
        assert '\n' not in detail and '\r' not in detail
        assert 'line one' in detail and 'line three' in detail


class TestTheWriterAcceptsIt:
    def test_log_intent_dispatch_takes_error_detail(self):
        """Guards the seam: the dispatcher passes it, so the writer must take
        it. A TypeError here is swallowed by the belt-and-suspenders wrapper in
        registry.call_tool, which would silently lose ALL telemetry."""
        import inspect
        from carrac.mcp.runtime import log_intent_dispatch

        params = inspect.signature(log_intent_dispatch).parameters
        assert 'error_detail' in params
        assert params['error_detail'].default is None, (
            'must stay optional — older callers exist'
        )


class TestSilentlyDroppedArgumentsAreNamed:
    """Arguments a tool ignores must be named, not silently dropped.

    Every tool reads its arguments with `.get()`, and nothing compared them
    against the tool's own declared inputSchema. A client that posts
    symbol/side/stop_loss/take_profit/entry_time/notes to `trade_log` has five
    discarded without a word, and the only complaint is about the field that
    ended up missing ('asset is required' — true, and useless).
    """

    def _schema(self, *names):
        return {'type': 'object', 'properties': {n: {'type': 'string'} for n in names}}

    def test_the_real_agent_payload_now_names_what_was_dropped(self, captured, monkeypatch):
        from carrac.mcp import registry
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.registry import TOOL_REGISTRY

        def boom(arguments):
            raise ToolError('asset is required')

        monkeypatch.setitem(TOOL_REGISTRY, '_c479', {
            'handler': boom, 'name': '_c479',
            'inputSchema': self._schema('asset', 'entry_price', 'tags'),
        })
        # Byte-for-byte the shape the client was sending.
        try:
            registry.call_tool('_c479', {
                'symbol': 'BTC', 'side': 'long', 'stop_loss': 1.0,
                'take_profit': 2.0, 'entry_time': 'x', 'notes': 'y',
            })
        except Exception:
            pass

        detail = captured[-1]['error_detail']
        assert 'asset is required' in detail, 'the original message must survive'
        assert 'ignored unknown args' in detail
        for dropped in ('symbol', 'side', 'stop_loss', 'take_profit', 'entry_time', 'notes'):
            assert dropped in detail, f'{dropped} was dropped silently and is not named'

    def test_a_correct_payload_adds_no_noise(self, captured, monkeypatch):
        from carrac.mcp import registry
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.registry import TOOL_REGISTRY

        monkeypatch.setitem(TOOL_REGISTRY, '_c479ok', {
            'handler': lambda a: (_ for _ in ()).throw(ToolError('entry_price must be positive')),
            'name': '_c479ok',
            'inputSchema': self._schema('asset', 'entry_price'),
        })
        try:
            registry.call_tool('_c479ok', {'asset': 'BTC', 'entry_price': 0})
        except Exception:
            pass

        detail = captured[-1]['error_detail']
        assert 'entry_price must be positive' in detail
        assert 'unknown args' not in detail, (
            'a well-formed call must not be decorated with an empty accusation'
        )

    def test_it_reports_and_never_rejects(self, captured, monkeypatch):
        """Lenient by design: LEGACY_ARG_DEFAULTS aliases and long-standing
        stray fields must keep working. This makes the drop visible, not fatal."""
        from carrac.mcp import registry
        from carrac.mcp.registry import TOOL_REGISTRY

        monkeypatch.setitem(TOOL_REGISTRY, '_c479pass', {
            'handler': lambda a: {'ok': True}, 'name': '_c479pass',
            'inputSchema': self._schema('asset'),
        })
        out = registry.call_tool('_c479pass', {'asset': 'BTC', 'strayfield': 1})
        assert out == {'ok': True}, 'an unknown argument must not break the call'

    def test_a_toolless_or_malformed_schema_accuses_nobody(self):
        from carrac.mcp.registry import _unknown_arguments

        assert _unknown_arguments({}, {'a': 1}) == []
        assert _unknown_arguments({'inputSchema': None}, {'a': 1}) == []
        assert _unknown_arguments({'inputSchema': {'properties': {}}}, {'a': 1}) == []
        assert _unknown_arguments({'inputSchema': {'properties': {'a': {}}}}, None) == []
