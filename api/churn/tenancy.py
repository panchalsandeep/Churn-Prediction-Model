"""Workspaces, memberships, invitations and roles.

Roles (each includes everything below it):
    viewer   read dashboards, customers, alerts; run single predictions
    analyst  + export, work alerts, log interventions
    admin    + train / score / promote models, settings, invite and manage members (except owners/admins)
    owner    + manage admins and owners, SSO enforcement, delete the workspace

Cross-tenant lookups (which workspaces does this login belong to? who owns this invite token?) run in the database's
system context; everything else is scoped to one workspace.
"""
from __future__ import annotations

import hashlib
import secrets
import time

import sqlalchemy as sa

from . import db

ROLES = ['viewer', 'analyst', 'admin', 'owner']
RANK = {r: i for i, r in enumerate(ROLES)}
INVITE_TTL_DAYS = 7


class TenancyError(Exception):
    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


def has_role(role: str, minimum: str) -> bool:
    return RANK.get(role, -1) >= RANK[minimum]


# ── Workspaces ──────────────────────────────────────────────────────────────────────────────────

def create_workspace(name: str, owner_uid: str | None = None, owner_email: str | None = None, ws_id: str | None = None,
                     plan: str = 'trial') -> str:
    ws_id = ws_id or f'w_{secrets.token_hex(6)}'
    now = time.time()
    with db.system_context(), db.get_engine().begin() as c:
        if c.execute(sa.select(db.workspaces_t.c.id).where(db.workspaces_t.c.id == ws_id)).first():
            return ws_id
        c.execute(db.workspaces_t.insert().values(id=ws_id, name=name[:160], plan=plan, status='active', created_at=now))
        if owner_uid:
            c.execute(db.memberships_t.insert().values(workspace_id=ws_id, user_uid=owner_uid, email=(owner_email or '').lower() or None,
                                                       role='owner', status='active', created_at=now))
    db.audit(ws_id, owner_email or owner_uid, 'workspace.create', {'name': name})
    return ws_id


def personal_workspace(uid: str, email: str | None) -> str:
    """The workspace a first-time user lands in (development, or allow-listed administrators)."""
    return create_workspace(f'{(email or uid).split("@")[0]}\'s workspace', uid, email, ws_id=f'u_{uid}')


def get_workspace(ws: str) -> dict | None:
    with db.system_context(), db.get_engine().connect() as c:
        r = c.execute(sa.select(db.workspaces_t).where(db.workspaces_t.c.id == ws)).first()
    return dict(r._mapping) if r else None


def memberships_for(uid: str) -> list[dict]:
    m, w = db.memberships_t, db.workspaces_t
    with db.system_context(), db.get_engine().connect() as c:
        rows = c.execute(sa.select(m.c.workspace_id, m.c.role, m.c.status, w.c.name, w.c.plan, w.c.sso_required, w.c.status.label('ws_status'))
                         .join(w, w.c.id == m.c.workspace_id).where(m.c.user_uid == uid, m.c.status == 'active', w.c.status == 'active')
                         .order_by(m.c.created_at)).all()
    return [dict(r._mapping) for r in rows]


def list_members(ws: str) -> list[dict]:
    m = db.memberships_t
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(m).where(m.c.workspace_id == ws).order_by(m.c.created_at)).all()
    return [{'uid': r.user_uid, 'email': r.email, 'role': r.role, 'status': r.status, 'since': r.created_at} for r in rows]


def seat_count(ws: str) -> int:
    m = db.memberships_t
    with db.get_engine().connect() as c:
        return int(c.execute(sa.select(sa.func.count()).where(m.c.workspace_id == ws, m.c.status == 'active')).scalar() or 0)


def _owners(c, ws: str) -> int:
    m = db.memberships_t
    return int(c.execute(sa.select(sa.func.count()).where(m.c.workspace_id == ws, m.c.role == 'owner', m.c.status == 'active')).scalar() or 0)


def _may_manage(actor_role: str, target_role: str, new_role: str | None = None) -> bool:
    """Owners manage everyone; admins manage analysts and viewers only (and cannot create admins or owners)."""
    if actor_role == 'owner':
        return True
    if actor_role == 'admin':
        return target_role in ('viewer', 'analyst') and (new_role is None or new_role in ('viewer', 'analyst'))
    return False


def set_role(ws: str, actor_uid: str, actor_role: str, actor_email: str | None, target_uid: str, new_role: str) -> None:
    if new_role not in ROLES:
        raise TenancyError('Unknown role.')
    m = db.memberships_t
    with db.get_engine().begin() as c:
        row = c.execute(sa.select(m).where(m.c.workspace_id == ws, m.c.user_uid == target_uid)).first()
        if not row:
            raise TenancyError('Member not found.', 404)
        if not _may_manage(actor_role, row.role, new_role):
            raise TenancyError('You are not allowed to make this change.', 403, 'forbidden')
        if row.role == 'owner' and new_role != 'owner' and _owners(c, ws) <= 1:
            raise TenancyError('A workspace needs at least one owner.', 409)
        c.execute(sa.update(m).where(m.c.workspace_id == ws, m.c.user_uid == target_uid).values(role=new_role))
    db.audit(ws, actor_email or actor_uid, 'member.role', {'uid': target_uid, 'from': row.role, 'to': new_role})


