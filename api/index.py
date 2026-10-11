"""ChurnSight API.

Thin HTTP layer over api/churn/*. Each request is scoped to a workspace (see churn.config.WORKSPACE_MODE),
so accounts never see each other's models, customers or alerts.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
import sys
import time
import uuid
from collections import Counter
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
from flask import Flask, Response, g, jsonify, request, send_from_directory, stream_with_context  # noqa: E402
from flask_cors import CORS  # noqa: E402

import firebase_admin  # noqa: E402
from firebase_admin import auth  # noqa: E402

from churn import config, db, handlers, jobs, observability, privacy, registry, service, tenancy  # noqa: E402,F401
from churn.tenancy import TenancyError  # noqa: E402
from churn.artifacts import ArtifactError  # noqa: E402
from churn.crypto import CryptoConfigError, KeyShredded  # noqa: E402
from churn.jobs import JobRejected  # noqa: E402
from churn import apikeys, connectors, features, ingest, mlops, notify, pipeline, quotas, shadow  # noqa: E402
from churn.quotas import QuotaExceeded  # noqa: E402
from churn.demo_data import generate_demo  # noqa: E402
from churn.schema import infer_schema  # noqa: E402
from churn.security import SECURITY_HEADERS, limiter  # noqa: E402
from churn.training import ALGORITHMS  # noqa: E402

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), format='%(message)s')
log = logging.getLogger('churnsight.api')

BASE_DIR = config.BASE_DIR
DIST_DIR = os.path.join(BASE_DIR, 'dist')

app = Flask(__name__, static_folder=None)
observability.init_sentry()
app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_MB * 1024 * 1024


def is_production() -> bool:
    return (config.PRODUCTION or os.environ.get('FLASK_ENV') == 'production'
            or os.environ.get('AUTH_FAIL_CLOSED', '').lower() in ('1', 'true', 'yes'))


_origins = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]
CORS(app, origins=_origins or ([] if is_production() else '*'))

# ── Firebase ──────────────────────────────────────────────────────────────────
firebase_initialized = False
try:
    firebase_admin.initialize_app()
    firebase_initialized = True
    print('[INFO] Firebase Admin SDK successfully initialized.')
except Exception as e:  # noqa: BLE001
    print(f'[WARNING] Firebase Admin SDK initialization failed: {e}')

ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get('ADMIN_EMAILS', '').split(',') if e.strip()]


def auth_configured() -> bool:
    return bool(
        firebase_initialized
        and (os.environ.get('FIREBASE_PROJECT_ID') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or os.environ.get('FIREBASE_CONFIG'))
    )


def is_admin_token(decoded_token) -> bool:
    if not decoded_token:
        return False
    return (decoded_token.get('email') or '').lower() in ADMIN_EMAILS or decoded_token.get('admin') is True


AUTO_PROVISION = os.environ.get('AUTO_PROVISION', '0' if config.PRODUCTION else '1').lower() in ('1', 'true', 'yes')


def _mfa_roles() -> set[str]:
    default = 'admin,owner' if (config.PRODUCTION or is_production()) else ''
    return {r.strip() for r in os.environ.get('REQUIRE_MFA_ROLES', default).split(',') if r.strip()}


def resolve_context(user) -> dict:
    """Who is calling, in which workspace, with which role. Raises TenancyError (with a code the UI understands) if not allowed."""
    if not user:                                   # local development without Firebase
        return {'ws': 'local', 'role': 'owner', 'uid': 'local-dev', 'email': 'local-dev', 'sso_required': False}
    uid = str(user.get('uid') or user.get('user_id') or '')
    email = (user.get('email') or '').lower()
    memberships = tenancy.memberships_for(uid)
    requested = request.headers.get('X-Workspace')
    if requested:
        chosen = next((m for m in memberships if m['workspace_id'] == requested), None)
        if not chosen:
            raise TenancyError('You do not belong to that workspace.', 403, 'forbidden_workspace')
    elif memberships:
        chosen = memberships[0]
    elif AUTO_PROVISION or is_admin_token(user):
        tenancy.personal_workspace(uid, email)
        chosen = tenancy.memberships_for(uid)[0]
    else:
        raise TenancyError('This account has no workspace yet. Ask an administrator to invite you.', 403, 'no_workspace')

    firebase_claims = user.get('firebase') or {}
    provider = firebase_claims.get('sign_in_provider', '') or ''
    if chosen['sso_required'] and not provider.startswith(('saml.', 'oidc.')):
        raise TenancyError('This workspace requires single sign-on.', 403, 'sso_required')
    if chosen['role'] in _mfa_roles() and not firebase_claims.get('sign_in_second_factor'):
        raise TenancyError('Multi-factor authentication is required for your role.', 403, 'mfa_required')
    return {'ws': chosen['workspace_id'], 'role': chosen['role'], 'uid': uid, 'email': email, 'sso_required': bool(chosen['sso_required'])}


def _client_key() -> str:
    user = getattr(request, 'user', None)
    return (user or {}).get('uid') or request.headers.get('X-Forwarded-For', request.remote_addr or 'anon').split(',')[0].strip()


def require_firebase_auth(f=None, *, heavy: bool = False, role: str = 'viewer', no_workspace: bool = False):
    """Authenticate, resolve the workspace and role, enforce the minimum role, and rate-limit.

    `heavy` is for training/scoring endpoints. `no_workspace` is for calls a user makes before they belong to one (accepting an invite).
    """
    def wrap(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if not auth_configured():
                if is_production() or os.environ.get('AUTH_REQUIRED', '').lower() in ('1', 'true', 'yes'):
                    return jsonify({'error': 'Authentication service unavailable. Configure Firebase Admin credentials.'}), 503
                request.user = None
            else:
                header = request.headers.get('Authorization')
                if not header or not header.startswith('Bearer '):
                    return jsonify({'error': 'Unauthorized: Missing or invalid token format'}), 401
                try:
                    request.user = auth.verify_id_token(header.split('Bearer ', 1)[1])
                except Exception as exc:  # noqa: BLE001
                    return jsonify({'error': f'Unauthorized: Token verification failed: {exc}'}), 401

            key = _client_key()
            ok, retry = limiter.allow(f'api:{key}', config.RATE_LIMIT_PER_MIN)
            if ok and heavy:
                ok, retry = limiter.allow(f'heavy:{key}', config.HEAVY_RATE_LIMIT_PER_MIN)
            if not ok:
                resp = jsonify({'error': 'Too many requests. Please slow down.'})
                resp.status_code = 429
                resp.headers['Retry-After'] = str(retry)
                return resp

            user = request.user or {}
            if no_workspace:
                g.ws, g.role, g.uid = None, None, str(user.get('uid') or 'local-dev')
                g.email, g.actor = (user.get('email') or '').lower(), user.get('email') or 'local-dev'
            else:
                with db.system_context():
                    ctx = resolve_context(request.user)
                if not tenancy.has_role(ctx['role'], role):
                    resp = jsonify({'error': f'This action needs the {role} role (you are {ctx["role"]}).', 'code': 'forbidden_role'})
                    resp.status_code = 403
                    return resp
                g.ws, g.role, g.uid, g.email = ctx['ws'], ctx['role'], ctx['uid'], ctx['email']
                g.actor = user.get('email') or 'local-dev'
                quotas.record(g.ws, 'api_calls', 1, buffered=True)
                db.workspace_context.set(g.ws)       # row-level security scope for everything this request does
            return fn(*args, **kwargs)
        return decorated
    return wrap(f) if f else wrap


def require_api_key(scope: str):
    """Machine authentication for data ingestion: a workspace-bound key, accepted nowhere else."""
    def wrap(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            header = request.headers.get('Authorization') or ''
            key = apikeys.verify(header.split('Bearer ', 1)[1].strip() if header.startswith('Bearer ') else '', scope)
            if not key:
                return jsonify({'error': 'Unauthorized: missing, invalid or revoked API key.'}), 401
            ok, retry = limiter.allow(f"key:{key['id']}", int(os.environ.get('INGEST_RATE_LIMIT_PER_MIN', '120')))
            if not ok:
                resp = jsonify({'error': 'Too many requests. Slow down or send larger batches.'})
                resp.status_code = 429
                resp.headers['Retry-After'] = str(retry)
                return resp
            g.ws, g.role, g.uid, g.email, g.actor = key['workspace_id'], 'analyst', f"key:{key['id']}", None, f"api-key:{key['name']}"
            quotas.record(g.ws, 'api_calls', 1, buffered=True)
            db.workspace_context.set(g.ws)
            return fn(*args, **kwargs)
        return decorated
    return wrap


def ensure_role(minimum: str) -> None:
    """For endpoints whose required role depends on the HTTP method."""
    if not tenancy.has_role(g.role, minimum):
        raise TenancyError(f'This action needs the {minimum} role (you are {g.role}).', 403, 'forbidden_role')


def require_admin(f):
    """Platform administrators (ADMIN_EMAILS / admin claim): Firebase user management and workspace provisioning."""
    @wraps(f)
    @require_firebase_auth(no_workspace=True)
    def decorated(*args, **kwargs):
        if not auth_configured():
            return f(*args, **kwargs)           # local dev without Firebase
        if not is_admin_token(request.user):
            return jsonify({'error': 'Forbidden: Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


# ── Request plumbing: ids, structured logs, metrics, headers, gzip/etag ──────────
_counters: Counter = Counter()
_latency: Counter = Counter()


@app.before_request
def _start():
    g.t0 = time.perf_counter()
    g.rid = observability.clean_request_id(request.headers.get('X-Request-ID'), uuid.uuid4().hex[:12])


@app.after_request
def _finish(response):
    import gzip
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https':
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    response.headers['X-Request-ID'] = getattr(g, 'rid', '-')
    try:
        if request.method == 'GET' and response.status_code == 200 and response.mimetype == 'application/json' and not response.direct_passthrough:
            response.add_etag()
            response.headers.setdefault('Cache-Control', 'private, no-cache')
            response.make_conditional(request)
        if (response.status_code == 200 and response.mimetype in ('application/json', 'text/csv') and not response.direct_passthrough
                and 'gzip' in request.headers.get('Accept-Encoding', '') and 'Content-Encoding' not in response.headers):
            data = response.get_data()
            if len(data) > 1024:
                response.set_data(gzip.compress(data, compresslevel=5))
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = str(len(response.get_data()))
                response.headers.add('Vary', 'Accept-Encoding')
    except Exception as exc:  # noqa: BLE001  never fail a request over an optimisation
        log.warning('response optimisation skipped: %s', exc)
    ms = (time.perf_counter() - getattr(g, 't0', time.perf_counter())) * 1000
    route = request.url_rule.rule if request.url_rule else 'unmatched'
    _counters[(route, request.method, response.status_code)] += 1
    _latency[route] += ms
    if request.path.startswith('/api/'):
        log.info(json.dumps({'rid': g.rid, 'method': request.method, 'path': request.path, 'status': response.status_code,
                             'ms': round(ms, 1), 'ws': getattr(g, 'ws', None)}))
    return response


@app.teardown_request
def _clear_tenant_context(_exc):
    db.workspace_context.set('')          # a pooled thread must never carry one request's tenant into the next


@app.errorhandler(TenancyError)
def _tenancy_error(exc):
    return jsonify({'error': str(exc), 'code': exc.code}), exc.status


@app.errorhandler(JobRejected)
def _job_rejected(exc):
    resp = jsonify({'error': str(exc)})
    resp.status_code = exc.status
    if exc.status == 503:
        resp.headers['Retry-After'] = '60'
    return resp


@app.errorhandler(QuotaExceeded)
def _quota_exceeded(exc):
    return jsonify({'error': str(exc), 'code': exc.code, 'metric': exc.metric, 'limit': exc.limit, 'used': exc.used}), exc.status


@app.errorhandler(ArtifactError)
def _artifact_error(exc):
    return jsonify({'error': str(exc), 'code': 'model_unavailable'}), 409


@app.errorhandler(KeyShredded)
def _key_shredded(exc):
    return jsonify({'error': 'This workspace\'s data was deleted.', 'code': 'workspace_deleted'}), 410


@app.errorhandler(413)
def _too_large(_):
    return jsonify({'error': f'File too large. Maximum upload is {config.MAX_UPLOAD_MB} MB.'}), 413


@app.errorhandler(Exception)
def _unhandled(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return jsonify({'error': exc.description}), exc.code
    log.exception('unhandled error rid=%s', getattr(g, 'rid', '-'))
    return jsonify({'error': 'Internal server error', 'request_id': getattr(g, 'rid', None)}), 500


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_upload(sample: bool = False) -> pd.DataFrame:
    """Parse the uploaded CSV. With sample=True the client sent only the head of a large file: drop the cut-off last line."""
    if 'file' not in request.files:
        raise ValueError('No file provided')
    raw = request.files['file'].read()
    if not raw.strip():
        raise ValueError('The file is empty')
    if sample and raw.count(b'\n') > 1:
        raw = raw[:raw.rfind(b'\n')]
    for encoding in ('utf-8-sig', 'latin-1'):
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding=encoding, nrows=config.MAX_ROWS + 1, low_memory=False)
            break
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f'Could not parse CSV: {exc}') from exc
    else:
        raise ValueError('Could not decode the file. Please upload a UTF-8 CSV.')
    if len(df) > config.MAX_ROWS:
        raise ValueError(f'Too many rows (limit {config.MAX_ROWS:,}).')
    df.columns = [str(c).strip() for c in df.columns]
    return df


def bad_request(message: str, status: int = 400):
    return jsonify({'error': message}), status


def job_response(job_id: str):
    """202 with the job id; if the job already finished (inline mode, or ?wait=1) include its state."""
    if request.args.get('wait') == '1':
        deadline = time.time() + 600
        while time.time() < deadline:
            j = jobs.get(g.ws, job_id)
            if j and j['status'] in ('done', 'error'):
                break
            time.sleep(0.25)
    j = jobs.get(g.ws, job_id)
    finished = bool(j and j['status'] in ('done', 'error'))
    return jsonify({'job_id': job_id, 'job': j}), (200 if finished else 202)


def form_list(name: str) -> list[str]:
    return [c.strip() for c in request.form.get(name, '').split(',') if c.strip()]


def parse_algorithm() -> str:
    algo = request.form.get('algorithm', 'auto')
    return algo if algo in ALGORITHMS else 'auto'


def parse_promote():
    raw = request.form.get('promote')
    return None if raw in (None, '', 'auto') else raw.lower() in ('1', 'true', 'yes')


# ── Static / health ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    if os.path.isfile(os.path.join(DIST_DIR, 'index.html')):
        return send_from_directory(DIST_DIR, 'index.html')
    return jsonify({'service': 'ChurnSight API', 'message': 'Frontend is served by Vite (npm run dev) or the static build (dist/).'})


@app.route('/assets/<path:filename>')
def dist_assets(filename):
    assets = os.path.join(DIST_DIR, 'assets')
    if os.path.isdir(assets):
        resp = send_from_directory(assets, filename)
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return resp
    return jsonify({'error': 'Not found'}), 404


_demo_cache: dict[int, str] = {}


def demo_csv(rows: int) -> str:
    rows = max(500, min(rows, 50000))
    if rows not in _demo_cache:
        _demo_cache[rows] = generate_demo(rows).to_csv(index=False)
    return _demo_cache[rows]


@app.route('/sample_data.csv')
@app.route('/api/demo.csv')
def sample_csv():
    try:
        rows = int(request.args.get('rows', 5000))
    except ValueError:
        rows = 5000
    return Response(demo_csv(rows), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=churnsight_demo.csv'})


@app.route('/api/health', methods=['GET'])
def health():
    db_ok = True
    try:
        with db.get_engine().connect() as c:
            c.exec_driver_sql('SELECT 1')
    except Exception:  # noqa: BLE001
        db_ok = False
    return jsonify({'ok': db_ok, 'db': db_ok, 'auth_configured': auth_configured(), 'production': is_production(),
                    'workspace_mode': config.WORKSPACE_MODE, 'async_jobs': not config.SYNC_JOBS})


@app.route('/api/ready', methods=['GET'])
def readiness():
    ok, checks = observability.ready()
    return jsonify({'ready': ok, 'checks': checks}), (200 if ok else 503)


@app.route('/metrics')
def prometheus_metrics():
    token = os.environ.get('METRICS_TOKEN')
    given = (request.headers.get('Authorization') or '').removeprefix('Bearer ').strip() or request.headers.get('X-Metrics-Token', '')
    if not token or not hmac.compare_digest(given.encode(), token.encode()):      # header only: tokens in URLs end up in access logs
        return jsonify({'error': 'Not found'}), 404
    return Response(observability.metrics_text(_counters, _latency), mimetype='text/plain')


# ── B2B data pipeline ─────────────────────────────────────────────────────────

MAX_INGEST_ROWS = int(os.environ.get('MAX_INGEST_ROWS', '5000'))


def _mapping_from_form() -> dict:
    raw = request.form.get('mapping') or ''
    try:
        m = json.loads(raw) if raw else {}
    except ValueError:
        raise ValueError('mapping must be a JSON object such as {"account_id": "CustomerRef"}.') from None
    if not isinstance(m, dict):
        raise ValueError('mapping must be a JSON object.')
    return {str(k): str(v) for k, v in m.items()}


@app.route('/api/data/<role>', methods=['POST'])
@require_firebase_auth(role='admin')
def data_upload(role):
    try:
        df = ingest.normalize(role, read_upload(), _mapping_from_form())
        res = ingest.put_table(g.ws, role, df, request.form.get('mode', 'replace' if role == 'accounts' else 'append'))
    except ingest.IngestError as exc:
        return bad_request(str(exc))
    except ValueError as exc:
        return bad_request(str(exc))
    db.audit(g.ws, g.actor, 'data.upload', {'table': role, 'rows': res['rows_added'], 'mode': res['mode']})
    return jsonify(res), 201


@app.route('/api/ingest/<role>', methods=['POST'])
@require_api_key('ingest')
def data_ingest(role):
    """Push rows from your own systems: POST {"rows": [...], "mapping": {...}} with an ingest API key."""
    body = request.get_json(silent=True) or {}
    rows = body.get('rows')
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return bad_request('Send {"rows": [ {...}, ... ]} with at least one row.')
    if len(rows) > MAX_INGEST_ROWS:
        return bad_request(f'At most {MAX_INGEST_ROWS:,} rows per request. Split the batch.', 413)
    try:
        df = ingest.normalize(role, pd.DataFrame(rows), body.get('mapping') if isinstance(body.get('mapping'), dict) else None)
        res = ingest.put_table(g.ws, role, df, 'append')
    except ValueError as exc:
        return bad_request(str(exc))
    db.audit(g.ws, g.actor, 'data.ingest', {'table': role, 'rows': res['rows_added']})
    return jsonify(res), 202


@app.route('/api/data', methods=['DELETE'])
@require_firebase_auth(role='owner')
def data_drop():
    ingest.drop_all(g.ws)
    db.audit(g.ws, g.actor, 'data.drop_tables')
    return jsonify({'ok': True})


@app.route('/api/pipeline', methods=['GET'])
@require_firebase_auth(role='admin')
def pipeline_status():
    return jsonify(pipeline.status(g.ws))


@app.route('/api/pipeline/target', methods=['POST'])
@require_firebase_auth(role='admin')
def pipeline_target():
    """Preview what a churn definition produces; with "save": true also keep it as the workspace's target."""
    body = request.get_json(silent=True) or {}
    spec = {k: body[k] for k in ('type', 'horizon_days', 'observed_through') if k in body}
    try:
        out = pipeline.set_target(g.ws, spec) if body.get('save') else pipeline.preview(g.ws, spec)
    except (features.TargetError, ValueError) as exc:
        return bad_request(str(exc))
    if body.get('save'):
        db.audit(g.ws, g.actor, 'pipeline.target', out['spec'])
    return jsonify(out)


