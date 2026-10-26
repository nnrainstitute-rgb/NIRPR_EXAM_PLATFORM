# Railway deployment and running costs

Checked against Railway documentation on 26 October 2026.

## Plan and cost

The owner chose **Pro with SMTP**. Pro is a $20/month minimum commitment including
$20 of usage; $25 of usage means a $25 Railway bill, not $45. The lower paid plan,
Hobby, starts at $5 including $5 usage. Free offers $1/month of resources and is
better suited to evaluation than scheduled certification exams.

RAM costs $10/GB-month, CPU $20/vCPU-month, outbound traffic $0.05/GB, and volume
storage $0.15/GB-month. These are consumption rates, not reserved capacity fees.
For illustration, 0.5 GB of average combined RAM for an entire month costs $5
in RAM alone; CPU, storage, backups and traffic add to this. Actual usage and
candidate concurrency must be measured; twice-monthly exams do not guarantee a
particular bill. Email-provider and domain fees are separate.

Sources: [Pricing](https://docs.railway.com/pricing),
[plans](https://docs.railway.com/pricing/plans).

SMTP is blocked on Free, Trial and Hobby. Pro supports SMTP after redeploying.
The app retains its existing SMTP integration; no new email provider is added.
Source: [Outbound networking](https://docs.railway.com/networking/outbound-networking).

## Deploy this repository

1. Create a Railway project and a PostgreSQL service. Use PostgreSQL 18 if importing
   the current local PostgreSQL 18 dump. Keep the database and web service in the
   same region. Connect the GitHub repository `Olumide66/NIRPR_EXAM_PLATFORM`.
2. Set the web service Root Directory to `/nirpr-exam-platform`. Set its config
   file path to `/nirpr-exam-platform/railway.toml` if Railway does not discover it.
   The Dockerfile runs one Uvicorn worker, serving frontend and API together on
   Railway's `PORT`. No separate frontend, Redis or Celery service is needed for
   this single-worker setup.
3. Attach a web-service volume at `/data`. Set `UPLOAD_DIR=/data/uploads` to persist
   receipts and signatures across deployments. Enable daily volume backups and
   database backups. Database storage is on the PostgreSQL service's own volume.
4. Generate the web service's public domain, then configure these Variables:

   | Variable | Value |
   | --- | --- |
   | `APP_ENV` | `production` |
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (replace `Postgres` with the actual database service name) |
   | `SECRET_KEY` | Private random value, at least 32 characters |
   | `APP_BASE_URL` | `https://YOUR-DOMAIN` |
   | `ALLOWED_ORIGINS` | `https://YOUR-DOMAIN` |
   | `UPLOAD_DIR` | `/data/uploads` |
   | `DB_POOL_MODE` | `null` |
   | `RECAPTCHA_SITE_KEY` | Real Google reCAPTCHA v2 site key |
   | `RECAPTCHA_SECRET_KEY` | Matching private secret key |
   | `RECAPTCHA_ALLOWED_HOSTNAMES` | `YOUR-DOMAIN` without scheme |
   | `SMTP_HOST` | Your SMTP provider hostname |
   | `SMTP_PORT` | `587` for STARTTLS, or your provider's SSL port |
   | `SMTP_USE_TLS` | `true` for STARTTLS; `false` for implicit SSL |
   | `SMTP_USER`, `SMTP_PASSWORD` | Your private provider credentials/app password |
   | `SMTP_FROM` | Authorized sender address |
   | `SMTP_FROM_NAME` | `NIRPR RSO Examination Platform` |

   Leave `REDIS_URL`, `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` unset.
   Production startup requires real signing/reCAPTCHA settings. Add the domain
   to the reCAPTCHA console. Do not copy development credentials from `.env`.
5. Deploy and verify `/api/health`, registration, actual email delivery, login,
   receipt upload and download, and an exam/approval/certificate flow.

## Existing content and first administrator

GitHub contains application code, not your local database. A fresh deployment
creates empty tables; it will not automatically contain your existing question
banks. Import a **post-reset** PostgreSQL dump into the empty Railway database
before starting the web service if you want to preserve existing content. Use
`pg_restore --no-owner --no-privileges --exit-on-error` with the target connection
configured privately in your environment. Do not restore into an already populated
database. Use the database public connection only for migration from your machine;
the app should use the private `DATABASE_URL` thereafter.

Do not run `seed.py` in production: it creates demonstration users. From
`backend`, run `python prepare_first_admin.py` locally to generate private SQL,
choose your own password, and execute the complete generated transaction against
the Railway database in pgAdmin. Then sign in at `https://YOUR-DOMAIN/admin-login`.
Delete the generated SQL after use. All former accounts, including administrators,
must be replaced after a reset.

The standalone `reset_users.py` previews its scope by default. `--execute` first
creates a private database backup, then clears users and required dependent rows
in one transaction. It preserves question banks, questions, programmes, exams,
settings and audit logs, removing nullable references to deleted users. User-linked
attempt history, receipts, signatures, revisions and training batches are removed.
Backups are ignored by Git. Never add this command to startup or deployment hooks.

## Reduce consumption between exams

- Keep one web worker and one replica. Start with a 1 GB web RAM limit and 1 vCPU
  ceiling, then load-test the expected simultaneous candidates before reducing
  limits. Limits are ceilings, not a fixed bill. PostgreSQL needs its own capacity.
- Enable **Settings > Deploy > Serverless** on the web service between exam
  periods, then redeploy. Idle services sleep after roughly 5–10 minutes without
  outbound traffic. Visits wake them, allowing registration outside exam days.
  The first request can be delayed or return 502, so disable sleeping and redeploy
  before exams, verify readiness, then re-enable it after the exam window.
- `DB_POOL_MODE=null` closes PostgreSQL connections after use, helping idle sleep.
  If connection overhead becomes significant during exams, unset it and redeploy
  to use connection pooling. Open dashboard tabs, traffic and uptime pings can
  keep the service awake. PostgreSQL remains separately billed; web sleeping does
  not eliminate database or volume costs.
- Use the database's private network URL. Avoid duplicate staging services and
  background workers while they are unnecessary. Access logging is disabled in
  the Docker start command; application errors and database audit records remain.
- Use Workspace Usage alerts. A hard spending limit stops workloads, so choose
  sufficient headroom for an exam and check remaining budget before each session.
  Check measured web/database RAM and CPU after a week and after the first exam.
- Before scaling to multiple workers/replicas, configure shared Redis rate
  limiting and review shared upload storage. The in-memory limiter is per worker.

Sources: [Serverless](https://docs.railway.com/deployments/serverless),
[cost control](https://docs.railway.com/pricing/cost-control),
[volumes](https://docs.railway.com/volumes),
[backups](https://docs.railway.com/volumes/backups),
[PostgreSQL migration/backups](https://docs.railway.com/guides/postgres-backups-restores).
