"""Application services: settings, scoring, summaries, alerts, interventions, monitoring."""
from __future__ import annotations

import os
import time
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa

from . import db, economics, registry, training
from .config import data_dir
from .schema import prepare_features

DEFAULT_SETTINGS = {
    'economics': dict(economics.DEFAULT_ECONOMICS),
    'thresholds': {'mode': 'auto', 'high': None, 'medium': None},
    'alert_rules': {'high_risk': {'enabled': True}, 'risk_jump': {'enabled': True, 'delta': 0.15}},
    'revenue_column': None,
}
MAX_ALERTS = 300
REASON_ROWS = 300

ACTION_RULES = [
    (('login', 'activity', 'usage', 'feature', 'seat', 'session', 'active'), 'Run a re-engagement campaign and offer a product-adoption session'),
    (('ticket', 'support', 'complaint', 'sentiment', 'unresolved'), 'Escalate open support issues and assign a dedicated support contact'),
    (('payment', 'billing', 'invoice', 'failure', 'overdue', 'balance'), 'Reach out about billing and offer a more flexible payment method'),
    (('contract', 'renewal', 'term'), 'Open a renewal conversation and offer a longer-term incentive'),
    (('tenure', 'signup', 'age_days', 'onboard'), 'Schedule an onboarding check-in to secure early value'),
    (('discount', 'price', 'charge', 'plan', 'revenue', 'spend'), 'Review pricing and plan fit; propose a right-sized plan'),
    (('nps', 'satisfaction', 'score'), 'Follow up on satisfaction feedback with a CSM call'),
]


def humanize(name: str) -> str:
    return name.replace('_age_days', ' (days)').replace('_', ' ')


# ── Settings ────────────────────────────────────────────────────────────────────

def get_settings(ws: str) -> dict:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.settings_t.c.settings).where(db.settings_t.c.workspace_id == ws)).first()
    saved = db.loads(r[0], {}) if r else {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for kk, vv in v.items():
                out[k][kk] = {**out[k].get(kk, {}), **vv} if isinstance(vv, dict) and isinstance(out[k].get(kk), dict) else vv
        else:
            out[k] = v
    out['economics'] = economics.merged(out['economics'])
    return out


def save_settings(ws: str, patch: dict) -> dict:
    current = get_settings(ws)
    econ = patch.get('economics') or {}
    for key, lo, hi in (('outreach_cost', 0, 1e7), ('save_rate', 0.001, 1), ('value_months', 0.5, 120)):
        if key in econ:
            try:
                current['economics'][key] = min(hi, max(lo, float(econ[key])))
            except (TypeError, ValueError):
                raise ValueError(f'{key} must be a number')
    thr = patch.get('thresholds')
    if thr:
        if thr.get('mode') in ('auto', 'manual'):
            current['thresholds']['mode'] = thr['mode']
        for key in ('high', 'medium'):
            if thr.get(key) is not None:
                v = float(thr[key])
                if not 0 < v <= 1:
                    raise ValueError('Thresholds must be between 0 and 1')
                current['thresholds'][key] = v
        if current['thresholds']['mode'] == 'manual':
            h, m = current['thresholds'].get('high'), current['thresholds'].get('medium')
            if h is None or m is None or m > h:
                raise ValueError('Manual thresholds need high ≥ medium, both set.')
    rules = patch.get('alert_rules')
    if rules:
        for rid in ('high_risk', 'risk_jump'):
            if rid in rules:
                current['alert_rules'][rid].update({k: v for k, v in rules[rid].items() if k in ('enabled', 'delta')})
    if 'revenue_column' in patch:
        current['revenue_column'] = patch['revenue_column'] or None
    with db.get_engine().begin() as c:
        c.execute(sa.delete(db.settings_t).where(db.settings_t.c.workspace_id == ws))
        c.execute(db.settings_t.insert().values(workspace_id=ws, settings=db.dumps(current), updated_at=time.time()))
    return current


def effective_thresholds(settings: dict, bundle: dict) -> dict:
    t = settings['thresholds']
    econ = settings['economics']
    auto = economics.derive_thresholds(bundle.get('base_rate', 0.1), bundle.get('avg_revenue', 1.0), econ)
    if t.get('mode') == 'manual' and t.get('high') is not None and t.get('medium') is not None:
        return {'high': float(t['high']), 'medium': float(t['medium']), 'breakeven': auto['breakeven'], 'mode': 'manual'}
    return {**auto, 'mode': 'auto'}


# ── KV store (segments, drift) ─────────────────────────────────────────────────

def kv_set(ws: str, key: str, value: Any) -> None:
    with db.get_engine().begin() as c:
        c.execute(sa.delete(db.kv_t).where(db.kv_t.c.workspace_id == ws, db.kv_t.c.key == key))
        c.execute(db.kv_t.insert().values(workspace_id=ws, key=key, value=db.dumps(value), updated_at=time.time()))


def kv_get(ws: str, key: str, default: Any = None) -> Any:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.kv_t.c.value).where(db.kv_t.c.workspace_id == ws, db.kv_t.c.key == key)).first()
    return db.loads(r[0], default) if r else default


