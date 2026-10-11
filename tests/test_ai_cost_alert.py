"""Unit tests for scripts/ai_cost_alert.py."""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch


# Ensure the project root is on sys.path so the script can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import ai_cost_alert as aca


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(spend_cents: int, existing_alerts: dict | None = None):
    """Build a mock Redis client for ai_cost_alert tests."""
    existing_alerts = existing_alerts or {}
    client = MagicMock()
    client.get.side_effect = lambda key: (
        existing_alerts[key] if key in existing_alerts else
        str(spend_cents) if key.startswith('mcd:ai:cost:today:') else
        None
    )
    client.set = MagicMock()
    return client


# ---------------------------------------------------------------------------
# WARN threshold fires at 50%
# ---------------------------------------------------------------------------

class TestWarnThreshold:
    def test_warn_fires_when_at_50_pct(self):
        """WARN alert sends ntfy when spend reaches 50% of cap.

        Telegram must NOT also fire: it is a fallback, not a second copy.
        Sending both put every alert on the phone twice.
        """
        cap_cents = 3000  # $30
        spend_cents = 1500  # exactly 50%

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_called_once()
        mock_tg.assert_not_called()
        title_arg = mock_ntfy.call_args[0][0]
        assert 'warning' in title_arg.lower()

    def test_warn_falls_back_to_telegram_when_ntfy_fails(self):
        """The fallback half: ntfy down means Telegram MUST carry the alert.

        The ALLOWED branch is asserted too, not just the blocked one — a
        fallback nobody has watched fire is not a fallback.
        """
        cap_cents = 3000
        spend_cents = 1500

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=False) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_called_once()
        mock_tg.assert_called_once()

    def test_warn_does_not_fire_below_threshold(self):
        """No alert when spend is below 50%."""
        cap_cents = 3000
        spend_cents = 1000  # 33%

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_not_called()
        mock_tg.assert_not_called()


# ---------------------------------------------------------------------------
# ALERT threshold fires at 83%
# ---------------------------------------------------------------------------

class TestAlertThreshold:
    def test_alert_fires_when_at_83_pct(self):
        """ALERT alert sends ntfy when spend reaches 83% of cap.

        Telegram stays quiet while ntfy is delivering — fallback, not duplicate.
        """
        cap_cents = 3000
        spend_cents = 2500  # 83%

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_called_once()
        mock_tg.assert_not_called()
        title_arg = mock_ntfy.call_args[0][0]
        assert 'alert' in title_arg.lower() or 'ALERT' in title_arg

    def test_alert_falls_back_to_telegram_when_ntfy_fails(self):
        """ALERT keeps its redundancy: ntfy down means Telegram still pages."""
        cap_cents = 3000
        spend_cents = 2500

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=False), \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            aca.run(client=client)

        mock_tg.assert_called_once()

    def test_alert_uses_higher_ntfy_priority(self):
        """ALERT must use ntfy priority 4 (WARN uses 3)."""
        cap_cents = 3000
        spend_cents = 2500  # 83%

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True):

            aca.run(client=client)

        priority_arg = mock_ntfy.call_args[1].get('priority') or mock_ntfy.call_args[0][2]
        assert priority_arg == '4'


# ---------------------------------------------------------------------------
# Same-day idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_warn_not_resent_same_day(self):
        """WARN is not re-sent if Redis dedup key already set for today."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        dedup_key = f'ai_cost_alerted:{today}:WARN'

        cap_cents = 3000
        spend_cents = 1500  # 50% — WARN territory

        client = _make_client(spend_cents, existing_alerts={dedup_key: '1'})

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_not_called()
        mock_tg.assert_not_called()

    def test_alert_not_resent_same_day(self):
        """ALERT is not re-sent if Redis dedup key already set for today."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        dedup_key = f'ai_cost_alerted:{today}:ALERT'

        cap_cents = 3000
        spend_cents = 2500  # 83% — ALERT territory

        client = _make_client(spend_cents, existing_alerts={dedup_key: '1'})

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True) as mock_ntfy, \
             patch.object(aca, '_send_telegram', return_value=True) as mock_tg:

            result = aca.run(client=client)

        assert result == 0
        mock_ntfy.assert_not_called()
        mock_tg.assert_not_called()

    def test_dedup_key_is_set_after_warn_fires(self):
        """After WARN fires, the dedup Redis key is set."""
        from datetime import datetime, timezone
        # The date is read on both sides of the run so a UTC midnight crossing
        # mid-test cannot make the expected key wrong.
        before = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        cap_cents = 3000
        spend_cents = 1500  # 50%

        client = _make_client(spend_cents)

        with patch.object(aca, '_daily_cap_cents', return_value=cap_cents), \
             patch.object(aca, '_send_ntfy', return_value=True), \
             patch.object(aca, '_send_telegram', return_value=True):

            aca.run(client=client)

        client.set.assert_called()
        after = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        set_keys = [c.args[0] for c in client.set.call_args_list]
        assert any(f'ai_cost_alerted:{day}:WARN' in set_keys for day in {before, after})
