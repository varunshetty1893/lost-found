# Campus Lost & Found Portal (verified members only)

A private lost-and-found system for a college. Only real students and staff can register, because
every account is checked against the college's own records. Members post lost or found items with
photos, search listings, and send ownership claims. Ownership is verified under a risk-based policy,
and an item is marked returned only after a confirmed handover (by college staff, or by both people).

**Tech stack:** Python 3, Flask, SQLite (built into Python), Jinja2 templates, responsive CSS.
No Node, no database server, no paid services.

## Run it

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. A source-only project ZIP does not include the SQLite database or private
uploads. Back up and restore those separately when replacing an existing project folder. If you start
with an empty database, the app creates the schema and loads `data/sample_roster.csv`.

Optional demo data for screenshots: `python scripts/seed_demo.py` (demo members use the password `Demo@1234`).
Run the tests from the project root: `python -m unittest` (111 tests: `tests/test_app.py` for accounts, the
admin console and course end dates, `tests/test_workflow.py` for the business workflow).

| Account | Login |
|---------|-------|
| Local demo admin | `admin@campus.edu` / password provided separately with the ZIP |
| Sample students | `aarav.shetty@campus.edu`, `diya.nair@campus.edu` and others in `data/sample_roster.csv` |
| Sample staff | `imran.khan@campus.edu` (Security Office), `suresh.bhat@campus.edu` |

To try registration, register as a sample person (for example roll number `S2305`, email
`karthik.rao@campus.edu`). In demo mode the 6-digit code is printed in the terminal running the server.

## Admin console

Admins land on the console straight after login (a left sidebar plus a top bar with one search box). The pages use plain names:

| Page | What it does |
|------|--------------|
| Home | A "To do" list first (people waiting for approval, unanswered claims, locked handovers, old listings), then four key numbers, shortcuts, a 14-day chart and the latest activity. |
| Approvals | Review ID card photos with a roster check, then approve or reject with an emailed reason. |
| People | Search and filter members, open a profile (items, claims, history), suspend with a reason or reactivate, export CSV. |
| Items | Search and filter every post, delete rule-breaking items with a reason (the poster is notified and it is logged), export CSV. |
| Campus list | The college roster: CSV import, add, edit, remove, filter, export CSV. |
| Activity log | Search and filter the full history, export CSV. |

The top-bar search looks across people, items and the campus list at once.

## How only campus people can register

1. **Campus roster check.** The admin uploads the college's student and staff list (CSV) in the admin
   panel. To register, a person must enter an ID number and the email the college has on file. Name,
   role and department come from the roster, not from the form.
2. **Email code.** A 6-digit code is sent to that roster email. It expires in 10 minutes, allows
   5 tries, and can be resent once a minute. Only the true owner of the inbox can finish registering.
3. **Manual ID review for exceptions.** A person who is genuinely on campus but missing from the roster
   (a new joiner, for example) uploads a photo of their ID card. An admin compares it with the entered
   details and approves or rejects. ID card photos are stored privately and only admins can open them.
4. **Members-only portal.** Visitors only see a landing page. Listings, photos and contact details need
   a login. Suspended members are logged out immediately.

## Features

| Area | What it does |
|------|--------------|
| Roles | Student, Staff and Admin, shown as badges. |
| Listings | Lost or found reports with a reference code (for example `LF-2026-00042`), category, place, date, public description, an optional photo and optional *private identifying details* that are never shown publicly. |
| Moderation | New reports wait for staff approval before they are public (`MODERATION_REQUIRED`, on by default). |
| Search | Keyword search, lost/found tabs, category filter, sorting, include returned items, removable chips, pagination. Private details are never searched. |
| Matches | Explainable suggestions (category, name, place, dates, description). Suggestions only: they never verify, approve, or move custody. |
| Claims | A member describes the item privately (and may add up to 3 photos). Statuses: pending, needs more info, under admin review, approved, rejected, withdrawn. |
| Two handover methods | See "Business rules" below. |
| Staff desk | Queues for pending reports, items awaiting physical acceptance, claims needing review, high-risk claims, disputes, approved claims awaiting handover and completed returns. |
| Privacy | The finder sees a claim's private description but never the claimant's identity, phone or email until the claim is verified; then only name and campus email. Notifications never contain evidence. |
| Alerts | In-app alerts for new claims, approvals, rejections and confirmed handovers. |
| Dashboard | My listings, pending-claim counts, and my claims with next steps. |
| Admin panel | Overview numbers, ID requests with the card photo, roster upload and editing, member suspend/reactivate, custody-officer authorization, verification policy page, and an audit log. |

