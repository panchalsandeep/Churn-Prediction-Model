# Threat model

ChurnSight holds customer behaviour and revenue data for B2B companies. The data is highly sensitive and the service is
business-critical, so this document states what we defend against, how, and where the defence is thin.

Status key: **✅ implemented and tested** · **◐ implemented, not verified in this repo's environment** · **○ planned**

## Assets
1. Customer records per tenant: attributes, revenue, risk scores, explanations, actions taken.
2. Trained models and their scored snapshots (they encode the tenant's data).
3. Identity: Firebase tokens, workspace membership, invitations.
4. Encryption keys: the master key and per-workspace data keys.
5. The audit log.

## Actors
* **Anonymous internet user.** **Authenticated user of tenant A** attempting to reach tenant B.
* **Insider with a lower role** attempting privileged actions. **Compromised admin account.**
* **Malicious uploader** (hostile CSV). **Someone with read/write access to storage or backups.**
* **A database administrator** (can alter rows). **Supply chain** (a vulnerable or malicious dependency).

## Trust boundaries
Browser → API (TLS, Firebase ID token) · API → database · API → object/file storage · worker → database · API → external providers (Firebase; later connectors, LLMs).

## Threats and controls

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | Anonymous access to any API route | Every `/api/*` route except health and the demo CSV requires a verified Firebase token; a test walks the URL map and fails if any route answers anonymously | ✅ |
| T2 | Tenant A reads tenant B's data | Every query is workspace-scoped; membership is resolved server-side from the token (never from a client-supplied id); `X-Workspace` is honoured only if the user belongs to it; isolation test covers lists, detail, export, jobs, models, alerts, privacy endpoints | ✅ |
| T3 | Application bug forgets a tenant filter | PostgreSQL **row-level security** with `FORCE` and a fail-closed default (no tenant context ⇒ no rows); tenant context set per transaction from a request/job context variable; cleared after every request | ◐ (migration and tests written, CI-only) |
| T4 | Low-role user performs privileged action | Four roles enforced per route (`viewer < analyst < admin < owner`); role matrix test; admins cannot create or demote admins/owners; last owner protected | ✅ |
| T5 | Stolen password of a privileged user | MFA enforcement for admin/owner roles (`REQUIRE_MFA_ROLES`, default on in production) checked from the token's second-factor claim; SSO-required workspaces reject password logins | ✅ (claim checks; the IdP side is Firebase/Identity Platform configuration) |
| T6 | Invitation abuse | Single-use, expiring, 256-bit tokens; stored only as SHA-256; must be accepted by the invited email address | ✅ |
| T7 | Model file executes code on load (pickle) | Models are stored with `skops`; every referenced type is checked against an allow-list before anything is constructed; legacy pickles refuse to load | ✅ |
| T8 | Model artifact or snapshot swapped between tenants/versions, or tampered | AES-256-GCM with the tenant data key, context-bound (workspace + model id); test copies one tenant's artifact into another's slot and fails to load | ✅ |
| T9 | Data exposed from storage, backups or a database dump | Artifacts and snapshots are always encrypted; customer attributes and explanations are encrypted field-by-field when `ENCRYPT_FIELDS` is on (default in production) | ✅ |
| T10 | Key compromise or tenant departure | Per-tenant data keys wrapped by a master key; master-key rotation re-wraps without re-encrypting data; **crypto-shredding** on workspace deletion makes all copies unreadable | ✅ (local KEK). ◐ AWS KMS provider written, untested. Production refuses to start without an externally supplied master key |
| T11 | Customer PII ends up in models or storage | Columns that look like personal data (by name or by content) are excluded from features and from stored attributes; shown only as a warning; explicit allow-list for false positives; optional per-workspace ID pseudonymisation (HMAC) | ✅ |
| T12 | "Delete my data" cannot be honoured | Erase by customer id from live tables **and** from every retained snapshot; DSAR export; full workspace deletion with key destruction | ✅ |
| T13 | Audit log altered to hide an action | Hash-chained entries (each commits to the previous), append-only triggers in SQLite and PostgreSQL, a verify endpoint and NDJSON export | ✅ chain + SQLite triggers. ◐ PostgreSQL triggers |
| T14 | Leakage inflating model quality (an integrity threat to customers' decisions) | Group-aware validation so a customer is never in both train and test; a test proves memorisation is caught | ✅ |
| T15 | Silently wrong scores from malformed batches | Scoring checks the batch against the model's data contract; missing important columns, unreadable numbers and null spikes are reported or rejected | ✅ |
| T16 | Hostile CSV (formula injection, huge files, odd encodings) | Exports neutralise formulas; upload byte and row caps; encoding fallback; schema inference never evaluates content | ✅ for injection/caps. ○ zip/CSV-bomb and column-count limits |
| T17 | Brute force and abuse | Per-user rate limits, stricter for training/scoring; per-workspace job admission; bounded queue | ✅ single process. ○ shared limiter (Redis) behind a load balancer |
| T18 | Known-vulnerable dependencies | Minimum versions raised for every package with a published advisory; `npm audit` is clean; CI runs `pip-audit`, `npm audit` and Trivy on every push | ✅ today's findings. CI keeps it current |
| T19 | Cross-site and clickjacking attacks | Security headers on the API and the static frontend (nosniff, frame deny, referrer policy, HSTS over HTTPS) | ✅. ○ Content-Security-Policy |
| T20 | Secrets in the repository | No secrets committed; master key only from environment/KMS in production; the Firebase *web* key is public by design and must be restricted by domain in the Firebase console | ✅ / action needed outside the repo |
| T21 | Schema changes corrupt production data | Versioned migrations (Alembic) with drift test between models and migrations; a legacy-database adoption test | ✅ |
| T22 | One tenant exhausts shared capacity | One active job per workspace, concurrency and queue caps, size-aware training plan, capped deep paging, explanation concurrency gate | ✅ |

## Residual risks (known, accepted for now)
* **Redis rate limiting is tested only against an in-memory fake** (`fakeredis`), not a real Redis. If Redis is down the limit falls back to per-process counting, so limits loosen rather than requests failing.
* **Embedded job threads cannot be force-killed.** A runaway job in the web process is only stopped cooperatively; use dedicated workers (`python -m churn.worker`), which exit on timeout, for untrusted workloads.
* **Queued uploads sit in blob storage (encrypted) until their job ends.** They are deleted on completion, cancellation and exhausted retries; a crash between steps can leave an orphan until the workspace is deleted.
* **Backups contain ciphertext only; the master key is out of band.** Losing the key loses the backup. Restores have been tested only for SQLite.
* **PostgreSQL RLS has not been run locally.** The Docker daemon was unavailable during development and no embedded Postgres exists for this platform. The Postgres CI job is therefore marked experimental until its first green run. Treat T3 and the PostgreSQL half of T13 as unproven.
* **Firebase is a trusted third party** for authentication, MFA and SSO. A compromise there is a compromise here.
* **Outbound requests are a new attack surface (SSRF).** Webhook and connector URLs are typed by customers. `safehttp.py` resolves once, requires every address to be public, connects to that address, refuses redirects and caps size and time; the checks are unit-tested including DNS-rebinding style answers, but have not had a third-party review. IPv6 and unusual DNS setups deserve attention in a pen test.
* **Alert channels send customer information to third parties** (Slack, email providers) only when a channel opts in to account details; the default digest has counts and totals only.
* **Connector credentials** (API tokens for the customer's vendor) are sealed with the workspace key but must be decrypted in the worker to be used, so a compromised worker can read them. Prefer read-only, narrowly scoped tokens.
* **Ingest API keys** are bearer secrets with no IP allow-list or expiry; revoke on leak. Scope is limited to appending rows to the four tables.
* **Local key provider** keeps the master key in an environment variable. Use KMS/HSM in production (provider written, unverified).
* **Field encryption disables attribute search** by design; searching is by customer id only.
* **No Content-Security-Policy yet**, and no penetration test has been performed. Both are required before handling real customer data at scale.
* **LLM features do not exist yet.** When added, ticket text is untrusted input (prompt injection) and must never be able to trigger actions (see ROADMAP §6).

## Verification
`tests/test_phase0_*.py` cover T1, T2, T4, T6–T9, T11–T15, T21; `tests/test_phase0_postgres.py` covers T3 and the PostgreSQL audit triggers when run against Postgres.
