# NIRPR RSO Examination Platform

For Railway deployment, SMTP requirements and operating-cost settings, see
[RAILWAY-DEPLOYMENT.md](RAILWAY-DEPLOYMENT.md).

A professional certification examination platform for Radiation Safety
Officers (RSO) in Nigeria, built for the National Institute of Radiation
Protection and Research (NIRPR) — the technical arm of the Nigerian Nuclear
Regulatory Authority (NNRA).

## What's new in this update

**Governance, security and certification expansion (August 2026):**
- Public QR-backed certificate verification at `/verify-certificate`, candidate/examination numbers on certificates, and admin-managed General Manager/Course Coordinator signature images.
- Candidate payment-receipt upload with admin/examiner approval gates, result appeals, dashboard notifications/reminders, question version history and admin review decisions.
- Extended per-exam delivery policy: navigation/skip/revisit/resume/sequential rules, negative marking configuration, randomization/topic distribution, payment requirement, full-screen/clipboard/print controls and question snapshot policy.
- CAPTCHA, staff authenticator-app 2FA, brute-force lockout, login rate limiting, new-device alerts, revocable sessions, security headers, exam heartbeat, event telemetry and candidate watermarking.
- Dark/light themes, adjustable candidate font size, official examination-report PDF export, Redis helpers and a Celery worker entry point in `backend/infrastructure.py`.
- Automated smoke tests for authentication infrastructure, the public verification page, QR certificate PDF generation and official report PDF generation.

**New admin capabilities:**
- **Question bank Edit/Delete** — the backend already supported this; there
  was just no button for it. Now in the admin console.
- **Add a candidate directly** ("+ Add Candidate" in the Users tab) —
  pre-verified, with a proper training-programme profile, credentials
  emailed immediately. (The earlier "+ New Staff Account" form technically
  let you pick "candidate" as a role, but it silently created a broken
  account with no training-programme profile — fixed.)
- **Retakes**: exams now have a configurable `max_attempts`. Once a
  candidate uses them up, an admin can grant a one-time retake (with a
  reason, fully audit-logged) from the exam's "Candidates" panel. Full
  attempt history is kept — retaking never erases the earlier attempt.
- **Reschedule**: from an exam's "Candidates" panel, select one or more
  candidates and give them a different exam window than everyone else,
  without touching the exam for the rest of the cohort.
- **"Log in as candidate"** — from the Users tab (per-candidate) or the
  new "View as candidate" button in the admin topbar. Shows exactly what
  that candidate's dashboard looks like. **Exam-taking actions (start,
  answer, submit) are deliberately blocked during impersonation** — this
  is for checking the candidate experience, not for an admin to sit an
  exam on someone's behalf, which would be a serious integrity problem
  for a certification platform. Every impersonation is audit-logged.

**Candidate portal:**
- The calculator is now a genuine **scientific calculator** — trig
  functions (sin/cos/tan with a DEG/RAD toggle), log/ln, square root,
  powers (x², xʸ), 1/x, π, e, and parentheses. It evaluates expressions
  with a small hand-written parser (not JavaScript `eval()`), so a
  candidate typing something invalid just gets "Error," not a security
  concern.


**Link expiry — fixed and made visible to candidates:**
- The verification/reset email expiry times were controlled by hardcoded
  values in `main.py` that `.env` couldn't actually change (a leftover
  mismatch — `auth.py` had its own copy of these settings, reading from a
  differently-named, unused environment variable). Fixed: `VERIFICATION_TOKEN_EXPIRE_HOURS`
  and `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES` in `.env` now genuinely control
  the expiry — verified with a live test.
- **Verification and password-reset emails now state the real expiry** —
  both a duration ("expires in 24 hours") and the exact date/time — instead
  of a hardcoded "24 hours" that would silently go stale if you changed the
  `.env` setting.
- **The reset-password page now shows a live, ticking countdown** of the
  time left on that link, and disables the form automatically once it
  expires (rather than only failing after the candidate fills in a new
  password and submits).