## Security measures

- Roster match plus email code plus optional admin review (three layers of identity checking).
- An unverified registration attempt can never block the real owner of an ID.
- Passwords hashed with Werkzeug, minimum 8 characters with a letter and a number.
- Login lockout after 5 failed attempts in 10 minutes.
- CSRF token on every POST form, parameterised SQL only, Jinja auto-escaping.
- Uploads: extension whitelist plus a check of the real file signature, random filenames, 5 MB limit.
- All uploaded photos and ID cards sit in `private_uploads/` and are served only to logged-in members
  (ID cards only to admins).
- Role checks on every sensitive action, safe `next` redirect, security response headers.
- Audit log (actor, action, record type and id, time, short summary) for registrations, report moderation, claim
  decisions, custody acceptance and release, handover completion, disputes, return corrections, suspensions,
  policy changes and admin deletions. It never stores passwords, tokens or claim evidence.
- Every state change goes through `services.py`: server-side role and object checks, explicit transition
  tables, and `BEGIN IMMEDIATE` transactions that re-check eligibility. Two concurrent approvals cannot both
  succeed (also enforced by a partial unique index: one approved claim per item).
- Claim evidence photos are stored in `private_uploads/evidence/` and served only to the claimant, to the finder
  while the finder is the assigned reviewer, and to authorized staff (`Cache-Control: private, no-store`).
- Unpublished reports and their photos are visible only to the reporter and staff.
- Abuse limits: 10 new reports per hour and 10 claims per day per member (`services.py`).
- Handover confirmations are unique per person and claim, so a network retry or double click cannot create a
  second return.

## Sending real emails (optional)

In development without SMTP, the app prints verification codes in the terminal. Production refuses to
print OTPs; configure SMTP before enabling production registration. In Replit, add the non-secret
settings below under **Environment variables** for the **Production** environment, and add the
credentials under **Secrets**:

| Setting | Where to store it | Notes |
|---------|-------------------|-------|
| `SMTP_HOST` | Environment variable | SMTP server host; required for production email. |
| `SMTP_PORT` | Environment variable | Use `587` for STARTTLS; defaults to `587`. |
| `MAIL_FROM` | Environment variable | Optional sender address/name approved by your mail provider. |
| `SMTP_USER` | Secret | SMTP login name. Configure together with `SMTP_PASS`. |
| `SMTP_PASS` | Secret | SMTP password or provider app password. Configure together with `SMTP_USER`. Leave both unset only if the provider allows unauthenticated sending. |

Use a mail account/provider authorized to send on behalf of the campus. The app connects with STARTTLS,
so configure the provider's STARTTLS port (normally `587`), not implicit-TLS port `465`. After adding or
changing the Production settings, restart/redeploy the app so the process reads them. Missing or invalid
SMTP settings and send failures show a retry message to the user; logs contain no OTP or SMTP error
response. In development without SMTP, demo codes are still printed in the local terminal.

Other settings: `CAMPUS_NAME` (shown in the header, for example `GITAM`) and `SESSION_SECRET` or `SECRET_KEY`.

## Business rules

### Three independent states (never one combined field)

| Lifecycle | Values | Meaning |
|-----------|--------|---------|
| Report (`items.status`) | `pending_review`, `active`, `rejected`, `closed`, `returned` | Moderation and closure. `returned` and `rejected` are terminal. |
| Custody (`items.custody_status`, found items only) | `with_finder`, `admin_handover_requested`, `with_admin`, `direct_handover_pending`, `disputed`, `returned` | Where the physical item is, **as confirmed**. |
| Claim (`claims.status`) | `pending`, `needs_more_info`, `under_admin_review`, `approved`, `rejected`, `withdrawn` | Ownership decision for one claimant. |

