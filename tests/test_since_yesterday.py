from morning_engine.data_assembly import (
    format_since_yesterday_payload,
    format_since_yesterday_block,
)


def test_block_states_prior_report_date():
    """The Since-Yesterday block must name the prior report's real date + weekday
    so the LLM never has to guess (and never mislabels e.g. 'Tuesday')."""
    payload = format_since_yesterday_payload(
        signal_flips=["[HIGH] BTC: flip"],
        yesterday_regime=None,
        quant_context={},
        yesterday_report={
            "content": "## TL;DR\n- prior call\n",
            "created_at": "2026-06-23T09:34:07+00:00",
        },
    )
    block = format_since_yesterday_block(payload)
    assert "2026-06-23" in block
    assert "Tuesday" in block


def test_payload_tolerates_missing_date():
    payload = format_since_yesterday_payload(
        signal_flips=["[HIGH] BTC: flip"],
        yesterday_regime=None,
        quant_context={},
        yesterday_report={"content": "## TL;DR\n- prior call\n"},  # no created_at
    )
    assert "yesterday_date" not in payload
    block = format_since_yesterday_block(payload)
    assert "Prior report:" not in block
