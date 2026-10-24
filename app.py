"""Campus Lost & Found Portal
Flask + SQLite. Only verified campus members (students and staff) can use it.
"""
import csv
import hashlib
import html
import io
import math
import os
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import matching
import services
from services import WorkflowError

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Load settings from a .env file next to app.py (optional; real environment variables win).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

DB_PATH = os.path.join(BASE_DIR, "lostfound.db")
ITEM_DIR = os.path.join(BASE_DIR, "private_uploads", "items")
CARD_DIR = os.path.join(BASE_DIR, "private_uploads", "idcards")
EVIDENCE_DIR = os.path.join(BASE_DIR, "private_uploads", "evidence")
SAMPLE_ROSTER = os.path.join(BASE_DIR, "data", "sample_roster.csv")

CAMPUS_NAME = os.environ.get("CAMPUS_NAME", "Campus")
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
CATEGORIES = ["Electronics", "ID & Cards", "Books & Notes", "Bags", "Clothing",
              "Keys", "Wallets", "Accessories", "Other"]
TS = "%Y-%m-%d %H:%M:%S"
OTP_MINUTES, OTP_MAX_TRIES, OTP_RESEND_SECONDS = 10, 5, 60
LOGIN_MAX_FAILS, LOGIN_WINDOW_MIN = 5, 10
PER_PAGE = 12
SORTS = {"newest": ("Newest first", "items.id DESC"),
         "oldest": ("Oldest first", "items.id ASC"),
         "date": ("Most recent date", "items.event_date DESC, items.id DESC")}
DUPLICATE_WINDOW_MINUTES = 2
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,14}$")

app = Flask(__name__)
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
secret_key = os.environ.get("SECRET_KEY") or os.environ.get("SESSION_SECRET")
if APP_ENV == "production" and not secret_key:
    raise RuntimeError("Set SECRET_KEY or SESSION_SECRET before starting in production.")
app.config["APP_ENV"] = APP_ENV
app.config["SECRET_KEY"] = secret_key or "dev-only-secret-change-before-deploying"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    APP_ENV == "production"
    or os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