Allowed transitions are the tables at the top of `services.py` (`REPORT_TRANSITIONS`, `CUSTODY_TRANSITIONS`,
`CLAIM_TRANSITIONS`). Examples: `pending -> needs_more_info -> pending`; `pending -> under_admin_review`;
a `rejected` claim can only go to `under_admin_review` through the explicit, one-time appeal; `withdrawn` never
returns. `returned` can only be reopened by an administrator through the audited "Correct this return" action.
Routes never accept a status or custody value from the client.

### Method A: hand the item to the college office

1. The finder reports the item and selects "Hand it to the college office". This sets a *preference*
   (`custody_status = admin_handover_requested`). It is **not** proof of deposit.
2. The report is published after staff approval. The finder takes the item to an authorized staff member and
   shows the reference code.
3. Staff open the report (or look it up on the staff desk) and press **Confirm receipt** after ticking that they
   physically hold the item. Only then does custody become `with_admin`, with a `custody_events` row (who gave,
   who received, which staff member, when, notes) and an audit entry. Staff can record an earlier receipt date.
4. Claims on office-handled items go straight to staff (`under_admin_review`). Staff approve or reject with a
   recorded reason, or request more information.
5. Staff can approve before the item arrives, but **cannot release it** until custody is `with_admin`.
6. At collection staff tick that the claimant acknowledged receipt (and was checked) and press **Record handover**.
   The report becomes `returned`, custody `returned`, and finder and claimant are notified.

### Method B: the finder returns the item directly

1. The finder reports the item and selects "Return it directly" (custody `with_finder`). Nobody has to visit the office.
2. A claimant submits a private description (and optional photos) against that specific report.
3. The finder sees the evidence only, not the claimant's identity. The finder may: mark the evidence convincing
   (only where the policy allows), ask a question, send the claim to staff, or reject with a reason.
4. Verification sets custody to `direct_handover_pending` and shows each party the other's name and campus email,
   plus a place to suggest a meeting spot and safe-handover advice. No custody event is written yet, because
   nothing has been transferred.
5. Finder and claimant each confirm the exchange. One confirmation shows "Handover confirmation pending". When
   both have confirmed the item becomes `returned` and a `DIRECT_RETURN` custody event is written.
6. If either side reports a problem the item becomes `disputed`. It cannot be released, claimed or confirmed
   until an uninvolved staff member resolves it ("record the return" or "reopen the item").

### Ownership verification policy (admin can change the category risk on *Verification policy*)

| Risk | Default categories | Who verifies |
|------|-------------------|--------------|
| Low | Books & Notes, Other | The finder may verify from the private description. |
| Medium | Bags, Clothing, Keys, Accessories | The finder may verify only if the claim has a photo or a detailed description (60+ characters); otherwise it must go to staff. |
| High | Electronics, Wallets, ID & Cards (and anything flagged "unsafe") | Always staff. Never direct handover: the item goes through the college office. |

Always routed to staff regardless of risk: more than one open claim on an item, items held by the college,
appeals, and claims whose finder was suspended. A college ID proves identity, not ownership. A public photo is
not proof. Matching scores never verify anything.

### Edge cases (decided behaviour)