def remove_member(ws: str, actor_uid: str, actor_role: str, actor_email: str | None, target_uid: str) -> None:
    m = db.memberships_t
    with db.get_engine().begin() as c:
        row = c.execute(sa.select(m).where(m.c.workspace_id == ws, m.c.user_uid == target_uid)).first()
        if not row:
            raise TenancyError('Member not found.', 404)
        if target_uid != actor_uid and not _may_manage(actor_role, row.role):
            raise TenancyError('You are not allowed to remove this member.', 403, 'forbidden')
        if row.role == 'owner' and _owners(c, ws) <= 1:
            raise TenancyError('A workspace needs at least one owner.', 409)
        c.execute(sa.delete(m).where(m.c.workspace_id == ws, m.c.user_uid == target_uid))
    db.audit(ws, actor_email or actor_uid, 'member.remove', {'uid': target_uid, 'email': row.email, 'role': row.role})


# ── Invitations ─────────────────────────────────────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_invite(ws: str, email: str, role: str, creator_role: str, creator: str | None) -> dict:
    email = (email or '').strip().lower()
    if '@' not in email or len(email) > 254:
        raise TenancyError('Enter a valid email address.')
    if role not in ROLES:
        raise TenancyError('Unknown role.')
    if not _may_manage(creator_role, 'viewer', role):
        raise TenancyError('You cannot invite someone to this role.', 403, 'forbidden')
    from . import quotas
    quotas.check_seat(ws)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.get_engine().begin() as c:
        c.execute(db.invites_t.insert().values(token_hash=_hash(token), workspace_id=ws, email=email, role=role, created_by=creator,
                                               created_at=now, expires_at=now + INVITE_TTL_DAYS * 86400))
    db.audit(ws, creator, 'invite.create', {'email': email, 'role': role})
    return {'token': token, 'email': email, 'role': role, 'expires_at': now + INVITE_TTL_DAYS * 86400}


def list_invites(ws: str) -> list[dict]:
    inv = db.invites_t
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(inv).where(inv.c.workspace_id == ws, inv.c.accepted_at.is_(None), inv.c.expires_at > time.time())
                         .order_by(inv.c.created_at)).all()
    return [{'id': r.token_hash[:16], 'email': r.email, 'role': r.role, 'created_by': r.created_by, 'expires_at': r.expires_at} for r in rows]


def revoke_invite(ws: str, invite_id: str, creator_role: str, actor: str | None) -> bool:
    inv = db.invites_t
    with db.get_engine().begin() as c:
        row = c.execute(sa.select(inv).where(inv.c.workspace_id == ws, inv.c.accepted_at.is_(None), inv.c.token_hash.like(invite_id + '%'))).first()             if len(invite_id) >= 16 else None
        if not row:
            return False
        if not _may_manage(creator_role, 'viewer', row.role):
            raise TenancyError('You cannot revoke an invitation for this role.', 403, 'forbidden')
        c.execute(sa.delete(inv).where(inv.c.token_hash == row.token_hash))
    db.audit(ws, actor, 'invite.revoke', {'email': row.email})
    return True


def accept_invite(token: str, uid: str, email: str | None) -> dict:
    """Consume an invite. It only works for the invited email address, once, before it expires."""
    inv = db.invites_t
    now = time.time()
    with db.system_context(), db.get_engine().begin() as c:
        row = c.execute(sa.select(inv).where(inv.c.token_hash == _hash(token or ''))).first()
        if not row or row.accepted_at is not None or row.expires_at < now:
            raise TenancyError('This invitation is invalid, expired, or already used.', 400, 'invalid_invite')
        if (email or '').lower() != row.email:
            raise TenancyError(f'This invitation was sent to a different email address.', 403, 'wrong_email')
        existing = c.execute(sa.select(db.memberships_t).where(db.memberships_t.c.workspace_id == row.workspace_id,
                                                               db.memberships_t.c.user_uid == uid)).first()
        if existing:
            if RANK[row.role] > RANK[existing.role]:
                c.execute(sa.update(db.memberships_t).where(db.memberships_t.c.workspace_id == row.workspace_id,
                                                            db.memberships_t.c.user_uid == uid).values(role=row.role, status='active'))
        else:
            c.execute(db.memberships_t.insert().values(workspace_id=row.workspace_id, user_uid=uid, email=row.email, role=row.role,
                                                       status='active', created_at=now))
        c.execute(sa.update(inv).where(inv.c.token_hash == row.token_hash).values(accepted_at=now))
    db.audit(row.workspace_id, email, 'invite.accept', {'role': row.role})
    return {'workspace_id': row.workspace_id, 'role': row.role}


def set_plan(ws: str, plan: str) -> None:
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.workspaces_t).where(db.workspaces_t.c.id == ws).values(plan=plan))


def set_sso_required(ws: str, required: bool, actor: str | None) -> None:
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.workspaces_t).where(db.workspaces_t.c.id == ws).values(sso_required=bool(required)))
    db.audit(ws, actor, 'workspace.sso', {'required': bool(required)})


def delete_workspace_record(ws: str) -> None:
    """Mark the workspace deleted and remove memberships/invites. Customer data is erased separately (privacy.py)."""
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.workspaces_t).where(db.workspaces_t.c.id == ws).values(status='deleted'))
        c.execute(sa.delete(db.memberships_t).where(db.memberships_t.c.workspace_id == ws))
        c.execute(sa.delete(db.invites_t).where(db.invites_t.c.workspace_id == ws))
