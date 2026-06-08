# ChurnSight — UI/UX Design Specification
> Version 2.0 | All 4 Core Screens | React + Next.js | Web + Mobile

---

## Design System

### Color Tokens
| Token | Purpose | Hex (Light) |
|-------|---------|-------------|
| `--cs-bg-primary` | Page background | #F8F9FB |
| `--cs-bg-card` | Card surface | #FFFFFF |
| `--cs-bg-sidebar` | Sidebar background | #F2F4F7 |
| `--cs-border` | Default border | rgba(0,0,0,0.08) |
| `--cs-text-primary` | Headlines, values | #0F172A |
| `--cs-text-secondary` | Labels, metadata | #64748B |
| `--cs-accent` | Primary CTA, active nav | #2563EB |
| `--cs-risk-high` | High risk indicator | #EF4444 |
| `--cs-risk-medium` | Medium risk indicator | #F59E0B |
| `--cs-risk-low` | Low / healthy indicator | #10B981 |

### Typography
| Role | Font | Size | Weight |
|------|------|------|--------|
| Page title | IBM Plex Sans | 16px | 500 |
| Section heading | IBM Plex Sans | 12px | 500, UPPERCASE |
| KPI value | DM Serif Display | 22–28px | 400 |
| Body / table | IBM Plex Sans | 13px | 400 |
| Label / metadata | IBM Plex Sans | 11px | 400 |
| Data scores | JetBrains Mono | 13px | 400 |

### Spacing Scale
```
4px  — icon gap, badge padding
8px  — inner component gap
12px — card padding (mobile)
16px — card padding (desktop), section gap
24px — page padding, section separation
32px — major section breaks
```

### Component Specs

#### Sidebar Navigation
- Width: 200px (desktop), hidden (mobile — bottom nav)
- Background: `--cs-bg-sidebar`
- Border-right: `0.5px solid --cs-border`
- Logo: 15px / 500 weight, top 20px
- Nav item height: 36px, padding 0 16px
- Active state: left border 2px `--cs-accent`, bg `--cs-bg-card`
- Badge (alert count): 10px pill, `--cs-risk-high` bg

#### KPI Card
- Background: `--cs-bg-card`
- Border: `0.5px solid --cs-border`
- Border-radius: 12px
- Padding: 14px 16px
- Label: 11px, uppercase, `--cs-text-secondary`
- Value: 22px, `DM Serif Display`
- Delta: 11px, color matches direction (green=good, red=bad)

#### Risk Tier Badge
- Padding: 3px 8px, border-radius: 4px
- High: bg `rgba(239,68,68,0.1)`, text `#EF4444`
- Medium: bg `rgba(245,158,11,0.1)`, text `#F59E0B`
- Low: bg `rgba(16,185,129,0.1)`, text `#10B981`

#### Health Score Badge (clickable)
- Circle: 40×40px, border 2px (color = risk tier)
- Score: 14px / 500, monospace
- Tier label: 9px, uppercase
- Delta: 11px with arrow icon
- Click → expands inline breakdown panel

#### Alert Card
- Background: `--cs-bg-card`
- Left accent border: 3px solid (red=critical, amber=warning)
- Padding: 14px 16px
- Structure: icon (32px) + body (flex:1) + actions (buttons)
- Signal pills: 10px, border `--cs-border`, color `--cs-text-secondary`

---

## Screen 1 — Dashboard

### Layout
```
┌────────────┬────────────────────────────────────┐
│  Sidebar   │  Topbar (page title + actions)     │
│  200px     ├────────────────────────────────────┤
│            │  KPI Grid (4 cards)                │
│            ├───────────────────┬────────────────┤
│            │  Churn Trend Bar  │  Risk Donut    │
│            │  Chart (60%)      │  Chart (40%)   │
│            ├───────────────────┴────────────────┤
│            │  High Risk Customers Table         │
└────────────┴────────────────────────────────────┘
```

### KPI Cards (4-column grid)
1. **Churn Rate** — value + MoM delta (green if decreasing)
2. **High Risk Customers** — count + weekly delta
3. **ARR at Risk** — dollar value + MoM delta
4. **Model Accuracy** — AUC-ROC score + F1