| Case | Behaviour |
|------|-----------|
| Several people claim one item | All open claims go to staff. The earliest claim is never auto-approved. Approving one rejects the rest with a reason. |
| First claimant is not convincing | The finder asks a question, rejects with a reason, or sends it to staff. |
| Finder unsure | "Not sure: send to staff" moves the claim to staff and hides the evidence from the finder. |
| Finder stops responding | Finder claims still pending after 3 days appear in the staff "Claims needing review" queue; staff can decide. |
| Claimant stops responding | Staff can reject the claim, or revert an approval ("Revert approval") so the item is open again. |
| Finder never deposits an admin item | It stays in "Awaiting physical acceptance" with an Overdue tag. Nothing is auto-closed. |
| Staff receive an item late in the system | Record receipt with an earlier "received on" date; the note keeps the physical date. |
| Approved, handover never happens | Staff revert the approval (logged with a reason); the claimant may also withdraw. |
| One side confirms, the other disputes | Custody becomes `disputed`; staff resolve it. |
| Duplicate lost reports | Allowed (several people can lose similar things). An identical report within 2 minutes is not duplicated. Staff can close duplicates with a reason. |
| Abusive or misleading report | Rejected at moderation, or closed by staff with a reason. |
| Finder claims own item | Refused; the finder also cannot approve any claim that is theirs. Nobody can decide their own claim. |
| Claimant account suspended | Their open and approved claims are withdrawn and the item reopens. |
| Finder account suspended | Their pending claims go to staff; a direct handover in progress becomes `disputed` for staff. |
| Rejected claimant asks for reconsideration | One explicit appeal, only for claims the *finder* rejected; staff decisions are final unless an administrator reopens. |
| Unsafe or dangerous item | Tick "may be unsafe" when reporting: it is high risk, forced to the college office, and shown to staff as "Refer to security". |

### Roles and permissions

| Action | Student | Staff (custody officer) | Admin |
|--------|:------:|:------:|:-----:|
| Post reports, search, claim items, confirm own handovers | yes | yes | yes |
| Edit/close own report (before claims / any time before return) | yes | yes | yes |
| Verify a claim on their own low/medium-risk found item | yes | yes | yes |
| Approve or reject claims as staff, moderate reports | no | yes | yes |
| Confirm custody receipt, release items, resolve disputes, revert approvals | no | yes | yes |
| Authorize custody officers, policy page, correct a recorded return, delete listings, manage users | no | no | yes |

A plain staff account has no custody powers until an admin ticks "Authorize to confirm custody and review
claims" on the member's page.

## Manual test walkthrough

Use `python scripts/seed_demo.py` (password `Demo@1234`) or register sample members. Use separate browser profiles
(or private windows) for each person. Log in as the admin once and, on **Admin > Members**, open
`imran.khan@campus.edu` and press "Authorize to confirm custody and review claims".

**Method A (college office):**
1. As `aarav.shetty` report a found "Blue umbrella" (category Other) and choose "Hand it to the college office".
2. As admin or Imran, publish it from **Staff desk > Pending reports**. The item page says it is *not in custody*.
3. As `diya.nair` open the report and submit a claim. It appears under **Claims needing review**.
4. As Imran approve it with a reason. Try "Record handover": it is refused because nothing was received.
5. As Imran press **Confirm receipt** (tick the box). Custody becomes *In college custody* and a history line appears.
6. As Imran tick the acknowledgment and press **Record handover**. The report is *Returned*; both members have alerts.

**Method B (direct):**
1. As `aarav.shetty` report a found "Notebook" (category Books & Notes), choose "Return it directly", add a private detail.
2. As admin publish it. As `diya.nair` claim it with a private description. Aarav sees the description, not Diya's identity.
3. Aarav presses "Evidence is convincing". Both now see each other's name and campus email and can suggest a meeting place.
4. Aarav presses "The item was handed over": the page shows *Handover confirmation pending*; the item is still not returned.
5. Diya presses the same button: the item becomes *Returned*. Repeating a press changes nothing.
6. Variations: report a "Wallets" item (claims go to staff automatically); submit two claims on one item (both go to staff);
   press "Report a problem" instead of confirming (custody becomes *Disputed* and shows in the **Disputed handovers** queue).

## Roster CSV format

```
id_number,full_name,email,role,department
S2401,Student Name,student.name@yourcollege.edu,student,Computer Science
E2001,Staff Name,staff.name@yourcollege.edu,staff,Library
```

`role` is `student` or `staff`. Importing again updates existing IDs and adds new ones. A template can be
downloaded from the admin Roster tab.

## Project structure