# ── Scoring ────────────────────────────────────────────────────────────────────

def _clean_attr(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if np.isnan(v) else round(float(v), 4)
    if isinstance(v, pd.Timestamp):
        return v.strftime('%Y-%m-%d')
    return v


def build_scored_frame(ws: str, bundle: dict, df: pd.DataFrame, settings: dict, *, X: pd.DataFrame | None = None,
                       probs: np.ndarray | None = None, reasons_by_pos: dict | None = None,
                       revenue: np.ndarray | None = None) -> pd.DataFrame:
    """Everything the UI needs per customer, ready to bulk-insert."""
    schema = bundle['schema']
    thresholds = effective_thresholds(settings, bundle)
    econ = settings['economics']
    cols = bundle['cols']

    if X is None:
        X = prepare_features(df, schema).reindex(columns=cols)
        for c in bundle['cat_cols']:
            X[c] = X[c].fillna('Missing')
    if probs is None:
        probs = training.clip_probs(bundle['model'].predict_proba(X)[:, 1])
    n = len(df)

    rev_col = settings.get('revenue_column') or schema.get('revenue_col')
    if revenue is None:
        if rev_col and rev_col in df.columns:
            revenue = pd.to_numeric(df[rev_col], errors='coerce').fillna(0).clip(lower=0).to_numpy()
        else:
            revenue = np.ones(n)

    if schema.get('id') and schema['id'] in df.columns:
        ids = df[schema['id']].astype(str).to_numpy()
    else:
        ids = np.array([f'ROW-{i + 1:06d}' for i in range(n)])

    tenure = pd.to_numeric(df[schema['tenure_col']], errors='coerce').to_numpy() if schema.get('tenure_col') in df.columns else np.full(n, np.nan)
    activity = pd.to_numeric(df[schema['activity_col']], errors='coerce').to_numpy() if schema.get('activity_col') in df.columns else np.full(n, np.nan)
    actual = None
    if schema['target'] in df.columns:
        from .schema import normalize_target
        a = normalize_target(df[schema['target']])
        actual = a.to_numpy()

    attr_cols = [c for c in schema['numeric'] + schema['categorical'] + schema['dates'] if c in df.columns]
    attrs_df = df[attr_cols].copy()
    for c in schema['dates']:
        if c in attrs_df:
            attrs_df[c] = pd.to_datetime(attrs_df[c], errors='coerce').dt.strftime('%Y-%m-%d')
    attrs = [db.dumps({k: _clean_attr(v) for k, v in rec.items()}) for rec in attrs_df.astype(object).to_dict('records')]

    scored = pd.DataFrame({
        'customer_id': ids, 'prob': probs, 'revenue': revenue, 'tenure': tenure, 'activity': activity,
        'actual': actual if actual is not None else np.nan, 'attrs': attrs,
    })
    scored['tier'] = [economics.tier_for(p, thresholds) for p in probs]
    scored['expected_loss'] = probs * revenue * econ['value_months']
    scored = scored.drop_duplicates('customer_id', keep='last').reset_index(drop=True)

    # reasons for the riskiest customers (drives the "why" column and alerts)
    X_dedup = X.loc[~pd.Series(ids).duplicated(keep='last').to_numpy()].reset_index(drop=True) if len(scored) != n else X.reset_index(drop=True)
    top = np.argsort(-scored['prob'].to_numpy())[:REASON_ROWS]
    reasons: list[str | None] = [None] * len(scored)
    drivers: list[str | None] = [None] * len(scored)
    computed = training.explain_rows(bundle['model'], X_dedup.iloc[top], bundle['baseline'], cols) if reasons_by_pos is None or len(scored) != n \
        else [reasons_by_pos.get(int(i)) or [] for i in top]
    for pos, rs in zip(top, computed):
        reasons[pos] = db.dumps(rs)
        up = [r for r in rs if r['impact'] > 0]
        drivers[pos] = humanize(up[0]['feature']) if up else None
    scored['reasons'] = reasons
    scored['top_driver'] = drivers
    return scored


def persist_scores(ws: str, model_id: str, scored: pd.DataFrame) -> None:
    with db.get_engine().begin() as conn:
        prev = {r.customer_id: r.prob for r in conn.execute(
            sa.select(db.customers_t.c.customer_id, db.customers_t.c.prob).where(db.customers_t.c.workspace_id == ws))}
        conn.execute(sa.delete(db.customers_t).where(db.customers_t.c.workspace_id == ws))
        rows = []
        for r in scored.itertuples(index=False):
            rows.append({
                'workspace_id': ws, 'customer_id': r.customer_id, 'model_id': model_id, 'prob': float(r.prob),
                'prev_prob': prev.get(r.customer_id), 'tier': r.tier, 'revenue': float(r.revenue),
                'tenure': None if pd.isna(r.tenure) else float(r.tenure), 'activity': None if pd.isna(r.activity) else float(r.activity),
                'expected_loss': float(r.expected_loss), 'actual': None if pd.isna(r.actual) else int(r.actual),
                'top_driver': r.top_driver, 'reasons': r.reasons, 'attrs': r.attrs,
            })
        for i in range(0, len(rows), 5000):
            conn.execute(db.customers_t.insert(), rows[i:i + 5000])


def save_snapshot(ws: str, model_id: str, scored: pd.DataFrame) -> str:
    path = os.path.join(registry.model_dir(ws, model_id), 'customers.csv.gz')
    scored.to_csv(path, index=False, compression='gzip')
    return path


def load_snapshot(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, compression='gzip', keep_default_na=True)
    for c in ('reasons', 'top_driver', 'attrs'):
        df[c] = df[c].where(df[c].notna(), None)
    return df


# ── Alerts ─────────────────────────────────────────────────────────────────────

def _signals(reasons_json: str | None) -> list[str]:
    out = []
    for r in (db.loads(reasons_json, []) or [])[:3]:
        if r['impact'] > 0:
            out.append(f"{humanize(r['feature'])} = {r['value']} (+{r['impact']:.1f} pts)")
    return out


def regenerate_alerts(ws: str, settings: dict) -> int:
    rules = settings['alert_rules']
    now = time.time()
    cu = db.customers_t
    desired: dict[tuple[str, str], dict] = {}
    with db.get_engine().begin() as conn:
        if rules['high_risk']['enabled']:
            for r in conn.execute(sa.select(cu).where(cu.c.workspace_id == ws, cu.c.tier == 'High')
                                  .order_by(cu.c.expected_loss.desc()).limit(MAX_ALERTS)):
                desired[(r.customer_id, 'high_risk')] = {
                    'severity': 'High', 'title': f'High churn risk ({r.prob * 100:.0f}%)',
                    'details': f'{r.customer_id} has a {r.prob * 100:.1f}% churn probability; about {r.expected_loss:,.0f} of revenue at risk.',
                    'signals': _signals(r.reasons), 'expected_loss': r.expected_loss}
        if rules['risk_jump']['enabled']:
            delta = float(rules['risk_jump'].get('delta', 0.15))
            q = sa.select(cu).where(cu.c.workspace_id == ws, cu.c.prev_prob.is_not(None), (cu.c.prob - cu.c.prev_prob) >= delta) \
                .order_by((cu.c.prob - cu.c.prev_prob).desc()).limit(100)
            for r in conn.execute(q):
                desired.setdefault((r.customer_id, 'risk_jump'), {
                    'severity': 'High' if r.tier == 'High' else 'Medium',
                    'title': f'Risk jumped {(r.prob - r.prev_prob) * 100:+.0f} pts',
                    'details': f'{r.customer_id} moved from {r.prev_prob * 100:.0f}% to {r.prob * 100:.0f}% churn probability since the last scoring run.',
                    'signals': _signals(r.reasons), 'expected_loss': r.expected_loss})
        at = db.alerts_t
        existing = {(r.customer_id, r.rule): r for r in conn.execute(sa.select(at).where(at.c.workspace_id == ws, at.c.status != 'Resolved'))}
        for key, row in existing.items():
            if key not in desired and row.status == 'Open':
                conn.execute(sa.delete(at).where(at.c.id == row.id))
        inserts = []
        for (cid, rule), d in desired.items():
            if (cid, rule) in existing:
                conn.execute(sa.update(at).where(at.c.id == existing[(cid, rule)].id).values(
                    severity=d['severity'], title=d['title'], details=d['details'], signals=db.dumps(d['signals']),
                    expected_loss=d['expected_loss'], updated_at=now))
            else:
                inserts.append({'id': db.new_id(), 'workspace_id': ws, 'customer_id': cid, 'rule': rule, 'severity': d['severity'],
                                'title': d['title'], 'details': d['details'], 'signals': db.dumps(d['signals']), 'status': 'Open',
                                'expected_loss': d['expected_loss'], 'created_at': now, 'updated_at': now})
        if inserts:
            conn.execute(at.insert(), inserts)
    return len(desired)


def list_alerts(ws: str, status: str | None = None, limit: int = 100) -> dict:
    at = db.alerts_t
    with db.get_engine().connect() as c:
        counts = {r.status: r.n for r in c.execute(sa.select(at.c.status, sa.func.count().label('n')).where(at.c.workspace_id == ws).group_by(at.c.status))}
        q = sa.select(at).where(at.c.workspace_id == ws)
        if status:
            q = q.where(at.c.status == status)
        rows = c.execute(q.order_by(at.c.expected_loss.desc().nulls_last(), at.c.created_at.desc()).limit(limit)).all()
    items = []
    for r in rows:
        d = dict(r._mapping)
        d['signals'] = db.loads(d['signals'], [])
        items.append(d)
    return {'alerts': items, 'counts': {s: counts.get(s, 0) for s in ('Open', 'Acknowledged', 'Resolved')}}


def update_alert(ws: str, alert_id: str, actor: str | None, patch: dict) -> dict | None:
    allowed = {}
    if patch.get('status') in ('Open', 'Acknowledged', 'Resolved'):
        allowed['status'] = patch['status']
    if 'assignee' in patch:
        allowed['assignee'] = (patch['assignee'] or None)
    if 'note' in patch:
        allowed['note'] = str(patch['note'])[:2000]
    if not allowed:
        raise ValueError('Nothing to update')
    allowed['updated_at'] = time.time()
    with db.get_engine().begin() as c:
        res = c.execute(sa.update(db.alerts_t).where(db.alerts_t.c.id == alert_id, db.alerts_t.c.workspace_id == ws).values(**allowed))
        if not res.rowcount:
            return None
    db.audit(ws, actor, 'alert.update', {'id': alert_id, **{k: v for k, v in allowed.items() if k != 'updated_at'}})
    return {'ok': True}


# ── Aggregates ─────────────────────────────────────────────────────────────────

def _categorical_attr_names(schema: dict) -> list[str]:
    return [c for c in schema['categorical']][:6]


def refresh_segments(ws: str, bundle: dict) -> list[dict]:
    schema = bundle['schema']
    cats = _categorical_attr_names(schema)
    if not cats:
        kv_set(ws, 'segments', [])
        return []
    cu = db.customers_t
    recs = []
    with db.get_engine().connect() as c:
        for r in c.execute(sa.select(cu.c.prob, cu.c.expected_loss, cu.c.tier, cu.c.attrs).where(cu.c.workspace_id == ws)):
            attrs = db.loads(r.attrs, {})
            rec = {'prob': r.prob, 'expected_loss': r.expected_loss, 'tier': r.tier}
            for k in cats:
                rec[k] = attrs.get(k)
            recs.append(rec)
    out = training.build_segments(pd.DataFrame(recs), cats) if recs else []
    kv_set(ws, 'segments', out)
    return out


def summary(ws: str, settings: dict, thresholds: dict) -> dict:
    cu = db.customers_t
    econ = settings['economics']
    net = cu.c.prob * econ['save_rate'] * cu.c.revenue * econ['value_months'] - econ['outreach_cost']
    with db.get_engine().connect() as c:
        by_tier = {r.tier: r for r in c.execute(sa.select(
            cu.c.tier, sa.func.count().label('n'), sa.func.sum(cu.c.expected_loss).label('loss'),
            sa.func.sum(cu.c.revenue).label('rev'), sa.func.avg(cu.c.prob).label('avg')).where(cu.c.workspace_id == ws).group_by(cu.c.tier))}
        actionable = c.execute(sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(net), 0)).where(cu.c.workspace_id == ws, net > 0)).first()
        total = c.execute(sa.select(sa.func.count(), sa.func.avg(cu.c.prob), sa.func.sum(cu.c.revenue), sa.func.sum(cu.c.expected_loss),
                                    sa.func.sum(sa.case((cu.c.actual == 1, 1), else_=0)), sa.func.count(cu.c.actual))
                          .where(cu.c.workspace_id == ws)).first()
    n = int(total[0] or 0)
    g = lambda t, f: float(getattr(by_tier[t], f) or 0) if t in by_tier else 0.0
    return {
        'total_customers': n,
        'avg_risk': round(float(total[1] or 0) * 100, 2),
        'high_risk': int(g('High', 'n')), 'medium_risk': int(g('Medium', 'n')), 'low_risk': int(g('Low', 'n')),
        'monthly_revenue': round(float(total[2] or 0), 2),
        'expected_loss_total': round(float(total[3] or 0), 2),
        'expected_loss_high': round(g('High', 'loss'), 2),
        'high_risk_monthly_revenue': round(g('High', 'rev'), 2),
        'actionable_customers': int(actionable[0] or 0),
        'expected_net_value': round(float(actionable[1] or 0), 2),
        'observed_churn_rate': round(float(total[4] or 0) / max(int(total[5] or 0), 1) * 100, 2) if total[5] else None,
        'thresholds': thresholds, 'economics': econ,
    }