# New reports publish immediately by default without requiring staff or admin approval. Set MODERATION_REQUIRED=1 to require review.
MODERATION_REQUIRED = os.environ.get("MODERATION_REQUIRED", "0").strip().lower() in {"1", "true", "yes"}
os.makedirs(ITEM_DIR, exist_ok=True)
os.makedirs(CARD_DIR, exist_ok=True)
os.makedirs(EVIDENCE_DIR, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS roster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    id_number TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student', 'staff')),
    department TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student', 'staff', 'admin')),
    status TEXT NOT NULL DEFAULT 'pending_email' CHECK (status IN
        ('pending_email', 'pending_approval', 'active', 'suspended', 'rejected')),
    id_number TEXT UNIQUE,
    department TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT 'roster' CHECK (verification IN ('roster', 'manual')),
    id_card TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    is_custody_officer INTEGER NOT NULL DEFAULT 0,
    date_of_birth TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS otp_codes (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_code TEXT UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('lost', 'found')),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    private_details TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL,
    event_date TEXT NOT NULL,
    photo TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review' CHECK (status IN
        ('pending_review', 'active', 'rejected', 'closed', 'returned')),
    custody_status TEXT CHECK (custody_status IS NULL OR custody_status IN
        ('with_finder', 'admin_handover_requested', 'with_admin', 'direct_handover_pending',
         'returned', 'disputed')),
    custody_prior TEXT,
    handover_pref TEXT CHECK (handover_pref IS NULL OR handover_pref IN ('admin', 'direct')),
    risk_level TEXT NOT NULL DEFAULT 'medium' CHECK (risk_level IN ('low', 'medium', 'high')),
    unsafe INTEGER NOT NULL DEFAULT 0,
    approved_claim_id INTEGER,
    review_reason TEXT NOT NULL DEFAULT '',
    closure_reason TEXT NOT NULL DEFAULT '',
    meeting_note TEXT NOT NULL DEFAULT '',
    meeting_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    returned_at TEXT,
    CHECK ((type = 'found' AND custody_status IS NOT NULL AND handover_pref IS NOT NULL)
           OR (type = 'lost' AND custody_status IS NULL))
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    claimer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'needs_more_info', 'under_admin_review', 'approved', 'rejected', 'withdrawn')),
    handler TEXT NOT NULL DEFAULT 'finder' CHECK (handler IN ('finder', 'admin')),
    escalation_reason TEXT NOT NULL DEFAULT '',
    info_request TEXT NOT NULL DEFAULT '',
    reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    review_reason TEXT NOT NULL DEFAULT '',
    appeal_used INTEGER NOT NULL DEFAULT 0,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (item_id, claimer_id)
);
CREATE TABLE IF NOT EXISTS claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    handover_type TEXT NOT NULL CHECK (handover_type IN
        ('ADMIN_RECEIPT', 'ADMIN_RELEASE', 'DIRECT_RETURN', 'DISPUTE_RESOLUTION', 'CORRECTION')),
    released_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    received_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    staff_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    confirmed INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    party TEXT NOT NULL CHECK (party IN ('finder', 'claimant', 'staff')),
    outcome TEXT NOT NULL CHECK (outcome IN ('confirmed', 'disputed')),
    note TEXT NOT NULL DEFAULT '',
    recorded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE (claim_id, user_id)
);
CREATE TABLE IF NOT EXISTS category_policy (
    category TEXT PRIMARY KEY,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS found_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    finder_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    contact TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'returned', 'not_mine', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (item_id, finder_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'info',
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    record_type TEXT,
    record_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status_type_created ON items(status, type, created_at);
CREATE INDEX IF NOT EXISTS idx_items_category_status ON items(category, status);
CREATE INDEX IF NOT EXISTS idx_items_custody ON items(custody_status, status);
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);
CREATE INDEX IF NOT EXISTS idx_claims_item_status_created ON claims(item_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_claims_claimer_status ON claims(claimer_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_one_approved ON claims(item_id) WHERE status = 'approved';
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_custody_events_item ON custody_events(item_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user_read_created
    ON notifications(user_id, is_read, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_created
    ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_email_ts
    ON login_attempts(email, ts);
"""


# ================================================================ database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q1(sql, *params):
    return get_db().execute(sql, params).fetchone()


def qa(sql, *params):
    return get_db().execute(sql, params).fetchall()


def run(sql, *params):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def now():
    return datetime.now().strftime(TS)


NAME_RE = re.compile(r"^[^\W\d_]+(?:[ .'\u2019\-]+[^\W\d_]+)*\.?$")
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_/\-]{1,19}$")
AUTO_END_NOTE = "Course ended on "


def validate_name(name):
    if not (2 <= len(name) <= 60) or not NAME_RE.match(name):
        return "Enter a valid full name: 2 to 60 letters, with spaces, dots, hyphens or apostrophes only."
    return None


def validate_id_number(value):
    if not ID_RE.match(value):
        return "Enter a valid ID number: 2 to 20 letters or digits (a dash, slash or underscore is allowed)."
    return None


def validate_email(email):
    if len(email) > 120 or ".." in email or not EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def validate_end_date(value, required=False):
    """Course end date for students: a real date, today or later, and not more than 8 years ahead."""
    value = (value or "").strip()
    if not value:
        return "Enter the course end date for a student." if required else None
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return "Enter a valid course end date."
    today = datetime.now().date()
    if d < today:
        return "The course end date cannot be in the past."
    if d > today + timedelta(days=366 * 8):
        return "The course end date is too far ahead."
    return None


def roster_row_error(id_number, name, email, role, dept, end, require_end=False):
    """One place that decides whether a campus-list entry is acceptable. Returns a message or None."""
    if role not in ("student", "staff"):
        return "Choose whether the person is a student or a staff member."
    return (validate_id_number(id_number) or validate_name(name) or validate_email(email)
            or ("Choose the department (or pick Other and type it)." if len(dept) < 2 else None)
            or ("Department is too long." if len(dept) > 80 else None)
            or (validate_end_date(end, required=require_end) if role == "student" else None))


def import_roster(db, text, errors=None):
    """Add or update roster rows from CSV text. Returns (added, updated, skipped).
    Optional column end_date (YYYY-MM-DD) is the last day of a student's course; blank means no automatic end.
    If `errors` is a list, one (line_number, id_number, reason) tuple is appended for every skipped row, so the
    caller can explain exactly what was wrong. An ID that appears twice in one file is imported once (first row)."""
    reader = csv.DictReader(io.StringIO(text))
    names = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    if not {"id_number", "full_name", "email", "role"} <= names:
        raise ValueError("The CSV needs these columns: id_number, full_name, email, role, department "
                         "(and optionally end_date).")
    has_end = bool({"end_date", "academic_end"} & names)
    added = updated = skipped = 0
    seen = set()

    def skip(line, ident, reason):
        nonlocal skipped
        skipped += 1
        if errors is not None:
            errors.append((line, ident, reason))

    for n, raw in enumerate(reader):
        line = n + 2  # line 1 is the header
        if n >= 5000:
            skip(line, "", "Over the 5,000-row limit for one import.")
            continue
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        id_number = row.get("id_number", "").upper()
        name, email = row.get("full_name", ""), row.get("email", "").lower()
        role, dept = row.get("role", "").lower(), row.get("department", "")
        end = row.get("end_date", "") or row.get("academic_end", "")
        problem = roster_row_error(id_number, name, email, role, dept, end)
        if problem:
            skip(line, id_number, problem)
            continue
        if id_number in seen:
            skip(line, id_number, "This ID number appears more than once in the file; only the first row was used.")
            continue
        seen.add(id_number)
        if role != "student":
            end = ""
        if db.execute("SELECT 1 FROM roster WHERE id_number = ?", (id_number,)).fetchone():
            if has_end or role != "student":
                db.execute("UPDATE roster SET full_name = ?, email = ?, role = ?, department = ?, academic_end = ? "
                           "WHERE id_number = ?", (name, email, role, dept, end, id_number))
            else:
                db.execute("UPDATE roster SET full_name = ?, email = ?, role = ?, department = ? "
                           "WHERE id_number = ?", (name, email, role, dept, id_number))
            updated += 1
        else:
            db.execute("INSERT INTO roster (id_number, full_name, email, role, department, academic_end) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (id_number, name, email, role, dept, end))
            added += 1
    db.commit()
    return added, updated, skipped


POST_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_log(record_type, record_id);
CREATE INDEX IF NOT EXISTS idx_notifications_kind ON notifications(user_id, kind);
"""
SCHEMA_VERSION = 2


def _columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def ensure_columns(db):
    """Add columns that older databases lack (SQLite cannot alter CHECK constraints, but plain ADD COLUMN is fine)."""
    wanted = {
        "users": [("is_custody_officer", "INTEGER NOT NULL DEFAULT 0"), ("date_of_birth", "TEXT NOT NULL DEFAULT ''"),
                  ("academic_end", "TEXT NOT NULL DEFAULT ''")],
        "roster": [("academic_end", "TEXT NOT NULL DEFAULT ''")],
        "items": [("contact", "TEXT NOT NULL DEFAULT ''")],
        "notifications": [("kind", "TEXT NOT NULL DEFAULT 'info'")],
        "audit_log": [("record_type", "TEXT"), ("record_id", "INTEGER")],
    }
    for table, cols in wanted.items():
        have = _columns(db, table)
        for name, ddl in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_legacy(db):
    """One-time migration from the first release (items.status open/claimed/returned, items.custody
    finder/security, handover codes) to the separate report / custody / claim lifecycles.

    Rules applied to old data:
    - every old listing was already public, so it becomes `active` (moderation is not retroactive);
    - an old 'handed to the Security Office' answer was only a form value, never staff-confirmed, so it
      becomes `admin_handover_requested`, NOT `with_admin`; staff must confirm receipt to change that;
    - old handover codes are discarded; an approved claim on a not-yet-returned item stays approved and
      the item keeps waiting for the new two-party (or staff) confirmation;
    - claims on LOST reports are no longer supported and are closed with an explanation.
    """
    cols = _columns(db, "items")
    if not cols or "custody_status" in cols:
        return False
    for ix in ("idx_items_status_type_created", "idx_items_category_status",
               "idx_claims_item_status_created", "idx_claims_claimer_status"):
        db.execute(f"DROP INDEX IF EXISTS {ix}")
    # Without this, SQLite 3.26+ rewrites other tables' foreign keys (claim_evidence, custody_events, ...)
    # to point at the temporary *_v1 tables, which are dropped below and leave those keys dangling.
    db.execute("PRAGMA legacy_alter_table = ON")
    db.execute("ALTER TABLE claims RENAME TO claims_v1")
    db.execute("ALTER TABLE items RENAME TO items_v1")
    db.execute("PRAGMA legacy_alter_table = OFF")
    db.executescript(SCHEMA)
    ensure_columns(db)
    ts = now()
    approved = {}
    for c in db.execute("SELECT * FROM claims_v1 ORDER BY id").fetchall():
        item = db.execute("SELECT * FROM items_v1 WHERE id = ?", (c["item_id"],)).fetchone()
        if item is None:
            continue
        status, reason, handler = c["status"], "", "finder"
        if item["type"] == "lost" and status in ("pending", "approved"):
            status, reason = "rejected", "Claims on lost reports are no longer supported."
        elif status == "approved" and item["status"] != "claimed" and item["status"] != "returned":
            status = "rejected"
        db.execute("INSERT INTO claims (id, item_id, claimer_id, message, status, handler, review_reason, decided_at, "
                   "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (c["id"], c["item_id"], c["claimer_id"], c["message"], status, handler, reason,
                    None if status == "pending" else c["created_at"], c["created_at"], c["created_at"]))
        if status == "approved":
            approved[c["item_id"]] = c["id"]
    for i in db.execute("SELECT * FROM items_v1 ORDER BY id").fetchall():
        risk = services.DEFAULT_RISK.get(i["category"], "medium")
        status = "returned" if i["status"] == "returned" else "active"
        pref = custody = None
        if i["type"] == "found":
            pref = "admin" if i["custody"] == "security" else "direct"
            custody = "admin_handover_requested" if pref == "admin" else "with_finder"
            if i["status"] == "returned":
                custody = "returned"
            elif i["status"] == "claimed" and pref == "direct" and risk != "high" and i["id"] in approved:
                custody = "direct_handover_pending"
        year = (i["created_at"] or ts)[:4]
        db.execute(
            "INSERT INTO items (id, ref_code, user_id, type, title, category, description, location, event_date, photo, "
            "status, custody_status, custody_prior, handover_pref, risk_level, approved_claim_id, created_at, "
            "updated_at, returned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i["id"], f"LF-{year}-{i['id']:05d}", i["user_id"], i["type"], i["title"], i["category"], i["description"],
             i["location"], i["event_date"], i["photo"], status, custody,
             "with_finder" if custody == "direct_handover_pending" else None, pref, risk,
             approved.get(i["id"]), i["created_at"], i["created_at"], i["returned_at"]))
    db.execute("DROP TABLE claims_v1")
    db.execute("DROP TABLE items_v1")
    db.execute("INSERT INTO audit_log (actor_id, action, detail, created_at) VALUES (NULL, 'schema_migrated', ?, ?)",
               ("Upgraded to schema version 2 (separate report, custody and claim lifecycles).", ts))
    return True


def repair_dangling_v1_references(db):
    """Heal a database upgraded by an earlier build of migrate_legacy.

    That build left foreign keys in claim_evidence, custody_events, handover_confirmations and
    found_offers pointing at the dropped tables items_v1 / claims_v1, which made any DELETE FROM users
    (for example the first step of registration) fail with "no such table: main.items_v1". The keys are
    pointed back at items / claims. A backup copy of the database is written first. Returns True if repaired."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if {"items_v1", "claims_v1"} & tables:
        return False                                   # an upgrade is mid-way; do not touch
    broken = db.execute("SELECT type, name, sql FROM sqlite_master "
                        "WHERE sql LIKE '%items_v1%' OR sql LIKE '%claims_v1%'").fetchall()
    if not broken:
        return False
    db.commit()
    backup_path = f"{DB_PATH}.backup-before-repair"
    if not os.path.exists(backup_path):
        backup = sqlite3.connect(backup_path)
        try:
            db.backup(backup)
        finally:
            backup.close()
    version = db.execute("PRAGMA schema_version").fetchone()[0]
    db.execute("PRAGMA writable_schema = ON")
    for kind, name, sql in broken:
        fixed = sql.replace("items_v1", "items").replace("claims_v1", "claims")
        db.execute("UPDATE sqlite_master SET sql = ? WHERE type = ? AND name = ?", (fixed, kind, name))
    db.execute(f"PRAGMA schema_version = {version + 1}")
    db.execute("PRAGMA writable_schema = OFF")
    db.commit()
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError(f"Database repair failed its integrity check. Your data is safe in {backup_path}.")
    print(f"[database] Repaired {len(broken)} table definitions. A backup was saved as {backup_path}", flush=True)
    return True


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    try:
        fresh = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone() is None
        if not fresh:
            migrate_legacy(db)
            repair_dangling_v1_references(db)
        db.executescript(SCHEMA)
        ensure_columns(db)
        db.executescript(POST_SCHEMA)
        for category, risk in services.DEFAULT_RISK.items():
            db.execute("INSERT OR IGNORE INTO category_policy (category, risk_level, updated_at) VALUES (?, ?, ?)",
                       (category, risk, now()))
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        admins = db.execute(
            "SELECT id, password_hash FROM users WHERE role = 'admin'"
        ).fetchall()
        admin_password = os.environ.get("ADMIN_PASSWORD", "")
        if not admins:
            if APP_ENV == "production" and (
                not admin_password
                or admin_password == "Admin@123"
                or validate_password(admin_password)
            ):
                raise RuntimeError(
                    "Set a unique ADMIN_PASSWORD with at least 8 characters, a letter, and a number."
                )
            password = admin_password or "Admin@123"
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, status, department, verification, created_at) "
                "VALUES ('Campus Admin', 'admin@campus.edu', ?, 'admin', 'active', 'Administration', 'roster', ?)",
                (generate_password_hash(password), now()),
            )
        elif APP_ENV == "production":
            default_admins = [
                admin
                for admin in admins
                if check_password_hash(admin["password_hash"], "Admin@123")
            ]
            if default_admins:
                if (
                    not admin_password
                    or admin_password == "Admin@123"
                    or validate_password(admin_password)
                ):
                    raise RuntimeError(
                        "The imported admin account uses the default password. Set a unique ADMIN_PASSWORD before production startup."
                    )
                for admin in default_admins:
                    db.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(admin_password), admin["id"]),
                    )
        if fresh and os.path.isfile(SAMPLE_ROSTER):
            with open(SAMPLE_ROSTER, encoding="utf-8-sig") as fh:
                import_roster(db, fh.read())
        db.commit()
    finally:
        db.close()


# ================================================================ helpers
def audit(action, detail="", actor="current", rtype=None, rid=None):
    if actor == "current":
        user = current_user()
        actor = user["id"] if user else None
    run("INSERT INTO audit_log (actor_id, action, detail, record_type, record_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", actor, action, detail, rtype, rid, now())


def notify(user_id, message, link="", kind="info"):
    run("INSERT INTO notifications (user_id, message, link, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        user_id, message, link, kind, now())


def build_html_email(subject, body, campus_name=None):
    """Creates a modern, responsive HTML email matching campus branding."""
    campus = campus_name or CAMPUS_NAME
    otp_match = re.search(r"\b(\d{6})\b", body)
    otp_code = otp_match.group(1) if otp_match else None

    lines = [line.strip() for line in body.split("\n") if line.strip()]
    greeting = lines[0] if lines and lines[0].lower().startswith("hello") else ""
    remaining_lines = lines[1:] if greeting else lines

    cleaned_paragraphs = []
    for l in remaining_lines:
        if otp_code and otp_code in l:
            continue
        cleaned_paragraphs.append(l)

    paragraphs_html = "".join(
        f'<p style="margin: 0 0 16px; color: #334155; font-size: 15px; line-height: 1.6;">{html.escape(p)}</p>'
        for p in cleaned_paragraphs
    )

    otp_block = ""
    if otp_code:
        otp_block = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin: 24px 0; background: #f8fafc; border: 1.5px solid #cbd5e1; border-radius: 12px; text-align: center;">
          <tr>
            <td style="padding: 24px 16px;">
              <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: #64748b; margin-bottom: 8px;">
                Verification Code
              </div>
              <div style="font-family: 'SF Mono', Consolas, 'Liberation Mono', Menlo, Courier, monospace; font-size: 38px; font-weight: 800; letter-spacing: 10px; color: #0b3249; margin: 6px 0 10px; text-indent: 10px;">
                {otp_code}
              </div>
              <div style="display: inline-block; font-size: 12px; color: #475569; background: #e2e8f0; padding: 4px 12px; border-radius: 20px; font-weight: 500;">
                ⏱ Expires in {OTP_MINUTES} minutes · Single-use only
              </div>
            </td>
          </tr>
        </table>
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 20px; background: #f1f5f9; border-left: 4px solid #0b3249; border-radius: 4px;">
          <tr>
            <td style="padding: 12px 16px; font-size: 13px; color: #475569; line-height: 1.5;">
              <strong>Security tip:</strong> Never share this code with anyone. Campus staff will never ask for your verification code.
            </td>
          </tr>
        </table>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(subject)}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background-color: #f1f5f9; padding: 36px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width: 540px; background: #ffffff; border-radius: 14px; overflow: hidden; box-shadow: 0 4px 16px rgba(15, 23, 42, 0.08); border: 1px solid #e2e8f0;">
          <tr>
            <td style="background: linear-gradient(135deg, #0b3249 0%, #174b6b 100%); padding: 26px 32px; text-align: left;">
              <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
                <tr>
                  <td>
                    <div style="color: #ffffff; font-size: 18px; font-weight: 800; letter-spacing: -0.02em;">
                      {html.escape(campus)} Lost &amp; Found
                    </div>
                    <div style="color: #94a3b8; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; margin-top: 3px;">
                      Verified Campus Members
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding: 32px 32px 24px;">
              <h1 style="margin: 0 0 16px; font-size: 22px; font-weight: 700; color: #0f172a; letter-spacing: -0.02em;">
                {html.escape(subject)}
              </h1>
              {f'<p style="margin: 0 0 16px; font-size: 16px; font-weight: 600; color: #0f172a;">{html.escape(greeting)}</p>' if greeting else ''}
              {paragraphs_html}
              {otp_block}
            </td>
          </tr>
          <tr>
            <td style="background: #f8fafc; border-top: 1px solid #e2e8f0; padding: 20px 32px; text-align: center;">
              <p style="margin: 0 0 6px; font-size: 12px; color: #64748b; line-height: 1.5;">
                This is an automated security message from <strong>{html.escape(campus)} Lost &amp; Found</strong>.
              </p>
              <p style="margin: 0; font-size: 11px; color: #94a3b8;">
                If you did not initiate this request, you can safely ignore this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_mail(to, subject, body, html_body=None):
    """Returns 'email', 'console' (demo mode) or 'failed'."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        if app.config["APP_ENV"] == "production":
            app.logger.error("Email delivery is unavailable because SMTP_HOST is not configured.")
            return "failed"
        print(f"\n[DEMO MODE - SMTP not configured]\nTo: {to}\nSubject: {subject}\n{body}\n", flush=True)
        return "console"

    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if bool(smtp_user) != bool(smtp_pass):
        app.logger.error("Email delivery is unavailable because SMTP_USER and SMTP_PASS must be configured together.")
        return "failed"
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        if not 1 <= port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535.")
        msg = EmailMessage()
        msg["From"] = os.environ.get("MAIL_FROM", smtp_user or "noreply@campus.local")
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if smtp_user:
                smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)
        return "email"
    except Exception:  # noqa: BLE001 - failures must not crash the request or log message contents
        app.logger.error("Email delivery failed.")
        return "failed"


def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    if "user" not in g:
        user = q1("SELECT * FROM users WHERE id = ?", uid)
        if (user is not None and user["role"] == "student" and user["status"] == "active"
                and user["academic_end"] and user["academic_end"] < datetime.now().strftime("%Y-%m-%d")):
            expire_students(only_id=user["id"])
            user = q1("SELECT * FROM users WHERE id = ?", uid)
        g.user = user if user is not None and user["status"] == "active" else None
    if g.user is None:
        session.pop("user_id", None)
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def reviewer_required(view):
    """Admins and custody officers (staff an admin authorized)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        if not services.is_reviewer(user):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


def validate_password(pw):
    if len(pw) > 128:
        return "Password can be at most 128 characters."
    if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"[0-9]", pw):
        return "Password must be at least 8 characters and include a letter and a number."
    return None


def validate_phone(phone, required=False):
    if not phone:
        return "Enter your phone number." if required else None
    if not PHONE_RE.match(phone):
        return "Enter a valid phone number, for example 98765 43210."
    return None


def validate_dob(value):
    """Registration needs a real date of birth: not in the future and not absurdly old or young."""
    try:
        born = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return "Enter your date of birth."
    today = datetime.now().date()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if born > today or age < 14 or age > 100:
        return "Enter your real date of birth."
    return None


# Everyone belongs to a department, not a course. MCA, BCA, BSc CS etc. all sit under Computer Science.
# Students pick from the academic departments; employees can also pick administrative / support ones.
ACADEMIC_DEPARTMENTS = [
    "Computer Science", "Commerce & Management", "Science", "Arts & Humanities",
    "Electronics", "Mechanical", "Civil", "Information Science", "Business Administration",
]
ADMIN_DEPARTMENTS = [
    "Administration Office", "Accounts & Finance", "Admissions", "Examination Cell",
    "Human Resources", "Registrar Office",
]
SUPPORT_DEPARTMENTS = [
    "Library", "Security Office", "IT Support", "Hostel", "Transport", "Maintenance & Housekeeping",
    "Canteen", "Sports",
]
# (group label, options, who can pick it)
DEPARTMENT_GROUPS = [
    ("Academic departments", ACADEMIC_DEPARTMENTS, "all"),
    ("Administrative departments", ADMIN_DEPARTMENTS, "staff"),
    ("Support & services", SUPPORT_DEPARTMENTS, "staff"),
]
STAFF_ONLY_DEPARTMENTS = {d.lower() for d in ADMIN_DEPARTMENTS + SUPPORT_DEPARTMENTS}


def chosen_department(f):
    """The department from the dropdown, or the typed text when 'Other' was picked."""
    value = f.get("department", "").strip()
    if value == "__other__":
        value = f.get("department_other", "").strip()
    return value[:80]


@app.template_global()
def department_groups():
    """[(label, [(value, text)], audience)] - audience 'all' or 'staff'. Roster-only departments are appended."""
    known = {d.lower() for _g, opts, _a in DEPARTMENT_GROUPS for d in opts}
    groups = [(g, [(d, d) for d in opts], aud) for g, opts, aud in DEPARTMENT_GROUPS]
    extra = [r["department"] for r in qa("SELECT DISTINCT department FROM roster WHERE department != '' "
                                         "ORDER BY department") if r["department"].lower() not in known]
    if extra:
        groups.append(("Other campus departments", [(d, d) for d in extra], "all"))
    return groups


def validate_registration_extras(f, phone, password):
    """Checks shared by both registration forms: phone, date of birth, department, password twice."""
    dept = chosen_department(f)
    if len(dept) < 2:
        return "Choose your department."
    if f.get("role") == "student" and dept.lower() in STAFF_ONLY_DEPARTMENTS:
        return "That department is for staff. Students: pick your academic department."
    return (validate_phone(phone, required=True) or validate_dob(f.get("dob", ""))
            or validate_password(password)
            or (None if password == f.get("confirm_password", "") else "The two passwords do not match."))


@app.template_global()
def today_iso():
    return datetime.now().strftime("%Y-%m-%d")


def sniff_image(head):
    return (head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"\xff\xd8\xff"
            or head[:6] in (b"GIF87a", b"GIF89a") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def save_image(file, folder):
    """Validate and store an uploaded image. Returns the stored filename or None."""
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("Photo must be a PNG, JPG, GIF or WEBP image.")
    head = file.stream.read(16)
    file.stream.seek(0)
    if not sniff_image(head):
        raise ValueError("That file is not a valid image.")
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(folder, name))
    return name


def delete_file(folder, name):
    if name:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            os.remove(path)


def mask_email(email):
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def site_stats():
    """Aggregate counts only (no listing details), safe to show on the public landing page."""
    return q1("SELECT COALESCE(SUM(type='lost' AND status='active'), 0) AS lost, "
              "COALESCE(SUM(type='found' AND status='active'), 0) AS found, "
              "COALESCE(SUM(status='returned'), 0) AS returned FROM items")


def get_item_or_404(item_id):
    item = q1("SELECT items.*, users.name AS owner_name, users.email AS owner_email, "
              "users.role AS owner_role, users.department AS owner_dept "
              "FROM items JOIN users ON users.id = items.user_id WHERE items.id = ?", item_id)
    if item is None:
        abort(404)
    return item


def is_staff(user=None):
    """Authorized college staff: an admin, or a staff member flagged as a custody officer."""
    return services.is_reviewer(user if user is not None else current_user())


def can_view_item(item, user):
    """Published reports are visible to members; everything else only to the reporter and staff."""
    if item["status"] in ("active", "returned"):
        return True
    return user is not None and (user["id"] == item["user_id"] or services.is_reviewer(user))


def act(fn, *args, ok="", **kwargs):
    """Run one workflow service call and flash the outcome. Returns the result, or None if it was refused.
    403/404 refusals become real HTTP errors; everything else is shown to the user as a message."""
    try:
        result = fn(get_db(), *args, **kwargs)
    except WorkflowError as exc:
        if exc.status == 403:
            abort(403)
        if exc.status == 404:
            abort(404)
        flash(exc.message, "error")
        return None
    if ok:
        flash(ok, "success")
    return True if result is None else result


def like(text):
    """Escape LIKE wildcards in user input."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def issue_otp(user):
    code = f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.now() + timedelta(minutes=OTP_MINUTES)).strftime(TS)
    run("INSERT OR REPLACE INTO otp_codes (user_id, code_hash, expires_at, tries, sent_at) "
        "VALUES (?, ?, ?, 0, ?)", user["id"], otp_hash(user["id"], code), expires, now())
    subject = f"{CAMPUS_NAME} Lost & Found verification code"
    plain = (
        f"Hello {user['name']},\n\n"
        f"Your verification code is {code}. It expires in {OTP_MINUTES} minutes.\n\n"
        "If you did not try to register, you can ignore this email."
    )
    html = build_html_email(subject, plain, campus_name=CAMPUS_NAME)
    return send_mail(user["email"], subject, plain, html_body=html)


def otp_hash(user_id, code):
    return hashlib.sha256(f"{user_id}:{code}:{app.config['SECRET_KEY']}".encode()).hexdigest()


def flash_mail_result(result, email):
    if result == "email":
        flash(f"We sent a 6-digit code to {mask_email(email)}.", "success")
    elif result == "console":
        flash("Demo mode: email is not set up, so the code was printed in the server console.", "info")
    else:
        flash("We could not send the email. Please try again in a minute.", "error")


def too_many_attempts(email):
    since = (datetime.now() - timedelta(minutes=LOGIN_WINDOW_MIN)).strftime(TS)
    return q1("SELECT COUNT(*) AS c FROM login_attempts WHERE email = ? AND ts >= ?",
              email, since)["c"] >= LOGIN_MAX_FAILS


def record_failure(email):
    run("INSERT INTO login_attempts (email, ts) VALUES (?, ?)", email, now())
    day_ago = (datetime.now() - timedelta(days=1)).strftime(TS)
    run("DELETE FROM login_attempts WHERE ts < ?", day_ago)


def expire_students(only_id=None):
    """Disable student accounts whose course end date has passed (the end date itself is still allowed).
    Returns how many were disabled. Their open claims and handovers are handled like a suspension."""
    today = datetime.now().strftime("%Y-%m-%d")
    sql = ("SELECT id, name, id_number, academic_end FROM users WHERE role = 'student' AND status = 'active' "
           "AND academic_end != '' AND academic_end < ?")
    params = [today]
    if only_id is not None:
        sql += " AND id = ?"
        params.append(only_id)
    rows = qa(sql, *params)
    for u in rows:
        run("UPDATE users SET status = 'suspended', review_note = ? WHERE id = ?",
            f"{AUTO_END_NOTE}{u['academic_end']} (automatic)", u["id"])
        audit("user_auto_disabled", f"{u['name']} ({u['id_number']}) course ended {u['academic_end']}",
              actor=None, rtype="user", rid=u["id"])
        services.handle_user_suspended(get_db(), u["id"], None)
    return len(rows)


_last_sweep = [0.0]


@app.before_request
def sweep_expired_students():
    """At most once a minute, close every student account whose course has ended."""
    if request.endpoint == "static" or time.time() - _last_sweep[0] < 60:
        return
    _last_sweep[0] = time.time()
    expire_students()


# ================================================================ CSRF, headers, context
@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not secrets.compare_digest(token, session["csrf"]):
            abort(400, "Invalid or missing form token. Please go back and try again.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.context_processor
def inject_globals():
    user = current_user()
    unread = 0
    if user:
        unread = q1("SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0",
                    user["id"])["c"]
    return {"me": user, "csrf_token": session.get("csrf", ""), "categories": CATEGORIES,
            "campus": CAMPUS_NAME, "unread": unread, "is_staff": services.is_reviewer(user) if user else False}


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return value


@app.template_filter("stamp")
def stamp(value):
    return (value or "")[:16]


@app.template_filter("report_label")
def report_label(value):
    return services.REPORT_LABELS.get(value, value)


@app.template_filter("custody_label")
def custody_label(value):
    return services.CUSTODY_LABELS.get(value, value or "")


@app.template_filter("claim_label")
def claim_label(value):
    return services.CLAIM_LABELS.get(value, value)


@app.template_filter("risk_label")
def risk_label(value):
    return services.RISK_LABELS.get(value, value)


@app.template_filter("role_label")
def role_label(value):
    return {"student": "Student", "staff": "Staff", "admin": "Admin"}.get(value, value)


# ================================================================ registration
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        f = request.form
        role = f.get("role", "")
        id_number = f.get("id_number", "").strip().upper()
        email = f.get("email", "").strip().lower()
        phone = f.get("phone", "").strip()
        password = f.get("password", "")
        error = None
        if role not in ("student", "staff"):
            error = "Choose whether you are a student or a staff member."
        elif not id_number:
            error = "Enter your roll number or employee ID."
        elif validate_id_number(id_number):
            error = validate_id_number(id_number)
        elif validate_email(email):
            error = "Enter the email address registered with the college."
        else:
            error = validate_registration_extras(f, phone, password)
        if error:
            flash(error, "error")
            return render_template("register.html", form=f)

        row = q1("SELECT * FROM roster WHERE id_number = ? AND role = ?", id_number, role)
        if row is None or row["email"].lower() != email:
            audit("register_no_match", f"{role} {id_number} {email}", actor=None)
            flash("These details do not match the campus records. Check your ID number and "
                  "use the email address registered with the college.", "error")
            return render_template("register.html", form=f, show_manual=True)

        if role == "student" and row["academic_end"] and row["academic_end"] < datetime.now().strftime("%Y-%m-%d"):
            flash(f"Your course period ended on {row['academic_end']}, so you can no longer register. "
                  "Contact the campus admin if this is a mistake.", "error")
            return render_template("register.html", form=f)

        db = get_db()
        # An unverified earlier attempt must never block the real owner of this ID.
        db.execute("DELETE FROM users WHERE status = 'pending_email' AND (email = ? OR id_number = ?)",
                   (email, id_number))
        db.commit()
        if q1("SELECT 1 FROM users WHERE email = ? OR id_number = ?", email, id_number):
            flash("An account already exists for this ID or email. Try logging in.", "error")
            return render_template("register.html", form=f)
        cur = db.execute(
            "INSERT INTO users (name, email, phone, password_hash, role, status, id_number, department, "
            "verification, date_of_birth, academic_end, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending_email', ?, ?, 'roster', ?, ?, ?)",
            (row["full_name"], email, phone, generate_password_hash(password), row["role"],
             id_number, chosen_department(f), f.get("dob", "").strip(),
             row["academic_end"] if row["role"] == "student" else "", now()))
        db.commit()
        user = q1("SELECT * FROM users WHERE id = ?", cur.lastrowid)
        session["pending_user"] = user["id"]
        flash_mail_result(issue_otp(user), email)
        return redirect(url_for("verify"))
    return render_template("register.html", form={})


@app.route("/register/manual", methods=["GET", "POST"])
def register_manual():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        f = request.form
        role = f.get("role", "")
        name = f.get("name", "").strip()
        id_number = f.get("id_number", "").strip().upper()
        email = f.get("email", "").strip().lower()
        phone = f.get("phone", "").strip()
        dept = chosen_department(f)
        password = f.get("password", "")
        card = request.files.get("id_card")
        error = None
        if role not in ("student", "staff"):
            error = "Choose whether you are a student or a staff member."
        elif validate_name(name):
            error = validate_name(name)
        elif not id_number:
            error = "Enter the number printed on your ID card."
        elif validate_id_number(id_number):
            error = validate_id_number(id_number)
        elif validate_email(email):
            error = validate_email(email)
        elif len(dept) < 2:
            error = "Choose your department."
        elif not card or not card.filename:
            error = "Upload a clear photo of your college ID card."
        else:
            error = validate_registration_extras(f, phone, password)
        if error:
            flash(error, "error")
            return render_template("register_manual.html", form=f)
        db = get_db()
        db.execute("DELETE FROM users WHERE status = 'pending_email' AND (email = ? OR id_number = ?)",
                   (email, id_number))
        db.commit()
        if q1("SELECT 1 FROM users WHERE email = ? OR id_number = ?", email, id_number):
            flash("An account or request already exists for this ID or email.", "error")
            return render_template("register_manual.html", form=f)
        try:
            stored = save_image(card, CARD_DIR)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("register_manual.html", form=f)
        try:
            db.execute(
                "INSERT INTO users (name, email, phone, password_hash, role, status, id_number, department, "
                "verification, id_card, date_of_birth, created_at) VALUES (?, ?, ?, ?, ?, 'pending_approval', ?, ?, "
                "'manual', ?, ?, ?)",
                (name, email, phone, generate_password_hash(password), role, id_number, dept[:80], stored,
                 f.get("dob", "").strip(), now()))
            db.commit()
        except sqlite3.IntegrityError:
            delete_file(CARD_DIR, stored)
            flash("An account or request already exists for this ID or email.", "error")
            return render_template("register_manual.html", form=f)
        for admin in qa("SELECT id FROM users WHERE role = 'admin' AND status = 'active'"):
            notify(admin["id"], f"New registration request from {name} ({role}).",
                   url_for("admin_requests"))
        flash("Request sent. An admin will check your ID card. You can log in after approval.", "success")
        return redirect(url_for("login"))
    return render_template("register_manual.html", form={})


@app.route("/verify", methods=["GET", "POST"])
def verify():
    uid = session.get("pending_user")
    user = q1("SELECT * FROM users WHERE id = ? AND status = 'pending_email'", uid) if uid else None
    if user is None:
        session.pop("pending_user", None)
        flash("Start by creating your account.", "info")
        return redirect(url_for("register"))
    if request.method == "POST":
        code = re.sub(r"\D", "", request.form.get("code", ""))
        row = q1("SELECT * FROM otp_codes WHERE user_id = ?", user["id"])
        if row is None:
            flash("There is no active code. Request a new one.", "error")
        elif now() > row["expires_at"]:
            run("DELETE FROM otp_codes WHERE user_id = ?", user["id"])
            flash("That code has expired. Request a new one.", "error")
        elif row["tries"] >= OTP_MAX_TRIES:
            run("DELETE FROM otp_codes WHERE user_id = ?", user["id"])
            flash("Too many wrong attempts. Request a new code.", "error")
        elif secrets.compare_digest(otp_hash(user["id"], code), row["code_hash"]):
            run("UPDATE users SET status = 'active' WHERE id = ?", user["id"])
            run("DELETE FROM otp_codes WHERE user_id = ?", user["id"])
            audit("registered", f"{user['role']} {user['id_number']} verified by email", actor=user["id"])
            session.clear()
            session["user_id"] = user["id"]
            flash("Email verified. Welcome to the campus portal.", "success")
            return redirect(url_for("index"))
        else:
            tries = row["tries"] + 1
            run("UPDATE otp_codes SET tries = ? WHERE user_id = ?", tries, user["id"])
            if tries >= OTP_MAX_TRIES:
                run("DELETE FROM otp_codes WHERE user_id = ?", user["id"])
                flash("Too many wrong attempts. Request a new code.", "error")
            else:
                flash(f"That code is wrong. {OTP_MAX_TRIES - tries} tries left.", "error")
    return render_template("verify.html", masked=mask_email(user["email"]))


@app.route("/verify/resend", methods=["POST"])
def verify_resend():
    uid = session.get("pending_user")
    user = q1("SELECT * FROM users WHERE id = ? AND status = 'pending_email'", uid) if uid else None
    if user is None:
        return redirect(url_for("register"))
    row = q1("SELECT * FROM otp_codes WHERE user_id = ?", user["id"])
    if row is not None:
        waited = (datetime.now() - datetime.strptime(row["sent_at"], TS)).total_seconds()
        if waited < OTP_RESEND_SECONDS:
            flash(f"Please wait {int(OTP_RESEND_SECONDS - waited) + 1} seconds before asking for a new code.", "error")
            return redirect(url_for("verify"))
    flash_mail_result(issue_otp(user), user["email"])
    return redirect(url_for("verify"))


# ================================================================ login
@app.route("/login", methods=["GET", "POST"])
def login():
    me = current_user()
    if me:
        return redirect(url_for("admin_home" if me["role"] == "admin" else "index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if validate_email(email) or not password or len(password) > 128:
            flash("Enter a valid email address and your password.", "error")
            return render_template("login.html", form=request.form)
        if too_many_attempts(email):
            flash(f"Too many failed attempts. Try again in {LOGIN_WINDOW_MIN} minutes.", "error")
            return render_template("login.html", form=request.form), 429
        user = q1("SELECT * FROM users WHERE email = ?", email)
        if user is None or not check_password_hash(user["password_hash"], password):
            record_failure(email)
            flash("Email or password is incorrect.", "error")
            return render_template("login.html", form=request.form)
        run("DELETE FROM login_attempts WHERE email = ?", email)
        if user["role"] == "student" and user["status"] == "active":
            expire_students(only_id=user["id"])
            user = q1("SELECT * FROM users WHERE id = ?", user["id"])
        status = user["status"]
        if status == "pending_email":
            session["pending_user"] = user["id"]
            otp = q1("SELECT * FROM otp_codes WHERE user_id = ?", user["id"])
            if otp is None or now() > otp["expires_at"]:
                flash_mail_result(issue_otp(user), user["email"])
            else:
                flash("Enter the code we emailed you to finish registering.", "info")
            return redirect(url_for("verify"))
        if status == "pending_approval":
            flash("Your registration is waiting for admin approval.", "info")
            return render_template("login.html", form=request.form)
        if status == "rejected":
            reason = f" Reason: {user['review_note']}" if user["review_note"] else ""
            flash("Your registration request was not approved." + reason, "error")
            return render_template("login.html", form=request.form)
        if status == "suspended" and user["review_note"].startswith(AUTO_END_NOTE):
            flash("Your course period has ended, so this account is closed. "
                  "Contact the campus admin if this is a mistake.", "error")
            return render_template("login.html", form=request.form)
        if status == "suspended":
            flash("This account has been suspended. Contact the campus admin.", "error")
            return render_template("login.html", form=request.form)
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = True
        flash(f"Welcome back, {user['name'].split()[0]}.", "success")
        nxt = request.args.get("next")
        if user["role"] == "admin" and not (nxt and safe_next(nxt).startswith("/admin")):
            return redirect(url_for("admin_home"))
        return redirect(safe_next(nxt))
    return render_template("login.html", form={})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ================================================================ listings
@app.route("/")
def index():
    if current_user() is None:
        return render_template("landing.html", stats=site_stats())
    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "")
    category = request.args.get("category", "")
    show = request.args.get("show", "open")
    sort = request.args.get("sort", "newest")
    if sort not in SORTS:
        sort = "newest"

    # Only published reports are public. Pending, rejected and closed reports never appear here.
    where, params = ["items.status IN ('active', 'returned')" if show == "all" else "items.status = 'active'"], []
    if q:
        where.append("(items.title LIKE ? ESCAPE '\\' OR items.description LIKE ? ESCAPE '\\' "
                     "OR items.location LIKE ? ESCAPE '\\' OR items.ref_code LIKE ? ESCAPE '\\')")
        params += [like(q)] * 4
    if kind in ("lost", "found"):
        where.append("items.type = ?")
        params.append(kind)
    if category in CATEGORIES:
        where.append("items.category = ?")
        params.append(category)
    clause = " AND ".join(where)

    total = q1(f"SELECT COUNT(*) AS c FROM items WHERE {clause}", *params)["c"]
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    items = qa(f"SELECT items.* FROM items WHERE {clause} ORDER BY {SORTS[sort][1]} LIMIT ? OFFSET ?",
               *params, PER_PAGE, (page - 1) * PER_PAGE)
    stats = site_stats()
    qargs = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("index.html", items=items, stats=stats, q=q, kind=kind, category=category,
                           show=show, sort=sort, sorts={k: v[0] for k, v in SORTS.items()},
                           page=page, pages=pages, total=total, qargs=qargs)


@app.route("/media/<name>")
@login_required
def media(name):
    item = q1("SELECT * FROM items WHERE photo = ?", name)
    if item is None or not can_view_item(item, current_user()):
        abort(404)
    return send_from_directory(ITEM_DIR, name)


@app.route("/evidence/<int:evidence_id>")
@login_required
def evidence_file(evidence_id):
    """Private claim evidence: only the claimant, the assigned reviewer and authorized staff."""
    ev = q1("SELECT * FROM claim_evidence WHERE id = ?", evidence_id)
    if ev is None:
        abort(404)
    claim = q1("SELECT * FROM claims WHERE id = ?", ev["claim_id"])
    item = q1("SELECT * FROM items WHERE id = ?", claim["item_id"])
    if not services.can_view_claim(current_user(), claim, item):
        abort(404)
    resp = send_from_directory(EVIDENCE_DIR, ev["filename"])
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _report_form(f):
    """Read and validate the shared report fields. Returns (values, error, field)."""
    v = {
        "kind": f.get("type", ""), "title": f.get("title", "").strip(), "category": f.get("category", ""),
        "description": f.get("description", "").strip(), "private_details": f.get("private_details", "").strip(),
        "location": f.get("location", "").strip(), "event_date": f.get("event_date", ""),
        "handover": f.get("handover", "direct"), "unsafe": f.get("unsafe") == "1",
        "contact": f.get("contact", "").strip(),
    }
    try:
        parsed = datetime.strptime(v["event_date"], "%Y-%m-%d").date()
    except ValueError:
        parsed = None
    error = field = None
    if v["kind"] not in ("lost", "found"):
        error, field = "Choose whether the item was lost or found.", "type"
    elif len(v["title"]) < 3:
        error, field = "Give the item a short title (at least 3 characters).", "title"
    elif len(v["title"]) > 80:
        error, field = "Keep the title under 80 characters.", "title"
    elif v["category"] not in CATEGORIES:
        error, field = "Choose a category.", "category"
    elif len(v["description"]) < 10:
        error, field = "Describe the item in at least 10 characters.", "description"
    elif len(v["description"]) > 1000:
        error, field = "Keep the description under 1000 characters.", "description"
    elif len(v["private_details"]) > 500:
        error, field = "Keep the private details under 500 characters.", "private_details"
    elif len(v["location"]) < 2:
        error, field = "Enter where it was lost or found.", "location"
    elif parsed is None:
        error, field = "Enter a valid date.", "event_date"
    elif parsed > date.today():
        error, field = "The date cannot be in the future.", "event_date"
    elif v["kind"] == "found" and v["handover"] not in ("admin", "direct"):
        error, field = "Choose how you want to hand over the found item.", "handover"
    elif v["kind"] == "lost":
        problem = services.contact_problem(v["contact"])
        if problem:
            error, field = problem, "contact"
    return v, error, field


@app.route("/post", methods=["GET", "POST"])
@login_required
def post_item():
    today = date.today().isoformat()
    user = current_user()
    if request.method == "POST":
        f = request.form
        v, error, field = _report_form(f)
        if error is None:
            # A double-click or a refresh must not create the same listing twice.
            since = (datetime.now() - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)).strftime(TS)
            dup = q1("SELECT id FROM items WHERE user_id = ? AND type = ? AND title = ? AND location = ? "
                     "AND event_date = ? AND description = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                     user["id"], v["kind"], v["title"], v["location"], v["event_date"], v["description"], since)
            if dup:
                flash("You just posted this report, so we did not create a duplicate.", "info")
                return redirect(url_for("item_detail", item_id=dup["id"]))
            try:
                services.check_report_rate(get_db(), user)
            except WorkflowError as exc:
                error, field = exc.message, None
        photo = None
        if error is None:
            try:
                photo = save_image(request.files.get("photo"), ITEM_DIR)
            except ValueError as exc:
                error, field = str(exc), "photo"
        if error is None:
            try:
                item_id = services.create_report(
                    get_db(), user, kind=v["kind"], title=v["title"], category=v["category"],
                    description=v["description"], private_details=v["private_details"], location=v["location"],
                    event_date=v["event_date"], photo=photo, handover_pref=v["handover"] if v["kind"] == "found" else None,
                    unsafe=v["unsafe"], moderation_required=MODERATION_REQUIRED,
                    contact=v["contact"] if v["kind"] == "lost" else "")
            except WorkflowError as exc:
                delete_file(ITEM_DIR, photo)
                error, field = exc.message, None
        if error:
            flash(error, "error")
            return render_template("post.html", form=f, today=today, error_field=field, editing=None)
        item = q1("SELECT * FROM items WHERE id = ?", item_id)
        if item["status"] == "pending_review":
            flash(f"Report {item['ref_code']} was submitted. Staff will review it before it is published; "
                  "you will get an alert.", "success")
        else:
            flash(f"Report {item['ref_code']} is published.", "success")
        return redirect(url_for("item_detail", item_id=item_id))
    kind = request.args.get("type", "lost")
    return render_template("post.html", form={"type": kind if kind in ("lost", "found") else "lost"},
                           today=today, error_field=None, editing=None)


@app.route("/item/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    item = get_item_or_404(item_id)
    user = current_user()
    if item["user_id"] != user["id"]:
        abort(403)
    if item["status"] not in ("pending_review", "active") or q1("SELECT 1 FROM claims WHERE item_id = ?", item_id):
        flash("This report can no longer be edited.", "error")
        return redirect(url_for("item_detail", item_id=item_id))
    today = date.today().isoformat()
    if request.method == "POST":
        v, error, field = _report_form({**request.form, "type": item["type"]})
        if error is None:
            if act(services.edit_report, user, item_id, title=v["title"], category=v["category"],
                   description=v["description"], private_details=v["private_details"], location=v["location"],
                   event_date=v["event_date"], handover_pref=v["handover"] if item["type"] == "found" else None,
                   contact=v["contact"] if item["type"] == "lost" else "", ok="Report updated."):
                return redirect(url_for("item_detail", item_id=item_id))
            return redirect(url_for("item_detail", item_id=item_id))
        flash(error, "error")
        return render_template("post.html", form=request.form, today=today, error_field=field, editing=item)
    form = {"type": item["type"], "title": item["title"], "category": item["category"],
            "description": item["description"], "private_details": item["private_details"],
            "location": item["location"], "event_date": item["event_date"], "handover": item["handover_pref"],
            "contact": item["contact"]}
    return render_template("post.html", form=form, today=today, error_field=None, editing=item)


@app.route("/item/<int:item_id>")
@login_required
def item_detail(item_id):
    item = get_item_or_404(item_id)
    user = current_user()
    if not can_view_item(item, user):
        abort(404)
    staff = services.is_reviewer(user)
    owner = item["user_id"] == user["id"]
    found = item["type"] == "found"
    claims, my_claim, my_evidence = [], None, []
    if found and (owner or staff):
        rows = qa("SELECT claims.*, users.name AS claimer_name, users.department AS claimer_dept, "
                  "users.id_number AS claimer_idno, users.role AS claimer_role FROM claims "
                  "JOIN users ON users.id = claims.claimer_id WHERE claims.item_id = ? ORDER BY claims.id", item_id)
        for c in rows:
            visible = services.can_view_claim(user, c, item)
            can_verify, why = (False, "")
            if visible and c["handler"] == "finder" and c["status"] == "pending" and owner:
                can_verify, why = services.finder_can_verify(get_db(), item, c)
            claims.append({"c": c, "visible": visible, "can_verify": can_verify, "why_not": why,
                           "evidence": qa("SELECT id FROM claim_evidence WHERE claim_id = ?", c["id"]) if visible else []})
    if found:
        my_claim = q1("SELECT * FROM claims WHERE item_id = ? AND claimer_id = ?", item_id, user["id"])
        if my_claim:
            my_evidence = qa("SELECT id FROM claim_evidence WHERE claim_id = ?", my_claim["id"])
    approved = q1("SELECT * FROM claims WHERE id = ?", item["approved_claim_id"]) if item["approved_claim_id"] else None
    claimant_is_me = bool(approved and approved["claimer_id"] == user["id"])
    participant = owner or claimant_is_me
    mode = services.handover_mode(item) if found else None
    counterpart = confirmations = None
    stage = None
    if approved and (participant or staff):
        confirmations = qa("SELECT handover_confirmations.*, users.name FROM handover_confirmations "
                           "JOIN users ON users.id = handover_confirmations.user_id WHERE claim_id = ? ORDER BY id",
                           approved["id"])
        if participant and mode == "direct" and item["custody_status"] in ("direct_handover_pending", "disputed"):
            other_id = approved["claimer_id"] if owner else item["user_id"]
            counterpart = q1("SELECT name, email, role FROM users WHERE id = ?", other_id)
        if item["custody_status"] == "direct_handover_pending":
            done = [c for c in confirmations if c["outcome"] == "confirmed" and c["party"] in ("finder", "claimant")]
            stage = "HANDOVER_CONFIRMATION_PENDING" if done else "AWAITING_EXCHANGE"
    my_confirmation = None
    if confirmations:
        my_confirmation = next((c for c in confirmations if c["user_id"] == user["id"]), None)
    events = qa("SELECT custody_events.*, s.name AS staff_name FROM custody_events "
                "LEFT JOIN users s ON s.id = custody_events.staff_id WHERE item_id = ? ORDER BY custody_events.id",
                item_id) if (owner or staff) else []
    matches = matching.find_matches(get_db(), item) if (owner or staff) and item["status"] == "active" else []
    offers, my_offer = [], None
    if not found:
        if owner or staff:
            offers = qa("SELECT found_offers.*, users.name AS finder_name, users.role AS finder_role, "
                        "users.department AS finder_dept FROM found_offers JOIN users ON users.id = found_offers.finder_id "
                        "WHERE item_id = ? ORDER BY found_offers.id", item_id)
        my_offer = q1("SELECT * FROM found_offers WHERE item_id = ? AND finder_id = ?", item_id, user["id"])
    can_offer = (not found and not owner and item["status"] == "active" and my_offer is None)
    can_claim = (found and not owner and item["status"] == "active" and my_claim is None
                 and item["custody_status"] not in ("returned", "disputed") and not item["approved_claim_id"])
    return render_template("item.html", item=item, claims=claims, my_claim=my_claim, my_evidence=my_evidence,
                           matches=matches, owner=owner, staff=staff, approved=approved, claimant_is_me=claimant_is_me,
                           participant=participant, mode=mode, counterpart=counterpart, confirmations=confirmations,
                           my_confirmation=my_confirmation, stage=stage, events=events, can_claim=can_claim,
                           offers=offers, my_offer=my_offer, can_offer=can_offer,
                           has_claims=bool(claims) if (owner or staff) else bool(q1("SELECT 1 FROM claims WHERE item_id = ?", item_id)),
                           admin=user["role"] == "admin", now_date=date.today().isoformat())


def back_to_item(item_id):
    return redirect(url_for("item_detail", item_id=item_id))


# ---- "I found this" (a finder contacts the owner of a lost report directly, no found report needed)
@app.route("/item/<int:item_id>/found-it", methods=["POST"])
@login_required
def found_it(item_id):
    get_item_or_404(item_id)
    act(services.offer_found, current_user(), item_id, request.form.get("message", ""),
        request.form.get("contact", ""), ok="Sent. The owner can now see your contact details. "
                                            "Their contact details are shown below.")
    return back_to_item(item_id)


@app.route("/offer/<int:offer_id>/<decision>", methods=["POST"])
@login_required
def offer_decision(offer_id, decision):
    row = q1("SELECT item_id FROM found_offers WHERE id = ?", offer_id)
    if row is None:
        abort(404)
    act(services.decide_offer, current_user(), offer_id, decision,
        ok="Marked as returned. We are glad you got it back." if decision == "returned" else "Okay, we marked it as not yours.")
    return back_to_item(row["item_id"])


# ---- claims (claimant side)
@app.route("/item/<int:item_id>/claim", methods=["POST"])
@login_required
def submit_claim(item_id):
    get_item_or_404(item_id)
    user = current_user()
    names = []
    try:
        for f in request.files.getlist("evidence")[:services.MAX_EVIDENCE_PER_CLAIM + 1]:
            stored = save_image(f, EVIDENCE_DIR)
            if stored:
                names.append(stored)
    except ValueError as exc:
        for n in names:
            delete_file(EVIDENCE_DIR, n)
        flash(str(exc), "error")
        return back_to_item(item_id)
    if not act(services.submit_claim, user, item_id, request.form.get("message", ""), names,
               ok="Claim sent. It will be reviewed under the college's verification policy."):
        for n in names:
            delete_file(EVIDENCE_DIR, n)
    return back_to_item(item_id)


def _claim_or_404(claim_id):
    claim = q1("SELECT * FROM claims WHERE id = ?", claim_id)
    if claim is None:
        abort(404)
    return claim


@app.route("/claim/<int:claim_id>/respond", methods=["POST"])
@login_required
def claim_respond(claim_id):
    claim = _claim_or_404(claim_id)
    names = []
    try:
        for f in request.files.getlist("evidence")[:services.MAX_EVIDENCE_PER_CLAIM]:
            stored = save_image(f, EVIDENCE_DIR)
            if stored:
                names.append(stored)
    except ValueError as exc:
        for n in names:
            delete_file(EVIDENCE_DIR, n)
        flash(str(exc), "error")
        return back_to_item(claim["item_id"])
    if not act(services.respond_info, current_user(), claim_id, request.form.get("message", ""), names,
               ok="Thank you. Your answer was sent for review."):
        for n in names:
            delete_file(EVIDENCE_DIR, n)
    return back_to_item(claim["item_id"])


@app.route("/claim/<int:claim_id>/withdraw", methods=["POST"])
@login_required
def claim_withdraw(claim_id):
    claim = _claim_or_404(claim_id)
    act(services.withdraw_claim, current_user(), claim_id, ok="Your claim was withdrawn.")
    return back_to_item(claim["item_id"])


@app.route("/claim/<int:claim_id>/appeal", methods=["POST"])
@login_required
def claim_appeal(claim_id):
    claim = _claim_or_404(claim_id)
    act(services.appeal_claim, current_user(), claim_id, request.form.get("message", ""),
        ok="Your appeal was sent to college staff.")
    return back_to_item(claim["item_id"])


# ---- claims (reviewers)
@app.route("/claim/<int:claim_id>/finder/<action>", methods=["POST"])
@login_required
def finder_action(claim_id, action):
    if action not in ("verify", "request_info", "escalate", "reject"):
        abort(404)
    claim = _claim_or_404(claim_id)
    ok = {"verify": "Claim verified. Arrange a safe handover with the claimant.",
          "request_info": "Your question was sent to the claimant.",
          "escalate": "The claim was sent to college staff.", "reject": "Claim rejected."}[action]
    act(services.finder_review, current_user(), claim_id, action, request.form.get("note", ""), ok=ok)
    return back_to_item(claim["item_id"])


@app.route("/claim/<int:claim_id>/staff/<action>", methods=["POST"])
@login_required
def staff_action(claim_id, action):
    if action not in ("approve", "reject", "request_info"):
        abort(404)
    claim = _claim_or_404(claim_id)
    ok = {"approve": "Claim approved and the decision recorded.", "reject": "Claim rejected and the reason recorded.",
          "request_info": "Your question was sent to the claimant."}[action]
    act(services.staff_review, current_user(), claim_id, action, request.form.get("reason", ""), ok=ok)
    return back_to_item(claim["item_id"])


@app.route("/claim/<int:claim_id>/revert", methods=["POST"])
@login_required
def claim_revert(claim_id):
    claim = _claim_or_404(claim_id)
    act(services.revert_approval, current_user(), claim_id, request.form.get("reason", ""),
        ok="The approval was reverted and the item is open again.")
    return back_to_item(claim["item_id"])


# ---- reports and custody
@app.route("/item/<int:item_id>/moderate/<decision>", methods=["POST"])
@login_required
def moderate_item(item_id, decision):
    if decision not in ("approve", "reject"):
        abort(404)
    get_item_or_404(item_id)
    act(services.moderate_report, current_user(), item_id, decision, request.form.get("reason", ""),
        ok="Report published." if decision == "approve" else "Report rejected and the reason recorded.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/close", methods=["POST"])
@login_required
def close_item(item_id):
    get_item_or_404(item_id)
    act(services.close_report, current_user(), item_id, request.form.get("reason", ""), ok="The report was closed.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/pref", methods=["POST"])
@login_required
def item_pref(item_id):
    get_item_or_404(item_id)
    act(services.change_handover_pref, current_user(), item_id, request.form.get("handover", ""),
        ok="Handover method updated.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/receive", methods=["POST"])
@login_required
def receive_item(item_id):
    get_item_or_404(item_id)
    if request.form.get("confirm") != "1":
        if not is_staff():
            abort(403)
        flash("Tick the box to confirm you physically hold the item.", "error")
        return back_to_item(item_id)
    act(services.accept_custody, current_user(), item_id, request.form.get("received_on", ""),
        request.form.get("notes", ""), ok="Receipt recorded. The item is now in college custody.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/release", methods=["POST"])
@login_required
def release_item(item_id):
    get_item_or_404(item_id)
    act(services.release_to_claimant, current_user(), item_id, request.form.get("acknowledged") == "1",
        request.form.get("notes", ""), ok="Handover recorded. The item is marked as returned.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/handover", methods=["POST"])
@login_required
def confirm_handover(item_id):
    """Direct route: the finder and the verified claimant each confirm (or dispute) the exchange."""
    get_item_or_404(item_id)
    outcome = "disputed" if request.form.get("outcome") == "disputed" else "confirmed"
    result = act(services.confirm_direct_handover, current_user(), item_id, outcome, request.form.get("note", ""), ok="")
    messages = {"recorded": ("Your confirmation is recorded. We are waiting for the other person.", "success"),
                "completed": ("Both of you confirmed. The item is marked as returned.", "success"),
                "disputed": ("Your report was recorded and staff will review the handover.", "info"),
                "duplicate": ("Your answer was already recorded.", "info")}
    if result in messages:
        flash(*messages[result])
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/meeting", methods=["POST"])
@login_required
def arrange_meeting(item_id):
    get_item_or_404(item_id)
    act(services.arrange_meeting, current_user(), item_id, request.form.get("meeting", ""),
        ok="Your suggestion was sent.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/dispute", methods=["POST"])
@login_required
def resolve_dispute(item_id):
    get_item_or_404(item_id)
    act(services.resolve_dispute, current_user(), item_id, request.form.get("resolution", ""),
        request.form.get("reason", ""), ok="Dispute resolved and recorded.")
    return back_to_item(item_id)


@app.route("/item/<int:item_id>/correct-return", methods=["POST"])
@login_required
def correct_return(item_id):
    get_item_or_404(item_id)
    act(services.correct_return, current_user(), item_id, request.form.get("reason", ""),
        ok="The return was corrected and the correction is logged.")
    return back_to_item(item_id)


@app.route("/dashboard")
@login_required
def dashboard():
    uid = current_user()["id"]
    my_items = qa("SELECT items.*, (SELECT COUNT(*) FROM claims WHERE claims.item_id = items.id "
                  "AND claims.handler = 'finder' AND claims.status = 'pending') AS pending_claims FROM items "
                  "WHERE user_id = ? ORDER BY id DESC", uid)
    my_claims = qa("SELECT claims.*, items.title, items.ref_code, items.status AS item_status, "
                   "items.custody_status, items.risk_level, items.photo AS item_photo, items.type AS item_type, "
                   "items.category AS item_category, items.approved_claim_id FROM claims "
                   "JOIN items ON items.id = claims.item_id WHERE claims.claimer_id = ? ORDER BY claims.id DESC", uid)
    return render_template("dashboard.html", my_items=my_items, my_claims=my_claims)


@app.route("/notifications")
@login_required
def notifications():
    uid = current_user()["id"]
    rows = qa("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50", uid)
    run("UPDATE notifications SET is_read = 1 WHERE user_id = ?", uid)
    return render_template("notifications.html", rows=rows)


# ================================================================ staff desk (admins and custody officers)
QUEUES = [("pending_reports", "New reports", "New reports that wait for you to accept or reject them."),
          ("awaiting_receipt", "Items to receive",
           "These finders will bring the item to the college. The item is not with the college until you confirm you have it."),
          ("claims_review", "Claims to check",
           "Claims sent to staff, more than one claim on an item, and claims the finder did not answer for a few days."),
          ("needs_info", "Waiting for details",
           "We asked the person for more details. Nothing to do until they reply."),
          ("high_risk", "High-risk claims", "Phones, laptops, wallets, ID cards and other items that staff must always check."),
          ("disputes", "Handover problems", "One person says the item was not given back. Sort this out before anything is given out."),
          ("awaiting_collection", "Ready to give back", "The claim is accepted. Save the handover when it happens."),
          ("completed", "Given back", "Items that were given back to their owners.")]
QUEUE_NAMES = {q[0] for q in QUEUES}


def queue_rows(name):
    base = ("SELECT items.*, users.name AS reporter, NULL AS claim_id, NULL AS claim_status, NULL AS escalation_reason "
            "FROM items JOIN users ON users.id = items.user_id ")
    claim_base = ("SELECT items.*, users.name AS reporter, claims.id AS claim_id, claims.status AS claim_status, "
                  "claims.escalation_reason AS escalation_reason FROM claims JOIN items ON items.id = claims.item_id "
                  "JOIN users ON users.id = items.user_id ")
    if name == "pending_reports":
        return qa(base + "WHERE items.status = 'pending_review' ORDER BY items.created_at")
    if name == "awaiting_receipt":
        return qa(base + "WHERE items.type = 'found' AND items.status = 'active' "
                         "AND items.custody_status = 'admin_handover_requested' ORDER BY items.created_at")
    if name == "claims_review":
        cut = (datetime.now() - timedelta(days=services.STALE_FINDER_DAYS)).strftime(TS)
        return qa(claim_base + "WHERE items.status = 'active' AND (claims.status = 'under_admin_review' OR "
                               "(claims.status = 'pending' AND claims.handler = 'finder' AND claims.created_at < ?)) "
                               "ORDER BY claims.created_at", cut)
    if name == "needs_info":
        return qa(claim_base + "WHERE items.status = 'active' AND claims.status = 'needs_more_info' "
                               "ORDER BY claims.created_at")
    if name == "high_risk":
        return qa(claim_base + "WHERE items.status = 'active' AND items.risk_level = 'high' AND claims.status IN "
                               "('pending', 'needs_more_info', 'under_admin_review') ORDER BY claims.created_at")
    if name == "disputes":
        return qa(base + "WHERE items.status = 'active' AND items.custody_status = 'disputed' ORDER BY items.updated_at")
    if name == "awaiting_collection":
        return qa(base + "WHERE items.status = 'active' AND items.approved_claim_id IS NOT NULL ORDER BY items.updated_at")
    return qa(base + "WHERE items.status = 'returned' ORDER BY items.returned_at DESC LIMIT 100")


@app.route("/staff/queue/<name>")
@reviewer_required
def staff_queue(name):
    if name not in QUEUE_NAMES:
        abort(404)
    all_rows = queue_rows(name)
    counts = {key: (len(all_rows) if key == name else len(queue_rows(key))) for key in QUEUE_NAMES}
    per_page = admin_per_page(10)
    total = len(all_rows)
    pages = max(1, math.ceil(total / per_page))
    page = min(admin_page_number(), pages)
    rows = all_rows[(page - 1) * per_page: page * per_page]
    title, blurb = next((t, b) for k, t, b in QUEUES if k == name)
    stale = (datetime.now() - timedelta(days=services.STALE_FINDER_DAYS)).strftime(TS)
    template = "staff_queue.html" if current_user()["role"] == "admin" else "staff_queue_plain.html"
    return render_template(template, name=name, rows=rows, counts=counts, queues=QUEUES,
                           title=title, blurb=blurb, stale=stale, total=total, page=page, pages=pages,
                           per_page=per_page, page_sizes=ADMIN_PAGE_SIZES,
                           pager_args=pager_extra({}, per_page, 10))


@app.route("/staff/lookup")
@reviewer_required
def staff_lookup():
    ref = request.args.get("ref", "").strip().upper()
    item = q1("SELECT id FROM items WHERE ref_code = ?", ref) if ref else None
    if item is None:
        flash("No report has that reference code.", "error")
        return redirect(url_for("staff_queue", name="awaiting_receipt"))
    return redirect(url_for("item_detail", item_id=item["id"]))


# ================================================================ admin
ADMIN_PER_PAGE = 20
ROSTER_PER_PAGE = 10
ROSTER_PAGE_SIZES = (10, 20, 50, 100)
USER_STATUSES = ["active", "suspended", "pending_email", "pending_approval", "rejected"]


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime(TS)


def admin_page_number():
    try:
        return max(1, int(request.args.get("page", "1")))
    except ValueError:
        return 1


ADMIN_PAGE_SIZES = (10, 20, 50, 100)


def admin_per_page(default=ADMIN_PER_PAGE):
    """Rows per page from ?per_page=, limited to the sizes we offer."""
    try:
        n = int(request.args.get("per_page", default))
    except ValueError:
        return default
    return n if n in ADMIN_PAGE_SIZES else default


def pager_extra(filters, per_page, default=ADMIN_PER_PAGE):
    """Filters plus per_page (only when it is not the default), for page links."""
    args = dict(filters)
    if per_page != default:
        args["per_page"] = per_page
    return args


def paginate(count_sql, params, per_page=ADMIN_PER_PAGE):
    """Return (total, pages, page, offset) for a COUNT query."""
    total = q1(count_sql, *params)["c"]
    pages = max(1, math.ceil(total / per_page))
    page = min(admin_page_number(), pages)
    return total, pages, page, (page - 1) * per_page


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        # Prefix risky cells so spreadsheet apps never run them as formulas.
        writer.writerow([("'" + str(c)) if isinstance(c, str) and c[:1] in "=+-@" else c for c in row])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def admin_back(default_endpoint):
    target = request.form.get("next", "")
    if target.startswith("/admin") and "//" not in target and "\\" not in target:
        return redirect(target)
    return redirect(url_for(default_endpoint))


@app.context_processor
def inject_admin_counts():
    if not (request.path.startswith("/admin") or request.path.startswith("/staff")):
        return {}
    user = current_user()
    if user is None or user["role"] != "admin":
        return {}
    return {"nav_counts": {
        "requests": q1("SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'")["c"],
        "handovers": sum(len(queue_rows(k)) for k in ("pending_reports", "awaiting_receipt", "claims_review", "disputes")),
    }}


def people_tab_counts():
    """Badge numbers for the three People tabs."""
    return {"people_counts": {
        "verification": q1("SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'")["c"],
        "roster": q1("SELECT COUNT(*) AS c FROM roster")["c"]}}


PEOPLE_TABS = {"users": "admin_users", "verification": "admin_requests", "roster": "admin_roster"}


@app.route("/admin/people")
@admin_required
def admin_people():
    """Single People entry point: /admin/people?tab=users|verification|roster (other filters are carried over)."""
    endpoint = PEOPLE_TABS.get(request.args.get("tab", "users"), "admin_users")
    extra = {k: v for k, v in request.args.items() if k != "tab" and v}
    return redirect(url_for(endpoint, **extra))


@app.route("/admin/search")
@admin_required
def admin_search():
    """One search box for the whole console: people, items and the campus roster."""
    q = request.args.get("q", "").strip()
    people = items = roster = []
    if q:
        like = f"%{q}%"
        people = qa("SELECT * FROM users WHERE role != 'admin' AND (name LIKE ? OR email LIKE ? OR id_number LIKE ? "
                    "OR department LIKE ?) ORDER BY id DESC LIMIT 8", like, like, like, like)
        items = qa("SELECT items.*, users.name AS poster FROM items JOIN users ON users.id = items.user_id "
                   "WHERE items.title LIKE ? OR items.description LIKE ? OR items.location LIKE ? "
                   "OR users.name LIKE ? ORDER BY items.id DESC LIMIT 8", like, like, like, like)
        roster = qa("SELECT * FROM roster WHERE id_number LIKE ? OR full_name LIKE ? OR email LIKE ? "
                    "ORDER BY id_number LIMIT 8", like, like, like)
    return render_template("admin_search.html", q=q, people=people, items=items, roster=roster)


@app.route("/admin")
@admin_required
def admin_home():
    count = lambda sql, *p: q1(sql, *p)["c"]  # noqa: E731
    total_items = count("SELECT COUNT(*) AS c FROM items")
    returned = count("SELECT COUNT(*) AS c FROM items WHERE status = 'returned'")
    stats = {
        "members": count("SELECT COUNT(*) AS c FROM users WHERE status = 'active' AND role != 'admin'"),
        "students": count("SELECT COUNT(*) AS c FROM users WHERE status = 'active' AND role = 'student'"),
        "staff": count("SELECT COUNT(*) AS c FROM users WHERE status = 'active' AND role = 'staff'"),
        "new_members": count("SELECT COUNT(*) AS c FROM users WHERE role != 'admin' AND status = 'active' "
                             "AND created_at >= ?", days_ago(7)),
        "pending": count("SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'"),
        "suspended": count("SELECT COUNT(*) AS c FROM users WHERE status = 'suspended'"),
        "roster": count("SELECT COUNT(*) AS c FROM roster"),
        "total": total_items,
        "total_lost": count("SELECT COUNT(*) AS c FROM items WHERE type = 'lost'"),
        "total_found": count("SELECT COUNT(*) AS c FROM items WHERE type = 'found'"),
        "open": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active'"),
        "open_lost": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active' AND type = 'lost'"),
        "open_found": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active' AND type = 'found'"),
        "claimed": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active' AND approved_claim_id IS NOT NULL"),
        "returned": returned,
        "new_items": count("SELECT COUNT(*) AS c FROM items WHERE created_at >= ?", days_ago(7)),
        "rate": round(returned * 100 / total_items) if total_items else 0,
    }
    attention = {
        "requests": stats["pending"],
        "stale_items": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active' AND created_at < ?",
                             days_ago(30)),
        "slow_claims": count("SELECT COUNT(*) AS c FROM claims WHERE status = 'pending' AND created_at < ?",
                             days_ago(3)),
        "stuck_handovers": count("SELECT COUNT(*) AS c FROM items WHERE status = 'active' AND custody_status = 'disputed'"),
        "pending_reports": count("SELECT COUNT(*) AS c FROM items WHERE status = 'pending_review'"),
        "awaiting_receipt": count("SELECT COUNT(*) AS c FROM items WHERE type = 'found' AND status = 'active' "
                                  "AND custody_status = 'admin_handover_requested'"),
        "claims_review": count("SELECT COUNT(*) AS c FROM claims JOIN items ON items.id = claims.item_id "
                               "WHERE items.status = 'active' AND claims.status = 'under_admin_review'"),
    }
    # 14-day activity chart (new listings per day).
    days = [(date.today() - timedelta(days=n)) for n in range(13, -1, -1)]
    by_day = {}
    for r in qa("SELECT substr(created_at, 1, 10) AS d, type, COUNT(*) AS c FROM items "
                "WHERE created_at >= ? GROUP BY d, type", days_ago(14)):
        by_day[(r["d"], r["type"])] = r["c"]
    chart = []
    for d in days:
        key = d.isoformat()
        lost, found = by_day.get((key, "lost"), 0), by_day.get((key, "found"), 0)
        chart.append({"label": d.strftime("%d %b"), "short": d.strftime("%d"), "lost": lost,
                      "found": found, "total": lost + found})
    peak = max([c["total"] for c in chart] + [1])
    for c in chart:
        c["lost_h"] = round(c["lost"] * 100 / peak)
        c["found_h"] = round(c["found"] * 100 / peak)
    cats = qa("SELECT category, COUNT(*) AS c FROM items GROUP BY category ORDER BY c DESC LIMIT 8")
    cat_max = max([c["c"] for c in cats] + [1])
    categories = [{"name": c["category"], "count": c["c"], "pct": round(c["c"] * 100 / cat_max)} for c in cats]
    recent = qa("SELECT audit_log.*, users.name AS actor FROM audit_log LEFT JOIN users "
                "ON users.id = audit_log.actor_id ORDER BY audit_log.id DESC LIMIT 8")
    recent_items = qa("SELECT items.*, users.name AS poster FROM items JOIN users ON users.id = items.user_id "
                      "ORDER BY items.id DESC LIMIT 6")
    waiting = qa("SELECT * FROM users WHERE status = 'pending_approval' ORDER BY id LIMIT 5")
    slow = qa("SELECT items.id, items.title, items.type, users.name AS poster, "
              "(SELECT COUNT(*) FROM claims WHERE claims.item_id = items.id AND claims.status = 'pending') AS n "
              "FROM items JOIN users ON users.id = items.user_id WHERE EXISTS (SELECT 1 FROM claims "
              "WHERE claims.item_id = items.id AND claims.status = 'pending' AND claims.created_at < ?) "
              "ORDER BY items.id DESC LIMIT 5", days_ago(3))
    locked = qa("SELECT items.id, items.title, items.type, users.name AS poster FROM items "
                "JOIN users ON users.id = items.user_id WHERE items.status = 'active' "
                "AND items.custody_status = 'disputed' ORDER BY items.id DESC LIMIT 5")
    return render_template("admin_home.html", now_hour=datetime.now().hour, stats=stats, attention=attention, chart=chart,
                           chart_peak=peak, categories=categories, recent=recent,
                           recent_items=recent_items, waiting=waiting, slow=slow, locked=locked)


# ---------------------------------------------------------------- ID requests
@app.route("/admin/requests")
@admin_required
def admin_requests():
    rows = qa("SELECT users.*, roster.id AS roster_id, roster.email AS roster_email, "
              "roster.full_name AS roster_name FROM users "
              "LEFT JOIN roster ON roster.id_number = users.id_number "
              "WHERE users.status = 'pending_approval' ORDER BY users.id")
    return render_template("admin_requests.html", rows=rows, people_tab="verification", **people_tab_counts())


@app.route("/admin/idcard/<int:user_id>")
@admin_required
def admin_idcard(user_id):
    user = q1("SELECT id_card FROM users WHERE id = ?", user_id)
    if user is None or not user["id_card"]:
        abort(404)
    resp = send_from_directory(CARD_DIR, user["id_card"])
    resp.headers["Cache-Control"] = "private, no-store"   # ID card photos are sensitive: never cached
    return resp


@app.route("/admin/requests/<int:user_id>/<action>", methods=["POST"])
@admin_required
def admin_review_request(user_id, action):
    if action not in ("approve", "reject"):
        abort(404)
    user = q1("SELECT * FROM users WHERE id = ? AND status = 'pending_approval'", user_id)
    if user is None:
        flash("That request was already handled.", "info")
        return redirect(url_for("admin_requests"))
    # The status is re-checked inside the UPDATE, so two admins cannot both decide the same request.
    guard = run("UPDATE users SET status = ? WHERE id = ? AND status = 'pending_approval'",
                "active" if action == "approve" else "rejected", user_id)
    if guard.rowcount == 0:
        flash("That request was already handled.", "info")
        return redirect(url_for("admin_requests"))
    if action == "approve":
        audit("request_approved", f"{user['name']} ({user['role']} {user['id_number']})")
        subj = f"{CAMPUS_NAME} Lost & Found: account approved"
        plain = f"Hello {user['name']},\n\nYour registration was approved. You can log in now."
        send_mail(user["email"], subj, plain, html_body=build_html_email(subj, plain, campus_name=CAMPUS_NAME))
        flash(f"{user['name']} approved.", "success")
    else:
        note = request.form.get("note", "").strip()[:200]
        run("UPDATE users SET review_note = ? WHERE id = ?", note, user_id)
        audit("request_rejected", f"{user['name']} ({user['role']} {user['id_number']}) {note}")
        subj = f"{CAMPUS_NAME} Lost & Found: request not approved"
        plain = f"Hello {user['name']},\n\nYour registration request was not approved. {note}"
        send_mail(user["email"], subj, plain, html_body=build_html_email(subj, plain, campus_name=CAMPUS_NAME))
        flash(f"{user['name']} rejected.", "info")
    return redirect(url_for("admin_requests"))


# ---------------------------------------------------------------- roster
def roster_query():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    reg = request.args.get("reg", "")
    where, params = "WHERE 1 = 1", []
    if q:
        where += (" AND (roster.id_number LIKE ? OR roster.full_name LIKE ? OR roster.email LIKE ? "
                  "OR roster.department LIKE ?)")
        params += [f"%{q}%"] * 4
    if role in ("student", "staff"):
        where += " AND roster.role = ?"
        params.append(role)
    if reg == "yes":
        where += " AND users.id IS NOT NULL"
    elif reg == "no":
        where += " AND users.id IS NULL"
    base = "FROM roster LEFT JOIN users ON users.id_number = roster.id_number " + where
    return base, params, {"q": q, "role": role, "reg": reg}


@app.route("/admin/roster")
@admin_required
def admin_roster():
    base, params, filters = roster_query()
    total_all = q1("SELECT COUNT(*) AS c FROM roster")["c"]
    per_page = admin_per_page(ROSTER_PER_PAGE)
    total, pages, page, offset = paginate("SELECT COUNT(*) AS c " + base, params, per_page)
    rows = qa("SELECT roster.*, users.status AS user_status " + base +
              " ORDER BY roster.role, roster.id_number LIMIT ? OFFSET ?", *params, per_page, offset)
    pager_args = dict(filters)
    if per_page != ROSTER_PER_PAGE:
        pager_args["per_page"] = per_page
    return render_template("admin_roster.html", rows=rows, total=total, total_all=total_all,
                           page=page, pages=pages, filters=filters, q=filters["q"],
                           per_page=per_page, page_sizes=ADMIN_PAGE_SIZES, pager_args=pager_args,
                           import_problems=session.pop("roster_import_problems", None),
                           people_tab="roster", **people_tab_counts())


@app.route("/admin/roster/export.csv")
@admin_required
def admin_roster_export():
    base, params, _ = roster_query()
    rows = qa("SELECT roster.id_number, roster.full_name, roster.email, roster.role, roster.department, "
              "roster.academic_end " + base + " ORDER BY roster.role, roster.id_number", *params)
    return csv_response("roster.csv", ["id_number", "full_name", "email", "role", "department", "end_date"],
                        [tuple(r) for r in rows])


@app.route("/admin/roster/template.csv")
@admin_required
def admin_roster_template():
    text = ("id_number,full_name,email,role,department,end_date\n"
            "S2401,Student Name,student.name@yourcollege.edu,student,Computer Science,2027-05-20\n"
            "E2001,Staff Name,staff.name@yourcollege.edu,staff,Library,\n")
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=roster_template.csv"})


@app.route("/admin/roster/upload", methods=["POST"])
@admin_required
def admin_roster_upload():
    file = request.files.get("csv")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("admin_roster"))
    try:
        text = file.read().decode("utf-8-sig")
        problems = []
        added, updated, skipped = import_roster(get_db(), text, problems)
    except UnicodeDecodeError:
        flash("The file must be a UTF-8 CSV.", "error")
    except (ValueError, csv.Error) as exc:
        flash(str(exc), "error")
    else:
        audit("roster_import", f"added {added}, updated {updated}, skipped {skipped}")
        flash(f"Roster imported: {added} added, {updated} updated, {skipped} skipped.",
              "success" if not skipped else "info")
        if problems:
            # Kept short so it fits in the session cookie; shown once on the roster page.
            session["roster_import_problems"] = {
                "total": len(problems),
                "rows": [[ln, ident[:20], reason[:110]] for ln, ident, reason in problems[:10]]}
    return redirect(url_for("admin_roster"))


def apply_end_date(id_number, end, actor_note=""):
    """Copy a student's course end date to their account. If an automatic course-end block is now in the past,
    lift it. Returns True when the account was reactivated."""
    user = q1("SELECT * FROM users WHERE id_number = ? AND role = 'student'", id_number)
    if user is None:
        return False
    run("UPDATE users SET academic_end = ? WHERE id = ?", end, user["id"])
    if (user["status"] == "suspended" and user["review_note"].startswith(AUTO_END_NOTE)
            and (not end or end >= datetime.now().strftime("%Y-%m-%d"))):
        run("UPDATE users SET status = 'active', review_note = '' WHERE id = ?", user["id"])
        audit("user_reactivated", f"{user['name']} ({id_number}) course end date set to {end or 'none'}",
              rtype="user", rid=user["id"])
        return True
    return False


@app.route("/admin/roster/add", methods=["POST"])
@admin_required
def admin_roster_add():
    f = request.form
    id_number = f.get("id_number", "").strip().upper()
    name, email = f.get("full_name", "").strip(), f.get("email", "").strip().lower()
    role, dept = f.get("role", ""), chosen_department(f)
    end = f.get("academic_end", "").strip() if role == "student" else ""
    error = roster_row_error(id_number, name, email, role, dept, end, require_end=True)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_roster"))
    added, updated, _skipped = import_roster(
        get_db(), "id_number,full_name,email,role,department,end_date\n" + ",".join(
            '"' + v.replace('"', "'") + '"' for v in (id_number, name, email, role, dept, end)))
    apply_end_date(id_number, end) if role == "student" else None
    audit("roster_add", id_number)
    flash("Roster entry updated." if updated else "Roster entry saved.", "success")
    return redirect(url_for("admin_roster"))


@app.route("/admin/roster/<int:roster_id>/edit", methods=["POST"])
@admin_required
def admin_roster_edit(roster_id):
    row = q1("SELECT * FROM roster WHERE id = ?", roster_id)
    if row is None:
        abort(404)
    name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "")
    dept = chosen_department(request.form)
    end = request.form.get("academic_end", "").strip() if role == "student" else ""
    # An unchanged end date is accepted even if it is already in the past (the admin may be fixing something else).
    unchanged = role == "student" and end == row["academic_end"]
    error = roster_row_error(row["id_number"], name, email, role, dept, "" if unchanged else end,
                             require_end=False)
    if not error and role == "student" and not end:
        error = "Enter the course end date for a student."
    if error:
        flash(error, "error")
    else:
        run("UPDATE roster SET full_name = ?, email = ?, role = ?, department = ?, academic_end = ? WHERE id = ?",
            name, email, role, dept, end, roster_id)
        if role == "student":
            apply_end_date(row["id_number"], end)
        audit("roster_edit", row["id_number"])
        flash(f"{row['id_number']} updated.", "success")
    return admin_back("admin_roster")


@app.route("/admin/roster/<int:roster_id>/delete", methods=["POST"])
@admin_required
def admin_roster_delete(roster_id):
    row = q1("SELECT id_number FROM roster WHERE id = ?", roster_id)
    if row:
        run("DELETE FROM roster WHERE id = ?", roster_id)
        audit("roster_delete", row["id_number"])
        flash("Roster entry removed. Existing accounts are not affected.", "info")
    return redirect(url_for("admin_roster"))


# ---------------------------------------------------------------- members
def users_query():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    where, params = "WHERE users.role != 'admin'", []
    if q:
        where += (" AND (users.name LIKE ? OR users.email LIKE ? OR users.id_number LIKE ? "
                  "OR users.department LIKE ?)")
        params += [f"%{q}%"] * 4
    if role in ("student", "staff"):
        where += " AND users.role = ?"
        params.append(role)
    if status in USER_STATUSES:
        where += " AND users.status = ?"
        params.append(status)
    return where, params, {"q": q, "role": role, "status": status}


@app.route("/admin/users")
@admin_required
def admin_users():
    where, params, filters = users_query()
    per_page = admin_per_page()
    total, pages, page, offset = paginate("SELECT COUNT(*) AS c FROM users " + where, params, per_page)
    rows = qa("SELECT users.*, (SELECT COUNT(*) FROM items WHERE items.user_id = users.id) AS item_count "
              "FROM users " + where + " ORDER BY users.id DESC LIMIT ? OFFSET ?",
              *params, per_page, offset)
    return render_template("admin_users.html", rows=rows, total=total, page=page, pages=pages,
                           filters=filters, per_page=per_page, page_sizes=ADMIN_PAGE_SIZES,
                           pager_args=pager_extra(filters, per_page), statuses=USER_STATUSES, people_tab="users", **people_tab_counts())


@app.route("/admin/users/export.csv")
@admin_required
def admin_users_export():
    where, params, _ = users_query()
    rows = qa("SELECT name, email, id_number, role, department, academic_end, status, verification, created_at "
              "FROM users " + where + " ORDER BY id", *params)
    return csv_response("members.csv", ["name", "email", "id_number", "role", "department", "course_ends",
                                        "status", "verified_by", "joined"], [tuple(r) for r in rows])


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    user = q1("SELECT * FROM users WHERE id = ?", user_id)
    if user is None or user["role"] == "admin":
        abort(404)
    items = qa("SELECT * FROM items WHERE user_id = ? ORDER BY id DESC LIMIT 20", user_id)
    claims = qa("SELECT claims.*, items.title FROM claims JOIN items ON items.id = claims.item_id "
                "WHERE claims.claimer_id = ? ORDER BY claims.id DESC LIMIT 20", user_id)
    if user["id_number"]:
        history = qa("SELECT * FROM audit_log WHERE detail LIKE ? OR actor_id = ? ORDER BY id DESC LIMIT 15",
                     f"%{user['id_number']}%", user_id)
    else:
        history = qa("SELECT * FROM audit_log WHERE actor_id = ? ORDER BY id DESC LIMIT 15", user_id)
    returned = q1("SELECT COUNT(*) AS c FROM items WHERE user_id = ? AND status = 'returned'", user_id)["c"]
    return render_template("admin_user_detail.html", u=user, items=items, claims=claims,
                           history=history, returned=returned)


CUSTODY_BUSY = ("with_admin", "admin_handover_requested", "direct_handover_pending", "disputed")


def remove_user_completely(user):
    """Delete the account, their listings, claims and files, and their campus-list entry. Returns an error or None."""
    uid = user["id"]
    busy = q1("SELECT COUNT(*) AS c FROM items WHERE user_id = ? AND custody_status IN (%s)"
              % ",".join("?" * len(CUSTODY_BUSY)), uid, *CUSTODY_BUSY)["c"]
    if busy:
        return (f"{user['name']} still has an item in college custody or an open handover. "
                "Finish or close those first, then remove the account.")
    items = qa("SELECT id, title, photo FROM items WHERE user_id = ?", uid)
    for it in items:
        for c in qa("SELECT DISTINCT claimer_id FROM claims WHERE item_id = ? AND status IN "
                    "('pending','needs_more_info','under_admin_review','approved') AND claimer_id != ?", it["id"], uid):
            notify(c["claimer_id"], f"The listing \"{it['title']}\" was removed because its owner's account was removed.")
        for ev in qa("SELECT claim_evidence.filename FROM claim_evidence JOIN claims ON claims.id = claim_evidence.claim_id "
                     "WHERE claims.item_id = ?", it["id"]):
            delete_file(EVIDENCE_DIR, ev["filename"])
        delete_file(ITEM_DIR, it["photo"])
    for ev in qa("SELECT filename FROM claim_evidence WHERE uploaded_by = ?", uid):
        delete_file(EVIDENCE_DIR, ev["filename"])
    run("DELETE FROM items WHERE user_id = ?", uid)
    services.handle_user_suspended(get_db(), uid, current_user()["id"])  # frees claims they made on other items
    delete_file(CARD_DIR, user["id_card"])
    run("DELETE FROM users WHERE id = ?", uid)
    if user["id_number"]:
        run("DELETE FROM roster WHERE id_number = ?", user["id_number"])
    audit("user_removed", f"{user['name']} ({user['id_number']}, {user['role']}) removed completely", rtype="user", rid=None)
    return None


@app.route("/admin/users/<int:user_id>/<action>", methods=["POST"])
@admin_required
def admin_user_action(user_id, action):
    if action not in ("suspend", "reactivate", "set_end", "remove"):
        abort(404)
    user = q1("SELECT * FROM users WHERE id = ?", user_id)
    if user is None or user["role"] == "admin":
        abort(404)
    reason = request.form.get("reason", "").strip()[:200]
    today = datetime.now().strftime("%Y-%m-%d")
    if action == "suspend" and user["status"] == "active":
        run("UPDATE users SET status = 'suspended', review_note = ? WHERE id = ?", reason, user_id)
        audit("user_suspended", f"{user['name']} ({user['id_number']}) {reason}".strip(), rtype="user", rid=user_id)
        services.handle_user_suspended(get_db(), user_id, current_user()["id"])
        flash(f"{user['name']} suspended.", "info")
    elif action == "reactivate" and user["status"] == "suspended":
        if user["role"] == "student" and user["academic_end"] and user["academic_end"] < today:
            flash(f"{user['name']}'s course end date ({user['academic_end']}) has passed. "
                  "Set a new course end date first.", "error")
        else:
            run("UPDATE users SET status = 'active', review_note = '' WHERE id = ?", user_id)
            audit("user_reactivated", f"{user['name']} ({user['id_number']})")
            flash(f"{user['name']} reactivated.", "success")
    elif action == "set_end":
        end = request.form.get("end_date", "").strip()
        error = None if user["role"] == "student" else "Only students have a course end date."
        error = error or validate_end_date(end, required=True)
        if error:
            flash(error, "error")
        else:
            run("UPDATE roster SET academic_end = ? WHERE id_number = ?", end, user["id_number"])
            lifted = apply_end_date(user["id_number"], end)
            audit("course_end_changed", f"{user['name']} ({user['id_number']}) -> {end}", rtype="user", rid=user_id)
            flash(f"Course end date set to {end}." + (" Account reactivated." if lifted else ""), "success")
    elif action == "remove":
        if request.form.get("confirm_text", "").strip().upper() != "REMOVE":
            flash("Type REMOVE to confirm. Nothing was deleted.", "error")
        else:
            error = remove_user_completely(user)
            if error:
                flash(error, "error")
            else:
                flash(f"{user['name']} was removed completely.", "info")
                return redirect(url_for("admin_users"))
    return admin_back("admin_users")


@app.route("/admin/users/<int:user_id>/officer", methods=["POST"])
@admin_required
def admin_user_officer(user_id):
    """Authorize (or stop authorizing) a staff member to confirm college custody and review claims."""
    user = q1("SELECT * FROM users WHERE id = ?", user_id)
    if user is None or user["role"] != "staff" or user["status"] != "active":
        flash("Only active staff members can be made custody officers.", "error")
        return admin_back("admin_users")
    grant = request.form.get("grant") == "1"
    run("UPDATE users SET is_custody_officer = ? WHERE id = ?", 1 if grant else 0, user_id)
    audit("officer_granted" if grant else "officer_revoked", f"{user['name']} ({user['id_number']})",
          rtype="user", rid=user_id)
    notify(user_id, "You can now confirm item custody and review claims." if grant
           else "You are no longer authorized to confirm item custody.", url_for("staff_queue", name="pending_reports"))
    flash(f"{user['name']} is {'now' if grant else 'no longer'} a custody officer.", "success")
    return admin_back("admin_users")


@app.route("/admin/policy", methods=["GET", "POST"])
@admin_required
def admin_policy():
    """Verification policy: the risk level of each category decides who may verify a claim."""
    if request.method == "POST":
        for category in CATEGORIES:
            risk = request.form.get("risk_" + category.replace(" ", "_").replace("&", "and"), "")
            if risk in services.RISK_LEVELS:
                act(services.set_category_risk, current_user(), category, risk, ok="")
        flash("Verification policy saved.", "success")
        return redirect(url_for("admin_policy"))
    rows = [{"category": c, "risk": services.risk_for(get_db(), c),
             "field": "risk_" + c.replace(" ", "_").replace("&", "and")} for c in CATEGORIES]
    return render_template("admin_policy.html", rows=rows, risks=services.RISK_LEVELS,
                           moderation=MODERATION_REQUIRED, limits={
                               "reports": services.REPORTS_PER_HOUR, "claims": services.CLAIMS_PER_DAY,
                               "medium": services.MEDIUM_RISK_MIN_DETAIL, "stale": services.STALE_FINDER_DAYS})


# ---------------------------------------------------------------- listings
def listings_query():
    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "")
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    flag = request.args.get("flag", "")
    where, params = "WHERE 1 = 1", []
    if q:
        where += (" AND (items.title LIKE ? OR items.description LIKE ? OR items.location LIKE ? "
                  "OR users.name LIKE ?)")
        params += [f"%{q}%"] * 4
    if flag == "stale":
        where += " AND items.status = 'active' AND items.created_at < ?"
        params.append(days_ago(30))
    elif flag == "slow":
        where += (" AND EXISTS (SELECT 1 FROM claims WHERE claims.item_id = items.id "
                  "AND claims.status = 'pending' AND claims.created_at < ?)")
        params.append(days_ago(3))
    elif flag == "locked":
        where += " AND items.status = 'active' AND items.custody_status = 'disputed'"
    else:
        flag = ""
    if kind in ("lost", "found"):
        where += " AND items.type = ?"
        params.append(kind)
    if status in services.REPORT_STATUSES:
        where += " AND items.status = ?"
        params.append(status)
    if category in CATEGORIES:
        where += " AND items.category = ?"
        params.append(category)
    return ("FROM items JOIN users ON users.id = items.user_id " + where, params,
            {"q": q, "type": kind, "status": status, "category": category, "flag": flag})


