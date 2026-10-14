"""Deterministic column matching for uploaded tables: your column names and order do not have to match ours.

For each canonical column (account_id, started_at, ts, ...) every source column is scored from two kinds of evidence:
  name     exact match after normalisation, a synonym list ("customer_ref", "signup", "event_time"...), or fuzzy similarity
  content  does the column look like what it should be (parses as dates, as numbers, has repeated ids, few distinct categories)
The best non-conflicting assignment wins. Confident matches (>= 0.85) are applied automatically; weaker ones are only suggested and need a
person to confirm. No data leaves the server and the same file always gives the same answer.

An LLM could rank the low-confidence leftovers, but it would see column names and sample values (customer data), cost money per upload and
be non-deterministic. It is the planned optional step for the ambiguous remainder (roadmap Phase 6), behind redaction and an opt-in.
"""
from __future__ import annotations

import difflib
import re

import pandas as pd

AUTO = 0.85
SUGGEST = 0.55

SYNONYMS: dict[str, list[str]] = {
    'account_id': ['account', 'accountid', 'account_id', 'account_number', 'account_ref', 'customer', 'customer_id', 'customerid', 'customer_ref', 'cust_id',
                   'client_id', 'org_id', 'organization_id', 'organisation_id', 'company_id', 'tenant_id', 'workspace_id', 'id'],
    'started_at': ['started_at', 'start_date', 'signup_date', 'sign_up_date', 'signed_up', 'created', 'created_at', 'created_date', 'customer_since', 'since',
                   'onboarded_at', 'activation_date', 'contract_start', 'subscription_start', 'join_date'],
    'churned_at': ['churned_at', 'churn_date', 'cancelled_at', 'canceled_at', 'cancellation_date', 'terminated_at', 'end_date', 'ended_at', 'closed_at', 'lost_date'],
    'contract_end': ['contract_end', 'renewal_date', 'renews_on', 'term_end', 'subscription_end', 'expires_at', 'expiry_date', 'contract_expiry', 'next_renewal'],
    'mrr': ['mrr', 'monthly_revenue', 'monthly_recurring_revenue', 'monthly_fee', 'monthly_charges', 'arr_monthly', 'revenue', 'plan_price', 'amount_monthly'],
    'seats_licensed': ['seats', 'seats_licensed', 'licenses', 'licences', 'licensed_seats', 'user_limit', 'purchased_seats', 'num_seats'],
    'ts': ['ts', 'timestamp', 'time', 'when', 'date', 'datetime', 'event_at', 'event_time', 'event_date', 'occurred_at', 'created_at', 'opened_at', 'invoice_date', 'billed_at', 'paid_at',
           'logged_at', 'at'],
    'event': ['event', 'event_name', 'event_type', 'action', 'activity', 'type', 'name', 'feature'],
    'user_id': ['user_id', 'userid', 'user', 'actor', 'actor_id', 'member_id', 'seat_id', 'email_hash'],
    'value': ['value', 'amount', 'count', 'quantity', 'duration', 'duration_s', 'score'],
    'severity': ['severity', 'priority', 'urgency', 'level'],
    'status': ['status', 'state', 'payment_status', 'invoice_status', 'ticket_status', 'result'],
    'csat': ['csat', 'satisfaction', 'rating', 'nps', 'survey_score', 'score'],
    'amount': ['amount', 'total', 'invoice_amount', 'price', 'charge', 'net_amount', 'amount_due', 'value'],
}
KIND = {'started_at': 'date', 'churned_at': 'date', 'contract_end': 'date', 'ts': 'date', 'mrr': 'number', 'seats_licensed': 'number', 'value': 'number',
        'csat': 'number', 'amount': 'number', 'account_id': 'id', 'user_id': 'id', 'event': 'category', 'severity': 'category', 'status': 'category'}


def _norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', re.sub(r'([a-z])([A-Z])', r'\1_\2', str(name)).strip().lower()).strip('_')


def _name_score(canon: str, col: str) -> float:
    n = _norm(col)
    if n == canon:
        return 1.0
    syn = SYNONYMS.get(canon, [])
    if n in syn:
        return 0.95 if syn.index(n) < 6 else 0.9
    best = max([difflib.SequenceMatcher(None, n, s).ratio() for s in syn] + [0.0])
    tokens = set(n.split('_')) & {t for s in syn for t in s.split('_') if len(t) > 3}
    return max(best * 0.8 if best >= 0.75 else 0.0, 0.55 if tokens else 0.0)


def _content_score(kind: str, s: pd.Series, role: str, canon: str) -> float:
    s = s.dropna()
    if s.empty:
        return 0.0
    sample = s.head(500)
    if kind == 'date':
        if pd.api.types.is_numeric_dtype(sample) and not pd.api.types.is_datetime64_any_dtype(sample):
            return 0.0
        return float(pd.to_datetime(sample.astype(str), errors='coerce', utc=True, format='mixed').notna().mean())
    if kind == 'number':
        return float(pd.to_numeric(sample, errors='coerce').notna().mean())
    if kind == 'id':
        u = s.nunique() / len(s)
        if canon == 'account_id':
            return 0.9 if (role == 'accounts' and u > 0.9) or (role != 'accounts' and u < 0.9) else 0.5       # unique in accounts, repeated in event tables
        return 0.7
    if kind == 'category':
        return 1.0 if s.nunique() <= max(30, 0.05 * len(s)) else 0.3
    return 0.5


def suggest(role: str, df: pd.DataFrame, spec: dict) -> dict:
    """Best assignment of source columns to canonical ones. Returns {mapping, confidence, applied, needs_review, unmatched, missing}."""
    canon_fields = spec['required'] + spec['optional']
    cands: list[tuple[float, str, str, float, float]] = []
    for canon in canon_fields:
        kind = KIND.get(canon, 'any')
        for col in df.columns:
            name = _name_score(canon, col)
            if name <= 0:
                continue
            content = _content_score(kind, df[col], role, canon)
            if kind in ('date', 'number') and content < 0.6:
                continue                                                          # name says date but the values are not dates: not a match
            score = round(0.7 * name + 0.3 * content, 3) if name < 1.0 else round(0.85 + 0.15 * content, 3)
            cands.append((score, canon, col, name, content))
    cands.sort(key=lambda c: -c[0])
    mapping, conf, used_cols = {}, {}, set()
    for score, canon, col, name, content in cands:
        if canon in mapping or col in used_cols or score < SUGGEST:
            continue
        mapping[canon] = col
        conf[canon] = score
        used_cols.add(col)
    applied = {k: v for k, v in mapping.items() if conf[k] >= AUTO}
    review = {k: {'column': v, 'confidence': conf[k]} for k, v in mapping.items() if conf[k] < AUTO}
    return {'mapping': mapping, 'confidence': conf, 'applied': applied, 'needs_review': review,
            'unmatched': [str(c) for c in df.columns if c not in used_cols],
            'missing_required': [c for c in spec['required'] if c not in mapping]}