**Bug fixes:**
- **Result percentages were wrong.** They were computed against the exam's
  configured `total_marks` (an admin-entered field, defaulting to 100)
  instead of the actual marks achievable on each candidate's specific
  question set. A candidate scoring full marks on a 5-question, 5-mark
  paper was shown as 5%, not 100%. Now fixed — percentage is computed from
  the real question set every time, and each attempt records its own
  `total_possible_marks` so "82/100 marks" style displays are accurate.
- **Candidate dashboard "questions answered" progress never showed.** The
  dashboard read a field the API never returned, so the progress bar
  silently rendered nothing. Fixed on both ends — real counts now flow
  through and display correctly.
- `exam_analytics`'s highest/lowest score and topic performance were
  hardcoded to empty/zero even though the underlying data existed. Now
  computed properly.

**New admin features:**
- **Full edit + delete** for training programmes and exams (previously
  create-only, or status-toggle-only for exams). Deleting something with
  existing candidates/attempts on it deactivates/archives instead of
  destroying historical records.
- **View a candidate's full answer script** — every question on their
  paper, what they selected, the correct answer, and marks awarded — from
  the Approvals screen.
- **Exam monitoring**: a live view of everyone currently sitting an exam
  (progress, time remaining, tab-switch/full-screen-exit counts), plus a
  per-attempt chronological **activity/incident timeline** and a
  transparent 0–100 **risk score** built from those same signals. In line
  with the platform's existing philosophy, a high risk score is a prompt
  for human review — it never fails a candidate automatically.
- **Reports & statistical analysis**: per-candidate performance across
  exams, topic-level accuracy, a score-distribution chart, and overall
  pass-rate/average, filterable by training programme or exam.

**Candidate dashboard:**
- **On-screen calculator**, available as a toggleable panel during an
  exam.

**Structure:**
- Reorganized into `backend/` (FastAPI, database, business logic) and
  `frontend/` (HTML/CSS/JS) — see Project layout below.

*(Bulk CSV candidate/staff upload already existed in the version this
update builds on — see "Users" tab → "Import CSV" — so it wasn't rebuilt,
just confirmed still working.)*

## What it does

- **Candidate self-registration with email verification.** New candidates
  register through a dedicated `/register` page, choose their RSO training
  programme, and receive a verification link by email. They cannot log in
  until they click it. A "resend verification email" option is available
  from the login page if the link is lost or expires (24 hours).
- **Forgot / reset password** flow — request a link from `/forgot-password`,
  set a new password from `/reset-password` (link valid 60 minutes).
- **Bulk CSV import for users** — admins can download a CSV template,
  fill it in (or export from HR/registration records), and upload it to
  create many candidate or staff accounts at once instead of one by one.
  Each imported user is emailed their login credentials (auto-generated
  password if none is supplied in the CSV, optional toggle to skip emailing).
- **Bulk CSV import for question banks** — same idea for exam questions:
  download the template (which documents the exact format, including how to
  express fill-in-the-gap blanks and multiple acceptable short answers),
  fill it in, upload. A one-by-one "Add question" form and a raw-JSON bulk
  upload option are still available too.
- **Five question types**, including two new ones added in this build:
  multiple choice (single answer), multiple select (several correct
  answers), true/false, **fill in the gap** (one or more blanks per
  question, each blank can accept several acceptable phrasings), and
  **short answer** (free text, case-insensitive, accepts multiple
  acceptable phrasings).
- **Separate question banks per training programme** (Diagnostic Radiology,
  Nuclear Medicine, Industrial Radiography, or any others you add) — each
  training track draws only from its own bank.
- **Independently randomized exams**: every candidate who sits an exam gets
  a different, randomly-selected subset of questions (stratified across
  easy/medium/hard difficulty) with shuffled answer options, drawn fresh at
  the moment they start. There is no shared "paper," so copying a
  neighbour's answers is not useful.
- **Timed exam-taking interface** with autosave-per-answer, a question
  navigator, a countdown timer, automatic submission the instant time
  reaches zero, and basic anti-cheating telemetry (tab switches and
  full-screen exits are logged against the attempt and shown to reviewers).
- **Server-authoritative timer with resume support.** The countdown shown
  to the candidate re-syncs against the server every 20 seconds, and if the
  time genuinely expires while the tab is unfocused or the connection drops,
  the exam is auto-submitted server-side regardless — a candidate cannot
  extend their time by closing the tab. If a candidate's browser crashes or
  they get disconnected mid-exam, the dashboard offers a **"Resume exam"**
  banner that reloads their exact question set, previously-saved answers,
  and the real remaining time.
