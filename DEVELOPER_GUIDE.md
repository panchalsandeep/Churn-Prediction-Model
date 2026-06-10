# ChurnSight Developer Guide

## Overview
ChurnSight is an AI-powered churn prediction dashboard. The frontend is built using Vanilla JavaScript, HTML5, and CSS3, orchestrated by Vite. The backend API is powered by Flask.

## UI Structure
The application uses a Single Page Application (SPA) architecture with the following main sections:
- `section-upload`: Initial screen to upload the dataset and train the model.
- `section-overview`: High-level dashboard showing KPIs, Churn Rate Trend, and Risk Distribution.
- `section-analytics`: Detailed model analytics including ROC Curve, Probability Distribution, and Feature Importance.
- `section-customers`: A paginated table of customers with filtering and search capabilities.
- `section-health`: Customer Health Scores generated based on risk tiers and trend metrics.
- `section-cohort`: Cohort analysis visualizing retention heatmaps based on segments.
- `section-alerts`: Automated alerts and custom playbook management.
- `section-predict`: Form for making single-customer predictions.

Navigation is handled by a sidebar on desktop and a fixed bottom navigation bar on mobile (`max-width: 768px`).

## API Contracts

### 1. `POST /api/upload`
Uploads a dataset and trains the machine learning model.
- **Request:** `multipart/form-data` containing the CSV file and `algorithm` choice.
- **Response:** JSON payload including:
  - `metrics`: Accuracy, Precision, Recall, F1 Score, AUC-ROC.
  - `summary`: Total customers, Churn rate, Risk distribution.
  - `confusion_matrix`, `roc_curve`, `prob_distribution`, `feature_importance`, `trend_data`.
  - `customers`: Array of customer objects containing their features and predictions.

### 2. `POST /api/predict`
Predicts churn for a single customer.
- **Request:** JSON object containing customer features (`age`, `tenure_months`, `monthly_charges`, etc.).
- **Response:** JSON object containing `churn_prob`, `risk_level`, and `recommendations`.

### 3. `GET /api/results`
Fetches the latest trained model results without re-uploading.

### 4. `GET /api/customers`
Fetches a paginated list of customers (fallback support if doing server-side pagination).

## Extending Alerts & Cohorts

### Alerts & Playbooks
Currently, alerts are generated dynamically from the trained customer data in `app.js` (`generateAlertsFromCustomers()`). 
To extend alerts with real-time backend data:
1. Update `app.js` to fetch alerts via a new API endpoint (e.g., `GET /api/alerts`).
2. Modify the `alertsState` structure to sync with the database.
3. Update `buildAlertsView()` to render the backend-provided alerts.

### Cohort Analysis
The cohort heatmap currently segments customers locally based on data generated during the model training phase.
To implement server-side cohort generation:
1. Expose a `GET /api/cohorts?segmentBy={segment}` endpoint on the Flask backend.
2. In `app.js`, modify `buildCohortView()` to call the endpoint and map the returned retention matrix into the `renderHeatmap()` function.

## Mobile Adaptation & Accessibility
- The app uses CSS Grid and Flexbox for responsive layouts. Breakpoints are defined in `style.css` (e.g., `@media (max-width: 768px)`).
- A mobile bottom navigation bar (`.bottom-nav`) replaces the sidebar on smaller screens.
- Chart canvases and dynamic interactive elements include appropriate ARIA roles (`role="img"`, `aria-label`) to support screen readers.
- Modals and drawers use `role="dialog"` and `aria-modal="true"`.
