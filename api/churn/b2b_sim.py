"""A synthetic B2B SaaS book of business: accounts, product events, support tickets and invoices, with churn that *follows* behaviour.

Why it exists: there is no public account-level B2B churn dataset with raw event streams, so the pipeline (point-in-time features, target
definition, training, scoring) needs something to be validated on. The generator plants known mechanics, so tests can check the pipeline
recovers them, and leaves a large unobserved component so the achievable AUC stays realistic. It is a test fixture, not evidence about real
customers: results on it say the plumbing works, not how the model would perform on yours.

Mechanics planted
  * every account has a latent health; low health raises the monthly churn hazard
  * in the 2-3 months before churning, product usage decays, failed payments and tickets rise
  * shorter contracts and small accounts churn more
"""
from __future__ import annotations

import numpy as np
import pandas as pd

END = pd.Timestamp('2026-09-30')


def generate_b2b(n_accounts: int = 500, months: int = 18, seed: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    start_month = END - pd.DateOffset(months=months)
    ids = [f'ACME-{i:05d}' for i in range(n_accounts)]
    started = start_month - pd.to_timedelta(rng.integers(0, 24 * 30, n_accounts), unit='D') + pd.to_timedelta(rng.integers(0, months * 30 // 2, n_accounts), unit='D')
    size = rng.choice(['smb', 'mid', 'enterprise'], n_accounts, p=[0.55, 0.35, 0.10])
    seats = np.maximum(2, np.round(pd.Series(size).map({'smb': 6, 'mid': 40, 'enterprise': 300}).to_numpy() * rng.lognormal(0, 0.35, n_accounts))).astype(int)
    mrr = np.round(seats * rng.uniform(18, 42, n_accounts), 2)
    health = rng.normal(0, 1, n_accounts)
    unobserved = rng.normal(0, 0.9, n_accounts)
    term_months = rng.choice([1, 12, 24], n_accounts, p=[0.35, 0.5, 0.15])
    industry = rng.choice(['software', 'retail', 'finance', 'health', 'media'], n_accounts)
    region = rng.choice(['NA', 'EMEA', 'APAC'], n_accounts, p=[0.5, 0.3, 0.2])

    churned_at = np.full(n_accounts, np.datetime64('NaT', 'ns'))
    month_starts = pd.date_range(start_month, END, freq='MS')
    for i in range(n_accounts):
        for m in month_starts:
            if m < started[i] + pd.Timedelta(days=45):
                continue
            logit = (-4.2 - 0.9 * health[i] + 0.8 * unobserved[i] + (0.9 if term_months[i] == 1 else 0.0) - (0.4 if size[i] == 'enterprise' else 0.0))
            if rng.random() < 1 / (1 + np.exp(-logit)):
                churned_at[i] = (m + pd.Timedelta(days=int(rng.integers(0, 28)))).to_datetime64()
                break

    ev_rows, tk_rows, inv_rows = [], [], []
    event_types = np.array(['login', 'report_run', 'export', 'invite_user', 'integration_sync', 'dashboard_view'])
    type_p = np.array([0.35, 0.18, 0.07, 0.03, 0.12, 0.25])
    horizon_end = lambda i: (pd.Timestamp(churned_at[i]) if not pd.isna(churned_at[i]) else END)    # noqa: E731
    for i in range(n_accounts):
        t0, t1 = started[i], horizon_end(i)
        days = pd.date_range(t0.normalize(), t1.normalize(), freq='D')
        if len(days) == 0:
            continue
        base = 0.25 + 0.04 * np.log1p(seats[i]) + 0.12 * health[i] + 0.05 * unobserved[i]
        base = max(base, 0.03) * (1.0 + 0.3 * (size[i] == 'enterprise'))
        decay = np.ones(len(days))
        if not pd.isna(churned_at[i]):
            lead = np.clip((t1.normalize() - days).days.to_numpy() / 75.0, 0, 1)         # 1 far from churn, 0 at churn
            decay = 0.15 + 0.85 * lead
        counts = rng.poisson(base * decay * np.where(days.dayofweek < 5, 1.4, 0.3))
        total = int(counts.sum())
        if total:
            day_idx = np.repeat(np.arange(len(days)), counts)
            ts = days[day_idx] + pd.to_timedelta(rng.integers(0, 86400, total), unit='s')
            users = rng.integers(1, max(2, int(seats[i] * 0.7)) + 1, total)
            ev_rows.append(pd.DataFrame({'account_id': ids[i], 'ts': ts, 'event': rng.choice(event_types, total, p=type_p), 'user_id': [f'{ids[i]}-u{u}' for u in users]}))
        # tickets: more with low health and in the run-up to churn
        stress = float((1 - decay).mean())
        t_rate = (0.10 + 0.05 * max(0, -health[i]) + (0.25 if not pd.isna(churned_at[i]) else 0.0) * stress) / 30.0
        tn = rng.poisson(t_rate * len(days) * (1 + stress))
        if tn:
            tsd = days[rng.integers(0, len(days), tn)] + pd.to_timedelta(rng.integers(0, 86400, tn), unit='s')
            tk_rows.append(pd.DataFrame({'account_id': ids[i], 'ts': tsd, 'severity': rng.choice(['low', 'medium', 'high'], tn, p=[0.5, 0.35, 0.15]),
                                         'status': rng.choice(['closed', 'open'], tn, p=[0.85, 0.15]), 'csat': np.clip(rng.normal(4.2 + 0.3 * health[i] - 0.8 * stress, 0.9, tn), 1, 5).round(1)}))
        # monthly invoices; failures rise before churn
        bill_days = pd.date_range(t0.normalize(), t1.normalize(), freq=pd.DateOffset(months=1))
        fail_p = 0.02 + 0.03 * max(0, -health[i])
        for d in bill_days:
            near_end = (not pd.isna(churned_at[i])) and (t1 - d).days < 75
            failed = rng.random() < (fail_p + (0.3 if near_end else 0.0))
            inv_rows.append((ids[i], d, round(float(mrr[i]) * (0.85 if near_end and rng.random() < 0.4 else 1.0), 2), 'failed' if failed else 'paid'))

    events = pd.concat(ev_rows, ignore_index=True) if ev_rows else pd.DataFrame(columns=['account_id', 'ts', 'event', 'user_id'])
    tickets = pd.concat(tk_rows, ignore_index=True) if tk_rows else pd.DataFrame(columns=['account_id', 'ts', 'severity', 'status', 'csat'])
    invoices = pd.DataFrame(inv_rows, columns=['account_id', 'ts', 'amount', 'status'])

    term_end = []
    for i in range(n_accounts):
        anchor = horizon_end(i)
        k = max(1, int(np.ceil(((anchor - started[i]).days + 1) / (30.4 * term_months[i]))))
        term_end.append(started[i] + pd.DateOffset(months=int(k * term_months[i])))
    accounts = pd.DataFrame({'account_id': ids, 'started_at': started, 'churned_at': pd.to_datetime(churned_at), 'contract_end': term_end,
                             'mrr': mrr, 'seats_licensed': seats, 'company_size': size, 'industry': industry, 'region': region})
    # Events after the end of the observation window do not exist yet.
    for df in (events, tickets, invoices):
        df.drop(df.index[df['ts'] > END], inplace=True)
    return {'accounts': accounts, 'events': events.reset_index(drop=True), 'tickets': tickets.reset_index(drop=True),
            'invoices': invoices.reset_index(drop=True), 'observed_through': END}