@app.route("/admin/listings")
@admin_required
def admin_listings():
    base, params, filters = listings_query()
    per_page = admin_per_page()
    total, pages, page, offset = paginate("SELECT COUNT(*) AS c " + base, params, per_page)
    rows = qa("SELECT items.*, users.name AS poster, "
              "(SELECT COUNT(*) FROM claims WHERE claims.item_id = items.id) AS claim_count, "
              "(SELECT COUNT(*) FROM claims WHERE claims.item_id = items.id AND claims.status = 'pending') "
              "AS pending_claims " + base + " ORDER BY items.id DESC LIMIT ? OFFSET ?",
              *params, per_page, offset)
    return render_template("admin_listings.html", rows=rows, total=total, page=page, pages=pages,
                           filters=filters, per_page=per_page, page_sizes=ADMIN_PAGE_SIZES,
                           pager_args=pager_extra(filters, per_page))


@app.route("/admin/listings/export.csv")
@admin_required
def admin_listings_export():
    base, params, _ = listings_query()
    rows = qa("SELECT items.id, items.type, items.title, items.category, items.location, items.event_date, "
              "items.status, users.name, users.email, items.created_at, items.returned_at, items.ref_code, items.custody_status " + base +
              " ORDER BY items.id", *params)
    return csv_response("listings.csv", ["id", "type", "title", "category", "location", "date", "status",
                                         "posted_by", "poster_email", "posted_at", "returned_at", "reference", "custody"],
                        [tuple(r) for r in rows])