- **Results are withheld until NIRPR approval**: candidates never see a
  score until an examiner/admin has reviewed and approved (or rejected) the
  attempt. Candidates are emailed when their result is released.
- **Admin console**: manage training programmes, question banks (one-by-one,
  JSON bulk, or CSV bulk), exams (auto-activates at its scheduled start
  time — no manual "activate" step needed), the approval queue, staff and
  candidate accounts (create, deactivate/reactivate, **permanently delete**
  with safeguards), and a full audit log.
- **Admin can delete a user.** Deleting a user who already has exam
  attempts on record requires an explicit confirmation (`force=true`) that
  also removes their exam history; a super admin cannot delete their own
  account or the last remaining super admin.

## Project layout

```
backend/
  requirements.txt        Python dependencies
  database.py              Async SQLAlchemy engine/session setup
  models.py                  Database tables
  schemas.py                  Pydantic request/response models
  auth.py                      JWT auth, password hashing, role checks, audit logging, tokens
  exam_engine.py                Randomized question selection, scoring, approval, risk scoring, reports
  email_utils.py                  Email sending (SMTP, with a dev-mode file fallback)
  csv_utils.py                     CSV parsing + template generation for bulk import
  main.py                            FastAPI application — all API routes
  seed.py                              One-time bootstrap script (admin/examiner accounts + demo data)
  outbox/                                Dev-mode: emails land here as .html files if SMTP isn't configured
frontend/
  login.html                           Login page
  register.html                          Candidate self-registration page
  verify-email.html                        Landing page for the emailed verification link
  forgot-password.html                       Request a password reset link
  reset-password.html                          Set a new password (emailed link)
  candidate.html                                 Candidate dashboard + exam-taking UI (incl. calculator) + results
  admin.html                                       Admin/examiner console (incl. Monitoring + Reports)
  css/style.css                                      Shared admin styling
  css/candidate.css                                    Candidate portal styling
```

The backend serves the frontend directly — there is one process to run, no
separate frontend server or build step.

## Setup

Requires Python 3.10+.

```bash
cd nirpr-exam-platform/backend
pip install -r requirements.txt

# (Optional) set your own admin credentials before seeding
export ADMIN_EMAIL="you@nirpr.gov.ng"
export ADMIN_PASSWORD="choose-a-strong-password"

python seed.py      # creates the database, a super admin, a demo examiner,
                     # 3 demo training programmes, and a starter question bank
                     # (including fill-in-the-gap / short-answer examples)

python main.py       # or: uvicorn main:app --reload --port 8000
```

Then open **http://localhost:8000** — that's the login page. Log in with
the admin credentials printed by `seed.py` (defaults: `admin@nirpr.gov.ng`
/ `ChangeMe#2026` — **change this immediately in production**).

All commands run from `backend/` — that's where `main.py`, `seed.py`, and
the database file live. `main.py` finds `../frontend` automatically.

## Sending real email

By default, if no SMTP server is configured, the platform does **not**
fail — verification links, password reset links, and generated credentials
are written to `outbox/*.html` on disk and printed to the console so you
can develop and test the full flow without a mail server. Open those files
in a browser to click the links.

To send real email, set these environment variables before starting the
app (e.g. in a `.env` file, or your process manager):

```bash
SMTP_HOST=smtp.yourprovider.com
SMTP_PORT=587
SMTP_USER=apikey-or-username
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=noreply@nirpr.gov.ng
SMTP_FROM_NAME="NIRPR RSO Examination Platform"
SMTP_USE_TLS=true          # false if your provider requires implicit SSL (SMTP_SSL)

# Used to build the links inside emails (verification, reset, login):
APP_BASE_URL=https://exams.nirpr.gov.ng
```

Any standard transactional SMTP provider works (SendGrid, Mailgun, Amazon
SES SMTP interface, Office 365, Gmail with an app password, etc).

## CSV import formats

