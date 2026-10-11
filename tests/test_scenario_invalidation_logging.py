"""A scenario invalidation rule that fails to evaluate is logged, not swallowed."""
import logging

from carrac import conditions


def test_failing_invalidation_rule_is_logged(monkeypatch, caplog):
    def boom(params, **kwargs):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setitem(conditions._EVALUATORS, 'price_level', boom)
    scenario = {
        'id': 7,
        'status': 'watching',
        'conditions': {
            'logic': 'all',
            'rules': [{'id': 'r1', 'type': 'price_level',
                       'params': {'level': 1, 'direction': 'above'}}],
            'invalidation_rules': [{'id': 'i1', 'type': 'price_level',
                                    'params': {'level': 1, 'direction': 'below'}}],
        },
    }
    with caplog.at_level(logging.ERROR, logger='carrac.conditions'):
        result = conditions.evaluate_scenario(scenario, {'price': 100, 'asset_data': {}, 'all_assets': {}})
    assert result['invalidation_triggered'] is False
    assert any("invalidation rule 'i1'" in r.getMessage() and r.exc_info for r in caplog.records)
