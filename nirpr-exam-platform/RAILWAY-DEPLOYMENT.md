# Railway deployment and running costs

Checked against Railway documentation on 26 October 2026.

## Plan and cost

The deployment now targets **Hobby with Brevo HTTPS email**. Hobby starts at
$5/month including $5 usage; $8 of usage means an $8 Railway bill. Pro starts
at $20 including $20 usage. Free offers $1/month of resources and is
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
Use Brevo's HTTPS API on Hobby. SMTP remains available for other deployments.
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
   | `BREVO_API_KEY` | Your private Brevo API key |
   | `EMAIL_FROM` | Sender address verified in Brevo |
   | `EMAIL_FROM_NAME` | `NIRPR RSO Examination Platform` |

   Leave `REDIS_URL`, `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` unset.
   Leave `SMTP_HOST`, `SMTP_USER` and `SMTP_PASSWORD` unset on Hobby.
   Production startup requires real signing/reCAPTCHA settings. Add the domain
   to the reCAPTCHA console. Do not copy development credentials from `.env`.
5. Deploy and verify `/api/health`, registration, actual email delivery, login,
   receipt upload and download, and an exam/approval/certificate flow.

## Set up Brevo email

1. Create your account at [Brevo](https://www.brevo.com/). Enable transactional
   email if approval is required for your account.
2. Add and verify your sender address. Authenticate the sending domain using the
   DNS records Brevo provides; you need permission to change that domain's DNS.
3. Generate an API key in Brevo's SMTP & API settings, under API keys (not an
   SMTP credential). Add it directly to Railway Variables as `BREVO_API_KEY`.
   Set `EMAIL_FROM` to the verified sender and redeploy. Do not put keys in Git,
   frontend code or chat.
4. Register an address you control, confirm the verification message arrives,
   and test password reset. Check Brevo transactional logs for delivery problems.

When configured, Brevo receives recipient addresses and email contents, including
verification/reset links and any generated temporary credentials. API credentials
stay on the backend. Brevo takes priority over SMTP when its API key is set. Failed
API calls return a failure result without saving email contents to the local
outbox or falling back to SMTP. Existing registration screens do not prove delivery;
check the provider logs. No real email is sent by the automated tests.

Brevo's Free plan allows 300 emails/day shared across message types. Plan capacity
for registration, password resets, schedules and results together. Transactional
messages above the daily allowance can be queued, so a reset link may expire before
delivery. Check your allowance before exam-day messages and upgrade the email plan
if necessary; this does not require upgrading Railway.
See [Brevo limits](https://help.brevo.com/hc/en-us/articles/208580669-FAQs-What-are-the-limits-of-the-Free-plan)
and [transactional API](https://developers.brevo.com/reference/send-transac-email).

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