def _customer_row(r) -> dict:
    return {'id': r.customer_id, 'churn_prob': round(r.prob * 100, 1), 'risk_level': r.tier, 'revenue': r.revenue,
            'tenure': r.tenure, 'last_login': r.activity, 'expected_loss': round(r.expected_loss, 2),
            'actual': r.actual, 'top_driver': r.top_driver,
            'delta': None if r.prev_prob is None else round((r.prob - r.prev_prob) * 100, 1)}


SORTS = {'id': 'customer_id', 'prob': 'prob', 'risk': 'prob', 'revenue': 'revenue', 'tenure': 'tenure',
         'activity': 'activity', 'loss': 'expected_loss', 'actual': 'actual', 'driver': 'top_driver'}


def query_customers(ws: str, tier: str | None = None, q: str | None = None, sort: str = 'prob', direction: str = 'desc',
                    page: int = 1, limit: int = 20, ids: list[str] | None = None) -> dict:
    cu = db.customers_t
    cond = [cu.c.workspace_id == ws]
    if tier in ('High', 'Medium', 'Low'):
        cond.append(cu.c.tier == tier)
    if q:
        like = f'%{q.lower()}%'
        cond.append(sa.or_(sa.func.lower(cu.c.customer_id).like(like), sa.func.lower(sa.func.coalesce(cu.c.attrs, '')).like(like),
                           sa.func.lower(sa.func.coalesce(cu.c.top_driver, '')).like(like)))
    if ids:
        cond.append(cu.c.customer_id.in_(ids))
    col = cu.c[SORTS.get(sort, 'prob')]
    order = col.asc() if direction == 'asc' else col.desc()
    page, limit = max(1, page), max(1, min(limit, 500))
    with db.get_engine().connect() as c:
        total = c.execute(sa.select(sa.func.count()).select_from(cu).where(*cond)).scalar() or 0
        rows = c.execute(sa.select(cu).where(*cond).order_by(order.nulls_last(), cu.c.customer_id).limit(limit).offset((page - 1) * limit)).all()
    return {'customers': [_customer_row(r) for r in rows], 'total': int(total), 'page': page, 'limit': limit,
            'pages': max(1, -(-int(total) // limit))}


def customer_detail(ws: str, customer_id: str) -> dict | None:
    cu = db.customers_t
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(cu).where(cu.c.workspace_id == ws, cu.c.customer_id == customer_id)).first()
    if not r:
        return None
    attrs = db.loads(r.attrs, {})
    reasons = db.loads(r.reasons)
    if reasons is None:                       # beyond the pre-computed top-N: explain on demand
        row, bundle = registry.champion_bundle(ws)
        if bundle:
            try:
                raw = pd.DataFrame([attrs])
                X = prepare_features(raw, bundle['schema']).reindex(columns=bundle['cols'])
                for cc in bundle['cat_cols']:
                    X[cc] = X[cc].fillna('Missing')
                reasons = training.explain_rows(bundle['model'], X, bundle['baseline'], bundle['cols'])[0]
            except Exception:  # noqa: BLE001
                reasons = []
    detail = _customer_row(r)
    detail['attrs'] = attrs
    detail['reasons'] = [{**x, 'label': humanize(x['feature'])} for x in (reasons or [])]
    detail['recommendations'] = recommendations(reasons or [], r.tier)
    detail['interventions'] = list_interventions(ws, customer_id)
    return detail


def recommendations(impacts: list[dict], tier: str) -> list[str]:
    recs: list[str] = []
    for imp in sorted((i for i in impacts if i['impact'] > 0.5), key=lambda i: -i['impact']):
        name = imp['feature'].lower()
        for keys, action in ACTION_RULES:
            if any(k in name for k in keys) and action not in recs:
                recs.append(action)
                break
        if len(recs) >= 3:
            break
    if tier == 'High':
        recs.append('Schedule a proactive CSM call within 48 hours')
    elif tier == 'Medium' and not recs:
        recs.append('Send a satisfaction check-in and review account health monthly')
    if not recs:
        recs = ['Continue standard engagement', 'Re-score next cycle']
    return recs


# ── Prediction ─────────────────────────────────────────────────────────────────

def form_schema(ws: str) -> dict | None:
    row, bundle = registry.champion_bundle(ws)
    if not bundle:
        return None
    imp_rank = {d['feature']: i for i, d in enumerate(bundle['importance'])}
    fields = []
    for c in bundle['cols']:
        rng = bundle['ranges'][c]
        base = {'name': c, 'label': humanize(c).capitalize(), 'default': bundle['baseline'][c], 'rank': imp_rank.get(c, 99)}
        if 'options' in rng:
            fields.append({**base, 'kind': 'category', 'options': rng['options']})
        else:
            fields.append({**base, 'kind': 'number', 'min': rng['min'], 'max': rng['max'], 'integer': rng['integer']})
    return {'fields': fields, 'model_id': row['id'], 'thresholds': effective_thresholds(get_settings(ws), bundle),
            'revenue_field': (bundle['schema'].get('revenue_col') if bundle['schema'].get('revenue_col') in bundle['cols'] else None)}


def predict(ws: str, values: dict, explain: bool = True) -> dict:
    row, bundle = registry.champion_bundle(ws)
    if not bundle:
        raise LookupError('No model trained yet.')
    record = {}
    for c in bundle['cols']:
        v = values.get(c, bundle['baseline'][c])
        if c in bundle['num_cols']:
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = np.nan
        else:
            v = 'Missing' if v in (None, '') else str(v)
        record[c] = v
    X = pd.DataFrame([record])[bundle['cols']]
    for c in bundle['num_cols']:
        X[c] = X[c].astype(float)
    prob = float(training.clip_probs(bundle['model'].predict_proba(X)[0, 1]))
    settings = get_settings(ws)
    thresholds = effective_thresholds(settings, bundle)
    tier = economics.tier_for(prob, thresholds)
    out = {'churn_probability': round(prob * 100, 1), 'risk_level': tier, 'model_id': row['id'], 'thresholds': thresholds}
    rev_col = bundle['schema'].get('revenue_col')
    if rev_col and rev_col in record and not np.isnan(record[rev_col]):
        out['expected_loss'] = round(economics.expected_loss(prob, record[rev_col], settings['economics']), 2)
        out['expected_net_value'] = round(economics.expected_net_value(prob, record[rev_col], settings['economics']), 2)
    impacts = []
    if explain:
        impacts = training.explain_rows(bundle['model'], X, bundle['baseline'], bundle['cols'], top_n=len(bundle['cols']))[0]
        out['impacts'] = [{**i, 'label': humanize(i['feature'])} for i in impacts]
    out['recommendation'] = recommendations(impacts, tier)
    return out


# ── Interventions (closed loop) ────────────────────────────────────────────────

def add_intervention(ws: str, customer_id: str, kind: str, notes: str, actor: str | None) -> dict:
    kind = (kind or 'other')[:40]
    with db.get_engine().begin() as c:
        cust = c.execute(sa.select(db.customers_t.c.prob).where(db.customers_t.c.workspace_id == ws, db.customers_t.c.customer_id == customer_id)).first()
        if not cust:
            raise LookupError('Customer not found')
        now = time.time()
        iid = db.new_id()
        c.execute(db.interventions_t.insert().values(id=iid, workspace_id=ws, customer_id=customer_id, kind=kind, notes=(notes or '')[:2000],
                                                     actor=actor, risk_at_action=cust[0], outcome='pending', created_at=now, updated_at=now))
    db.audit(ws, actor, 'intervention.add', {'customer': customer_id, 'kind': kind})
    return {'id': iid}


def list_interventions(ws: str, customer_id: str) -> list[dict]:
    it = db.interventions_t
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(it).where(it.c.workspace_id == ws, it.c.customer_id == customer_id).order_by(it.c.created_at.desc())).all()
    return [dict(r._mapping) for r in rows]


def set_outcome(ws: str, intervention_id: str, outcome: str, actor: str | None) -> bool:
    if outcome not in ('pending', 'saved', 'churned', 'no_effect'):
        raise ValueError('Invalid outcome')
    with db.get_engine().begin() as c:
        res = c.execute(sa.update(db.interventions_t).where(db.interventions_t.c.id == intervention_id, db.interventions_t.c.workspace_id == ws)
                        .values(outcome=outcome, updated_at=time.time()))
    if res.rowcount:
        db.audit(ws, actor, 'intervention.outcome', {'id': intervention_id, 'outcome': outcome})
    return bool(res.rowcount)


def intervention_summary(ws: str) -> dict:
    it = db.interventions_t
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(it.c.kind, it.c.outcome, sa.func.count().label('n'), sa.func.avg(it.c.risk_at_action).label('risk'))
                         .where(it.c.workspace_id == ws).group_by(it.c.kind, it.c.outcome)).all()
    kinds: dict[str, dict] = {}
    for r in rows:
        k = kinds.setdefault(r.kind, {'kind': r.kind, 'total': 0, 'saved': 0, 'churned': 0, 'no_effect': 0, 'pending': 0})
        k[r.outcome] += r.n
        k['total'] += r.n
    out = []
    for k in kinds.values():
        resolved = k['saved'] + k['churned'] + k['no_effect']
        k['save_rate'] = round(k['saved'] / resolved * 100, 1) if resolved else None
        out.append(k)
    out.sort(key=lambda k: -k['total'])
    total = sum(k['total'] for k in out)
    saved = sum(k['saved'] for k in out)
    resolved = sum(k['saved'] + k['churned'] + k['no_effect'] for k in out)
    return {'by_kind': out, 'total': total, 'saved': saved, 'resolved': resolved,
            'save_rate': round(saved / resolved * 100, 1) if resolved else None}