@app.route('/api/pipeline/run', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def pipeline_run():
    body = request.get_json(silent=True) or {}
    algorithm = body.get('algorithm') if body.get('algorithm') in ALGORITHMS else 'auto'
    job_id = jobs.submit(g.ws, 'pipeline_train', {'algorithm': algorithm, 'actor': g.actor, 'rid': g.rid}, None, request.headers.get('Idempotency-Key'))
    return job_response(job_id)


@app.route('/api/pipeline/schedule', methods=['PUT'])
@require_firebase_auth(role='admin')
def pipeline_schedule():
    body = request.get_json(silent=True) or {}
    try:
        out = pipeline.set_schedule(g.ws, bool(body.get('enabled', True)), body.get('every_hours', 24))
    except ValueError as exc:
        return bad_request(str(exc))
    db.audit(g.ws, g.actor, 'pipeline.schedule', {'enabled': out['enabled'], 'every_hours': out['every_hours']})
    return jsonify(out)


@app.route('/api/sources', methods=['GET', 'POST'])
@require_firebase_auth(role='admin')
def sources():
    if request.method == 'GET':
        return jsonify({'sources': connectors.list_sources(g.ws)})
    try:
        return jsonify(connectors.add_source(g.ws, request.get_json(silent=True) or {}, g.actor)), 201
    except connectors.SourceError as exc:
        return bad_request(str(exc))


@app.route('/api/sources/<sid>', methods=['PATCH', 'DELETE'])
@require_firebase_auth(role='admin')
def source_item(sid):
    if request.method == 'DELETE':
        return (jsonify({'ok': True}), 200) if connectors.remove_source(g.ws, sid, g.actor) else bad_request('Source not found', 404)
    out = connectors.set_enabled(g.ws, sid, bool((request.get_json(silent=True) or {}).get('enabled', True)))
    return jsonify(out) if out else bad_request('Source not found', 404)


@app.route('/api/sources/<sid>/sync', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def source_sync(sid):
    try:
        out = connectors.sync(g.ws, sid)
    except connectors.SourceError as exc:
        return bad_request(str(exc), 404)
    db.audit(g.ws, g.actor, 'source.sync', {'id': sid, 'ok': out['ok'], 'rows': out['rows']})
    return jsonify(out)


@app.route('/api/channels', methods=['GET', 'POST'])
@require_firebase_auth(role='admin')
def channels():
    if request.method == 'GET':
        return jsonify({'channels': notify.list_channels(g.ws), 'email_configured': bool(os.environ.get('SMTP_HOST'))})
    try:
        return jsonify(notify.add_channel(g.ws, request.get_json(silent=True) or {}, g.actor)), 201
    except notify.NotifyError as exc:
        return bad_request(str(exc))


@app.route('/api/channels/<cid>', methods=['PATCH', 'DELETE'])
@require_firebase_auth(role='admin')
def channel_item(cid):
    if request.method == 'DELETE':
        return (jsonify({'ok': True}), 200) if notify.remove_channel(g.ws, cid, g.actor) else bad_request('Channel not found', 404)
    out = notify.update_channel(g.ws, cid, request.get_json(silent=True) or {}, g.actor)
    return jsonify(out) if out else bad_request('Channel not found', 404)


@app.route('/api/channels/<cid>/test', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def channel_test(cid):
    res = notify.test_channel(g.ws, cid)
    if res is None:
        return bad_request('Channel not found', 404)
    return jsonify({'ok': res[0], 'detail': res[1]})


@app.route('/api/api-keys', methods=['GET', 'POST'])
@require_firebase_auth(role='admin')
def api_keys():
    if request.method == 'GET':
        return jsonify({'keys': apikeys.list_keys(g.ws)})
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(apikeys.create(g.ws, body.get('name', ''), body.get('scope', 'ingest'), g.actor)), 201       # the key is shown once
    except apikeys.KeyError_ as exc:
        return bad_request(str(exc))


@app.route('/api/api-keys/<key_id>', methods=['DELETE'])
@require_firebase_auth(role='admin')
def api_key_revoke(key_id):
    return (jsonify({'ok': True}), 200) if apikeys.revoke(g.ws, key_id, g.actor) else bad_request('Key not found', 404)


# ── MLOps: realized performance, shadow evaluation, retraining ────────────────

def _run_summary(r: dict) -> dict:
    rz = r['realized'] or {}
    return {'id': r['id'], 'model_id': r['model_id'], 'kind': r['kind'], 'source': r['source'], 'as_of': r['as_of'], 'horizon_days': r['horizon_days'],
            'scored_at': r['scored_at'], 'rows': r['rows'], 'status': r['status'], 'expected': r['expected'],
            'realized': {k: rz.get(k) for k in ('n', 'positives', 'base_rate', 'roc_auc', 'pr_auc', 'brier', 'lift_top10', 'calibration_ratio', 'ece', 'usable')} if rz else None}


@app.route('/api/mlops', methods=['GET'])
@require_firebase_auth(role='admin')
def mlops_status():
    from churn import registry as reg
    ch = shadow.challenger(g.ws)
    return jsonify({'health': mlops.stored_health(g.ws), 'policy': shadow.get_policy(g.ws), 'shadow': service.kv_get(g.ws, 'mlops_shadow', None), 'challenger': ch,
                    'previous_champion': (service.kv_get(g.ws, 'mlops_previous', None) or {}).get('model_id'), 'backtest': service.kv_get(g.ws, 'backtest', None),
                    'champion': (lambda c: {'id': c['id'], 'created_at': c['created_at'], 'pr_auc': c['pr_auc'], 'roc_auc': c['roc_auc']} if c else None)(reg.champion(g.ws)),
                    'runs': [_run_summary(r) for r in mlops.list_runs(g.ws, 20)], 'retrains_automatically': bool(pipeline.get(g.ws)['target'])})


@app.route('/api/mlops/policy', methods=['PUT'])
@require_firebase_auth(role='admin')
def mlops_policy():
    try:
        pol = shadow.set_policy(g.ws, request.get_json(silent=True) or {})
    except ValueError as exc:
        return bad_request(str(exc))
    db.audit(g.ws, g.actor, 'mlops.policy', pol)
    return jsonify(pol)


@app.route('/api/mlops/check', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def mlops_check_now():
    body = request.get_json(silent=True) or {}
    return job_response(jobs.submit(g.ws, 'mlops_check', {'actor': g.actor, 'force': bool(body.get('force')), 'rid': g.rid}, None, request.headers.get('Idempotency-Key')))


@app.route('/api/mlops/backtest', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def mlops_backtest():
    body = request.get_json(silent=True) or {}
    origins = max(2, min(int(body.get('origins', 4)), 8)) if str(body.get('origins', 4)).isdigit() else 4
    return job_response(jobs.submit(g.ws, 'backtest', {'origins': origins, 'actor': g.actor, 'rid': g.rid}, None, request.headers.get('Idempotency-Key')))


@app.route('/api/mlops/rollback', methods=['POST'])
@require_firebase_auth(role='admin')
def mlops_rollback():
    try:
        return jsonify(shadow.rollback(g.ws, g.actor))
    except LookupError as exc:
        return bad_request(str(exc), 404)


@app.route('/api/mlops/promote', methods=['POST'])
@require_firebase_auth(role='admin')
def mlops_promote():
    ch = shadow.challenger(g.ws)
    if not ch:
        return bad_request('There is no challenger to promote.', 404)
    return jsonify(shadow.promote(g.ws, ch['id'], g.actor))


@app.route('/api/runs', methods=['GET'])
@require_firebase_auth(role='admin')
def runs_list():
    return jsonify({'runs': [_run_summary(r) for r in mlops.list_runs(g.ws, 40)]})


@app.route('/api/runs/<run_id>/outcomes', methods=['POST'])
@require_firebase_auth(role='admin')
def runs_outcomes(run_id):
    """Upload what actually happened for a scoring run: a CSV with customer_id and churned (0/1, yes/no, true/false)."""
    from churn.schema import normalize_target
    try:
        df = read_upload()
        cols = {str(c).strip().lower(): c for c in df.columns}
        idc, yc = cols.get('customer_id') or cols.get('id'), cols.get('churned') or cols.get('churn') or cols.get('outcome')
        if not idc or not yc:
            return bad_request('The file needs two columns: customer_id and churned (0/1).')
        y = normalize_target(df[yc])
        ids = df[idc].astype(str)
        if service.get_settings(g.ws).get('privacy', {}).get('hash_ids'):
            from churn import crypto
            ids = ids.map(lambda v: crypto.pseudonymize_id(g.ws, v))
        outcomes = pd.Series(y.to_numpy(), index=ids.to_numpy())[y.notna().to_numpy()].astype(int)
        run = mlops.get_run(g.ws, run_id)
        if not run:
            return bad_request('Run not found', 404)
        resolved = [mlops.resolve_run(g.ws, run['id'], outcomes)]
        for sib in mlops.siblings(g.ws, run):
            mlops.resolve_run(g.ws, sib['id'], outcomes)
    except ValueError as exc:
        return bad_request(str(exc))
    health = mlops.update_health(g.ws)
    db.audit(g.ws, g.actor, 'mlops.outcomes', {'run': run_id, 'accounts': int(len(outcomes))})
    return jsonify({'realized': resolved[0], 'health': health})


# ── Training / scoring jobs ───────────────────────────────────────────────────

@app.route('/api/preview', methods=['POST'])
@require_firebase_auth(role='admin')
def preview():
    """Show how a CSV will be interpreted before training (the upload wizard)."""
    truncated = request.form.get('truncated') == '1'
    try:
        df = read_upload(sample=truncated)
        schema = infer_schema(df, request.form.get('target') or None, form_list('exclude_columns'), form_list('allow_pii'))
    except ValueError as exc:
        return bad_request(str(exc))
    rows = int(len(df))
    if truncated:                                   # extrapolate the row count from how much of the file we saw
        try:
            total, seen = float(request.form['total_bytes']), float(request.form['sent_bytes'])
            rows = int(len(df) * total / max(seen, 1))
        except (KeyError, ValueError):
            pass
    schema['rows'] = rows
    pii = set(schema.get('pii', []))
    sample = json.loads(df.drop(columns=[c for c in df.columns if c in pii]).head(5).to_json(orient='records', date_format='iso'))
    return jsonify({'schema': schema, 'sample': sample, 'columns': list(df.columns), 'rows': rows, 'estimated': truncated})


@app.route('/api/upload', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def upload_file():
    try:
        df = read_upload()
    except ValueError as exc:
        return bad_request(str(exc))
    algorithm, promote, target = parse_algorithm(), parse_promote(), request.form.get('target') or None
    exclude, allow_pii = form_list('exclude_columns'), form_list('allow_pii')
    ws, actor = g.ws, g.actor
    job_id = jobs.submit(ws, 'train', {'algorithm': algorithm, 'actor': actor, 'promote': promote, 'source': 'upload', 'target': target,
                                       'exclude': exclude, 'allow_pii': allow_pii, 'rid': g.rid}, df, request.headers.get('Idempotency-Key'))
    return job_response(job_id)


@app.route('/api/demo', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def load_demo():
    body = request.get_json(silent=True) or {}
    try:
        rows = max(1000, min(int(body.get('rows', 8000)), 30000))
    except (TypeError, ValueError):
        rows = 8000
    ws, actor = g.ws, g.actor
    algorithm = body.get('algorithm') if body.get('algorithm') in ALGORITHMS else 'auto'
    job_id = jobs.submit(ws, 'demo', {'rows': rows, 'algorithm': algorithm, 'actor': actor, 'rid': g.rid}, None, request.headers.get('Idempotency-Key'))
    return job_response(job_id)


@app.route('/api/score', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def score_batch():
    try:
        df = read_upload()
    except ValueError as exc:
        return bad_request(str(exc))
    ws, actor = g.ws, g.actor
    allow_missing = request.form.get('allow_missing', '').lower() in ('1', 'true', 'yes')
    job_id = jobs.submit(ws, 'score', {'actor': actor, 'allow_missing': allow_missing, 'rid': g.rid}, df, request.headers.get('Idempotency-Key'))
    return job_response(job_id)


@app.route('/api/jobs/<job_id>', methods=['GET'])
@require_firebase_auth
def job_status(job_id):
    job = jobs.get(g.ws, job_id)
    return (jsonify(job), 200) if job else bad_request('Job not found', 404)


@app.route('/api/jobs/<job_id>/cancel', methods=['POST'])
@require_firebase_auth(role='admin')
def job_cancel(job_id):
    job = jobs.cancel(g.ws, job_id)
    if not job:
        return bad_request('Job not found', 404)
    db.audit(g.ws, g.actor, 'job.cancel', {'job': job_id})
    return jsonify(job)


# ── Results / customers ───────────────────────────────────────────────────────

@app.route('/api/results', methods=['GET'])
@require_firebase_auth
def get_results():
    packed = service.results_bytes(g.ws)
    if packed is None:
        return bad_request('No model yet. Upload data or load the demo dataset.', 404)
    etag, raw, gz = packed
    if request.headers.get('If-None-Match', '').strip('"') == etag:
        resp = Response(status=304)
    elif 'gzip' in request.headers.get('Accept-Encoding', ''):
        resp = Response(gz, mimetype='application/json')
        resp.headers['Content-Encoding'] = 'gzip'
    else:
        resp = Response(raw, mimetype='application/json')
    resp.headers['ETag'] = f'"{etag}"'
    resp.headers['Cache-Control'] = 'private, no-cache'
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.route('/api/customers', methods=['GET'])
@require_firebase_auth
def get_customers():
    a = request.args
    try:
        res = service.query_customers(g.ws, a.get('tier'), a.get('q'), a.get('sort', 'prob'), 'asc' if a.get('dir') == 'asc' else 'desc',
                                      int(a.get('page', 1)), int(a.get('limit', 20)))
    except ValueError as exc:
        return bad_request(str(exc) if 'Pages beyond' in str(exc) else 'Invalid paging parameters')
    return jsonify(res)


@app.route('/api/customers/export', methods=['GET', 'POST'])
@require_firebase_auth(role='analyst')
def export_customers():
    a = request.args
    ids = (request.get_json(silent=True) or {}).get('ids') if request.method == 'POST' else None
    rows = service.export_csv_rows(g.ws, a.get('tier'), a.get('q'), ids, a.get('sort', 'prob'), 'asc' if a.get('dir') == 'asc' else 'desc')

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow(row)
            if buf.tell() > 65536:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return Response(stream_with_context(generate()), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=churnsight-export-{time.strftime("%Y-%m-%d")}.csv'})


@app.route('/api/customers/<path:customer_id>', methods=['GET'])
@require_firebase_auth
def customer_detail(customer_id):
    detail = service.customer_detail(g.ws, customer_id)
    return jsonify(detail) if detail else bad_request('Customer not found', 404)


# ── Prediction ────────────────────────────────────────────────────────────────

@app.route('/api/model/schema', methods=['GET'])
@require_firebase_auth
def model_schema():
    form = service.form_schema(g.ws)
    return jsonify(form) if form else bad_request('No model yet.', 404)


@app.route('/api/predict', methods=['POST'])
@require_firebase_auth
def predict_single():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return bad_request('No data provided')
    try:
        return jsonify(service.predict(g.ws, data.get('values', data), explain=bool(data.get('explain', True))))
    except LookupError as exc:
        return bad_request(str(exc), 404)


# ── Registry / monitoring / settings ──────────────────────────────────────────

@app.route('/api/models', methods=['GET'])
@require_firebase_auth
def models_list():
    return jsonify({'models': registry.list_models(g.ws)})


@app.route('/api/models/<model_id>/promote', methods=['POST'])
@require_firebase_auth(heavy=True, role='admin')
def models_promote(model_id):
    try:
        return jsonify(service.promote_model(g.ws, model_id, g.actor))
    except LookupError as exc:
        return bad_request(str(exc), 404)


@app.route('/api/monitoring/drift', methods=['GET'])
@require_firebase_auth
def drift():
    return jsonify({'drift': service.kv_get(g.ws, 'drift')})


@app.route('/api/settings', methods=['GET', 'POST'])
@require_firebase_auth
def settings():
    if request.method == 'POST':
        ensure_role('admin')
    if request.method == 'GET':
        row, bundle = registry.champion_bundle(g.ws)
        current = service.get_settings(g.ws)
        out = {**current}
        if bundle:
            out['effective_thresholds'] = service.effective_thresholds(current, bundle)
            out['numeric_columns'] = bundle['schema']['numeric']
        return jsonify(out)
    try:
        updated = service.save_settings(g.ws, request.get_json(silent=True) or {})
    except ValueError as exc:
        return bad_request(str(exc))
    service.apply_settings_change(g.ws)
    db.audit(g.ws, g.actor, 'settings.update', updated)
    return jsonify(updated)


@app.route('/api/workspace/reset', methods=['POST'])
@require_firebase_auth(role='owner')
def workspace_reset():
    registry.wipe(g.ws)
    db.audit(g.ws, g.actor, 'workspace.reset')
    return jsonify({'status': 'reset'})


# ── Alerts & interventions ────────────────────────────────────────────────────

@app.route('/api/alerts', methods=['GET'])
@require_firebase_auth
def alerts_list():
    status = request.args.get('status')
    return jsonify(service.list_alerts(g.ws, status if status in ('Open', 'Acknowledged', 'Resolved') else None,
                                       min(int(request.args.get('limit', 100)), 300)))


@app.route('/api/alerts/<alert_id>', methods=['PATCH'])
@require_firebase_auth(role='analyst')
def alerts_update(alert_id):
    body = request.get_json(silent=True) or {}
    if body.get('assignee') == '@me':
        body['assignee'] = g.actor
    try:
        res = service.update_alert(g.ws, alert_id, g.actor, body)
    except ValueError as exc:
        return bad_request(str(exc))
    return jsonify(res) if res else bad_request('Alert not found', 404)


@app.route('/api/customers/<path:customer_id>/interventions', methods=['POST'])
@require_firebase_auth(role='analyst')
def interventions_add(customer_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(service.add_intervention(g.ws, customer_id, body.get('kind', 'other'), body.get('notes', ''), g.actor)), 201
    except LookupError as exc:
        return bad_request(str(exc), 404)


@app.route('/api/interventions/<intervention_id>', methods=['PATCH'])
@require_firebase_auth(role='analyst')
def interventions_outcome(intervention_id):
    try:
        ok = service.set_outcome(g.ws, intervention_id, (request.get_json(silent=True) or {}).get('outcome', ''), g.actor)
    except ValueError as exc:
        return bad_request(str(exc))
    return jsonify({'ok': True}) if ok else bad_request('Intervention not found', 404)


@app.route('/api/interventions/summary', methods=['GET'])
@require_firebase_auth
def interventions_summary():
    return jsonify(service.intervention_summary(g.ws))


# ── Admin (Firebase user management) ──────────────────────────────────────────

@app.route('/api/admin/whoami', methods=['GET'])
@require_firebase_auth
def admin_whoami():
    if not auth_configured():
        return jsonify({'is_admin': False, 'email': None, 'message': 'Auth not fully configured in this environment.'})
    return jsonify({'is_admin': is_admin_token(request.user), 'email': request.user.get('email'), 'uid': request.user.get('uid')})


@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    if not auth_configured():
        return jsonify({'error': 'Firebase admin SDK unavailable.'}), 501
    users = []
    for user in auth.list_users().iterate_all():
        users.append({
            'uid': user.uid, 'email': user.email, 'display_name': user.display_name, 'disabled': user.disabled,
            'provider_ids': [p.provider_id for p in user.provider_data] if user.provider_data else [],
            'created_at': user.user_metadata.creation_timestamp, 'last_sign_in_at': user.user_metadata.last_sign_in_timestamp,
        })
    return jsonify({'users': users})


@app.route('/api/admin/users/<uid>', methods=['PATCH'])
@require_admin
def admin_update_user(uid):
    if not auth_configured():
        return jsonify({'error': 'Firebase admin SDK unavailable.'}), 501
    action = (request.get_json(silent=True) or {}).get('action')
    if action == 'disable':
        auth.update_user(uid, disabled=True)
    elif action == 'enable':
        auth.update_user(uid, disabled=False)
    elif action == 'delete':
        auth.delete_user(uid)
    else:
        return bad_request('Invalid action')
    db.audit(g.ws, g.actor, f'admin.user.{action}', {'uid': uid})
    return jsonify({'status': 'success', 'action': action})


@app.route('/api/me', methods=['GET'])
@require_firebase_auth
def me():
    info = tenancy.get_workspace(g.ws) or {'id': g.ws, 'name': g.ws, 'plan': 'dev'}
    return jsonify({'uid': g.uid, 'email': g.email, 'role': g.role,
                    'workspace': {'id': info['id'], 'name': info['name'], 'plan': info.get('plan'), 'sso_required': bool(info.get('sso_required'))},
                    'workspaces': [{'id': m['workspace_id'], 'name': m['name'], 'role': m['role']} for m in tenancy.memberships_for(g.uid)] if g.uid != 'local-dev' else [],
                    'seats': tenancy.seat_count(g.ws), 'platform_admin': bool(request.user and is_admin_token(request.user))})


@app.route('/api/usage', methods=['GET'])
@require_firebase_auth(role='admin')
def usage_summary():
    return jsonify(quotas.summary(g.ws))


@app.route('/api/usage/export', methods=['GET'])
@require_firebase_auth(role='admin')
def usage_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['workspace', 'period', 'metric', 'value'])
    for r in quotas.export_rows(g.ws):
        w.writerow([g.ws, r['period'], r['metric'], r['value']])
    return Response(buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=usage.csv'})


@app.route('/api/platform/workspaces/<ws_id>/plan', methods=['PUT'])
@require_admin
def platform_set_plan(ws_id):
    plan = (request.get_json(silent=True) or {}).get('plan', '')
    if plan not in quotas.plans() or plan == 'dev':
        return bad_request('Unknown plan. Choose one of: ' + ', '.join(p for p in quotas.plans() if p != 'dev'))
    if not tenancy.get_workspace(ws_id):
        return bad_request('Workspace not found', 404)
    tenancy.set_plan(ws_id, plan)
    quotas.forget_plans()
    with db.system_context():
        db.audit(ws_id, g.actor, 'workspace.plan', {'plan': plan})
    return jsonify({'workspace': ws_id, 'plan': plan})


@app.route('/api/members', methods=['GET'])
@require_firebase_auth(role='admin')
def members_list():
    return jsonify({'members': tenancy.list_members(g.ws), 'seats': tenancy.seat_count(g.ws)})


@app.route('/api/members/<uid>', methods=['PATCH', 'DELETE'])
@require_firebase_auth(role='admin')
def members_update(uid):
    if request.method == 'PATCH':
        tenancy.set_role(g.ws, g.uid, g.role, g.actor, uid, (request.get_json(silent=True) or {}).get('role', ''))
    else:
        tenancy.remove_member(g.ws, g.uid, g.role, g.actor, uid)
    return jsonify({'ok': True})


@app.route('/api/invites', methods=['POST'])
@require_firebase_auth(role='admin')
def invites_create():
    body = request.get_json(silent=True) or {}
    invite = tenancy.create_invite(g.ws, body.get('email', ''), body.get('role', 'viewer'), g.role, g.actor)
    return jsonify(invite), 201          # the token is shown once; only its hash is stored


@app.route('/api/invites', methods=['GET'])
@require_firebase_auth(role='admin')
def invites_list():
    return jsonify({'invites': tenancy.list_invites(g.ws)})


@app.route('/api/invites/<invite_id>', methods=['DELETE'])
@require_firebase_auth(role='admin')
def invites_revoke(invite_id):
    if not tenancy.revoke_invite(g.ws, invite_id, g.role, g.actor):
        return bad_request('Invitation not found', 404)
    return jsonify({'ok': True})


@app.route('/api/invites/accept', methods=['POST'])
@require_firebase_auth(no_workspace=True)
def invites_accept():
    return jsonify(tenancy.accept_invite((request.get_json(silent=True) or {}).get('token', ''), g.uid, g.email))


@app.route('/api/workspaces', methods=['POST'])
@require_admin
def workspaces_create():
    """Platform administrators provision a customer workspace and get a one-time owner invitation."""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name or '@' not in (body.get('owner_email') or ''):
        return bad_request('A workspace name and the owner\'s email are required.')
    ws = tenancy.create_workspace(name)
    with db.workspace_scope(ws):
        invite = tenancy.create_invite(ws, body['owner_email'], 'owner', 'owner', getattr(g, 'actor', None))
    return jsonify({'workspace_id': ws, 'invite': invite}), 201


@app.route('/api/workspace/sso', methods=['POST'])
@require_firebase_auth(role='owner')
def workspace_sso():
    tenancy.set_sso_required(g.ws, bool((request.get_json(silent=True) or {}).get('required')), g.actor)
    return jsonify({'ok': True})


@app.route('/api/audit', methods=['GET'])
@require_firebase_auth(role='admin')
def audit_list():
    import sqlalchemy as sa
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(db.audit_t).where(db.audit_t.c.workspace_id == g.ws).order_by(db.audit_t.c.id.desc()).limit(200)).all()
    return jsonify({'events': [{'id': r.id, 'actor': r.actor, 'action': r.action, 'detail': db.loads(r.detail), 'ts': r.ts} for r in rows],
                    'chain': db.verify_audit(g.ws)})


@app.route('/api/audit/export', methods=['GET'])
@require_firebase_auth(role='admin')
def audit_export():
    import sqlalchemy as sa
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(db.audit_t).where(db.audit_t.c.workspace_id == g.ws).order_by(db.audit_t.c.id)).all()
    lines = [json.dumps({'id': r.id, 'ts': r.ts, 'actor': r.actor, 'action': r.action, 'detail': db.loads(r.detail), 'prev_hash': r.prev_hash, 'hash': r.hash})
             for r in rows]
    return Response('\n'.join(lines) + '\n', mimetype='application/x-ndjson',
                    headers={'Content-Disposition': f'attachment; filename=audit-{g.ws}.ndjson'})


@app.route('/api/privacy/erase', methods=['POST'])
@require_firebase_auth(role='admin')
def privacy_erase():
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    if not isinstance(ids, list) or not ids:
        return bad_request('Provide a non-empty list of customer ids.')
    return jsonify(privacy.erase_customers(g.ws, ids, g.actor))


@app.route('/api/privacy/export/<path:customer_id>', methods=['GET'])
@require_firebase_auth(role='admin')
def privacy_export(customer_id):
    data = privacy.export_customer(g.ws, customer_id, g.actor)
    return jsonify(data) if data else bad_request('Customer not found', 404)


@app.route('/api/privacy/lookup', methods=['POST'])
@require_firebase_auth(role='admin')
def privacy_lookup():
    """With ID pseudonymisation on, map your own customer ids to the stored stand-ins so you can search for them."""
    from churn import crypto
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    return jsonify({'ids': {str(i): crypto.pseudonymize_id(g.ws, i) for i in ids[:1000]}})


@app.route('/api/workspace', methods=['DELETE'])
@require_firebase_auth(role='owner')
def workspace_delete():
    if (request.get_json(silent=True) or {}).get('confirm') != g.ws:
        return bad_request('To delete this workspace, send {"confirm": "<workspace id>"}. This cannot be undone.')
    return jsonify(privacy.delete_workspace(g.ws, g.actor))


if __name__ == '__main__':
    app.run(debug=not is_production(), port=int(os.environ.get('PORT', 5050)))
