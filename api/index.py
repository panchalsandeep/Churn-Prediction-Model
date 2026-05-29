from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                              f1_score, roc_auc_score, confusion_matrix,
                              roc_curve)
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder
import joblib
import os
import json
import io
import warnings
warnings.filterwarnings('ignore')

import firebase_admin
from firebase_admin import credentials, auth
from functools import wraps

# Dynamic path resolution to support both serverless (Vercel) and local dev environments
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
static_dir = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, static_folder=static_dir, static_url_path='')
CORS(app)

# Initialize Firebase Admin SDK
# On local dev or if no configurations are set, this will fail gracefully and run in Dev Mode
firebase_initialized = False
try:
    firebase_admin.initialize_app()
    firebase_initialized = True
    print("[INFO] Firebase Admin SDK successfully initialized.")
except Exception as e:
    print(f"[WARNING] Firebase Admin SDK initialization failed: {e}. Running in Dev Mode (Bypass Auth).")

def require_firebase_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Enforce validation only if Firebase configuration is set in the environment
        has_firebase_config = os.environ.get('FIREBASE_PROJECT_ID') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        
        if not firebase_initialized or not has_firebase_config:
            return f(*args, **kwargs)
            
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized: Missing or invalid token format'}), 401
            
        token = auth_header.split('Bearer ')[1]
        try:
            decoded_token = auth.verify_id_token(token)
            request.user = decoded_token
        except Exception as e:
            return jsonify({'error': f'Unauthorized: Token verification failed: {str(e)}'}), 401
            
        return f(*args, **kwargs)
    return decorated_function

MODEL_PATH = '/tmp/churn_model.pkl'
ENCODERS_PATH = '/tmp/encoders.pkl'

# ── Global state ──────────────────────────────────────────────────────────────
trained_model = None
feature_names  = None
training_results = None
customer_predictions = None

CATEGORICAL_FEATURES = ['gender', 'location', 'contract_type', 'payment_method']
NUMERICAL_FEATURES   = ['age', 'tenure_months', 'monthly_charges', 'total_charges',
                        'num_products', 'num_logins_last30', 'support_tickets',
                        'last_login_days']

# ── Helpers ───────────────────────────────────────────────────────────────────

def preprocess_dataframe(df):
    """Drop ID column, coerce types, fill missing values."""
    df = df.copy()
    if 'customer_id' in df.columns:
        df = df.drop(columns=['customer_id'])

    for col in NUMERICAL_FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df[NUMERICAL_FEATURES] = df[NUMERICAL_FEATURES].fillna(
        df[NUMERICAL_FEATURES].median(numeric_only=True)
    )
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(df[col].mode()[0] if not df[col].mode().empty else 'Unknown')

    return df


def build_pipeline(algorithm='random_forest'):
    num_cols = [c for c in NUMERICAL_FEATURES]
    cat_cols = [c for c in CATEGORICAL_FEATURES]

    preprocessor = ColumnTransformer(transformers=[
        ('num', StandardScaler(), num_cols),
        ('cat', OneHotEncoder(handle_unknown='ignore', sparse_output=False), cat_cols)
    ])

    classifiers = {
        'random_forest':      RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1),
        'gradient_boosting':  GradientBoostingClassifier(n_estimators=100, random_state=42),
        'logistic_regression': LogisticRegression(max_iter=1000, random_state=42)
    }
    clf = classifiers.get(algorithm, classifiers['random_forest'])

    return Pipeline(steps=[('preprocessor', preprocessor), ('classifier', clf)])


def get_feature_importance(pipeline, num_cols, cat_cols):
    """Return sorted feature-importance list."""
    clf = pipeline.named_steps['classifier']
    preprocessor = pipeline.named_steps['preprocessor']

    # Build expanded cat feature names
    ohe = preprocessor.named_transformers_['cat']
    cat_feature_names = ohe.get_feature_names_out(cat_cols).tolist()
    all_features = num_cols + cat_feature_names

    if hasattr(clf, 'feature_importances_'):
        importances = clf.feature_importances_
    elif hasattr(clf, 'coef_'):
        importances = np.abs(clf.coef_[0])
    else:
        return []

    pairs = sorted(zip(all_features, importances), key=lambda x: x[1], reverse=True)
    return [{'feature': f, 'importance': round(float(v), 4)} for f, v in pairs[:15]]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(static_dir, 'index.html')