# ── Results payload ────────────────────────────────────────────────────────────

def build_results(ws: str) -> dict | None:
    row = registry.champion(ws)
    if not row:
        return None
    _, bundle = registry.champion_bundle(ws)
    if not bundle:
        return None
    meta = row['meta']
    ev = meta['evaluation']
    settings = get_settings(ws)
    thresholds = effective_thresholds(settings, bundle)
    schema = bundle['schema']
    top = query_customers(ws, sort='loss', direction='desc', limit=50)['customers']
    return {
        'model': {
            'id': row['id'], 'status': row['status'], 'algorithm': meta['algorithm_name'], 'created_at': row['created_at'],
            'split': meta['split'], 'rows': meta['rows'], 'train_rows': meta['train_rows'], 'test_rows': meta['test_rows'],
            'comparison': meta['comparison'], 'calibration_method': meta['calibration_method'], 'seconds': meta.get('seconds'),
            'feature_count': meta['feature_count'], 'warnings': meta['warnings'], 'data_hash': meta['data_hash'], 'source': meta.get('source'),
        },
        'metrics': ev['metrics'], 'metrics_at_f1': ev['metrics_at_f1'],
        'confusion_matrix': ev['confusion_matrix'], 'confusion_matrix_f1': ev['confusion_matrix_f1'],
        'roc_curve': ev['roc_curve'], 'lift': ev['lift'], 'calibration': ev['calibration'],
        'precision_at_k': ev['precision_at_k'], 'confidence_intervals': ev['confidence_intervals'],
        'score_distribution': ev['score_distribution'],
        'feature_importance': [{**i, 'label': humanize(i['feature'])} for i in meta['importance'][:15]],
        'cohorts': meta['cohorts'], 'segments': kv_get(ws, 'segments', []),
        'summary': summary(ws, settings, thresholds), 'thresholds': thresholds, 'economics': settings['economics'],
        'schema': {k: schema[k] for k in ('target', 'id', 'time_col', 'signup_col', 'numeric', 'categorical', 'dates', 'dropped',
                                          'revenue_col', 'tenure_col', 'activity_col', 'warnings', 'rows', 'positive_rate')},
        'has_revenue': bool(settings.get('revenue_column') or schema.get('revenue_col')),
        'top_risk': top, 'drift': kv_get(ws, 'drift'),
    }