```
lost-found/
  app.py                 Flask app: routes, schema and migration, verification, admin console
  services.py            business rules: state machines, risk policy, every state-changing operation
  matching.py            explainable match scoring (public fields only)
  requirements.txt       Python dependencies
  run.bat / run.sh       one-click start (creates venv, installs, runs)
  .env.example           settings template: copy to .env (never commit .env)
  data/
    sample_roster.csv    demo campus records, loaded when the database is empty
  scripts/
    seed_demo.py         optional demo members and listings
    seed_minimal.py      reset to a small, clean demo set
  tests/
    test_app.py          accounts, admin console, UI, course end dates, input validation
    test_workflow.py     both handover methods, verification, security, concurrency, migration
  templates/             pages (public, member and admin) plus _macros.html (icons, cards, form helpers)
  static/
    css/style.css        design system (shared by member and admin pages)
    css/admin.css        admin console layout (sidebar, dashboard, tables)
    css/modern.css       modern look (colours, buttons, hero) and the toast notifications
    css/polish.css       responsive layout, dialogs, registration form details
    js/app.js            small progressive-enhancement script (menus, dialogs, password toggle,
                         photo preview, department list, required-field stars); pages work without it
    img/                 favicons and app icon
  private_uploads/       created at runtime, not in Git: items/, idcards/ and evidence/ (never public)
  lostfound.db           created at runtime, not in Git: the SQLite database
```

## Database design

```
roster(id_number UNIQUE, full_name, email, role, department)
users(email UNIQUE, id_number UNIQUE, role, status, verification, id_card, department, is_custody_officer, ...)
  status: pending_email | pending_approval | active | suspended | rejected
  1 -- 1 otp_codes(user_id, code_hash, expires_at, tries)
  1 -- * items(ref_code UNIQUE, type, category, title, description, private_details, location, event_date,
               status, custody_status, custody_prior, handover_pref, risk_level, unsafe, approved_claim_id,
               review_reason, closure_reason, meeting_note, created_at, updated_at, returned_at)
          1 -- * claims(claimer_id, message, status, handler, escalation_reason, info_request, reviewer_id,
                        review_reason, appeal_used, decided_at, UNIQUE(item_id, claimer_id))
                  unique partial index: at most one claim per item with status 'approved'
                  1 -- * claim_evidence(filename, uploaded_by)          private photos
          1 -- * custody_events(from_state, to_state, handover_type, released_by, received_by, staff_id, claim_id,
                                confirmed, notes)                       only real, confirmed transfers
          1 -- * handover_confirmations(claim_id, user_id, party, outcome, note, recorded_by,
                                        UNIQUE(claim_id, user_id))      one row per person: retries are harmless
  1 -- * notifications(message, link, kind, is_read)
category_policy(category PRIMARY KEY, risk_level)
audit_log(actor_id, action, detail, record_type, record_id)    login_attempts(email, ts)
```

### Upgrading an existing database (schema version 2)

Nothing to run by hand: `init_db()` migrates automatically on first start and records `PRAGMA user_version = 2`
and an audit entry. **Back up `lostfound.db` first.** The one-time migration:

- rebuilds `items` and `claims` (SQLite cannot alter CHECK constraints) and adds the new tables and columns;
- old `open`/`claimed` items become `active`, `returned` stays `returned`; every old item gets a reference code;
- an old "at the Security Office" answer was only a form value, so it becomes `admin_handover_requested`
  (never `with_admin`). Staff must confirm receipt to change that;
- an old approved claim on an unreturned direct item becomes `direct_handover_pending`; old handover codes are discarded;
- claims on *lost* reports (no longer supported) are closed with an explanation.

## User interface notes

- Report a lost or found item at `/post?type=lost` or `/post?type=found`. The form adapts its wording and shows the
  handover options only for found items. Photos show a preview, and size and type are checked in the browser as well as on the server.
- Submitting the same listing twice within two minutes (double-click, refresh) returns the first listing instead of creating a duplicate.
- Listings without a photo show a category icon placeholder. The public landing page shows only aggregate totals, never listings.

## Colour palette

Ink `#12302D` (text), deep green `#123F3A` (hero and footer), coral `#B3412C` (lost items),
teal `#1D6A61` (found items and primary actions), gold `#D3A43D` (accent), soft grey-green `#F5F7F4` (background).

## Typical viva questions