### Churn Trend Chart
- Type: Vertical bar chart
- 12 bars (1 per month), color-coded by severity
- X-axis: month labels (Jun–May)
- Hover tooltip: exact churn %, customer count

### Risk Donut Chart
- 3 segments: High / Medium / Low
- Center label: total customers
- Legend: count per tier (right of donut)

### Customers Table
- Columns: Customer | Health Score | Churn Probability | MRR | Tenure | CSM
- Churn probability: mini inline bar + % text
- Health score: number + trend arrow + color
- Rows: hover highlight, click → customer detail drawer
- Default sort: Churn Probability descending
- Pagination: 25 rows per page

### Interactions
- "Retrain Model" → opens training modal
- Table row click → right-side drawer (customer detail)
- KPI card click → filtered table view

---

## Screen 2 — Customer Health Scores

### Layout
```
┌────────────┬────────────────────────────────────┐
│  Sidebar   │  Topbar                            │
│            ├────────────────────────────────────┤
│            │  KPI Grid (3 cards)                │
│            ├────────────────────────────────────┤
│            │  Health Score Cards (3-column)     │
│            │  ┌──────┐ ┌──────┐ ┌──────┐      │
│            │  │Card A│ │Card B│ │Card C│      │
│            │  └──────┘ └──────┘ └──────┘      │
└────────────┴────────────────────────────────────┘
```

### KPI Cards (3-column)
1. **Avg Health Score** — 0–100 with weekly delta
2. **Critical Accounts** — count (score < 40) with delta
3. **Improving Accounts** — count with delta

### Health Score Card Anatomy
```
┌─[left accent border]──────────────────┐
│ Customer name        Score (big)       │
│ Plan · MRR/month                       │
│ ─────────────────────────────────────  │
│ Churn model   [████░░░░░] 30           │
│ Login trend   [█░░░░░░░░]  8           │
│ Feature adpt  [████░░░░░] 35           │
│ Support hlth  [█░░░░░░░░] 10           │
│ Payment       [█████░░░░] 60           │
│ ─────────────────────────────────────  │
│ ⚠ Plain-English risk explanation       │
│ [View] [Assign CSM] [Start Playbook]   │
└────────────────────────────────────────┘
```
- Left border color = risk tier (red/amber/green)
- Score components: label (11px) + bar (height 3px) + numeric value
- Bar fill color = component score tier
- Explanation line: red if critical, amber if at-risk, green if healthy
- Action buttons appear on hover (desktop) or always (mobile)

### Score Formula Display
- On expand: shows formula breakdown
- Weight labels: e.g. "Churn model × 0.30"
- "What's dragging this down?" — highlights lowest 2 components

### Interactions
- Component bar hover → tooltip with raw metric value
- Card click → full customer detail page
- Filter bar: by CSM, by tier, by trend direction

---

## Screen 3 — Cohort Analysis

### Layout
```
┌────────────┬────────────────────────────────────┐
│  Sidebar   │  Topbar + Cohort Type Selector     │
│            ├────────────────────────────────────┤
│            │  KPI Grid (4 cards)                │
│            ├────────────────────────────────────┤
│            │  Retention Heatmap (full width)    │
│            ├────────────────────────────────────┤
│            │  Retention Curve  │  By Contract   │
│            │  (line chart)     │  (bar chart)   │
└────────────┴────────────────────────────────────┘
```

### Cohort Selector (top-right dropdown)
- Options: By Signup Month / By Contract Type / By Plan Tier / By Industry
- Changing selection rerenders heatmap and charts

### Retention Heatmap Spec
- Rows: cohort identifier (e.g. "Jun '24")
- Columns: Month 0 through Month N (up to 12)
- Cell: % value + background color
- Color scale: 85–100% = dark green → 0–40% = dark red
- Empty cells (future months): light gray, no value
- Hover: tooltip showing exact %, customer count, ARR retained
- Click: filters table below to that cohort's customers

