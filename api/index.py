from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score,
    f1_score, roc_auc_score, confusion_matrix, roc_curve
)
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
import joblib
import os
import json
import io
import copy
import gzip
import warnings
from functools import wraps

warnings.filterwarnings('ignore')

import firebase_admin
from firebase_admin import auth

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
DIST_DIR = os.path.join(BASE_DIR, 'dist')


def get_writable_dir():
    """Prefer local .data; use /tmp on Vercel (ephemeral, same-instance)."""
    if os.environ.get('VERCEL') == '1':
        return '/tmp'
    path = os.path.join(BASE_DIR, '.data')
    os.makedirs(path, exist_ok=True)
    return path


WRITABLE_DIR = get_writable_dir()
MODEL_PATH = os.path.join(WRITABLE_DIR, 'churn_model.pkl')
RESULTS_PATH = os.path.join(WRITABLE_DIR, 'training_results.json')
ADMIN_SETTINGS_FILE = os.path.join(WRITABLE_DIR, 'admin_settings.json')
REPO_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'churn_model.pkl')

app = Flask(__name__, static_folder=None)
CORS(app)


@app.after_request
def compress_and_cache(response):
    """Gzip large JSON and add ETags so repeat GETs of results revalidate cheaply."""
    try:
        if (
            request.method == 'GET'
            and response.status_code == 200
            and response.mimetype == 'application/json'
            and not response.direct_passthrough
        ):
            response.add_etag()
            response.headers.setdefault('Cache-Control', 'private, no-cache')
            response.make_conditional(request)
        if (
            response.status_code == 200
            and response.mimetype in ('application/json', 'text/csv')
            and not response.direct_passthrough
            and 'gzip' in request.headers.get('Accept-Encoding', '')
            and 'Content-Encoding' not in response.headers
        ):
            data = response.get_data()
            if len(data) > 1024:
                response.set_data(gzip.compress(data, compresslevel=5))
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = str(len(response.get_data()))
                response.headers.add('Vary', 'Accept-Encoding')
    except Exception as exc:  # never fail a request because of optimisation
        print('[WARN] response optimisation skipped:', exc)
    return response

# ── Firebase ──────────────────────────────────────────────────────────────────
firebase_initialized = False
try:
    firebase_admin.initialize_app()
    firebase_initialized = True
    print('[INFO] Firebase Admin SDK successfully initialized.')
except Exception as e:
    print(f'[WARNING] Firebase Admin SDK initialization failed: {e}')

ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.environ.get('ADMIN_EMAILS', '').split(',')
    if email.strip()
]

DEFAULT_ADMIN_SETTINGS = {
    'risk_thresholds': {
        'high': 0.7,
        'medium': 0.4
    },
    'alert_rules': [
        {
            'id': 'reengagement',
            'label': 'Re-engagement email trigger',
            'enabled': True,
            'last_login_days': 21
        },
        {
            'id': 'support_escalation',
            'label': 'Support escalation threshold',
            'enabled': True,
            'support_tickets': 4
        }
    ]
}


