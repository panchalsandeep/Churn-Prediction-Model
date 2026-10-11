"""Schema inference, evaluation helpers and the model-quality gate."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from churn import economics, metrics as M
from churn.schema import infer_schema, normalize_target, prepare_features


def telco_like(n=400, seed=0):
    rng = np.random.default_rng(seed)
    tenure = rng.integers(1, 72, n)
    monthly = np.round(rng.uniform(20, 110, n), 2)
    churn = (rng.random(n) < 1 / (1 + np.exp(0.04 * tenure - 0.6))).astype(int)
    total = (tenure * monthly).astype(object)
    total[:5] = ' '                                # Telco's famous blank TotalCharges
    return pd.DataFrame({
        'customerID': [f'{i:04d}-ABCDE' for i in range(n)], 'gender': rng.choice(['Male', 'Female'], n),
        'tenure': tenure, 'Contract': rng.choice(['Month-to-month', 'One year', 'Two year'], n),
        'MonthlyCharges': monthly, 'TotalCharges': total, 'Churn': np.where(churn == 1, 'Yes', 'No'),
    })


def test_schema_handles_telco_style_data():
    schema = infer_schema(telco_like())
    assert schema['target'] == 'Churn'
    assert schema['id'] == 'customerID'
    assert 'TotalCharges' in schema['numeric']          # numbers stored as text are recovered
    assert 'Contract' in schema['categorical']
    assert schema['revenue_col'] == 'MonthlyCharges'
    assert schema['tenure_col'] == 'tenure'


def test_schema_requires_a_churn_column():
    df = telco_like().drop(columns=['Churn'])
    with pytest.raises(ValueError, match='churn column'):
        infer_schema(df)


def test_schema_flags_leakage():
    df = telco_like()
    df['cancelled_flag_score'] = (df['Churn'] == 'Yes').astype(float) + np.random.default_rng(1).normal(0, 0.01, len(df))
    warnings = infer_schema(df)['warnings']
    assert any('cancelled_flag_score' in w for w in warnings)


def test_normalize_target_variants():
    s = pd.Series(['Yes', 'no', 'TRUE', 'churned', 'Retained', None, 'maybe', 1, 0], dtype=object)
    out = normalize_target(s).tolist()
    assert out[:5] == [1, 0, 1, 1, 0]
    assert np.isnan(out[5]) and np.isnan(out[6])


def test_prepare_features_derives_age_from_dates():
    df = pd.DataFrame({'snapshot_date': ['2026-01-31'] * 60, 'signup_date': ['2025-12-31'] * 60, 'x': range(60),
                       'churn': [0, 1] * 30})
    schema = infer_schema(df)
    X = prepare_features(df, schema)
    assert schema['time_col'] == 'snapshot_date' and schema['signup_col'] == 'signup_date'
    assert (X['signup_date_age_days'] == 31).all()


def test_lift_and_precision_at_k_on_perfect_ranking():
    y = np.array([1] * 10 + [0] * 90)
    p = np.r_[np.linspace(0.99, 0.9, 10), np.linspace(0.1, 0.0, 90)]
    lift = M.lift_table(y, p)
    assert lift[0]['lift'] == 10.0 and lift[0]['cumulative_capture'] == 100.0
    assert M.precision_at_fraction(y, p)['10%']['precision'] == 100.0


def test_psi_detects_shift():
    same = M.psi(np.array([.25, .25, .25, .25]), np.array([.25, .25, .25, .25]))
    shifted = M.psi(np.array([.25, .25, .25, .25]), np.array([.7, .1, .1, .1]))
    assert same < 0.001 and shifted > 0.25


def test_economics_thresholds_are_ordered_and_respond_to_cost():
    cheap = economics.derive_thresholds(0.1, 100, economics.merged({'outreach_cost': 5}))
    pricey = economics.derive_thresholds(0.1, 100, economics.merged({'outreach_cost': 500}))
    assert cheap['medium'] <= cheap['high'] and pricey['medium'] <= pricey['high']
    assert pricey['breakeven'] > cheap['breakeven']


def test_csv_injection_is_neutralised():
    from churn.security import csv_safe
    assert csv_safe('=HYPERLINK("x")') == "'=HYPERLINK(\"x\")"
    assert csv_safe('@SUM(A1)').startswith("'") and csv_safe('plain') == 'plain' and csv_safe(5) == 5


def test_model_quality_gate_on_demo_data(trained, client):
    """CI gate: if a code change degrades the model below these floors, the build fails."""
    results = client.get('/api/results').get_json()
    m = results['metrics']
    base = m['base_rate']
    assert m['roc_auc'] >= 72, m
    assert m['pr_auc'] >= base * 2.0, m                       # at least 2x better than guessing
    assert results['lift'][0]['lift'] >= 2.0                  # top decile finds >=2x the base churn rate
    assert m['brier'] < (base / 100) * (1 - base / 100), m    # better calibrated than a constant guess
    assert results['model']['split'] == 'time'
    assert len(results['model']['comparison']) == 3
