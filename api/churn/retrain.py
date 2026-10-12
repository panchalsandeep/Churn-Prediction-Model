"""The periodic MLOps check: resolve outcomes, refresh model health, judge any shadow challenger, and train a new challenger when warranted.

Retraining happens only for workspaces that feed the platform through tables (the pipeline), because only there are fresh, labelled
history and a point-in-time feature builder available. A retrained model never goes live by itself: it enters shadow, is scored next to
the champion on the same accounts, and is promoted only by the rules in shadow.py (automatically if the workspace opted in).
"""
from __future__ import annotations

import logging
import time

from . import features, ingest, mlops, pipeline, registry, service, shadow

log = logging.getLogger('churnsight.retrain')


def triggers(ws: str, pol: dict, cfg: dict, health: dict) -> list[str]:
    out = []
    champ = registry.champion(ws)
    if champ and pol['every_days'] > 0 and time.time() - champ['created_at'] > pol['every_days'] * 86400:
        out.append(f"The model is older than {pol['every_days']} days.")
    if pol['on_drift'] and (service.kv_get(ws, 'drift', None) or {}).get('status') == 'major_shift':
        out.append('Input data has shifted a lot compared with training (PSI above 0.25).')
    if pol['on_degradation'] and health.get('status') == 'degraded':
        out.append('Realized performance has degraded: ' + ' '.join(health.get('reasons', [])))
    if pol['min_new_positives'] > 0 and cfg.get('positives') is not None and cfg.get('target'):
        try:
            now = features.preview_target(ingest.load_all(ws), cfg['target'])['positives']
            if now - cfg['positives'] >= pol['min_new_positives']:
                out.append(f"{now - cfg['positives']} new churn events have been observed since the model was trained.")
        except (features.TargetError, ValueError):
            pass
    return out


def run_check(ws: str, actor: str | None, progress, force: bool = False) -> dict:
    cfg = pipeline.get(ws)
    out: dict = {'resolved': [], 'health': None, 'shadow': None, 'retrained': False, 'triggers': []}
    progress('Resolving outcomes', 10)
    if cfg['target']:
        out['resolved'] = mlops.resolve_due_runs(ws, cfg['target'])
    out['health'] = mlops.update_health(ws)
    progress('Judging the challenger', 30)
    if shadow.challenger(ws):
        out['shadow'] = shadow.evaluate(ws)
    pol = shadow.get_policy(ws)
    if not cfg['target'] or not registry.champion(ws):
        out['note'] = 'Automatic retraining needs a pipeline workspace with a trained model.'
        return out
    if shadow.challenger(ws):
        out['note'] = 'A challenger is already being shadowed; not training another.'
        return out
    why = triggers(ws, pol, cfg, out['health'])
    out['triggers'] = why
    if not (why or force):
        return out
    progress('Training a challenger', 40)
    try:
        res = pipeline.run_train(ws, actor, 'auto', progress, challenger_only=True)
    except ValueError as exc:
        out['note'] = f'Retraining skipped: {exc}'
        return out
    out['retrained'] = True
    out['challenger'] = res['model_id']
    out['challenger_status'] = res['status']
    from . import db
    db.audit(ws, actor, 'mlops.retrain', {'model_id': res['model_id'], 'triggers': why or ['manual']})
    return out
