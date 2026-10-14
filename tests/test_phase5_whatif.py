"""The what-if simulator rests on /api/predict and /api/model/schema: check that they behave like a sound simulator."""
from __future__ import annotations


def test_levers_exclude_what_nobody_can_change_and_schema_lists_correlated_pairs(client, trained):
    schema = client.get('/api/model/schema').get_json()
    by = {f['name']: f for f in schema['fields']}
    assert any(not f['actionable'] for f in schema['fields'] if 'tenure' in f['name'] or 'signup' in f['name'] or 'contract' in f['name'])
    assert any(f['actionable'] for f in schema['fields'] if f['kind'] == 'number')
    assert all(len(p) == 3 and p[0] in by and p[1] in by and 0.7 <= p[2] <= 1 for p in schema['correlated'])


def test_scoring_the_same_values_is_deterministic_and_reset_returns_exactly_the_original(client, trained):
    schema = client.get('/api/model/schema').get_json()
    values = {f['name']: f['default'] for f in schema['fields']}
    a = client.post('/api/predict', json={'values': values, 'explain': False}).get_json()
    b = client.post('/api/predict', json={'values': values, 'explain': False}).get_json()
    assert a['churn_probability'] == b['churn_probability']
    lever = next(f for f in schema['fields'] if f['kind'] == 'number' and f['actionable'] and f['max'] > f['min'])
    moved = client.post('/api/predict', json={'values': {**values, lever['name']: lever['max']}, 'explain': False}).get_json()
    back = client.post('/api/predict', json={'values': {**values, lever['name']: values[lever['name']]}, 'explain': False}).get_json()
    assert moved['churn_probability'] != a['churn_probability'] and back['churn_probability'] == a['churn_probability']


def test_risk_moves_the_sensible_way_for_the_strongest_planted_driver(client, trained):
    """In the demo data, more days since the last login means more churn. A simulator that disagrees would be wrong."""
    schema = client.get('/api/model/schema').get_json()
    values = {f['name']: f['default'] for f in schema['fields']}
    f = next(x for x in schema['fields'] if x['name'] == 'last_login_days')
    lo = client.post('/api/predict', json={'values': {**values, 'last_login_days': f['min']}, 'explain': False}).get_json()['churn_probability']
    hi = client.post('/api/predict', json={'values': {**values, 'last_login_days': f['max']}, 'explain': False}).get_json()['churn_probability']
    assert hi > lo + 1                                  # direction is right; the size is modest because every other input sits at its typical value
