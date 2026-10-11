"""Readiness checks, Prometheus text and optional Sentry. Nothing here may ever take a request down."""
from __future__ import annotations

import logging
import os
import re
import time

import sqlalchemy as sa

from . import config, crypto, db, jobs, storage

log = logging.getLogger('churnsight.obs')
_RID = re.compile(r'[^A-Za-z0-9_.-]')


def clean_request_id(raw: str | None, fresh: str) -> str:
    """Accept a caller's X-Request-ID for tracing across services, but only as a short, log-safe token."""
    cleaned = _RID.sub('', raw or '')[:64]
    return cleaned or fresh


def ready() -> tuple[bool, dict]:
    """Can this instance serve traffic? (Liveness is /api/health; this one is for load balancers and rollouts.)"""
    checks: dict[str, str] = {}

    def check(name, fn):
        try:
            fn()
            checks[name] = 'ok'
        except Exception as exc:  # noqa: BLE001
            checks[name] = f'failed: {type(exc).__name__}'
            log.warning('readiness check %s failed: %s', name, exc)

    def database():
        with db.get_engine().connect() as c:
            c.exec_driver_sql('SELECT 1')

    def schema():
        from alembic.script import ScriptDirectory
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option('script_location', db._MIGRATIONS_DIR)
        head = ScriptDirectory.from_config(cfg).get_current_head()
        with db.get_engine().connect() as c:
            current = c.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()
        if current != head:
            raise RuntimeError(f'schema {current} != {head}')

    def blobs():
        key = f'_ready/{os.getpid()}'
        store = storage.get_storage()
        store.put(key, b'ok')
        store.delete(key)

    def keys():
        crypto.provider()

    for name, fn in (('database', database), ('schema', schema), ('storage', blobs), ('keys', keys)):
        check(name, fn)
    return all(v == 'ok' for v in checks.values()), checks


def metrics_text(counters, latency) -> str:
    lines = ['# TYPE churnsight_requests_total counter']
    for (route, method, status), n in sorted(counters.items()):
        lines.append(f'churnsight_requests_total{{route="{route}",method="{method}",status="{status}"}} {n}')
    lines.append('# TYPE churnsight_request_ms_sum counter')
    for route, ms in sorted(latency.items()):
        lines.append(f'churnsight_request_ms_sum{{route="{route}"}} {ms:.1f}')
    try:
        depth = jobs.queue_depth()
        lines.append('# TYPE churnsight_jobs gauge')
        for status in ('queued', 'running', 'done', 'error', 'cancelled'):
            lines.append(f'churnsight_jobs{{status="{status}"}} {depth.get(status, 0)}')
        j = db.jobs_t
        with db.system_context(), db.get_engine().connect() as c:
            oldest = c.execute(sa.select(sa.func.min(j.c.created_at)).where(j.c.status == 'queued')).scalar()
            recent = c.execute(sa.select(j.c.kind, j.c.status, sa.func.count(), sa.func.sum(j.c.finished_at - j.c.started_at)).where(
                j.c.finished_at > time.time() - 3600, j.c.started_at.is_not(None)).group_by(j.c.kind, j.c.status)).all()
        lines.append('# TYPE churnsight_queue_oldest_age_seconds gauge')
        lines.append(f'churnsight_queue_oldest_age_seconds {max(0.0, time.time() - oldest) if oldest else 0:.1f}')
        lines.append('# HELP churnsight_jobs_finished_1h Jobs finished in the last hour; _seconds is their total run time.')
        for kind, status, n, secs in recent:
            lines.append(f'churnsight_jobs_finished_1h{{kind="{kind}",status="{status}"}} {n}')
            lines.append(f'churnsight_jobs_finished_seconds_1h{{kind="{kind}",status="{status}"}} {float(secs or 0):.1f}')
    except Exception as exc:  # noqa: BLE001
        log.warning('job metrics unavailable: %s', exc)
    return '\n'.join(lines) + '\n'


def init_sentry() -> bool:
    """Error reporting is opt-in (SENTRY_DSN) and never sends request bodies, cookies or user data: customer data is sensitive."""
    dsn = os.environ.get('SENTRY_DSN')
    if not dsn:
        return False
    try:
        import sentry_sdk  # noqa: PLC0415 (optional dependency)
    except ImportError:
        log.warning('SENTRY_DSN is set but sentry-sdk is not installed')
        return False

    def scrub(event, _hint):
        req = event.get('request') or {}
        for k in ('data', 'cookies', 'headers', 'query_string'):
            req.pop(k, None)
        event.pop('user', None)
        return event

    sentry_sdk.init(dsn=dsn, send_default_pii=False, before_send=scrub, traces_sample_rate=float(os.environ.get('SENTRY_TRACES', '0')),
                    environment=os.environ.get('APP_ENV', 'production' if config.PRODUCTION else 'development'))
    return True
