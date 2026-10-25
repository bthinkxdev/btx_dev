# BThinkX Business OS — setup, deployment and operations

The Business OS is one Django app, `crm`, inside this project. It is mounted at `/crm/`.
Each business area is its own folder under `crm/modules/<module>/` (services, forms, views,
urls, jobs) with matching templates under `crm/templates/crm/<module>/`.

| Folder | What it covers |
| --- | --- |
| `auth`, `common` | Sign-in, two-factor, profile, notifications, global search, approvals inbox, attachments |
| `leads`, `sales` | Leads, inbound sources, import, duplicates, My Day, follow-ups, pipeline, targets |
| `clients`, `quotations` | Client directory, service catalogue, quotations and the public quotation page |
| `payments`, `billing` | Payment links, gateway webhooks, manual payments, refunds, invoices, credit notes, receivables, TDS |
| `accounting` | Chart of accounts, journals, ledger, trial balance, P&L, balance sheet, cash flow, GST/TDS reports, periods, banking and reconciliation |
| `contracts`, `projects`, `expenses` | Contracts and recurring billing, projects, tasks, timesheets, tickets, expenses, vendors |
| `hr`, `payroll` | Employees, attendance, leave, payroll runs, payslips, commissions |
| `reports` | Dashboards (owner, sales, finance, delivery, HR) and the report catalogue with scheduled reports |
| `administration` | Company, policy, tax and numbering, payment gateway, users, roles, teams, templates, jobs and health, audit log, security events |

## 1. First-time setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # if present; otherwise create .env with the variables below
python manage.py migrate
python manage.py crm_bootstrap            # roles, chart of accounts, number series, templates, leave types
python manage.py createsuperuser          # first Owner account
```

Sign in at `/crm/login/`. Then, in order:

1. **Administration → Company settings**: legal name, GSTIN, PAN, state, address, bank details.
2. **Administration → Tax and numbering**: GST behaviour and document prefixes (fixed per financial year once used).
3. **Administration → Teams and departments**, then **Users and roles**: add people. Each new user gets a
   one-time password and must set their own at first sign-in.
4. **Administration → Payment gateway**: provider keys (write-only), then *Test connection*. Only the Owner can
   switch to live mode.
5. **Clients → Service catalogue**: services, SAC codes and GST rates.

For a demo database (never on production): `python manage.py crm_demo_data --password 'a-strong-password'`.

## 2. Environment variables

| Variable | Required in production | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Yes | Django signing key. Also derives customer link tokens. |
| `DJANGO_DEBUG` | Yes (`False`) | Turns on secure cookies and HTTPS settings when false. |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | Yes | Your domain(s), e.g. `https://erp.bthinkx.com`. |
| `DB_ENGINE=postgres`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Yes | PostgreSQL is the production database. SQLite is for local development only. |
| `CRM_ENCRYPTION_KEY` | Yes | Fernet key for gateway secrets at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it outside the database; losing it means re-entering gateway keys. |
| `CRM_PRIVATE_MEDIA_ROOT` | Yes | Folder for invoices, receipts, payslips and uploads. Never serve it directly; the app serves files through signed, permission-checked links. Back it up with the database. |
| `CRM_MAX_UPLOAD_MB` | No (10) | Upload size limit. |
| `CASHFREE_APP_ID`, `CASHFREE_SECRET_KEY`, `CASHFREE_ENV` | No | Fallback gateway keys when none are saved in Payment gateway settings. `CASHFREE_ENV=production` for live. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | Yes | Outgoing mail (quotations, invoices, reminders, scheduled reports). Every message is logged under Message templates → Delivery log. |
| `REDIS_URL` | Recommended | Cache, rate limiting and (optionally) Celery. |
| `CELERY_BROKER_URL` | No | Send email and run jobs through Celery instead of cron/threads. |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` | Recommended | HTTPS enforcement behind your proxy. |
| `LOG_LEVEL` | No (INFO) | Logs include a request id that also appears on audit rows. |

Inbound lead sources (website forms, Meta lead ads, telephony) each have their own secret key, created and
rotated under **Leads → Sources and campaigns**; no environment variable is needed.

## 3. Scheduled jobs

All automation (SLA alerts, follow-up escalation, payment-link expiry, gateway back-stop polling, dunning,
recurring invoices, renewals, deferred revenue, approvals reminders, audit checkpoints, nightly integrity
checks, scheduled reports) runs through one command. Each run is recorded with an idempotency key, so a
duplicate cron fire or a retry never creates a second invoice, payment or email.

```cron
*/5 * * * *  cd /srv/btx_dev && venv/bin/python manage.py crm_run_jobs >> /var/log/crm-jobs.log 2>&1
```

Daily jobs run once per day inside that same command. With Celery, use the beat schedule in
`config/settings.py` instead of cron. Watch **Administration → Jobs and health**: failed runs can be
retried or marked resolved with a reason, and every job can be run on demand.

## 4. PostgreSQL hardening

Migrations install triggers that refuse any UPDATE, DELETE or TRUNCATE on `crm_auditlog` and
`crm_auditcheckpoint`. For defence in depth, run the app as a role without those privileges:

```sql
REVOKE UPDATE, DELETE, TRUNCATE ON crm_auditlog, crm_auditcheckpoint FROM bthinkx_app;
GRANT SELECT, INSERT ON crm_auditlog, crm_auditcheckpoint TO bthinkx_app;
```

Posted journal lines are protected by a database constraint (debits must equal credits) and by the
application: posted entries are never edited, only reversed.

Because of the audit triggers, `manage.py flush` cannot empty the audit tables. Restore from backup instead.

## 5. Backups and restore

Back up the PostgreSQL database and `CRM_PRIVATE_MEDIA_ROOT` together, at least daily, and keep copies off
the server. Test a restore every quarter:

```bash
pg_dump -Fc -d bthinkx > bthinkx-$(date +%F).dump
tar czf private-media-$(date +%F).tgz "$CRM_PRIVATE_MEDIA_ROOT"
# restore
pg_restore -d bthinkx_restore bthinkx-YYYY-MM-DD.dump
```

After a restore, open **Jobs and health → Run checks now**. The integrity checks confirm journals balance,
the receivables and payables ledgers match open invoices and bills, invoice balances match their payments and
credit notes, no payment is over-allocated, invoice and credit-note numbers have no gaps, and the audit log
matches its hash checkpoints.

## 6. Deploy checklist

```bash
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py crm_bootstrap        # safe to re-run; adds new defaults only
python manage.py collectstatic --noinput
python manage.py check --deploy
# restart the app server (gunicorn/uwsgi) and confirm cron is running
```

The compiled stylesheet `crm/static/crm/css/crm.css` is committed. After changing templates or
`crm/frontend/src/*.css`, rebuild it with `cd crm/frontend && npm install && npm run build`.

## 7. Tests

```bash
python manage.py test crm                                 # SQLite
DB_ENGINE=postgres DB_PASSWORD=... python manage.py test crm   # PostgreSQL (run before every release)
```

The suite includes the acceptance tests from the plan (webhook idempotency, maker-checker, ledger balance,
AR tie-out, payroll locks, permission scope, dashboard card totals equal list totals, audit rows cannot be
changed in the database) and a crawl that opens every page in the app as every role and fails on any server
error.
