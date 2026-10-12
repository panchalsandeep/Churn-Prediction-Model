"""Turn probabilities into decisions: who is worth contacting, and what is at stake."""
from __future__ import annotations

DEFAULT_ECONOMICS = {
    'outreach_cost': 25.0,   # cost of one retention touch (CSM time, discount, ...)
    'save_rate': 0.30,       # share of at-risk customers an intervention actually saves
    'value_months': 12.0,    # horizon used to value a saved customer
}


def merged(econ: dict | None) -> dict:
    out = dict(DEFAULT_ECONOMICS)
    for k, v in (econ or {}).items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def expected_loss(prob: float, revenue: float, econ: dict) -> float:
    return float(prob) * float(revenue) * econ['value_months']


def expected_net_value(prob: float, revenue: float, econ: dict) -> float:
    """Expected profit of contacting this customer: saved revenue minus outreach cost."""
    return float(prob) * econ['save_rate'] * float(revenue) * econ['value_months'] - econ['outreach_cost']


def derive_thresholds(base_rate: float, avg_revenue: float, econ: dict) -> dict:
    """Default tier cut-offs from the data and the economics (admins can override)."""
    value = max(avg_revenue, 1e-6) * econ['value_months'] * max(econ['save_rate'], 1e-6)
    breakeven = min(0.95, max(0.02, econ['outreach_cost'] / value))
    high = min(0.9, max(0.15, breakeven, 2.5 * base_rate))
    medium = min(high, max(0.05, 1.3 * base_rate, 0.5 * high))
    return {'high': round(high, 4), 'medium': round(medium, 4), 'breakeven': round(breakeven, 4)}


def tier_for(prob: float, thresholds: dict) -> str:
    if prob >= thresholds['high']:
        return 'High'
    if prob >= thresholds['medium']:
        return 'Medium'
    return 'Low'
