"""Known coverage failures and rule boundaries remain distinct from source presence."""
from copy import deepcopy

import pytest

from scripts.benchmarks.report_writing_architectures import (
    IDS,
    OBSERVATIONS,
    deterministic_ledger,
    score,
)
from scripts.benchmarks.reporting_quality_replay import replay


def ledger_for(eid, rule_id, observations=None):
    rows = deterministic_ledger({'selections': [{'id': eid, 'rule_id': rule_id}]},
                                observations=observations)
    return next(row for row in rows if row['id'] == eid)


def test_archived_captions_and_current_delivery_do_not_prove_prose_or_action_coverage():
    result = replay()
    assert result['provider_calls'] == result['new_report_runs'] == 0
    assert [result['groups'][name]['records'] for name in ('residual_current', 'thermal', 'spd')] == [14, 13, 9]
    assert all(g['delivered'] == g['records'] for g in result['groups'].values())
    rows = {row['evidence_id']: row for g in result['groups'].values() for row in g['rows']}
    for eid in ('E-0071', 'E-0133', 'E-0142'):
        assert rows[eid]['source_alias_in_historical_section_including_captions']
        assert not rows[eid]['source_alias_in_historical_narrative']
        assert rows[eid]['judgment_coverage'] == 'requires_semantic_review'
        assert rows[eid]['action_coverage'] == 'requires_object_mapping'
    assert all(result['known_historical_defect_checks'].values())


def test_missing_temperature_reference_preserves_rule_candidates_without_inventing_selection():
    row = ledger_for(IDS[3], 'unknown')
    assert row['status'] == 'unknown'
    assert row['comparison_state'] == 'rule_not_selected'
    assert row['rule_ids'] == [] and row['rule'] is None
    assert list(row['candidate_rules']) == ['R-004']
    assert row['candidate_rules']['R-004']['max'] == '30'
    assert row['missing_inputs'] == ['ambient_temperature']
    selected = ledger_for(IDS[3], 'R-004')
    assert selected['status'] == 'unknown'
    assert selected['comparison_state'] == 'missing_measurement'
    assert selected['rule_ids'] == ['R-004']


@pytest.mark.parametrize('ambient,quantity,unit,status,state', [
    ('25', 'measured_temperature', 'C', 'normal', 'compared'),
    ('24.9', 'measured_temperature', '℃', 'abnormal', 'compared'),
    ('25', 'temperature_rise', 'C', 'unknown', 'incompatible_quantity_or_unit'),
    ('25', 'measured_temperature', 'F', 'unknown', 'incompatible_quantity_or_unit'),
])
def test_temperature_rise_uses_same_observation_reference_and_quantity(ambient, quantity, unit, status, state):
    observations = deepcopy(OBSERVATIONS)
    observations[3].update(ambient=ambient, quantity=quantity, unit=unit)
    row = ledger_for(IDS[3], 'R-004', observations)
    assert (row['status'], row['comparison_state']) == (status, state)
    if state == 'compared':
        assert '55 C' not in row['comparison']
        assert row['comparison'].endswith('30 K')


def test_same_object_different_conditions_can_have_different_conclusions():
    a, b = ledger_for(IDS[0], 'R-001'), ledger_for(IDS[1], 'R-002')
    assert a['observation']['object'] == b['observation']['object']
    assert a['observation']['condition'] != b['observation']['condition']
    assert (a['status'], b['status']) == ('abnormal', 'normal')
    wrong_condition = ledger_for(IDS[0], 'R-002')
    assert wrong_condition['status'] == 'unknown'
    assert wrong_condition['comparison_state'] == 'inapplicable_rule'
    assert list(wrong_condition['candidate_rules']) == ['R-001']


def test_decimal_percent_unit_conversion_and_retired_rule_are_independent():
    assert ledger_for(IDS[2], 'R-003')['comparison'] == '2.3 % <= 3 %'
    assert ledger_for(IDS[4], 'R-005')['comparison'] == '11.3000 mA > 10 mA'
    retired = ledger_for(IDS[6], 'R-006')
    assert retired['status'] == 'unknown' and retired['comparison_state'] == 'retired_rule'
    assert list(retired['candidate_rules']) == ['R-007']
    assert ledger_for(IDS[6], 'R-007')['status'] == 'normal'


def test_missing_observation_condition_and_selection_are_not_missing_rules():
    absent = ledger_for(IDS[5], 'unknown')
    assert absent['comparison_state'] == 'missing_observation' and absent['evidence_ids'] == []
    assert ledger_for(IDS[7], 'unknown')['comparison_state'] == 'missing_condition'
    observations = deepcopy(OBSERVATIONS)
    observations[0]['condition'] = 'no_matching_rule_in_fixture'
    assert ledger_for(IDS[0], 'unknown', observations)['comparison_state'] == 'no_applicable_rule'
    rows = deterministic_ledger({'selections': [
        {'id': IDS[0], 'rule_id': 'R-001'}, {'id': IDS[0], 'rule_id': 'R-002'}]})
    assert rows[0]['status'] == 'unknown' and rows[0]['comparison_state'] == 'ambiguous_selection'


def test_same_observation_cannot_change_conclusion_between_body_and_summary():
    rows = deterministic_ledger({'selections': [
        {'id': eid, 'rule_id': rid} for eid, rid in zip(IDS, [
            'R-001', 'R-002', 'R-003', 'R-004', 'R-005', 'unknown', 'R-007', 'unknown'])]})
    result = {'judgments': rows, 'summary': [{'id': r['id'], 'status': r['status']} for r in rows],
              'chapters': ['2.4 设备', '3.2 行动']}
    assert score(result) == []
    result['summary'][0]['status'] = 'normal'
    assert score(result) == ['consistency:E-0001']