# ── Training / scoring jobs ────────────────────────────────────────────────────

def run_training(ws: str, df: pd.DataFrame, algorithm: str, actor: str | None, progress, promote: bool | None = None,
                 source: str = 'upload', target: str | None = None) -> dict:
    settings = get_settings(ws)
    result = training.train_model(df, algorithm, settings['economics'], progress, target)
    bundle, meta = result['bundle'], result['meta']
    meta['source'] = source
    progress('Saving model and scores', 97)

    scored = build_scored_frame(ws, bundle, result['df'], settings, X=result['X'], probs=result['p_oof'],
                                reasons_by_pos=result['reasons_by_pos'], revenue=result['revenue'])
    evaluation = result['evaluation']
    meta['evaluation'] = evaluation
    meta['importance'] = result['importance']
    meta['cohorts'] = result['cohorts']

    model_id = db.new_id()
    snapshot = save_snapshot(ws, model_id, scored)
    status, reason = registry.register(ws, model_id, bundle, meta, evaluation['metrics'], snapshot, promote)
    if status == 'champion':
        persist_scores(ws, model_id, scored)
        regenerate_alerts(ws, settings)
        refresh_segments(ws, bundle)
        kv_set(ws, 'drift', None)
    db.audit(ws, actor, 'model.train', {'model_id': model_id, 'status': status, 'rows': meta['rows'], 'pr_auc': evaluation['metrics']['pr_auc']})
    return {'model_id': model_id, 'status': status, 'reason': reason, 'metrics': evaluation['metrics'], 'warnings': meta['warnings']}


