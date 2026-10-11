"""MCP report tools apply the same serving policy as the HTTP report routes."""

import json
from datetime import datetime, timedelta

import pytest

from carrac import content_db as cdb
from carrac.mcp import tools  # noqa: F401
from carrac.mcp.registry import TOOL_REGISTRY
import carrac.mcp.tools.reports as reports_mod

SECRET = 'Entered at $50000 with stop $48000'


def _row(rtype, **over):
    row = {
        'id': 5, 'report_type': rtype,
        'content': f'## The Story\nMarkets calm.\n## Action Dashboard\n{SECRET}.\n',
        'html': f'## The Story\nMarkets calm.\n## Action Dashboard\n{SECRET}.\n',
        'summary': 'Markets calm.',
        'audio_script': f'Spoken: {SECRET}.',
        'source_briefing': f'briefing with {SECRET}',
        'metadata': json.dumps({
            'x_candidates': ['BTC holds range support.'],
            'report_prices': {'BTC': 50000},
            'positions': [SECRET],
        }),
        'created_at': datetime.utcnow() - timedelta(hours=1),
    }
    row.update(over)
    return row


@pytest.fixture
def serve(monkeypatch):
    def _serve(report, *, admin):
        class FakeDB:
            def get_latest_report(self, report_type=None):
                return report if report_type == report['report_type'] else None

            def get_report_by_date(self, report_type=None, date=None):
                return report if report_type == report['report_type'] else None

        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())
        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: admin)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: not admin)
    return _serve


def test_non_operator_raw_report_is_filtered(serve):
    serve(_row('weekly'), admin=False)
    out = TOOL_REGISTRY['report_get_raw']['handler']({'type': 'weekly'})['report']
    assert 'Action Dashboard' not in out['content']
    assert 'Action Dashboard' not in out['html']
    assert out['audio_script'] is None
    assert 'source_briefing' not in out
    assert out['metadata'] == {'x_candidates': ['BTC holds range support.']}
    assert '50000' not in json.dumps(out, default=str)


def test_non_operator_morning_summary_withheld(serve, monkeypatch):
    serve(_row('morning', summary='Operator holds a long from 50000.'), admin=False)
    # The existence fallback serves the morning row to a paid, non-admin caller.
    monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: False)
    out = TOOL_REGISTRY['report_get_raw']['handler']({'type': 'general'})['report']
    assert out['summary'] is None
    assert out['audio_script'] is None


def test_operator_raw_report_is_unchanged(serve):
    row = _row('weekly')
    serve(row, admin=True)
    out = TOOL_REGISTRY['report_get_raw']['handler']({'type': 'weekly'})['report']
    assert out['audio_script'] == row['audio_script']
    assert out['source_briefing'] == row['source_briefing']
    assert out['metadata'] == row['metadata']
    assert 'Action Dashboard' in out['content']


def test_non_operator_general_report_gets_public_filter(serve):
    serve(_row('general', content='## The Story\nCalm.', html=None,
               source_briefing=None, metadata=None), admin=False)
    out = TOOL_REGISTRY['report_get_raw']['handler']({'type': 'general'})['report']
    assert out['content'].startswith('## The Story')
    assert out['audio_script'] is None
    assert out['metadata'] == {}


def test_non_operator_section_lookup_cannot_reach_operator_section(serve):
    serve(_row('weekly'), admin=False)
    from carrac.mcp.protocol import ToolError
    with pytest.raises(ToolError):
        TOOL_REGISTRY['report_get_section']['handler'](
            {'section': 'Action Dashboard', 'type': 'weekly'})


def test_non_operator_diff_hides_operator_sections(serve):
    serve(_row('weekly'), admin=False)
    out = TOOL_REGISTRY['report_get_diff']['handler'](
        {'date_a': '2026-01-01', 'date_b': '2026-01-02', 'type': 'weekly'})
    names = [s['section'] for s in out['sections_unchanged'] + out['sections_changed']]
    assert 'Action Dashboard' not in names
    assert 'The Story' in names


def test_non_operator_compact_has_no_audio_script(serve):
    serve(_row('weekly', summary=f'{SECRET}, calm week.'), admin=False)
    out = TOOL_REGISTRY['report_get_compact']['handler']({'type': 'weekly'})
    assert out['audio_script'] is None
    assert '50000' not in (out['summary'] or '')