Both CSV import features have a **"Download CSV template"** button right
next to the import button in the admin console, which downloads a ready
example file with the exact expected column headers and a couple of filled
example rows (so you can just delete the example rows and add your own).

**Users** (`user_import_template.csv`):
`full_name, email, phone, role, password, training_program_code, institution, qualification, practice_type`
— `role` defaults to `candidate` if left blank; `password` is
auto-generated if left blank (and emailed to the user, unless you untick
"email credentials" in the import dialog); `training_program_code` /
`institution` / `qualification` / `practice_type` only apply to candidates.

**Questions** (`question_import_template.csv`):
`question_text, question_type, option_a, option_b, option_c, option_d, correct_answer, explanation, marks, difficulty, topic`
— `question_type` is one of `multiple_choice`, `multiple_select`,
`true_false`, `fill_in_gap`, `short_answer`. For the first three, fill the
option columns and put the correct option letter(s) in `correct_answer`
(e.g. `B` or `A;C`). For `fill_in_gap` with several blanks, leave the
option columns blank and put one blank's acceptable answer(s) per line
inside `correct_answer`, separated with `|`, e.g.
`ALARA/As Low As Reasonably Achievable|20 mSv`; alternatives for the *same*
blank are separated with `/`. For `short_answer`, just list acceptable
phrasings separated by `/` on one line.

## Environment variables (all optional, sensible defaults provided)

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy async DB URL | local SQLite file |
| `SECRET_KEY` | JWT signing secret — **set this in production** | dev key baked in |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Login session length | 480 (8 hours) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Seeded super admin login | see `seed.py` |
| `EXAMINER_EMAIL` / `EXAMINER_PASSWORD` | Seeded demo examiner login | see `seed.py` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_FROM_NAME` / `SMTP_USE_TLS` | Outbound email | unset → dev-mode file outbox |
| `APP_BASE_URL` | Used to build links inside emails | `http://localhost:8000` |
| `ALLOWED_ORIGINS` | Comma-separated browser origins allowed by CORS | local port 8000 origins |
| `REDIS_URL` | Rate limits, cache and exam heartbeat | `redis://localhost:6379/0` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Background notification worker | `REDIS_URL` |
| `RECAPTCHA_SITE_KEY` | Google reCAPTCHA v2 public site key; register localhost for development | Google official development test key |
| `RECAPTCHA_SECRET_KEY` | Google reCAPTCHA v2 server verification secret | Google official development test secret |

## Move the existing data to PostgreSQL

The active SQLite database is `backend/nirpr_exam.db`. The similarly named file
in the project root is an older, empty database; do not use it as the migration
source. The migration copies all 26 application tables, checks row counts,
resets PostgreSQL ID sequences, and creates a timestamped SQLite backup in
`backend/backups/`. It accepts either a new database or the empty schema in
`sql/01_postgresql_schema.sql`, and refuses a database that already has data.

1. Finish installing PostgreSQL. Create a **new, empty** database (for example,
   `nirpr_exam`) and a user with permission to create tables in its `public`
   schema. Stop the platform and any other process writing to SQLite.
2. From `backend/`, install the updated requirements in the project's virtual
   environment: `..\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
3. Run `..\.venv\Scripts\python.exe migrate_sqlite_to_postgres.py --check` to
   inspect the source. Keep the original SQLite file as a recovery copy.
4. Set `DATABASE_URL` in `backend/.env` to an async PostgreSQL URL, such as
   `postgresql+asyncpg://nirpr_user:PASSWORD@localhost:5432/nirpr_exam`.
   URL-encode special characters in the password. Keep `.env` private.
5. Run `..\.venv\Scripts\python.exe migrate_sqlite_to_postgres.py`. Do not run
   `seed.py` against the new database; it would create demo accounts and data.
6. Start the platform from `backend/` as usual and verify candidate and staff
   logins, exam lists, and prior results. The application uses PostgreSQL when
   `DATABASE_URL` is set. Keep the SQLite backup until these checks pass.

Uploaded receipts and signature images are files outside the database. Keep
the `backend/uploads/` and `backend/assets/` directories when moving the app
to another computer or server.

## Candidate tags, names, and password resets