def promote_model(ws: str, model_id: str, actor: str | None) -> dict:
    target = registry.set_champion(ws, model_id)
    if not target:
        raise LookupError('Model not found')
    bundle = registry.load_bundle(ws, model_id)
    snapshot = target.get('data_path')
    if bundle and snapshot and os.path.exists(snapshot):
        settings = get_settings(ws)
        scored = load_snapshot(snapshot)
        thresholds = effective_thresholds(settings, bundle)
        scored['tier'] = [economics.tier_for(p, thresholds) for p in scored['prob']]
        scored['expected_loss'] = scored['prob'] * scored['revenue'] * settings['economics']['value_months']
        persist_scores(ws, model_id, scored)
        regenerate_alerts(ws, settings)
        refresh_segments(ws, bundle)
        kv_set(ws, 'drift', None)
    db.audit(ws, actor, 'model.promote', {'model_id': model_id})
    return {'model_id': model_id}


def run_batch_scoring(ws: str, df: pd.DataFrame, actor: str | None, progress) -> dict:
    row, bundle = registry.champion_bundle(ws)
    if not bundle:
        raise ValueError('Train a model first, then score new data with it.')
    settings = get_settings(ws)
    progress('Scoring new data', 20)
    scored = build_scored_frame(ws, bundle, df, settings)
    X = prepare_features(df, bundle['schema']).reindex(columns=bundle['cols'])
    for c in bundle['cat_cols']:
        X[c] = X[c].fillna('Missing').astype(str)
    drift = training.drift_report(bundle['drift_ref'], X, scored['prob'].to_numpy())
    drift['rows'] = int(len(scored))
    drift['scored_at'] = time.time()
    progress('Saving scores', 70)
    persist_scores(ws, row['id'], scored)
    regenerate_alerts(ws, settings)
    refresh_segments(ws, bundle)
    kv_set(ws, 'drift', drift)
    with db.get_engine().begin() as c:
        c.execute(db.score_batches_t.insert().values(id=db.new_id(), workspace_id=ws, model_id=row['id'], created_at=time.time(),
                                                     rows=int(len(scored)), drift=db.dumps(drift)))
    db.audit(ws, actor, 'batch.score', {'rows': int(len(scored)), 'drift': drift['status']})
    return {'rows': int(len(scored)), 'drift': drift}


