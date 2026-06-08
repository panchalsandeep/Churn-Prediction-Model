# ChurnSight Project Plan

## 1. Current Implementation Status

### Frontend
- Existing app is a Vite-powered static SPA with `index.html`, `src/main.js`, and `static/style.css`.
- Current screens implemented:
  - Upload & Train Model
  - Overview Dashboard with KPI cards, churn trend, risk donut
  - Analytics view with ROC, probability distribution, feature importance, confusion matrix
  - Customer table with search, risk filters, pagination
  - Predict Single Customer form and result view
- Firebase Authentication flow exists with email/password and Google sign-in.
- Frontend currently manages app state, charts, and API calls in vanilla JavaScript.

### Backend
- Backend is a Flask API in `api/index.py`.
- Supported endpoints:
  - `POST /api/upload` — train model from uploaded CSV and return dashboard payload
  - `POST /api/predict` — predict single-customer churn
  - `GET /api/results` — retrieve last training results
  - `GET /api/customers` — paginated customer list with optional risk filter
- Backend includes fallback model loading from `churn_model.pkl` or `sample_data.csv`.

## 2. Gap Analysis Against Design Spec

### Matches
- Overview/dashboard card layout and trend/risk charts are present.
- Analytics section exists and is already populated with model performance visuals.
- Customer list and single-prediction functionality are implemented.
- Authentication overlay exists, though the design spec is not strictly enforced.

### Missing or incomplete
- `Customer Health Scores` screen is not present.
- `Cohort Analysis` screen is not present.
- `Alerts & Playbooks` screen is not present.
- Dashboard interactions from the spec (card click filters, customer drawer, retrain modal) are not fully implemented.
- Mobile-specific UI adaptation and layout behaviors are not yet verified or completed.
- Accessibility improvements required by the spec are not fully covered.
- The existing app is vanilla JS, while the spec calls out React + Next.js; this plan assumes continuing from current implementation unless the user wants a rewrite.

## 3. Proposed Implementation Plan

### Goal
Continue from the current working app and implement the pending design-spec features in phased increments, while preserving the existing functional upload/train and analytics flow.

### Phase 1 — Stabilize current core app
- Review and clean up `src/main.js` and `index.html` for consistent naming/structure.
- Ensure all current dashboard charts and customer table behaviors are stable.
- Add missing model status / retrain UX as a modal or panel.
- Harden API error handling and user messages.

### Phase 2 — Build Customer Health Scores view
- Add a new screen for health score cards.
- Create reusable score card components in HTML/CSS and populate from customer predictions.
- Add score breakdown bars and risk tier styling.
- Implement interaction for card expansion and formula breakdown.
- Add filters by CSM, tier, trend direction (can use simulated or derived data).

### Phase 3 — Build Cohort Analysis view
- Add a cohort selector and cohort-based view layout.
- Implement a retention heatmap using HTML/CSS and chart.js if needed.
- Add retention curve and contract-type charts.
- Use demo cohort segmentation derived from the loaded dataset or simulated cohorts.
- Add click-to-filter behavior for cohort cells.

### Phase 4 — Build Alerts & Playbooks view
- Add a new view for alerts and playbooks.
- Create alert cards with severity badges, signal pills, and action buttons.
- Create playbook cards and builder UI placeholders.
- Add tabbed navigation for Open / Acknowledged / Resolved / Playbooks.
- Populate with sample alert data and allow basic state changes.

### Phase 5 — Mobile adaptation and accessibility
- Ensure responsive layout across breakpoints.
- Implement mobile bottom tab navigation and card stacking.
- Update UI spacing, tap target sizes, and mobile-specific behaviors.
- Add ARIA attributes, focus outlines, and keyboard support for key controls.
- Verify contrast and text readability.

### Phase 6 — Documentation and handoff
- Add project documentation for the completed UI structure and developer notes.
- Document API contracts, screen mapping, and how to extend the alerts/cohorts screens.
- Create a simple `PROJECT_PLAN.md` and `CHANGELOG` if needed.

## 4. Detailed Task List

### Phase 1 Tasks
1. Create a `contents` section for the app to map current screens to spec screens.
2. Add `Re-train Model` modal and make it accessible from the overview.
3. Add KPI-card click filters for customer table by risk levels.
4. Add customer detail drawer or modal on row click.
5. Validate the existing login flow and ensure the sidebar/profile state is stable.

### Phase 2 Tasks
1. Add a `Health Scores` navigation item.
2. Build the UI for risk-tier score cards.
3. Derive score components for each customer from available features.
4. Add “What’s dragging this down?” summary for low-score cards.
5. Add CSM filters and trend filters.

### Phase 3 Tasks
1. Add a `Cohort Analysis` navigation item.
2. Implement a cohort selector dropdown.
3. Build retention heatmap and chart panels.
4. Simulate cohort data if dataset lacks explicit cohort metadata.
5. Add click-to-filter behavior.

### Phase 4 Tasks
1. Add an `Alerts & Playbooks` navigation item.
2. Build tabbed alert list UI.
3. Create sample alerts with severity rules.
4. Build playbook cards and a simplified builder modal.
5. Add actions for acknowledge, resolve, and run playbook.

### Phase 5 Tasks
1. Review and update CSS for mobile breakpoints.
2. Add bottom nav for mobile and hide the sidebar.
3. Ensure all interactive controls are keyboard usable.
4. Add ARIA roles and labels to charts, tables, and dialogs.
5. Validate with viewport sizes down to 320px.

## 5. Success Criteria
- The app includes all four spec screens with matching layout and interactions.
- The dashboard is fully interactive and the customer table is filterable.
- Health scores, cohort analysis, and alerts/playbooks are implemented.
- The application works on desktop and mobile layouts.
- Authentication remains usable and model training/prediction flows work.

## 6. Assumptions
- We continue with the existing vanilla JS + Vite architecture.
- The current Flask API endpoints remain the data source.
- Cohort and alerts data may initially use derived or sample data for UI functionality.
- A full React/Next.js rewrite is not part of this immediate execution plan unless requested.

## 7. Next Step
- Continue implementation through Phase 2 and Phase 3, focusing on UI polish and analytics refinement.
- Validate the app in-browser and confirm layout across mobile breakpoints.
- If desired, convert the current architecture into a dedicated `README` or developer guide.

## 8. Phase 1 Progress
- Added navigation entries for `Health Scores`, `Cohort Analysis`, and `Alerts & Playbooks`.
- Created new screen layouts for health score cards, cohort heatmap/chart panels, and alerts/playbooks UI.
- Added a customer detail drawer and KPI interactions to filter the customer table.
- Connected cohort and alert views to derived data from the trained customer dataset.
- Included a retrain button to jump back to the upload screen.

## 9. Recent Updates
- Corrected the stylesheet reference for `index.html` so the app uses `static/style.css` successfully.
- Added alert tab styling and dynamic tab summary text for the `Alerts & Playbooks` screen.
- Improved cohort summary messaging to reflect the selected cohort segmentation.
- Strengthened health score calculations to average across the current filter scope.
- Added overlay click-to-close behavior for the customer detail drawer.