In the admin Dashboard, open **Candidate tags** to reach the Candidates list
and download a print-ready tag for each candidate. The tag shows the candidate
name and number, training course, NIRPR logo, and the uploaded
General Manager signature. Upload that signature under **Governance &
Services → Programme signatures** before downloading tags.
The printable tag uses the standard ID-1 card size (85.60 × 53.98 mm).

Users now store only `surname`, `first_name`, and `other_name` in the database.
The application assembles a display name from those fields for existing screens
and reports. Existing names were
split using the last word as surname; review and correct ambiguous names via
**Users → Edit name** before issuing tags. New candidate forms collect these
parts separately.

Use **Users → Reset password** to change another user's password. The app
hashes the new temporary password and revokes existing sessions. Editing
`users.hashed_password` directly in pgAdmin stores exactly what you type; it
does not hash a plain password automatically and can break login.

## Roles

- **Candidate** — registers, verifies email, sits exams for their chosen
  training programme, views released results.
- **Examiner** — reviews and approves/rejects submitted attempts, manages
  question banks and exams.
- **Admin** — everything an examiner can do, plus manage training
  programmes, staff/candidate accounts (including CSV import and delete),
  and the audit log.
- **Super Admin** — same as Admin; the platform always keeps at least one
  Super Admin account and won't let you delete the last one.

## Notes on exam integrity

- Every candidate's question set and option order is independently
  randomized at the moment they click Start — there is no shared "paper."
- Tab switches and exiting full-screen mode during an exam are logged and
  surfaced to reviewers in the approval queue as a warning badge; the
  platform does not auto-fail a candidate for this, leaving the judgment
  call to a human reviewer.
- The exam timer is enforced server-side (based on the recorded start
  timestamp), so a candidate cannot extend their time by manipulating the
  client, closing the tab, or losing their connection.
- The new activity timeline and risk score reuse the same tab-switch/
  full-screen signals already recorded — no new tracking was added.

## Production notes

Browser-side exam restrictions are deterrence and evidence collection, not a
guarantee: no ordinary website can prevent an operating-system screenshot or
a photograph from a second device. For high-stakes remote delivery, pair these
controls with a managed kiosk/lockdown browser and a documented human review
process. Configure PostgreSQL, Redis, Celery workers, TLS, production SMTP,
strong secrets, backups and centralized logs before deployment.

## Per-exam payment receipt requirement

Under Administration → Payment receipts, use **Receipt requirement by exam** to
turn the requirement on or off for each examination. The programme filter also
filters these switches. Administrators and super administrators can change them;
changes are recorded in the audit log.

When on, candidates need an approved receipt or an administrator access grant
before starting. When off, no payment approval is needed. Exam schedules,
programme eligibility and attempt limits still apply. Existing attempts remain
accessible. Exams without a saved payment policy use the existing default, off.
Receipt history remains available when the requirement is disabled. Saving other
exam delivery rules preserves this setting.

## Training batches, new candidates and renewal approval

A **new candidate** has never attempted any examination. A **returning candidate**
has a previous attempt, including a passed examination. Returning candidates need
explicit **renewal approval** for a different exam. A retake remains a separate
permission for another attempt on the same exam.

1. Open **Exam setup → Training batches**, select a programme, and create a named
   batch (for example, October 2026 cohort).
2. Select candidates and use **Assign / reassign selected** to place them into
   that batch. Candidates can have one current batch and must belong to its
   programme. Assignments and changes are audit logged.
3. When scheduling an examination, select its programme and training batch. Only
   eligible members of that batch receive schedule emails and notifications.
4. Open the exam's **Candidates** panel and choose **Approve renewal** for a
   returning candidate. Enter a reason. The candidate must be in the exam's batch.
   Approval sends that candidate the schedule and is audit logged. Repeating it
   does not send duplicate emails. Payment requirements still apply.

Candidate dashboards, receipt uploads and server-side exam admission enforce the
same batch and renewal rules. Payment approval alone does not bypass them.
Candidates assigned after scheduling receive only their new batch's applicable
upcoming schedules; repeating an unchanged assignment does not resend emails.

Existing exams are not automatically assigned to a batch. Use **Edit exam** to
select a batch before admitting new candidates. An exam's batch is locked after
an attempt exists. Reassignment does not erase existing attempts, results or
receipts, and a candidate can continue an existing attempt after reassignment.
Schedule emails still go only to the exam's current batch.

