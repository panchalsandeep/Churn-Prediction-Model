"""Shadow scoring, retraining policy, guarded promotion and rollback (see mlops.py for the monitoring half)."""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd
import sqlalchemy as sa

from . import db, mlops, registry

log = logging.getLogger('churnsight.shadow')

DEFAULT_POLICY = {'enabled': False, 'every_days': 30, 'on_drift': True, 'on_degradation': True, 'min_new_positives': 0, 'auto_promote': False,
                  'shadow_min_runs': 2, 'margin_pr_auc': 1.0, 'max_brier_worsening': 0.05, 'reject_after_runs': 4, 'check_every_hours': 6.0}


def _kv_get(ws, key, default=None):
    from . import service
    return service.kv_get(ws, key, default)


def _kv_set(ws, key, value):
    from . import service
    service.kv_set(ws, key, value)


# ── shadow scoring ──────────────────────────────────────────────────────────────────────────────────

def challenger(ws: str) -> dict | None:
    """The one unpromoted model created after the current champion (newest wins). Only it is scored in shadow."""
    champ = registry.champion(ws)
    if not champ:
        return None
    cands = [m for m in registry.list_models(ws) if m['status'] == 'challenger' and m['created_at'] > champ['created_at']]
    return cands[0] if cands else None


def score(ws: str, df: pd.DataFrame, settings: dict, run_meta: dict) -> str | None:
    """Score the challenger on the same rows as a live run and log it as a shadow run. Never raises: shadowing must not hurt serving."""
    from . import service
    try:
        ch = challenger(ws)
        if not ch:
            return None
        bundle = registry.load_bundle(ws, ch['id'])
        if not bundle:
            return None
        scored = service.build_scored_frame(ws, bundle, service.apply_id_policy(ws, df, settings), settings)
        return mlops.log_run(ws, ch['id'], 'shadow', scored['customer_id'], scored['prob'], **run_meta)
    except Exception:  # noqa: BLE001
        log.warning('shadow scoring failed', exc_info=True)
        return None


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────

def get_policy(ws: str) -> dict:
    return {**DEFAULT_POLICY, **(_kv_get(ws, 'mlops_policy', {}) or {})}


def set_policy(ws: str, body: dict) -> dict:
    pol = get_policy(ws)
    for k, default in DEFAULT_POLICY.items():
        if k in body:
            if isinstance(default, bool):
                pol[k] = bool(body[k])
            else:
                try:
                    pol[k] = type(default)(body[k])
                except (TypeError, ValueError):
                    raise ValueError(f'{k} must be a number.') from None
    if not (1 <= pol['shadow_min_runs'] <= 12 and 0 <= pol['every_days'] <= 365 and 1 <= pol['check_every_hours'] <= 168 and pol['margin_pr_auc'] >= 0):
        raise ValueError('shadow_min_runs must be 1-12, every_days 0-365, check_every_hours 1-168 and margin_pr_auc at least 0.')
    _kv_set(ws, 'mlops_policy', pol)
    s, now = db.schedules_t, time.time()
    with db.get_engine().begin() as c:
        c.execute(sa.delete(s).where(s.c.workspace_id == ws, s.c.kind == 'mlops_check'))
        if pol['enabled']:
            c.execute(s.insert().values(workspace_id=ws, kind='mlops_check', every_hours=pol['check_every_hours'],
                                        next_run_at=now + pol['check_every_hours'] * 3600, enabled=True, updated_at=now))
    return pol


# ── evaluation ──────────────────────────────────────────────────────────────────────────────────────

def _pool(ws: str, challenger_id: str) -> dict | None:
    """Champion and challenger on the same resolved runs (same as_of, same accounts): the only fair comparison."""
    shadow = {r['as_of']: r for r in mlops.list_runs(ws, mlops.RUNS_KEPT, kind='shadow', status='resolved') if r['model_id'] == challenger_id and r['as_of']}
    live = {r['as_of']: r for r in mlops.list_runs(ws, mlops.RUNS_KEPT, kind='live', status='resolved') if r['as_of']}
    ys, pc, ps = [], [], []
    for as_of in sorted(set(shadow) & set(live)):
        out = mlops.outcomes_for(ws, live[as_of])
        if out is None:
            continue
        j = mlops.load_log(ws, live[as_of]).merge(mlops.load_log(ws, shadow[as_of]), on='customer_id', suffixes=('_c', '_s'))
        j = j[j['customer_id'].isin(out.index)]
        ys.append(out.loc[j['customer_id']].to_numpy())
        pc.append(j['prob_c'].to_numpy())
        ps.append(j['prob_s'].to_numpy())
    if not ys:
        return None
    y, c, s = np.concatenate(ys), np.concatenate(pc), np.concatenate(ps)
    return {'runs': len(ys), 'n': int(len(y)), 'positives': int(y.sum()), 'champion': mlops.realized_metrics(y, c), 'challenger': mlops.realized_metrics(y, s)}