def load_admin_settings():
    settings = copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
    if os.path.exists(ADMIN_SETTINGS_FILE):
        try:
            with open(ADMIN_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                stored = json.load(f)
            if isinstance(stored.get('risk_thresholds'), dict):
                settings['risk_thresholds'].update(stored['risk_thresholds'])
            if isinstance(stored.get('alert_rules'), list):
                by_id = {r['id']: r for r in settings['alert_rules']}
                for rule in stored['alert_rules']:
                    rid = rule.get('id')
                    if rid in by_id:
                        by_id[rid].update(rule)
                settings['alert_rules'] = list(by_id.values())
        except Exception:
            print('[WARNING] Could not load admin_settings.json, using defaults.')
    return settings


ADMIN_SETTINGS = load_admin_settings()


def save_admin_settings():
    try:
        with open(ADMIN_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ADMIN_SETTINGS, f, indent=2)
        return True
    except Exception as e:
        print('Failed to save admin settings:', e)
        return False


def is_production():
    return (
        os.environ.get('VERCEL') == '1'
        or os.environ.get('FLASK_ENV') == 'production'
        or os.environ.get('AUTH_FAIL_CLOSED', '').lower() in ('1', 'true', 'yes')
    )


def auth_configured():
    return bool(
        firebase_initialized
        and (
            os.environ.get('FIREBASE_PROJECT_ID')
            or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
            or os.environ.get('FIREBASE_CONFIG')
        )
    )


def require_firebase_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not auth_configured():
            if is_production() or os.environ.get('AUTH_REQUIRED', '').lower() in ('1', 'true', 'yes'):
                return jsonify({
                    'error': 'Authentication service unavailable. Configure Firebase Admin credentials.'
                }), 503
            # Local/dev only: allow unauthenticated access
            request.user = None
            return f(*args, **kwargs)

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized: Missing or invalid token format'}), 401

        token = auth_header.split('Bearer ', 1)[1]
        try:
            request.user = auth.verify_id_token(token)
        except Exception as e:
            return jsonify({'error': f'Unauthorized: Token verification failed: {str(e)}'}), 401

        return f(*args, **kwargs)
    return decorated_function


def is_admin_token(decoded_token):
    if not decoded_token:
        return False
    email = (decoded_token.get('email') or '').lower()
    if email in ADMIN_EMAILS:
        return True
    return decoded_token.get('admin') is True


def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not auth_configured():
            if is_production() or os.environ.get('AUTH_REQUIRED', '').lower() in ('1', 'true', 'yes'):
                return jsonify({'error': 'Authentication service unavailable.'}), 503
            request.user = None
            return f(*args, **kwargs)
        if not getattr(request, 'user', None):
            return jsonify({'error': 'Unauthorized: Admin access required'}), 401
        if not is_admin_token(request.user):
            return jsonify({'error': 'Forbidden: Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function


# ── Global state (hydrated from disk on demand) ───────────────────────────────
trained_model = None
training_results = None
customer_predictions = None
feature_baseline = None  # typical-customer values used for what-if impact

CATEGORICAL_FEATURES = ['gender', 'location', 'contract_type', 'payment_method']
NUMERICAL_FEATURES = [
    'age', 'tenure_months', 'monthly_charges', 'total_charges',
    'num_products', 'num_logins_last30', 'support_tickets', 'last_login_days'
]


def classify_risk(prob):
    thresholds = ADMIN_SETTINGS.get('risk_thresholds', {})
    high = float(thresholds.get('high', 0.7))
    medium = float(thresholds.get('medium', 0.4))
    if medium > high:
        medium = high
    if prob >= high:
        return 'High'
    if prob >= medium:
        return 'Medium'
    return 'Low'


def recompute_customer_risk_levels():
    global customer_predictions, training_results
    if not customer_predictions:
        return
    for customer in customer_predictions:
        prob = float(customer.get('churn_prob', 0)) / 100.0
        customer['risk_level'] = classify_risk(prob)
    if training_results:
        training_results['customers'] = customer_predictions
        summary = training_results.get('summary', {})
        summary['high_risk'] = sum(1 for c in customer_predictions if c['risk_level'] == 'High')
        summary['medium_risk'] = sum(1 for c in customer_predictions if c['risk_level'] == 'Medium')
        summary['low_risk'] = sum(1 for c in customer_predictions if c['risk_level'] == 'Low')
        summary['risk_thresholds'] = copy.deepcopy(ADMIN_SETTINGS['risk_thresholds'])
        training_results['summary'] = summary
        persist_results(training_results)


def persist_results(results):
    try:
        with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
            json.dump(results, f)
        return True
    except Exception as e:
        print('Failed to persist training results:', e)
        return False


def persist_model(pipeline):
    try:
        joblib.dump(pipeline, MODEL_PATH)
        return True
    except Exception as e:
        print('Failed to persist model:', e)
        return False


def load_persisted_results():
    global training_results, customer_predictions
    if training_results is not None:
        return training_results
    if not os.path.exists(RESULTS_PATH):
        return None
    try:
        with open(RESULTS_PATH, 'r', encoding='utf-8') as f:
            training_results = json.load(f)
        customer_predictions = training_results.get('customers')
        return training_results
    except Exception as e:
        print('Failed to load persisted results:', e)
        return None


def preprocess_dataframe(df):
    df = df.copy()
    if 'customer_id' in df.columns:
        df = df.drop(columns=['customer_id'])

    for col in NUMERICAL_FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    present_num = [c for c in NUMERICAL_FEATURES if c in df.columns]
    if present_num:
        df[present_num] = df[present_num].fillna(df[present_num].median(numeric_only=True))

    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            mode = df[col].mode()
            df[col] = df[col].fillna(mode[0] if not mode.empty else 'Unknown')

    return df


def build_pipeline(algorithm='random_forest'):
    preprocessor = ColumnTransformer(transformers=[
        ('num', StandardScaler(), NUMERICAL_FEATURES),
        ('cat', OneHotEncoder(handle_unknown='ignore', sparse_output=False), CATEGORICAL_FEATURES)
    ])

    classifiers = {
        'random_forest': RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1),
        'gradient_boosting': GradientBoostingClassifier(n_estimators=100, random_state=42),
        'logistic_regression': LogisticRegression(max_iter=1000, random_state=42)
    }
    clf = classifiers.get(algorithm, classifiers['random_forest'])
    return Pipeline(steps=[('preprocessor', preprocessor), ('classifier', clf)])


def get_feature_importance(pipeline):
    clf = pipeline.named_steps['classifier']
    preprocessor = pipeline.named_steps['preprocessor']
    ohe = preprocessor.named_transformers_['cat']
    cat_feature_names = ohe.get_feature_names_out(CATEGORICAL_FEATURES).tolist()
    all_features = NUMERICAL_FEATURES + cat_feature_names

    if hasattr(clf, 'feature_importances_'):
        importances = clf.feature_importances_
    elif hasattr(clf, 'coef_'):
        importances = np.abs(clf.coef_[0])
    else:
        return []

    pairs = sorted(zip(all_features, importances), key=lambda x: x[1], reverse=True)
    return [{'feature': f, 'importance': round(float(v), 4)} for f, v in pairs[:15]]


def build_tenure_trend(df_raw, churn_series):
    """Deterministic churn rate by tenure band (not random demo noise)."""
    tenure = pd.to_numeric(df_raw.get('tenure_months'), errors='coerce').fillna(0)
    bands = pd.cut(
        tenure,
        bins=[-0.1, 6, 12, 24, 36, 48, 1200],
        labels=['0-6m', '6-12m', '12-24m', '24-36m', '36-48m', '48m+']
    )
    rates = []
    labels = []
    for label in bands.cat.categories:
        mask = bands == label
        labels.append(str(label))
        if mask.any():
            rates.append(round(float(churn_series[mask].mean() * 100), 1))
        else:
            rates.append(0.0)
    retained = [round(100 - r, 1) for r in rates]
    return {
        'months': labels,
        'churn_rate': rates,
        'retained': retained,
        'note': 'Churn rate by tenure band on the uploaded dataset (not a calendar time series).'
    }


def build_customer_list(df_raw, customer_ids, probs, preds, actuals):
    customers = []
    for i, (cid, prob, pred, actual) in enumerate(zip(customer_ids, probs, preds, actuals)):
        row = df_raw.iloc[i]
        customers.append({
            'id': cid,
            'churn_prob': round(float(prob) * 100, 1),
            'predicted': int(pred),
            'actual': int(actual),
            'risk_level': classify_risk(float(prob)),
            'tenure': int(row.get('tenure_months', 0) or 0),
            'monthly': float(row.get('monthly_charges', 0) or 0),
            'support': int(row.get('support_tickets', 0) or 0),
            'last_login': int(row.get('last_login_days', 0) or 0),
            'location': str(row.get('location', 'Unknown')),
            'contract_type': str(row.get('contract_type', 'Unknown')),
            'payment_method': str(row.get('payment_method', 'Unknown')),
            'gender': str(row.get('gender', 'Unknown')),
            'age': int(row.get('age', 0) or 0),
        })
    return sorted(customers, key=lambda x: x['churn_prob'], reverse=True)


def train_from_dataframe(df_raw, algorithm='random_forest'):
    global trained_model, training_results, customer_predictions

    customer_ids = (
        df_raw['customer_id'].astype(str).tolist()
        if 'customer_id' in df_raw.columns
        else [f'C{str(i + 1).zfill(3)}' for i in range(len(df_raw))]
    )

    if 'churn' not in df_raw.columns:
        raise ValueError('CSV must contain a "churn" column (0 or 1)')

    df = preprocess_dataframe(df_raw)
    for col in NUMERICAL_FEATURES + CATEGORICAL_FEATURES:
        if col not in df.columns:
            raise ValueError(f'Missing required column: {col}')

    X = df[NUMERICAL_FEATURES + CATEGORICAL_FEATURES]
    y = df['churn'].astype(int)
    set_feature_baseline(X)

    if y.nunique() < 2:
        raise ValueError('Need both churn classes (0 and 1) in the dataset')

    min_class = int(y.value_counts().min())
    if min_class < 2:
        raise ValueError('Need at least 2 samples per churn class for stratified split')

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    pipeline = build_pipeline(algorithm)
    pipeline.fit(X_train, y_train)
    trained_model = pipeline

    y_pred = pipeline.predict(X_test)
    y_prob = pipeline.predict_proba(X_test)[:, 1]

    acc = round(accuracy_score(y_test, y_pred) * 100, 2)
    prec = round(precision_score(y_test, y_pred, zero_division=0) * 100, 2)
    rec = round(recall_score(y_test, y_pred, zero_division=0) * 100, 2)
    f1 = round(f1_score(y_test, y_pred, zero_division=0) * 100, 2)
    try:
        auc = round(roc_auc_score(y_test, y_prob) * 100, 2)
    except ValueError:
        auc = 0.0

    cm = confusion_matrix(y_test, y_pred).tolist()
    fpr, tpr, _ = roc_curve(y_test, y_prob)
    roc_data = {
        'fpr': [round(float(x), 4) for x in fpr],
        'tpr': [round(float(x), 4) for x in tpr]
    }

    bins = list(range(0, 110, 10))
    labels = ['0-10', '10-20', '20-30', '30-40', '40-50',
              '50-60', '60-70', '70-80', '80-90', '90-100']
    prob_pct = (y_prob * 100).tolist()
    bucket_counts = pd.cut(prob_pct, bins=bins, labels=labels, right=False).value_counts().sort_index()
    prob_distribution = {'labels': labels, 'counts': [int(x) for x in bucket_counts.tolist()]}

    feature_importance = get_feature_importance(pipeline)

    all_probs = pipeline.predict_proba(X)[:, 1]
    all_preds = pipeline.predict(X)
    customer_list = build_customer_list(df_raw, customer_ids, all_probs, all_preds, y)
    customer_predictions = customer_list

    trend_data = build_tenure_trend(df_raw, y)

    high_risk = sum(1 for c in customer_list if c['risk_level'] == 'High')
    medium_risk = sum(1 for c in customer_list if c['risk_level'] == 'Medium')
    low_risk = sum(1 for c in customer_list if c['risk_level'] == 'Low')
    total_churn = int(y.sum())

    training_results = {
        'metrics': {
            'accuracy': acc,
            'precision': prec,
            'recall': rec,
            'f1_score': f1,
            'auc_roc': auc
        },
        'metrics_source': 'holdout_test_set',
        'confusion_matrix': cm,
        'roc_curve': roc_data,
        'prob_distribution': prob_distribution,
        'feature_importance': feature_importance,
        'trend_data': trend_data,
        'summary': {
            'total_customers': len(customer_list),
            'total_churn': total_churn,
            'high_risk': high_risk,
            'medium_risk': medium_risk,
            'low_risk': low_risk,
            'churn_rate': round(total_churn / len(customer_list) * 100, 1) if customer_list else 0,
            'algorithm': algorithm.replace('_', ' ').title(),
            'train_size': int(len(X_train)),
            'test_size': int(len(X_test)),
            'risk_thresholds': copy.deepcopy(ADMIN_SETTINGS['risk_thresholds']),
            'customer_scores_note': 'Customer risk scores are generated for the full uploaded dataset; reported metrics use the holdout test set only.'
        },
        'customers': customer_list
    }

    persist_model(pipeline)
    persist_results(training_results)
    return training_results


def resolve_sample_csv():
    candidates = [
        os.path.join(BASE_DIR, 'sample_data.csv'),
        os.path.join(BASE_DIR, 'public', 'sample_data.csv'),
        os.path.join(STATIC_DIR, 'sample_data.csv'),
        os.path.join(BASE_DIR, 'Data', 'sample_data.csv'),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def get_fallback_model():
    global trained_model
    if trained_model is not None:
        return trained_model

    if os.path.exists(MODEL_PATH):
        try:
            trained_model = joblib.load(MODEL_PATH)
            return trained_model
        except Exception as e:
            print('Session model load failed:', e)

    if os.path.exists(REPO_MODEL_PATH):
        try:
            trained_model = joblib.load(REPO_MODEL_PATH)
            return trained_model
        except Exception as e:
            print('Repo model load failed (version skew?):', e)

    csv_path = resolve_sample_csv()
    if csv_path:
        try:
            df_raw = pd.read_csv(csv_path)
            train_from_dataframe(df_raw, 'random_forest')
            return trained_model
        except Exception as e:
            print('Fallback training failed:', e)
    return None


def ensure_results_loaded():
    load_persisted_results()
    if training_results is None and get_fallback_model() is not None:
        load_persisted_results()
    return training_results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if os.path.isdir(DIST_DIR) and os.path.isfile(os.path.join(DIST_DIR, 'index.html')):
        return send_from_directory(DIST_DIR, 'index.html')
    return jsonify({
        'service': 'ChurnSight API',
        'message': 'Frontend is served by Vite (npm run dev) or the Vercel static build (dist/).',
        'docs': 'See README.md'
    })


@app.route('/assets/<path:filename>')
def dist_assets(filename):
    assets = os.path.join(DIST_DIR, 'assets')
    if os.path.isdir(assets):
        return send_from_directory(assets, filename)
    return jsonify({'error': 'Not found'}), 404


@app.route('/sample_data.csv')
def sample_csv():
    path = resolve_sample_csv()
    if not path:
        return jsonify({'error': 'Sample data not found'}), 404
    directory, filename = os.path.split(path)
    return send_from_directory(directory, filename)


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'ok': True,
        'auth_configured': auth_configured(),
        'production': is_production(),
        'has_results': os.path.exists(RESULTS_PATH) or training_results is not None,
        'has_model': (
            trained_model is not None
            or os.path.exists(MODEL_PATH)
            or os.path.exists(REPO_MODEL_PATH)
            or resolve_sample_csv() is not None
        )
    })


@app.route('/api/upload', methods=['POST'])
@require_firebase_auth
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    algorithm = request.form.get('algorithm', 'random_forest')

    try:
        content = file.read().decode('utf-8')
        df_raw = pd.read_csv(io.StringIO(content))
    except Exception as e:
        return jsonify({'error': f'Could not parse CSV: {str(e)}'}), 400

    try:
        results = train_from_dataframe(df_raw, algorithm)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Training failed: {str(e)}'}), 500

    return jsonify(results)


@app.route('/api/predict', methods=['POST'])
@require_firebase_auth
def predict_single():
    model = get_fallback_model()
    if model is None:
        return jsonify({'error': 'No model trained yet. Please upload data first.'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        row = {
            'age': float(data.get('age', 30)),
            'tenure_months': float(data.get('tenure_months', 12)),
            'monthly_charges': float(data.get('monthly_charges', 65)),
            'total_charges': float(data.get('total_charges', 780)),
            'num_products': float(data.get('num_products', 1)),
            'num_logins_last30': float(data.get('num_logins_last30', 10)),
            'support_tickets': float(data.get('support_tickets', 2)),
            'last_login_days': float(data.get('last_login_days', 5)),
            'gender': data.get('gender', 'Male'),
            'location': data.get('location', 'New York'),
            'contract_type': data.get('contract_type', 'Month-to-month'),
            'payment_method': data.get('payment_method', 'Credit card')
        }

        df_input = pd.DataFrame([row])
        prob = float(model.predict_proba(df_input)[0][1])
        pred = int(model.predict(df_input)[0])
        risk = classify_risk(prob)

        payload = {
            'churn_probability': round(prob * 100, 1),
            'predicted_churn': pred,
            'risk_level': risk,
            'recommendation': get_recommendation(risk, row)
        }
        if data.get('explain'):
            payload['impacts'] = explain_prediction(model, row, prob)
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def set_feature_baseline(X):
    """Median for numeric columns, most common value for categoricals."""
    global feature_baseline
    baseline = {col: float(X[col].median()) for col in NUMERICAL_FEATURES}
    for col in CATEGORICAL_FEATURES:
        mode = X[col].mode()
        baseline[col] = str(mode.iloc[0]) if len(mode) else ''
    feature_baseline = baseline


def get_feature_baseline():
    if feature_baseline is None:
        csv_path = resolve_sample_csv()
        if csv_path:
            try:
                df = preprocess_dataframe(pd.read_csv(csv_path))
                set_feature_baseline(df[NUMERICAL_FEATURES + CATEGORICAL_FEATURES])
            except Exception as e:
                print('Baseline computation failed:', e)
    return feature_baseline


def explain_prediction(model, row, prob):
    """Per-feature impact: probability change (pct points) when that one feature
    is swapped for the typical-customer value. Positive = pushes risk up."""
    baseline = get_feature_baseline()
    if not baseline:
        return []
    variants = []
    cols = NUMERICAL_FEATURES + CATEGORICAL_FEATURES
    for col in cols:
        variant = dict(row)
        variant[col] = baseline[col]
        variants.append(variant)
    probs = model.predict_proba(pd.DataFrame(variants))[:, 1]
    impacts = [
        {'feature': col, 'value': row[col], 'baseline': baseline[col],
         'impact': round((prob - float(p)) * 100, 1)}
        for col, p in zip(cols, probs)
    ]
    impacts.sort(key=lambda item: abs(item['impact']), reverse=True)
    return impacts


def get_recommendation(risk, row):
    rules = {r['id']: r for r in ADMIN_SETTINGS.get('alert_rules', [])}
    if risk == 'High':
        recs = []
        support_rule = rules.get('support_escalation', {})
        reengage_rule = rules.get('reengagement', {})
        support_threshold = float(support_rule.get('support_tickets', 4))
        login_threshold = float(reengage_rule.get('last_login_days', 21))

        if support_rule.get('enabled', True) and float(row.get('support_tickets', 0)) > support_threshold:
            recs.append('Assign a dedicated support representative')
        if reengage_rule.get('enabled', True) and float(row.get('last_login_days', 0)) > login_threshold:
            recs.append('Send a re-engagement email campaign')
        if row.get('contract_type') == 'Month-to-month':
            recs.append('Offer an annual plan discount')
        recs.append('Schedule a proactive CSM check-in call')
        return recs
    if risk == 'Medium':
        return [
            'Send satisfaction survey',
            'Offer feature training session',
            'Review account health monthly'
        ]
    return ['Continue standard engagement', 'Monitor quarterly']


@app.route('/api/results', methods=['GET'])
@require_firebase_auth
def get_results():
    results = ensure_results_loaded()
    if results is None:
        return jsonify({'error': 'No results available. Please upload data first.'}), 404
    return jsonify(results)


@app.route('/api/customers', methods=['GET'])
@require_firebase_auth
def get_customers():
    ensure_results_loaded()
    if customer_predictions is None:
        return jsonify({'error': 'No predictions available.'}), 404

    risk_filter = request.args.get('risk', None)
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))

    data = customer_predictions
    if risk_filter and risk_filter != 'All':
        data = [c for c in data if c['risk_level'] == risk_filter]

    total = len(data)
    start = (page - 1) * limit
    paged = data[start:start + limit]
    return jsonify({'customers': paged, 'total': total, 'page': page, 'limit': limit})


@app.route('/api/admin/whoami', methods=['GET'])
@require_firebase_auth
def admin_whoami():
    if not auth_configured():
        return jsonify({
            'is_admin': False,
            'email': None,
            'message': 'Auth not fully configured in this environment.'
        })
    return jsonify({
        'is_admin': is_admin_token(request.user),
        'email': request.user.get('email'),
        'uid': request.user.get('uid')
    })


@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    if not auth_configured():
        return jsonify({'error': 'Firebase admin SDK unavailable.'}), 501

    users = []
    for user in auth.list_users().iterate_all():
        provider_ids = [p.provider_id for p in user.provider_data] if user.provider_data else []
        users.append({
            'uid': user.uid,
            'email': user.email,
            'display_name': user.display_name,
            'disabled': user.disabled,
            'provider_ids': provider_ids,
            'created_at': user.user_metadata.creation_timestamp,
            'last_sign_in_at': user.user_metadata.last_sign_in_timestamp
        })
    return jsonify({'users': users})


@app.route('/api/admin/users/<uid>', methods=['PATCH'])
@require_admin
def admin_update_user(uid):
    if not auth_configured():
        return jsonify({'error': 'Firebase admin SDK unavailable.'}), 501

    data = request.get_json() or {}
    action = data.get('action')
    if action == 'disable':
        auth.update_user(uid, disabled=True)
    elif action == 'enable':
        auth.update_user(uid, disabled=False)
    elif action == 'delete':
        auth.delete_user(uid)
    else:
        return jsonify({'error': 'Invalid action'}), 400

    return jsonify({'status': 'success', 'action': action})


@app.route('/api/admin/settings', methods=['GET', 'POST'])
@require_admin
def admin_settings():
    if request.method == 'GET':
        return jsonify(ADMIN_SETTINGS)

    data = request.get_json() or {}
    thresholds = data.get('risk_thresholds')
    if thresholds:
        ADMIN_SETTINGS['risk_thresholds'].update({
            k: float(v)
            for k, v in thresholds.items()
            if k in ADMIN_SETTINGS['risk_thresholds']
        })

    alert_rules = data.get('alert_rules')
    if isinstance(alert_rules, list):
        for rule in alert_rules:
            for stored_rule in ADMIN_SETTINGS['alert_rules']:
                if stored_rule['id'] == rule.get('id'):
                    stored_rule.update({
                        'enabled': bool(rule.get('enabled', stored_rule['enabled'])),
                        **{k: v for k, v in rule.items() if k not in ['id', 'label', 'enabled']}
                    })

    saved = save_admin_settings()
    ensure_results_loaded()
    recompute_customer_risk_levels()
    payload = copy.deepcopy(ADMIN_SETTINGS)
    payload['persisted'] = saved
    return jsonify(payload)


@app.route('/api/admin/model', methods=['GET'])
@require_admin
def admin_model_status():
    ensure_results_loaded()
    has_model = trained_model is not None or os.path.exists(MODEL_PATH) or os.path.exists(REPO_MODEL_PATH)
    return jsonify({
        'trained': training_results is not None,
        'has_model': has_model,
        'customer_count': len(customer_predictions) if customer_predictions else 0,
        'algorithm': training_results['summary']['algorithm'] if training_results else None,
        'metrics_source': training_results.get('metrics_source') if training_results else None
    })


@app.route('/api/admin/model/actions', methods=['POST'])
@require_admin
def admin_model_actions():
    global trained_model, training_results, customer_predictions
    data = request.get_json() or {}
    action = data.get('action')

    if action == 'reset':
        trained_model = None
        training_results = None
        customer_predictions = None
        for path in (MODEL_PATH, RESULTS_PATH):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        return jsonify({'status': 'reset'})

    if action == 'reload_sample':
        csv_path = resolve_sample_csv()
        if not csv_path:
            return jsonify({'error': 'Sample CSV not found.'}), 500
        try:
            df_raw = pd.read_csv(csv_path)
            results = train_from_dataframe(df_raw, 'random_forest')
            return jsonify({'status': 'sample_loaded', 'summary': results['summary']})
        except Exception as e:
            return jsonify({'error': f'Could not load sample model: {e}'}), 500

    return jsonify({'error': 'Invalid action.'}), 400


if __name__ == '__main__':
    # Local API server — frontend via Vite on :5173
    app.run(debug=not is_production(), port=5050)