@app.route('/api/upload', methods=['POST'])
@require_firebase_auth
def upload_file():
    """Receive a CSV file, train the model, return full dashboard payload."""
    global trained_model, feature_names, training_results, customer_predictions

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    algorithm = request.form.get('algorithm', 'random_forest')

    try:
        content = file.read().decode('utf-8')
        df_raw = pd.read_csv(io.StringIO(content))
    except Exception as e:
        return jsonify({'error': f'Could not parse CSV: {str(e)}'}), 400

    # Store customer IDs for later
    customer_ids = df_raw['customer_id'].tolist() if 'customer_id' in df_raw.columns else \
                   [f'C{str(i+1).zfill(3)}' for i in range(len(df_raw))]

    if 'churn' not in df_raw.columns:
        return jsonify({'error': 'CSV must contain a "churn" column (0 or 1)'}), 400

    df = preprocess_dataframe(df_raw)

    # Ensure all expected columns exist
    for col in NUMERICAL_FEATURES + CATEGORICAL_FEATURES:
        if col not in df.columns:
            return jsonify({'error': f'Missing required column: {col}'}), 400

    X = df[NUMERICAL_FEATURES + CATEGORICAL_FEATURES]
    y = df['churn'].astype(int)

    X_train, X_test, y_train, y_test, ids_train, ids_test = train_test_split(
        X, y, customer_ids, test_size=0.2, random_state=42, stratify=y
    )

    pipeline = build_pipeline(algorithm)
    pipeline.fit(X_train, y_train)
    trained_model = pipeline

    # Predictions & probabilities on test set
    y_pred       = pipeline.predict(X_test)
    y_prob_all   = pipeline.predict_proba(X_test)[:, 1]

    # Metrics
    acc  = round(accuracy_score(y_test, y_pred) * 100, 2)
    prec = round(precision_score(y_test, y_pred, zero_division=0) * 100, 2)
    rec  = round(recall_score(y_test, y_pred, zero_division=0) * 100, 2)
    f1   = round(f1_score(y_test, y_pred, zero_division=0) * 100, 2)
    auc  = round(roc_auc_score(y_test, y_prob_all) * 100, 2)

    cm = confusion_matrix(y_test, y_pred).tolist()

    # ROC curve
    fpr, tpr, _ = roc_curve(y_test, y_prob_all)
    roc_data = {
        'fpr': [round(float(x), 4) for x in fpr],
        'tpr': [round(float(x), 4) for x in tpr]
    }

    # Churn probability distribution (buckets of 10 %)
    bins    = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    labels  = ['0-10', '10-20', '20-30', '30-40', '40-50',
                '50-60', '60-70', '70-80', '80-90', '90-100']
    prob_pct = (y_prob_all * 100).tolist()
    bucket_counts = pd.cut(prob_pct, bins=bins, labels=labels, right=False).value_counts().sort_index()
    prob_distribution = {'labels': labels, 'counts': bucket_counts.tolist()}

    # Feature importance
    num_cols = NUMERICAL_FEATURES
    cat_cols = [c for c in CATEGORICAL_FEATURES if c in X.columns]
    feature_importance = get_feature_importance(pipeline, num_cols, cat_cols)

    # Customer list (full dataset predictions)
    all_probs = pipeline.predict_proba(X)[:, 1]
    all_preds = pipeline.predict(X)

    customer_list = []
    for i, (cid, prob, pred, actual) in enumerate(zip(customer_ids, all_probs, all_preds, y)):
        row = df_raw.iloc[i] if i < len(df_raw) else {}
        customer_list.append({
            'id':           cid,
            'churn_prob':   round(float(prob) * 100, 1),
            'predicted':    int(pred),
            'actual':       int(actual),
            'risk_level':   'High' if prob >= 0.7 else ('Medium' if prob >= 0.4 else 'Low'),
            'tenure':       int(row.get('tenure_months', 0)) if hasattr(row, 'get') else 0,
            'monthly':      float(row.get('monthly_charges', 0)) if hasattr(row, 'get') else 0,
            'support':      int(row.get('support_tickets', 0)) if hasattr(row, 'get') else 0,
            'last_login':   int(row.get('last_login_days', 0)) if hasattr(row, 'get') else 0,
        })

    customer_list_sorted = sorted(customer_list, key=lambda x: x['churn_prob'], reverse=True)
    customer_predictions = customer_list_sorted

    # Monthly churn trend (simulated from probability buckets for demo)
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    np.random.seed(42)
    base_churn = sum(y) / len(y) * 100
    trend_data = {
        'months': months,
        'churn_rate': [round(base_churn + np.random.uniform(-5, 5), 1) for _ in months],
        'retained':   [round(100 - base_churn + np.random.uniform(-3, 3), 1) for _ in months]
    }

    # Summary counts
    high_risk   = sum(1 for c in customer_list if c['risk_level'] == 'High')
    medium_risk = sum(1 for c in customer_list if c['risk_level'] == 'Medium')
    low_risk    = sum(1 for c in customer_list if c['risk_level'] == 'Low')
    total_churn = sum(y)

    training_results = {
        'metrics': {
            'accuracy':  acc,
            'precision': prec,
            'recall':    rec,
            'f1_score':  f1,
            'auc_roc':   auc
        },
        'confusion_matrix': cm,
        'roc_curve':        roc_data,
        'prob_distribution': prob_distribution,
        'feature_importance': feature_importance,
        'trend_data':        trend_data,
        'summary': {
            'total_customers': len(customer_list),
            'total_churn':     total_churn,
            'high_risk':       high_risk,
            'medium_risk':     medium_risk,
            'low_risk':        low_risk,
            'churn_rate':      round(total_churn / len(customer_list) * 100, 1),
            'algorithm':       algorithm.replace('_', ' ').title()
        },
        'customers': customer_list_sorted
    }

    # Save model
    joblib.dump(pipeline, MODEL_PATH)

    return jsonify(training_results)


