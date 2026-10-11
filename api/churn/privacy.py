"""Data-subject and workspace deletion: erase customers on request, export what is stored about one, destroy a whole tenant.

* **Erasure** removes a customer from the live tables *and* from the encrypted score snapshots of every retained model
  version (rewritten without them), so a later rollback cannot resurrect their data.
* **Export** returns everything stored about one customer (a data-subject access request).
* **Workspace deletion** wipes rows and files, then destroys the workspace's encryption key (crypto-shredding), which also
  renders any surviving copy (backups, replicas) permanently unreadable.
* The audit log is retained (it is append-only and holds actor emails and action names, never customer attributes).
"""
from __future__ import annotations

import sqlalchemy as sa

from . import crypto, db, registry, service, tenancy

MAX_ERASE_IDS = 5000


def erase_customers(ws: str, ids: list[str], actor: str | None) -> dict:
    ids = [str(i) for i in dict.fromkeys(ids)][:MAX_ERASE_IDS]
    if not ids:
        return {'erased': 0, 'snapshots_rewritten': 0}
    erased = 0
    with db.get_engine().begin() as c:
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            erased += c.execute(sa.delete(db.customers_t).where(db.customers_t.c.workspace_id == ws, db.customers_t.c.customer_id.in_(chunk))).rowcount
            c.execute(sa.delete(db.details_t).where(db.details_t.c.workspace_id == ws, db.details_t.c.customer_id.in_(chunk)))
            c.execute(sa.delete(db.alerts_t).where(db.alerts_t.c.workspace_id == ws, db.alerts_t.c.customer_id.in_(chunk)))
            c.execute(sa.delete(db.interventions_t).where(db.interventions_t.c.workspace_id == ws, db.interventions_t.c.customer_id.in_(chunk)))
    rewritten = 0
    for m in registry.list_models(ws):
        row = registry.get_model(ws, m['id'])
        path = row and row.get('data_path')
        if not path:
            continue
        try:
            scored = service.load_snapshot(ws, m['id'], path)
        except FileNotFoundError:
            continue
        kept = scored[~scored['customer_id'].astype(str).isin(set(ids))]
        if len(kept) != len(scored):
            service.save_snapshot(ws, m['id'], kept)
            rewritten += 1
    service.refresh_counts(ws)
    service.bump(ws)
    db.audit(ws, actor, 'privacy.erase', {'customers': len(ids), 'removed_rows': erased})        # ids are not logged
    return {'erased': erased, 'snapshots_rewritten': rewritten}


def export_customer(ws: str, customer_id: str, actor: str | None) -> dict | None:
    detail = service.customer_detail(ws, customer_id)
    if detail is None:
        return None
    al = db.alerts_t
    with db.get_engine().connect() as c:
        alerts = [dict(r._mapping) for r in c.execute(sa.select(al).where(al.c.workspace_id == ws, al.c.customer_id == customer_id))]
    for a in alerts:
        a['signals'] = db.loads(a.get('signals'), [])
    db.audit(ws, actor, 'privacy.export', {'customer': 'redacted'})
    return {'customer_id': customer_id, 'score': {k: detail[k] for k in ('churn_prob', 'risk_level', 'revenue', 'tenure', 'expected_loss', 'actual')},
            'attributes': detail['attrs'], 'drivers': detail['reasons'], 'alerts': alerts, 'interventions': detail['interventions']}


def delete_workspace(ws: str, actor: str | None) -> dict:
    """Irreversible. Rows and files are removed, the key is destroyed, and the workspace is marked deleted."""
    db.audit(ws, actor, 'workspace.delete', {})                    # recorded first; the chain itself is kept
    registry.wipe(ws)
    shredded = crypto.shred(ws)
    tenancy.delete_workspace_record(ws)
    return {'deleted': True, 'key_destroyed': shredded}
