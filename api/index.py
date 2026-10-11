"""ChurnSight API.

Thin HTTP layer over api/churn/*. Each request is scoped to a workspace (see churn.config.WORKSPACE_MODE),
so accounts never see each other's models, customers or alerts.
"""
from __future__ import annotations

import csv
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

from churn import config, db, jobs, registry, service  # noqa: E402
from churn.demo_data import generate_demo  # noqa: E402
from churn.schema import infer_schema  # noqa: E402
from churn.security import SECURITY_HEADERS, limiter  # noqa: E402
from churn.training import ALGORITHMS  # noqa: E402

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), format='%(message)s')
log = logging.getLogger('churnsight.api')

BASE_DIR = config.BASE_DIR
DIST_DIR = os.path.join(BASE_DIR, 'dist')

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_MB * 1024 * 1024


def is_production() -> bool:
    return (
        os.environ.get('VERCEL') == '1'
        or os.environ.get('FLASK_ENV') == 'production'
        or os.environ.get('AUTH_FAIL_CLOSED', '').lower() in ('1', 'true', 'yes')
    )


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


def workspace_for(user) -> str:
    mode = config.WORKSPACE_MODE
    if not user:
        return 'local'
    if mode == 'shared':
        return 'shared'
    if mode == 'domain' and user.get('email') and not user['email'].lower().endswith(('@gmail.com', '@outlook.com', '@yahoo.com', '@hotmail.com')):
        return 'd_' + user['email'].split('@', 1)[1].lower()
    return 'u_' + str(user.get('uid') or user.get('user_id') or 'unknown')


def _client_key() -> str:
    user = getattr(request, 'user', None)
    return (user or {}).get('uid') or request.headers.get('X-Forwarded-For', request.remote_addr or 'anon').split(',')[0].strip()


def require_firebase_auth(f=None, *, heavy: bool = False):
    """Authenticate, bind the workspace, and rate-limit. `heavy` is for training/scoring endpoints."""
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

            g.ws = workspace_for(request.user)
            g.actor = (request.user or {}).get('email') or 'local-dev'
            return fn(*args, **kwargs)
        return decorated
    return wrap(f) if f else wrap


def require_admin(f):
    @wraps(f)
    @require_firebase_auth
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
    g.rid = request.headers.get('X-Request-ID') or uuid.uuid4().hex[:12]


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

def read_upload() -> pd.DataFrame:
    if 'file' not in request.files:
        raise ValueError('No file provided')
    raw = request.files['file'].read()
    if not raw.strip():
        raise ValueError('The file is empty')
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


@app.route('/metrics')
def prometheus_metrics():
    token = os.environ.get('METRICS_TOKEN')
    if not token or request.args.get('token') != token:
        return jsonify({'error': 'Not found'}), 404
    lines = ['# TYPE churnsight_requests_total counter']
    for (route, method, status), n in sorted(_counters.items()):
        lines.append(f'churnsight_requests_total{{route="{route}",method="{method}",status="{status}"}} {n}')
    lines.append('# TYPE churnsight_request_ms_sum counter')
    for route, ms in sorted(_latency.items()):
        lines.append(f'churnsight_request_ms_sum{{route="{route}"}} {ms:.1f}')
    return Response('\n'.join(lines) + '\n', mimetype='text/plain')


# ── Training / scoring jobs ───────────────────────────────────────────────────

@app.route('/api/preview', methods=['POST'])
@require_firebase_auth
def preview():
    """Show how a CSV will be interpreted before training (the upload wizard)."""
    try:
        df = read_upload()
        schema = infer_schema(df, request.form.get('target') or None)
    except ValueError as exc:
        return bad_request(str(exc))
    sample = json.loads(df.head(5).to_json(orient='records', date_format='iso'))
    return jsonify({'schema': schema, 'sample': sample, 'columns': list(df.columns), 'rows': int(len(df))})


@app.route('/api/upload', methods=['POST'])
@require_firebase_auth(heavy=True)
def upload_file():
    try:
        df = read_upload()
    except ValueError as exc:
        return bad_request(str(exc))
    algorithm, promote, target = parse_algorithm(), parse_promote(), request.form.get('target') or None
    ws, actor = g.ws, g.actor
    job_id = jobs.submit(ws, 'train', lambda progress: service.run_training(ws, df, algorithm, actor, progress, promote, 'upload', target))
    return job_response(job_id)


@app.route('/api/demo', methods=['POST'])
@require_firebase_auth(heavy=True)
def load_demo():
    body = request.get_json(silent=True) or {}
    try:
        rows = max(1000, min(int(body.get('rows', 8000)), 30000))
    except (TypeError, ValueError):
        rows = 8000
    ws, actor = g.ws, g.actor
    algorithm = body.get('algorithm') if body.get('algorithm') in ALGORITHMS else 'auto'
    job_id = jobs.submit(ws, 'train', lambda progress: service.run_training(ws, generate_demo(rows), algorithm, actor, progress, None, 'demo'))
    return job_response(job_id)


@app.route('/api/score', methods=['POST'])
@require_firebase_auth(heavy=True)
def score_batch():
    try:
        df = read_upload()
    except ValueError as exc:
        return bad_request(str(exc))
    ws, actor = g.ws, g.actor
    job_id = jobs.submit(ws, 'score', lambda progress: service.run_batch_scoring(ws, df, actor, progress))
    return job_response(job_id)


@app.route('/api/jobs/<job_id>', methods=['GET'])
@require_firebase_auth
def job_status(job_id):
    job = jobs.get(g.ws, job_id)
    return (jsonify(job), 200) if job else bad_request('Job not found', 404)


# ── Results / customers ───────────────────────────────────────────────────────

@app.route('/api/results', methods=['GET'])
@require_firebase_auth
def get_results():
    results = service.build_results(g.ws)
    if results is None:
        return bad_request('No model yet. Upload data or load the demo dataset.', 404)
    return jsonify(results)


@app.route('/api/customers', methods=['GET'])
@require_firebase_auth
def get_customers():
    a = request.args
    try:
        res = service.query_customers(g.ws, a.get('tier'), a.get('q'), a.get('sort', 'prob'), 'asc' if a.get('dir') == 'asc' else 'desc',
                                      int(a.get('page', 1)), int(a.get('limit', 20)))
    except ValueError:
        return bad_request('Invalid paging parameters')
    return jsonify(res)


@app.route('/api/customers/export', methods=['GET', 'POST'])
@require_firebase_auth
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
@require_firebase_auth(heavy=True)
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
@require_firebase_auth
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
@require_firebase_auth
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
@require_firebase_auth
def interventions_add(customer_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(service.add_intervention(g.ws, customer_id, body.get('kind', 'other'), body.get('notes', ''), g.actor)), 201
    except LookupError as exc:
        return bad_request(str(exc), 404)


@app.route('/api/interventions/<intervention_id>', methods=['PATCH'])
@require_firebase_auth
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


@app.route('/api/admin/audit', methods=['GET'])
@require_admin
def admin_audit():
    import sqlalchemy as sa
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(db.audit_t).where(db.audit_t.c.workspace_id == g.ws).order_by(db.audit_t.c.id.desc()).limit(100)).all()
    return jsonify({'events': [{'actor': r.actor, 'action': r.action, 'detail': db.loads(r.detail), 'ts': r.ts} for r in rows]})


if __name__ == '__main__':
    app.run(debug=not is_production(), port=int(os.environ.get('PORT', 5050)))
