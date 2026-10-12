"""Synthetic B2B-SaaS churn data with realistic structure.

Why synthetic: public churn sets are small and snapshot-only. This generator produces
rolling-window behavioural features, several monthly snapshots (so a time-based split is
meaningful), mild concept drift, missing values and an unobserved per-customer effect that
caps achievable AUC well below 1.0, like real data. Target: churn within the next 30 days.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

AS_OF = pd.Timestamp('2026-09-30')


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def generate_demo(n: int = 8000, seed: int = 7, target_rate: float = 0.115) -> pd.DataFrame:
    rng = np.random.default_rng(seed)

    # Nine month-end snapshots; later snapshots are slightly more at risk (drift).
    snapshots = pd.date_range(end=AS_OF, periods=9, freq='ME')
    snap_idx = rng.integers(0, len(snapshots), n)
    snapshot_date = snapshots[snap_idx]

    plan = rng.choice(['Basic', 'Plus', 'Premium'], n, p=[0.45, 0.35, 0.20])
    base_price = pd.Series(plan).map({'Basic': 29.0, 'Plus': 59.0, 'Premium': 119.0}).to_numpy()
    monthly_charges = np.round(base_price * rng.lognormal(0, 0.12, n), 2)

    tenure = np.clip(np.round(rng.gamma(2.2, 9.0, n)), 1, 72).astype(int)
    signup_date = snapshot_date - pd.to_timedelta(tenure * 30, unit='D')

    contract = np.where(
        rng.random(n) < 0.55, 'Month-to-month',
        np.where(rng.random(n) < 0.66, 'One year', 'Two year'),
    )
    payment = rng.choice(['Card', 'Bank transfer', 'PayPal', 'Invoice'], n, p=[0.40, 0.25, 0.20, 0.15])
    region = rng.choice(['North America', 'Europe', 'APAC', 'LATAM'], n, p=[0.40, 0.30, 0.20, 0.10])
    channel = rng.choice(['Organic', 'Paid search', 'Referral', 'Partner', 'Outbound'], n, p=[0.30, 0.25, 0.15, 0.15, 0.15])
    company_size = rng.choice(['1-10', '11-50', '51-200', '201-1000', '1000+'], n, p=[0.25, 0.30, 0.25, 0.14, 0.06])
    seats = np.maximum(1, np.round(pd.Series(company_size).map(
        {'1-10': 5, '11-50': 25, '51-200': 90, '201-1000': 350, '1000+': 1200}).to_numpy() * rng.lognormal(0, 0.3, n))).astype(int)

    # Latent engagement (observed through noisy behaviour) + an unobserved customer effect.
    engagement = np.clip(rng.beta(2.2, 2.2, n) + 0.004 * np.minimum(tenure, 24) - 0.05, 0.02, 0.99)
    unobserved = rng.normal(0, 0.75, n)

    logins_30d = rng.poisson(1.5 + 38 * engagement)
    trend_factor = np.exp(rng.normal(0, 0.45, n) + 0.9 * (engagement - 0.5) * 0.0 - unobserved * 0.15)
    prior_monthly = np.maximum(0.5, (1.5 + 38 * engagement) / np.maximum(trend_factor, 0.15))
    logins_90d = logins_30d + rng.poisson(prior_monthly * 2)
    login_trend = np.round(logins_30d / np.maximum(1.0, (logins_90d - logins_30d) / 2.0), 3)
    last_login_days = np.minimum(120, rng.geometric(np.clip(0.08 + 0.55 * engagement, 0.05, 0.95)) - 1)
    features_used = rng.binomial(12, np.clip(0.15 + 0.75 * engagement, 0.05, 0.95))
    seats_active_pct = np.round(np.clip(engagement * 100 + rng.normal(0, 12, n), 1, 100), 1)

    tickets = rng.poisson(0.5 + 2.2 * (1 - engagement) + 0.3 * (plan == 'Premium'))
    sentiment = np.clip(rng.normal(0.25 + 0.5 * (engagement - 0.5) - 0.2 * np.minimum(tickets, 5) / 5 - unobserved * 0.1, 0.35), -1, 1)
    sentiment = np.round(np.where(tickets == 0, np.nan, sentiment), 2)
    unresolved = rng.binomial(tickets, np.clip(0.12 + 0.25 * (1 - engagement), 0, 0.6))
    payment_failures = rng.poisson(0.05 + 0.5 * (payment == 'Invoice') * (1 - engagement) + 0.25 * (payment == 'Card') * (1 - engagement))
    discount_pct = rng.choice([0, 10, 20, 30], n, p=[0.55, 0.20, 0.17, 0.08])
    nps = np.clip(np.round(rng.normal(6.5 + 4 * (engagement - 0.5) - unobserved * 0.6, 2.4)), 0, 10)
    nps = np.where(rng.random(n) < 0.55, np.nan, nps)  # most customers never answer the survey

    renewal_span = np.where(contract == 'Month-to-month', 30, np.where(contract == 'One year', 365, 730))
    days_to_renewal = np.floor(rng.random(n) * renewal_span).astype(int)

    sent = np.nan_to_num(sentiment, nan=0.2)
    z = (
        -2.2 * (engagement - 0.5)
        + 1.2 * (login_trend < 0.6) + 0.5 * (login_trend < 0.3)
        + 0.03 * np.minimum(last_login_days, 60)
        + 0.35 * (tickets >= 3) + 0.9 * (sent < -0.3) + 0.45 * unresolved
        + 0.65 * np.minimum(payment_failures, 3)
        + 1.0 * (contract == 'Month-to-month') - 0.9 * (contract == 'Two year')
        + 0.85 * (tenure <= 3)
        + 0.55 * ((days_to_renewal <= 30) & (contract != 'Month-to-month'))
        - 0.25 * (discount_pct >= 20)
        - 0.06 * features_used
        + np.select([channel == 'Paid search', channel == 'Referral'], [0.25, -0.3], 0.0)
        + unobserved
        + 0.04 * snap_idx  # concept drift
    )

    # Choose the intercept so the overall base rate is realistic (~11-12%).
    lo, hi = -10.0, 10.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if _sigmoid(z + mid).mean() > target_rate:
            hi = mid
        else:
            lo = mid
    churn = (rng.random(n) < _sigmoid(z + (lo + hi) / 2)).astype(int)

    df = pd.DataFrame({
        'customer_id': [f'CUST-{i + 1:06d}' for i in range(n)],
        'snapshot_date': snapshot_date.strftime('%Y-%m-%d'),
        'signup_date': signup_date.strftime('%Y-%m-%d'),
        'plan': plan,
        'contract_type': contract,
        'payment_method': payment,
        'region': region,
        'acquisition_channel': channel,
        'company_size': company_size,
        'tenure_months': tenure,
        'monthly_charges': monthly_charges,
        'seats_purchased': seats,
        'seats_active_pct': seats_active_pct,
        'logins_30d': logins_30d,
        'logins_90d': logins_90d,
        'login_trend': login_trend,
        'last_login_days': last_login_days,
        'features_used': features_used,
        'support_tickets_90d': tickets,
        'avg_ticket_sentiment': sentiment,
        'unresolved_tickets': unresolved,
        'payment_failures_6m': payment_failures,
        'discount_pct': discount_pct,
        'nps_score': nps,
        'days_to_renewal': days_to_renewal,
        'churn': churn,
    })
    return df.sort_values('snapshot_date').reset_index(drop=True)