### Color Scale (heatmap cells)
| Range | Background | Text |
|-------|-----------|------|
| 85–100% | `rgba(16,185,129,0.25)` | `#065F46` |
| 70–84% | `rgba(16,185,129,0.15)` | `#047857` |
| 55–69% | `rgba(245,158,11,0.2)` | `#92400E` |
| 40–54% | `rgba(239,68,68,0.15)` | `#991B1B` |
| 0–39% | `rgba(239,68,68,0.3)` | `#7F1D1D` |

### Retention Curve
- Line per cohort (max 6 cohorts shown at once)
- Color: auto-assigned from a 6-color set
- X-axis: Month 0–N
- Y-axis: % remaining, 0–100
- Legend: cohort label + final retention %

### By Contract Type Chart
- Horizontal bar chart
- One bar per contract type
- Bar fill = risk color (long contracts green, M2M red)
- Value shown right of bar

---

## Screen 4 — Alerts & Playbooks

### Layout
```
┌────────────┬────────────────────────────────────┐
│  Sidebar   │  Topbar                            │
│            ├────────────────────────────────────┤
│            │  Tabs: Open | Acknowledged | Resolved | Playbooks
│            ├──────────────────┬─────────────────┤
│            │  Alert List      │  Playbook List  │
│            │  (left ~55%)     │  (right ~45%)   │
└────────────┴────────────────────────────────────┘
```

### Tabs
- Open (N) / Acknowledged (N) / Resolved (N) / Playbooks
- Active tab: 2px bottom border, `--cs-accent`, font-weight 500
- Badge: count pill next to label

### Alert Card Spec
```
[Icon 32px] [Company Name] [Severity Badge]    [Time]
            [Signal Pill] [Signal Pill] [...]
            [Assign CSM] [Start Playbook]
```
- Left border: 3px red (critical) or 3px amber (warning)
- Signal pills: each triggered condition as separate pill
- Minimum 2 signals required to fire alert
- Actions: "Assign" → CSM picker; "Playbook" → playbook selector
- Resolved alerts show: who resolved, when, outcome (retained/churned)

### Alert Severity Rules
| Severity | Condition | Border Color |
|----------|-----------|-------------|
| Critical | 3+ signals OR payment failure | Red |
| Warning | 2 signals | Amber |
| Info | 1 signal (monitoring) | Blue |

### Playbook Card Spec
```
[Icon 28px] Playbook Name         [Active/Paused badge]
            Trigger condition
───────────────────────────────────
[1] Step description
[2] Step description
[3] Step description
───────────────────────────────────
[Edit] [Pause] [Run Manually]
```
- Steps: numbered list, 11.5px
- Icon background: `--cs-bg-info`
- "Run Manually" → confirmation modal with customer selector

### Playbook Builder (modal)
- Step types: Send Email / Create CRM Task / Send Slack / Trigger Stripe / In-App Message
- Condition types: Score below N / Signal type / Payment status / Days since login
- Delay between steps: configurable in hours/days

---

## Mobile Adaptation

### Navigation
- Sidebar hidden on mobile
- Bottom tab bar: Dashboard | Customers | Alerts | More
- 4 tabs max, icon + label

### Cards
- Stack vertically (1 column)
- KPI grid: 2×2 grid on mobile
- Health score cards: single column, collapsed by default (tap to expand)
- Heatmap: horizontal scroll, reduced to 6 months

### Alert cards
- Action buttons always visible (not hover-only)
- Swipe right = acknowledge, swipe left = assign

### Typography adjustments (mobile)
- KPI value: 18px (from 22px)
- Body text: 14px (from 13px) for readability
- Minimum tap target: 44×44px for all interactive elements

---

## Accessibility

| Check | Standard |
|-------|---------|
| Color contrast | WCAG AA minimum (4.5:1 text, 3:1 UI) |
| Focus indicators | Visible 2px outline, `--cs-accent` color |
| Screen reader | All charts have `aria-label` + table fallback |
| Risk tier encoding | Color + icon + text (never color alone) |
| Keyboard navigation | Full sidebar + table + modal keyboard support |
| Reduced motion | Animations respect `prefers-reduced-motion` |

---

*ChurnSight Design Spec v2.0 — May 2026*