@app.route("/admin/listings/<int:item_id>/close", methods=["POST"])
@admin_required
def admin_listing_close(item_id):
    """Close a report with a recorded reason (keeps the record, ends open claims). Rules live in services.close_report."""
    get_item_or_404(item_id)
    act(services.close_report, current_user(), item_id, request.form.get("reason", ""),
        ok="The report was closed and the reason recorded.")
    return admin_back("admin_listings")


@app.route("/admin/listings/<int:item_id>/delete", methods=["POST"])
@admin_required
def admin_listing_delete(item_id):
    item = q1("SELECT * FROM items WHERE id = ?", item_id)
    if item is None:
        flash("That listing no longer exists.", "info")
        return admin_back("admin_listings")
    reason = request.form.get("reason", "").strip()[:200]
    evidence = [r["filename"] for r in qa("SELECT claim_evidence.filename FROM claim_evidence JOIN claims "
                                          "ON claims.id = claim_evidence.claim_id WHERE claims.item_id = ?", item_id)]
    run("DELETE FROM items WHERE id = ?", item_id)
    delete_file(ITEM_DIR, item["photo"])
    for name in evidence:
        delete_file(EVIDENCE_DIR, name)
    audit("item_deleted", f"{item['ref_code']} \"{item['title']}\" {reason}".strip(), rtype="item", rid=item_id)
    notify(item["user_id"], f"Your listing \"{item['title']}\" was removed by an admin."
           + (f" Reason: {reason}" if reason else ""), "")
    flash("Listing deleted.", "info")
    return admin_back("admin_listings")