def apply_settings_change(ws: str) -> None:
    """Re-tier customers and refresh alerts/segments after thresholds or economics change."""
    row, bundle = registry.champion_bundle(ws)
    if not bundle:
        return
    settings = get_settings(ws)
    thr = effective_thresholds(settings, bundle)
    econ = settings['economics']
    cu = db.customers_t
    tier = sa.case((cu.c.prob >= thr['high'], 'High'), (cu.c.prob >= thr['medium'], 'Medium'), else_='Low')
    with db.get_engine().begin() as c:
        c.execute(sa.update(cu).where(cu.c.workspace_id == ws).values(tier=tier, expected_loss=cu.c.prob * cu.c.revenue * econ['value_months']))
    regenerate_alerts(ws, settings)
    refresh_segments(ws, bundle)


def export_csv_rows(ws: str, tier: str | None, q: str | None, ids: list[str] | None, sort: str, direction: str, cap: int = 200_000):
    """Yield CSV-safe rows for export (formula injection neutralised)."""
    from .security import csv_safe
    header = ['customer_id', 'risk_level', 'churn_probability_pct', 'monthly_revenue', 'expected_loss', 'tenure', 'top_driver', 'actual_churn']
    yield [csv_safe(h) for h in header]
    page = 1
    sent = 0
    while sent < cap:
        res = query_customers(ws, tier, q, sort, direction, page=page, limit=500, ids=ids)
        if not res['customers']:
            break
        for c in res['customers']:
            yield [csv_safe(v) for v in (c['id'], c['risk_level'], c['churn_prob'], c['revenue'], c['expected_loss'], c['tenure'], c['top_driver'], c['actual'])]
            sent += 1
        if page >= res['pages']:
            break
        page += 1