def get_fallback_model():
    global trained_model
    if trained_model is not None:
        return trained_model
    
    # 1. Try loading from temporary session model
    if os.path.exists(MODEL_PATH):
        try:
            trained_model = joblib.load(MODEL_PATH)
            return trained_model
        except:
            pass
            
    # 2. Try loading pre-trained repository model packaged by Vercel
    repo_model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'churn_model.pkl')
    if os.path.exists(repo_model_path):
        try:
            trained_model = joblib.load(repo_model_path)
            return trained_model
        except:
            pass

    # 3. Fallback: train on sample_data.csv instantly
    try:
        csv_path = os.path.join(BASE_DIR, 'sample_data.csv')
        if not os.path.exists(csv_path):
            csv_path = os.path.join(static_dir, 'sample_data.csv')
            
        if os.path.exists(csv_path):
            df_raw = pd.read_csv(csv_path)
            df = preprocess_dataframe(df_raw)
            X = df[NUMERICAL_FEATURES + CATEGORICAL_FEATURES]
            y = df['churn'].astype(int)
            pipeline = build_pipeline('random_forest')
            pipeline.fit(X, y)
            trained_model = pipeline
            try:
                joblib.dump(pipeline, MODEL_PATH)
            except:
                pass
            return trained_model
    except Exception as e:
        print("Fallback training failed:", e)
    return None


@app.route('/api/predict', methods=['POST'])
@require_firebase_auth
def predict_single():
    """Predict churn for a single customer submitted via form."""
    model = get_fallback_model()
    if model is None:
        return jsonify({'error': 'No model trained yet. Please upload data first.'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        row = {
            'age':              float(data.get('age', 30)),
            'tenure_months':    float(data.get('tenure_months', 12)),
            'monthly_charges':  float(data.get('monthly_charges', 65)),
            'total_charges':    float(data.get('total_charges', 780)),
            'num_products':     float(data.get('num_products', 1)),
            'num_logins_last30': float(data.get('num_logins_last30', 10)),
            'support_tickets':  float(data.get('support_tickets', 2)),
            'last_login_days':  float(data.get('last_login_days', 5)),
            'gender':           data.get('gender', 'Male'),
            'location':         data.get('location', 'New York'),
            'contract_type':    data.get('contract_type', 'Month-to-month'),
            'payment_method':   data.get('payment_method', 'Credit card')
        }

        df_input = pd.DataFrame([row])
        prob  = model.predict_proba(df_input)[0][1]
        pred  = int(model.predict(df_input)[0])
        risk  = 'High' if prob >= 0.7 else ('Medium' if prob >= 0.4 else 'Low')

        return jsonify({
            'churn_probability': round(float(prob) * 100, 1),
            'predicted_churn':   pred,
            'risk_level':        risk,
            'recommendation':    get_recommendation(risk, row)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def get_recommendation(risk, row):
    if risk == 'High':
        recs = []
        if float(row.get('support_tickets', 0)) > 4:
            recs.append('Assign a dedicated support representative')
        if float(row.get('last_login_days', 0)) > 20:
            recs.append('Send a re-engagement email campaign')
        if row.get('contract_type') == 'Month-to-month':
            recs.append('Offer an annual plan discount')
        recs.append('Schedule a proactive CSM check-in call')
        return recs
    elif risk == 'Medium':
        return ['Send satisfaction survey', 'Offer feature training session',
                'Review account health monthly']
    else:
        return ['Continue standard engagement', 'Monitor quarterly']


@app.route('/api/results', methods=['GET'])
@require_firebase_auth
def get_results():
    if training_results is None:
        return jsonify({'error': 'No results available. Please upload data first.'}), 404
    return jsonify(training_results)


@app.route('/api/customers', methods=['GET'])
@require_firebase_auth
def get_customers():
    if customer_predictions is None:
        return jsonify({'error': 'No predictions available.'}), 404
    risk_filter = request.args.get('risk', None)
    page  = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))

    data = customer_predictions
    if risk_filter and risk_filter != 'All':
        data = [c for c in data if c['risk_level'] == risk_filter]

    total = len(data)
    start = (page - 1) * limit
    paged = data[start:start + limit]

    return jsonify({'customers': paged, 'total': total, 'page': page, 'limit': limit})


if __name__ == '__main__':
    app.run(debug=True, port=5050)