# ---------------------------------------------------------------- audit log
def audit_query():
    q = request.args.get("q", "").strip()
    action = request.args.get("action", "")
    where, params = "WHERE 1 = 1", []
    if q:
        where += " AND (audit_log.detail LIKE ? OR users.name LIKE ?)"
        params += [f"%{q}%"] * 2
    if action:
        where += " AND audit_log.action = ?"
        params.append(action)
    return ("FROM audit_log LEFT JOIN users ON users.id = audit_log.actor_id " + where, params,
            {"q": q, "action": action})


@app.route("/admin/audit")
@admin_required
def admin_audit():
    base, params, filters = audit_query()
    per_page = admin_per_page(50)
    total, pages, page, offset = paginate("SELECT COUNT(*) AS c " + base, params, per_page)
    rows = qa("SELECT audit_log.*, users.name AS actor " + base + " ORDER BY audit_log.id DESC LIMIT ? OFFSET ?",
              *params, per_page, offset)
    actions = [r["action"] for r in qa("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return render_template("admin_audit.html", rows=rows, total=total, page=page, pages=pages,
                           filters=filters, actions=actions, per_page=per_page, page_sizes=ADMIN_PAGE_SIZES,
                           pager_args=pager_extra(filters, per_page, 50))


@app.route("/admin/audit/export.csv")
@admin_required
def admin_audit_export():
    base, params, _ = audit_query()
    rows = qa("SELECT audit_log.created_at, users.name, audit_log.action, audit_log.detail " + base +
              " ORDER BY audit_log.id DESC", *params)
    return csv_response("audit_log.csv", ["when", "who", "action", "details"],
                        [(r[0], r[1] or "System", r[2], r[3]) for r in rows])


# ================================================================ errors
@app.errorhandler(413)
def too_large(_e):
    flash("That file is too large. The maximum size is 5 MB.", "error")
    return redirect(safe_next(request.referrer))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, message="You do not have permission to do that."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="We could not find that page."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400


init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