1. **How do you make sure only campus people register?** Roster match, then an email code to the roster
   email, and manual ID-card review with admin approval for exceptions.
2. **Why use the roster's email and not any email?** Anyone can create an email address. The roster email
   is the one the college already trusts, so proving access to it proves identity.
3. **How is a wrong person stopped from collecting an item?** Ownership is verified under a risk policy before
   any handover, and the return is recorded only after staff release it or both people confirm the exchange.
4. **Why can staff confirm custody?** Only authorized staff can say the college really holds an item. The
   finder's choice is only a preference. Every staff confirmation is written to the audit log.
5. **How are ID cards protected?** They are stored outside the public folder and only an admin session can
   open them.
6. **What would you add next?** A mobile app, push notifications, campus map pins, and image-based matching.

## Deploying (optional)

Set `SESSION_SECRET` or `SECRET_KEY`, a unique `ADMIN_PASSWORD`, and SMTP settings before running with
`APP_ENV=production`. A supplied admin account using the demo password is rotated to `ADMIN_PASSWORD`;
startup fails closed if no replacement is configured. Run behind Gunicorn with `gunicorn app:app`.
Use persistent storage for the SQLite file and `private_uploads/` before handling real campus records.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAMPUS_NAME` | `Campus` | Name in the header. |
| `PORT` | `5000` | Port. |
| `APP_ENV` | `development` | `production` requires `SECRET_KEY`, a unique `ADMIN_PASSWORD` and SMTP. |
| `SECRET_KEY` / `SESSION_SECRET` | fixed development value | Session signing key. Required in production. |
| `ADMIN_PASSWORD` | demo password in development | Used when the database is first created. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM` | empty | Real email delivery. |
| `MODERATION_REQUIRED` | `1` | `0` publishes new reports immediately. |

## Known limitations

- A custody officer is any staff member an admin authorizes; there is no separate "security office" role or shift roster.
- The claimant's college ID check at collection is recorded by a tick box. The system does not scan or store ID documents.
- Categories and locations are fixed lists in code; only the risk level of each category is editable (Verification policy).
- Finder-to-claimant coordination is limited to exchanging name and campus email plus one meeting suggestion; there is no in-app chat.
- Notifications are in-app only (no email or push for workflow events).
- Rate limits are per member and stored in the database; there is no IP-based limiting. Behind a public deployment add a reverse proxy limit.
- Matching weights (30/25/20/15/10, minimum score 40) are starting values and have not been tuned on real campus data.
- Stale items are surfaced to staff but never closed automatically; auto-expiry is a policy decision for the college.
- SQLite with `BEGIN IMMEDIATE` is correct for one server process (or a few workers on one host). Use a client/server database for multi-host deployments.
- Evidence files stay on disk until the report is deleted by an admin; there is no automatic retention period yet.
- This has been tested locally with automated tests. It has not had an independent security review or load testing and should not be called production-ready until the college has validated the policy, deployment and data-protection setup.

## Course end dates, disabling and removing people

- Every **student** on the campus list has a **course end date** (the last day of the course, for example
  `2027-05-20`). Staff have none. Add it in *Campus list > Add person / Edit*, or with an `end_date`
  column in the CSV import (blank means no automatic end).
- The day **after** the end date the student's account is closed automatically ("Course ended on ... (automatic)"),
  their open claims and handovers are handled like a suspension, and they see a clear message on login.
  A student whose end date has already passed cannot register.
- To keep a student longer, open *People > the student > Course end date* and pick a later date. That also
  reopens an account that was closed automatically. A manual suspension is never lifted by changing the date.
- **Suspend** keeps the account and history (use it when someone may return). **Remove** deletes the account,
  their listings, claims and files, and their campus-list entry, so they cannot register again. You must type
  `REMOVE` to confirm, and removal is blocked while one of their items is in college custody or in an open handover.
- Input checks: names (2-60 letters), ID numbers (2-20 letters/digits), emails (valid, up to 120 characters),
  phone numbers, passwords (8-128 characters with a letter and a number), dates of birth and end dates are
  validated on login, both registration forms, Add person, Edit person and CSV import.