def evaluate(ws: str, actor: str = 'mlops') -> dict:
    """Decide whether the shadow challenger should replace the champion. Returns the decision record (also stored and audited)."""
    from . import notify
    pol = get_policy(ws)
    ch = challenger(ws)
    if not ch:
        return {'decision': 'none', 'reasons': ['No challenger is being shadowed.']}
    pooled = _pool(ws, ch['id'])
    rec = {'challenger': ch['id'], 'evaluated_at': time.time(), 'min_runs': pol['shadow_min_runs']}
    if not pooled or pooled['runs'] < pol['shadow_min_runs'] or pooled['positives'] < mlops.MIN_POSITIVES:
        have = pooled['runs'] if pooled else 0
        rec.update(decision='pending', matched_runs=have,
                   reasons=[f"Needs {pol['shadow_min_runs']} resolved runs with {mlops.MIN_POSITIVES}+ churn events scored by both models; has {have}."])
        _kv_set(ws, 'mlops_shadow', rec)
        return rec
    a, b = pooled['champion'], pooled['challenger']
    rec.update(matched_runs=pooled['runs'], accounts=pooled['n'], positives=pooled['positives'], champion=a, challenger_metrics=b)
    reasons, ok = [], True
    gain = (b['pr_auc'] or 0) - (a['pr_auc'] or 0)
    if gain < pol['margin_pr_auc']:
        ok = False
        reasons.append(f"PR-AUC gain {gain:+.2f} points is below the required {pol['margin_pr_auc']:.2f}.")
    if (b['brier'] or 1) > (a['brier'] or 1) * (1 + pol['max_brier_worsening']):
        ok = False
        reasons.append(f"Brier score is worse ({b['brier']:.4f} vs {a['brier']:.4f}): the challenger's probabilities are less honest.")
    if (b['roc_auc'] or 0) < (a['roc_auc'] or 0) - 0.5:
        ok = False
        reasons.append(f"ROC-AUC is lower ({b['roc_auc']:.1f} vs {a['roc_auc']:.1f}).")
    if ok:
        rec.update(decision='promote', reasons=[f"Better on the same {pooled['n']:,} outcomes: PR-AUC {a['pr_auc']:.1f} to {b['pr_auc']:.1f}, Brier {a['brier']:.4f} to {b['brier']:.4f}."])
    elif pooled['runs'] >= pol['reject_after_runs']:
        rec.update(decision='reject', reasons=reasons)
    else:
        rec.update(decision='keep_waiting', reasons=reasons)
    db.audit(ws, actor, 'mlops.shadow', {'challenger': ch['id'], 'decision': rec['decision'], 'runs': pooled['runs']})
    if rec['decision'] == 'promote':
        if pol['auto_promote']:
            promote(ws, ch['id'], actor)
            rec['promoted'] = True
        else:
            notify.broadcast(ws, 'A better model is ready', rec['reasons'][0] + ' Review it under Admin, Model operations, and promote it.')
    elif rec['decision'] == 'reject':
        registry.set_status(ws, ch['id'], 'archived')
        rec['archived'] = True
    _kv_set(ws, 'mlops_shadow', rec)
    return rec


def promote(ws: str, model_id: str, actor: str) -> dict:
    from . import pipeline, service
    prev = registry.champion(ws)
    service.promote_model(ws, model_id, actor)                         # also remembers the previous champion for rollback
    if pipeline.get(ws).get('target'):                                  # put today's scores from the new champion in front of users
        pipeline.run_score(ws, actor, lambda *_: None, record=True)
    db.audit(ws, actor, 'mlops.promote', {'model_id': model_id, 'previous': prev['id'] if prev else None})
    return {'model_id': model_id, 'previous': prev['id'] if prev else None}


def rollback(ws: str, actor: str) -> dict:
    """Put back the model that was champion before the last promotion."""
    from . import pipeline, service
    prev = (_kv_get(ws, 'mlops_previous', None) or {}).get('model_id')
    if not prev or not registry.get_model(ws, prev):
        raise LookupError('There is no earlier champion to roll back to.')
    current = registry.champion(ws)
    service.promote_model(ws, prev, actor)
    if pipeline.get(ws).get('target'):
        pipeline.run_score(ws, actor, lambda *_: None, record=True)
    db.audit(ws, actor, 'mlops.rollback', {'to': prev, 'from': current['id'] if current else None})
    return {'model_id': prev}