Restart the application to create the batch and assignment tables through
`init_db()`. Existing renewal records retain their original storage table for
compatibility. No existing candidates are automatically assigned to batches.

## Candidate announcements

The candidate **Announcements** tab and unread count show only notices published
by administrators. Security events, login activity, receipts, renewal decisions
and automated exam reminders remain stored separately and are not marked read
when a candidate opens Announcements.

Under **Administration → Announcements**, admins and super admins can publish a
title and message to all active candidates, a selected training programme, or a
batch. Email delivery is optional and off by default. The panel shows publication
history, recipient counts, and read counts. Posting is immediate and audit logged.
Previously stored system notifications are preserved but excluded from this feed.

## Import candidates for an examination

Open **Exam setup → Manage candidates & schedules → Import candidates (CSV / Excel)**.
Download the CSV template or use a `.csv`, `.tsv`, `.txt`, `.xls`, `.xlsx` or `.xlsm` file with an **email** column in
its first row (Excel uses the first worksheet). Up to 500 candidates and 5 MB are
accepted. Existing candidate accounts are matched by email, ignoring case.

Choose **Preview file**, review every row, then use **Select all matched candidates**
or individual checkboxes and **Import selected candidates**. Unknown addresses,
duplicates, staff accounts, inactive candidates and candidates from another
programme cannot be selected. Correct those rows and preview the file again.
No accounts or assignments are changed during preview.

Importing assigns or reassigns the selected accounts to the examination's batch,
which also affects other exams assigned to that batch. Returning candidates still
need renewal approval, and receipt requirements remain in force. The candidate
panel and assigned count refresh after import or after editing an exam's batch.
Existing attempts remain visible even when a candidate moves to another batch.

### Import a training batch directly

Open **Exam setup → Training batches**, choose the programme and target batch,
then **Import candidate file (CSV / Excel)**. Preview the file, select individual
matches or **Select all matched candidates**, then **Import selected candidates**.
This also works before scheduling an exam. The existing exam importer supports
the same extensions: `.csv`, `.tsv`, `.txt`, `.xls`, `.xlsx`, `.xlsm`.
Text files must be UTF-8 or BOM-marked UTF-16; `.txt` accepts comma, semicolon or
tab separators. Excel uses its first worksheet. Macros are not executed.

## Certificate branding, numbers and signatures

Downloaded certificates retain the original layout and full organisation-name
headings, with the NNRA logo and cleaned signatures. Numbers now end with one candidate identifier,
for example `NIRPR/NTC/RSO/NWLST/2026/0006`. The QR link includes the exact attempt
reference separately. Previously issued longer numbers remain verifiable.
Manual verification of a short number returns the most recent matching released,
approved passing certificate; its QR code identifies the exact attempt.

Certificate signatures use Pillow to isolate ink, remove light/gray paper
backgrounds and crop blank margins. New certificate signature uploads are saved
as transparent PNGs; existing images are cleaned when the certificate is drawn.
Upload actual handwriting on plain paper. For documents containing other text,
use **Certificate signatures → View → Extract signature** to select handwriting
before saving. A receipt or unrelated document is not a usable signature.

## Generated question option balance

Generated starter and NWLST questions assign correct answers across A, B, C and D,
while storing and displaying option labels alphabetically. Correct answer text
is remapped with its label. The local maintenance tool
`backend/rebalance_generated_questions.py` previews imbalanced generated banks;
`--apply` writes a JSON backup and revisions before updating them. Questions used
by previous attempts are retained as inactive historical versions and replaced
with revised active questions. Candidate attempts, answers and results are not
rewritten. The admin question list defaults to active questions; select
**Show inactive / historical questions** to inspect preserved versions.

### Dashboard candidate number

The candidate dashboard displays the certificate-style number, for example
`NIRPR/NTC/RSO/NWLST/2026/0006`. When an approved passing certificate has been
released for the current programme, it uses the latest certificate's exact
course/year/number. Before a certificate is issued it uses the current year and
the same formatter. Existing registry identities and candidate tags are retained.
