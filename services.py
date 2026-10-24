"""Business rules for the Campus Lost & Found workflow.

Everything that changes a report, a claim or custody lives here, so the rules cannot be
bypassed by a route that forgets a check.  Every function takes an open sqlite3 connection
(`db`), runs its writes inside one BEGIN IMMEDIATE transaction, re-checks eligibility inside
that transaction, and raises WorkflowError for any refused action.

Three independent lifecycles
----------------------------
report status   pending_review -> active -> returned | closed        (rejected from pending_review)
custody status  with_finder | admin_handover_requested -> with_admin | direct_handover_pending
                -> returned | disputed                               (found reports only)
claim status    pending | needs_more_info | under_admin_review -> approved | rejected | withdrawn

Routes never write these columns directly.
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import matching

TS = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------- vocabulary
REPORT_STATUSES = ("pending_review", "active", "rejected", "closed", "returned")
CUSTODY_STATUSES = ("with_finder", "admin_handover_requested", "with_admin",
                    "direct_handover_pending", "returned", "disputed")
CLAIM_STATUSES = ("pending", "needs_more_info", "under_admin_review", "approved", "rejected", "withdrawn")
OPEN_CLAIM = ("pending", "needs_more_info", "under_admin_review")
RISK_LEVELS = ("low", "medium", "high")

REPORT_LABELS = {"pending_review": "Waiting for staff", "active": "Live", "rejected": "Not accepted",
                 "closed": "Closed", "returned": "Given back"}
CUSTODY_LABELS = {"with_finder": "With the finder",
                  "admin_handover_requested": "Waiting to be given to the college office",
                  "with_admin": "At the college office", "direct_handover_pending": "Finder will give it back directly",
                  "returned": "Given back to the owner", "disputed": "There is a problem, staff are checking"}
CLAIM_LABELS = {"pending": "Waiting to be checked", "needs_more_info": "More details needed",
                "under_admin_review": "Staff are checking", "approved": "Accepted",
                "rejected": "Not accepted", "withdrawn": "Taken back"}
RISK_LABELS = {"low": "Low risk", "medium": "Medium risk", "high": "High risk"}

# Default risk by category. Admins can change these on the Policy page (table category_policy).
DEFAULT_RISK = {"Electronics": "high", "ID & Cards": "high", "Wallets": "high",
                "Bags": "medium", "Clothing": "medium", "Keys": "medium", "Accessories": "medium",
                "Books & Notes": "low", "Other": "low"}

# --------------------------------------------------------------------------- limits (configurable)
MIN_CLAIM_LEN, MAX_CLAIM_LEN = 15, 2000
MEDIUM_RISK_MIN_DETAIL = 60          # medium risk: finder may verify only with a photo or a detailed description
MAX_EVIDENCE_PER_CLAIM = 3
REPORTS_PER_HOUR = 10
CLAIMS_PER_DAY = 10
STALE_FINDER_DAYS = 3                # finder has not answered a claim for this long -> shown to staff

# --------------------------------------------------------------------------- transitions
REPORT_TRANSITIONS = {
    "pending_review": {"active", "rejected", "closed"},
    "active": {"closed", "returned"},
    "rejected": set(),
    "closed": set(),
    "returned": set(),               # terminal; only correct_return() may reopen (admin, audited)
}
CUSTODY_TRANSITIONS = {
    "with_finder": {"admin_handover_requested", "with_admin", "direct_handover_pending", "disputed"},
    "admin_handover_requested": {"with_finder", "with_admin", "disputed"},
    "with_admin": {"returned", "disputed"},
    "direct_handover_pending": {"with_finder", "with_admin", "returned", "disputed"},
    "disputed": {"with_finder", "with_admin", "admin_handover_requested", "direct_handover_pending", "returned"},
    "returned": set(),               # terminal; only correct_return() may change it
}
CLAIM_TRANSITIONS = {
    "pending": {"needs_more_info", "under_admin_review", "approved", "rejected", "withdrawn"},
    "needs_more_info": {"pending", "under_admin_review", "rejected", "withdrawn"},
    "under_admin_review": {"approved", "rejected", "needs_more_info", "withdrawn"},
    "approved": {"rejected", "withdrawn"},          # revert (reviewer) or withdraw (claimant), never after return
    "rejected": {"under_admin_review"},             # explicit one-time appeal only
    "withdrawn": set(),
}


class WorkflowError(Exception):
    """A refused action. `status` is the HTTP status a route should use (403 = not allowed)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- plumbing
def _now():
    return datetime.now().strftime(TS)


@contextmanager
def transaction(db):
    """BEGIN IMMEDIATE takes the write lock up front, so two approvals cannot interleave."""
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def audit(db, actor_id, action, detail="", rtype=None, rid=None):
    db.execute("INSERT INTO audit_log (actor_id, action, detail, record_type, record_id, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?)", (actor_id, action, detail[:400], rtype, rid, _now()))


def notify(db, user_id, message, link="", kind="info"):
    db.execute("INSERT INTO notifications (user_id, message, link, kind, created_at) VALUES (?, ?, ?, ?, ?)",
               (user_id, message, link, kind, _now()))


def is_reviewer(user):
    """Authorized college staff: an admin, or a staff member an admin made a custody officer."""
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    return user["role"] == "staff" and bool(user["is_custody_officer"])


def can_view_claim(user, claim, item):
    """Who may read a claim's private description and evidence: the claimant, authorized staff, and the
    finder only while the finder is the assigned reviewer (never after it moves to staff)."""
    if user is None:
        return False
    if user["id"] == claim["claimer_id"] or is_reviewer(user):
        return True
    return (user["id"] == item["user_id"] and claim["handler"] == "finder" and claim["status"] != "withdrawn")


def require_reviewer(user):
    if not is_reviewer(user):
        raise WorkflowError("Only authorized college staff can do that.", 403)


def reviewers(db):
    return db.execute("SELECT id FROM users WHERE status = 'active' AND "
                      "(role = 'admin' OR (role = 'staff' AND is_custody_officer = 1))").fetchall()


def notify_reviewers(db, message, link="", kind="review", exclude=()):
    for r in reviewers(db):
        if r["id"] not in exclude:
            notify(db, r["id"], message, link, kind)


def _item(db, item_id):
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise WorkflowError("That report does not exist.", 404)
    return item


def _claim(db, claim_id):
    claim = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        raise WorkflowError("That claim does not exist.", 404)
    return claim


def _user(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _label(item):
    return f"\"{item['title']}\" ({item['ref_code']})"


def _link(item):
    return f"/item/{item['id']}"


def risk_for(db, category):
    row = db.execute("SELECT risk_level FROM category_policy WHERE category = ?", (category,)).fetchone()
    return row["risk_level"] if row else DEFAULT_RISK.get(category, "medium")


def handover_mode(item):
    """'admin' = college staff must be involved in the handover; 'direct' = finder returns it themselves."""
    if (item["handover_pref"] == "admin" or item["risk_level"] == "high" or item["unsafe"]
            or item["custody_status"] in ("with_admin", "admin_handover_requested")):
        return "admin"
    return "direct"


def _set_report_status(db, item, new, **extra):
    if new not in REPORT_TRANSITIONS[item["status"]]:
        raise WorkflowError(f"A report that is {REPORT_LABELS[item['status']].lower()} cannot become "
                            f"{REPORT_LABELS[new].lower()}.", 409)
    sets, params = ["status = ?", "updated_at = ?"], [new, _now()]
    for k, v in extra.items():
        sets.append(f"{k} = ?")
        params.append(v)
    cur = db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ? AND status = ?",
                     (*params, item["id"], item["status"]))
    if cur.rowcount != 1:
        raise WorkflowError("This report was just changed by someone else. Reload and try again.", 409)


def _set_custody(db, item, new, **extra):
    cur_state = item["custody_status"]
    if new not in CUSTODY_TRANSITIONS.get(cur_state, set()):
        raise WorkflowError("That custody change is not allowed from the current state.", 409)
    sets, params = ["custody_status = ?", "updated_at = ?"], [new, _now()]
    for k, v in extra.items():
        sets.append(f"{k} = ?")
        params.append(v)
    cur = db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ? AND custody_status = ?",
                     (*params, item["id"], cur_state))
    if cur.rowcount != 1:
        raise WorkflowError("This item was just changed by someone else. Reload and try again.", 409)


def _move_claim(db, claim, new, *, reviewer_id=None, reason=None, handler=None, escalation=None,
                info_request=None, decided=False, **extra):
    if new not in CLAIM_TRANSITIONS[claim["status"]]:
        raise WorkflowError(f"A claim that is {CLAIM_LABELS[claim['status']].lower()} cannot become "
                            f"{CLAIM_LABELS[new].lower()}.", 409)
    ts = _now()
    sets, params = ["status = ?", "updated_at = ?"], [new, ts]
    for col, val in (("reviewer_id", reviewer_id), ("review_reason", reason), ("handler", handler),
                     ("escalation_reason", escalation), ("info_request", info_request)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if decided:
        sets.append("decided_at = ?")
        params.append(ts)
    for k, v in extra.items():
        sets.append(f"{k} = ?")
        params.append(v)
    cur = db.execute(f"UPDATE claims SET {', '.join(sets)} WHERE id = ? AND status = ?",
                     (*params, claim["id"], claim["status"]))
    if cur.rowcount != 1:
        raise WorkflowError("This claim was just changed by someone else. Reload and try again.", 409)


def _clean(text, minimum, maximum, what):
    text = (text or "").strip()
    if len(text) < minimum:
        raise WorkflowError(f"{what} needs at least {minimum} characters.")
    if len(text) > maximum:
        raise WorkflowError(f"{what} can be at most {maximum} characters.")
    return text


# --------------------------------------------------------------------------- reports
def check_report_rate(db, user):
    since = (datetime.now() - timedelta(hours=1)).strftime(TS)
    n = db.execute("SELECT COUNT(*) AS c FROM items WHERE user_id = ? AND created_at >= ?",
                   (user["id"], since)).fetchone()["c"]
    if n >= REPORTS_PER_HOUR:
        raise WorkflowError("You have posted a lot of reports in the last hour. Please try again later.", 429)


def contact_problem(text):
    """None if `text` looks like a phone number or an email address, else a short message for the user."""
    text = (text or "").strip()
    if not text:
        return "Add a phone number or email so people can reach you."
    if len(text) > 100:
        return "Keep the contact details under 100 characters."
    has_email = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) is not None
    digits = re.sub(r"\D", "", text)
    if not has_email and not (7 <= len(digits) <= 15):
        return "Enter a phone number (at least 7 digits) or an email address."
    return None


def create_report(db, user, *, kind, title, category, description, private_details, location, event_date,
                  photo, handover_pref=None, unsafe=False, moderation_required=True, contact=""):
    """Insert a report. Selecting a handover option only records a *preference*; custody is never
    WITH_ADMIN here, because nobody has confirmed physical receipt yet."""
    if kind not in ("lost", "found"):
        raise WorkflowError("Choose whether the item was lost or found.")
    risk = risk_for(db, category)
    pref = custody = None
    if kind == "found":
        if handover_pref not in ("admin", "direct"):
            raise WorkflowError("Choose how the found item will be handed over.")
        if unsafe:
            risk, handover_pref = "high", "admin"
        pref = handover_pref
        custody = "admin_handover_requested" if pref == "admin" else "with_finder"
    else:
        unsafe = False
    status = "pending_review" if moderation_required else "active"
    with transaction(db):
        check_report_rate(db, user)
        ts = _now()
        cur = db.execute(
            "INSERT INTO items (user_id, type, title, category, description, private_details, location, "
            "event_date, photo, status, custody_status, handover_pref, risk_level, unsafe, contact, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], kind, title, category, description, private_details, location, event_date, photo,
             status, custody, pref, risk, 1 if unsafe else 0, contact, ts, ts))
        item_id = cur.lastrowid
        db.execute("UPDATE items SET ref_code = ? WHERE id = ?",
                   (f"LF-{datetime.now().year}-{item_id:05d}", item_id))
        if status == "active":
            _notify_matches(db, _item(db, item_id))
        else:
            notify_reviewers(db, "A new report is waiting for review.", f"/item/{item_id}", "review",
                             exclude=(user["id"],))
    return item_id


def _notify_matches(db, item):
    for m in matching.find_matches(db, item, limit=5):
        other = m["item"]
        kind_word = "found" if item["type"] == "found" else "lost"
        mine = "lost" if item["type"] == "found" else "found"
        notify(db, other["user_id"],
               f"A new {kind_word} report may match your {mine} report \"{other['title']}\". "
               "Matches are only suggestions; nothing is verified automatically.",
               _link(item), "match")


def moderate_report(db, actor, item_id, decision, reason=""):
    require_reviewer(actor)
    if decision not in ("approve", "reject"):
        raise WorkflowError("Unknown decision.")
    with transaction(db):
        item = _item(db, item_id)
        if item["status"] != "pending_review":
            raise WorkflowError("This report is not waiting for review.", 409)
        if decision == "reject":
            reason = _clean(reason, 3, 300, "A reason")
            _set_report_status(db, item, "rejected", review_reason=reason)
            notify(db, item["user_id"], f"Your report {_label(item)} was not approved. Open it to see why.",
                   _link(item), "report")
            audit(db, actor["id"], "report_rejected", f"{item['ref_code']}: {reason}", "item", item_id)
        else:
            _set_report_status(db, item, "active", review_reason=(reason or "").strip()[:300])
            notify(db, item["user_id"], f"Your report {_label(item)} is now published.", _link(item), "report")
            audit(db, actor["id"], "report_approved", item["ref_code"], "item", item_id)
            _notify_matches(db, _item(db, item_id))


def edit_report(db, user, item_id, *, title, category, description, private_details, location, event_date,
                handover_pref=None, contact=""):
    with transaction(db):
        item = _item(db, item_id)
        if item["user_id"] != user["id"]:
            raise WorkflowError("You can only edit your own reports.", 403)
        if item["status"] not in ("pending_review", "active"):
            raise WorkflowError("This report can no longer be edited.", 409)
        if db.execute("SELECT 1 FROM claims WHERE item_id = ?", (item_id,)).fetchone():
            raise WorkflowError("This report has claims, so it can no longer be edited. "
                                "Close it and post a new report if something is wrong.", 409)
        risk = risk_for(db, category)
        pref, custody = item["handover_pref"], item["custody_status"]
        if item["type"] == "found":
            if item["unsafe"]:
                risk = "high"
            if handover_pref in ("admin", "direct") and handover_pref != pref and not item["unsafe"]:
                if custody not in ("with_finder", "admin_handover_requested"):
                    raise WorkflowError("The handover method cannot change after the item reached college custody.", 409)
                pref = handover_pref
                custody = "admin_handover_requested" if pref == "admin" else "with_finder"
        db.execute("UPDATE items SET title = ?, category = ?, description = ?, private_details = ?, "
                   "location = ?, event_date = ?, risk_level = ?, handover_pref = ?, custody_status = ?, "
                   "contact = ?, updated_at = ? WHERE id = ?",
                   (title, category, description, private_details, location, event_date, risk, pref,
                    custody, contact if item["type"] == "lost" else item["contact"], _now(), item_id))


def close_report(db, actor, item_id, reason):
    """Owner cancels their own report, or staff close a duplicate/abusive/obsolete one."""
    reason = _clean(reason, 3, 300, "A reason")
    with transaction(db):
        item = _item(db, item_id)
        staff = is_reviewer(actor)
        if item["user_id"] != actor["id"] and not staff:
            raise WorkflowError("You can only close your own reports.", 403)
        if item["status"] not in ("pending_review", "active"):
            raise WorkflowError("This report is already finished.", 409)
        if not staff:
            if item["custody_status"] in ("with_admin", "disputed"):
                raise WorkflowError("The college office holds this item, so only staff can close the report.", 409)
            if item["approved_claim_id"]:
                raise WorkflowError("A claim was approved for this item. Ask staff to resolve it first.", 409)
        _set_report_status(db, item, "closed", closure_reason=reason)
        for c in db.execute("SELECT * FROM claims WHERE item_id = ? AND status IN "
                            "('pending','needs_more_info','under_admin_review','approved')", (item_id,)).fetchall():
            _move_claim(db, c, "rejected", reason="The report was closed.", decided=True)
            notify(db, c["claimer_id"], f"The report {_label(item)} was closed, so your claim ended.",
                   _link(item), "decision")
        db.execute("UPDATE items SET approved_claim_id = NULL WHERE id = ?", (item_id,))
        audit(db, actor["id"], "report_closed", f"{item['ref_code']}: {reason}", "item", item_id)


def change_handover_pref(db, user, item_id, pref):
    if pref not in ("admin", "direct"):
        raise WorkflowError("Choose a handover method.")
    with transaction(db):
        item = _item(db, item_id)
        if item["user_id"] != user["id"] or item["type"] != "found":
            raise WorkflowError("You can only change the handover method of your own found report.", 403)
        if item["status"] not in ("pending_review", "active"):
            raise WorkflowError("This report is finished.", 409)
        if item["unsafe"] and pref == "direct":
            raise WorkflowError("Items flagged as unsafe must go to the college office.", 409)
        if item["custody_status"] not in ("with_finder", "admin_handover_requested") or item["approved_claim_id"]:
            raise WorkflowError("The handover method can no longer be changed.", 409)
        custody = "admin_handover_requested" if pref == "admin" else "with_finder"
        db.execute("UPDATE items SET handover_pref = ?, custody_status = ?, updated_at = ? WHERE id = ?",
                   (pref, custody, _now(), item_id))


# --------------------------------------------------------------------------- custody (admin route)
def accept_custody(db, actor, item_id, received_on="", notes=""):
    """Authorized staff confirm they physically hold the item. Only this makes custody WITH_ADMIN."""
    require_reviewer(actor)
    notes = (notes or "").strip()[:300]
    with transaction(db):
        item = _item(db, item_id)
        if item["type"] != "found":
            raise WorkflowError("Only found-item reports can be received into custody.", 409)
        if item["status"] == "pending_review":
            raise WorkflowError("Review and publish this report first (or reject it), then record receipt.", 409)
        if item["status"] != "active":
            raise WorkflowError("This report is finished, so it cannot be received into custody.", 409)
        if item["custody_status"] == "with_admin":
            raise WorkflowError("This item is already recorded as received.", 409)
        if item["custody_status"] not in ("with_finder", "admin_handover_requested", "direct_handover_pending"):
            raise WorkflowError("This item cannot be received while it is disputed or returned.", 409)
        if received_on:
            try:
                day = datetime.strptime(received_on, "%Y-%m-%d").date()
            except ValueError:
                raise WorkflowError("Enter a valid receipt date.")
            if day > datetime.now().date() or day < datetime.strptime(item["created_at"][:10], "%Y-%m-%d").date():
                raise WorkflowError("The receipt date must be between the report date and today.")
            notes = (f"Physically received on {received_on}. " + notes).strip()
        prev = item["custody_status"]
        _set_custody(db, item, "with_admin")
        db.execute("INSERT INTO custody_events (item_id, from_state, to_state, handover_type, released_by, "
                   "received_by, staff_id, claim_id, confirmed, notes, created_at) "
                   "VALUES (?, ?, 'with_admin', 'ADMIN_RECEIPT', ?, ?, ?, ?, 1, ?, ?)",
                   (item_id, prev, item["user_id"], actor["id"], actor["id"], item["approved_claim_id"],
                    notes, _now()))
        # A claim a finder was still weighing must now be decided by staff, who hold the item.
        escalate_open_claims(db, item, "The item is now held by the college office.", actor["id"])
        notify(db, item["user_id"], f"Thank you. College staff confirmed they received {_label(item)}.",
               _link(item), "handover")
        if item["approved_claim_id"]:
            claim = _claim(db, item["approved_claim_id"])
            notify(db, claim["claimer_id"], f"{_label(item)} is now at the college office. Staff will arrange collection.",
                   _link(item), "handover")
        audit(db, actor["id"], "custody_accepted", f"{item['ref_code']} {prev} -> with_admin. {notes}",
              "item", item_id)


def escalate_open_claims(db, item, reason, actor_id=None, only_finder_handled=True):
    """Move finder-handled open claims to staff review (used for competition, custody, suspension)."""
    q = "SELECT * FROM claims WHERE item_id = ? AND status IN ('pending','needs_more_info')"
    if only_finder_handled:
        q += " AND handler = 'finder'"
    moved = 0
    for c in db.execute(q, (item["id"],)).fetchall():
        _move_claim(db, c, "under_admin_review", handler="admin", escalation=reason)
        notify(db, c["claimer_id"], f"Your claim on {_label(item)} is now being reviewed by college staff.",
               _link(item), "review")
        moved += 1
    if moved:
        notify_reviewers(db, f"{moved} claim(s) on {_label(item)} need staff review: {reason}", _link(item),
                         "review", exclude=(actor_id,) if actor_id else ())
    return moved


# --------------------------------------------------------------------------- claims
def submit_claim(db, user, item_id, message, evidence=()):
    """A potential owner claims a specific found report. Never verifies ownership by itself."""
    message = _clean(message, MIN_CLAIM_LEN, MAX_CLAIM_LEN, "Your private description")
    with transaction(db):
        item = _item(db, item_id)
        if item["type"] != "found":
            raise WorkflowError("Claims can only be made on found-item reports.", 409)
        if item["user_id"] == user["id"]:
            raise WorkflowError("You cannot claim an item you reported yourself.", 409)
        if item["status"] != "active":
            raise WorkflowError("This report is not open for claims.", 409)
        if item["custody_status"] == "disputed":
            raise WorkflowError("This item is under an admin dispute review and cannot be claimed right now.", 409)
        if item["custody_status"] == "returned":
            raise WorkflowError("This item has already been returned.", 409)
        if item["approved_claim_id"]:
            raise WorkflowError("A claim has already been approved for this item and the handover is in progress.", 409)
        if db.execute("SELECT 1 FROM claims WHERE item_id = ? AND claimer_id = ?", (item_id, user["id"])).fetchone():
            raise WorkflowError("You have already submitted a claim for this item.", 409)
        since = (datetime.now() - timedelta(days=1)).strftime(TS)
        if db.execute("SELECT COUNT(*) AS c FROM claims WHERE claimer_id = ? AND created_at >= ?",
                      (user["id"], since)).fetchone()["c"] >= CLAIMS_PER_DAY:
            raise WorkflowError("You have submitted many claims today. Please try again tomorrow.", 429)
        if len(evidence) > MAX_EVIDENCE_PER_CLAIM:
            raise WorkflowError("Too many evidence files.")
        if item["risk_level"] == "high":
            handler, status, why = "admin", "under_admin_review", "High-risk item: staff verification required."
        elif item["custody_status"] == "with_admin" or item["handover_pref"] == "admin":
            handler, status, why = "admin", "under_admin_review", "The item is handled by the college office."
        else:
            handler, status, why = "finder", "pending", None
        ts = _now()
        cur = db.execute("INSERT INTO claims (item_id, claimer_id, message, status, handler, escalation_reason, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (item_id, user["id"], message, status, handler, why or "", ts, ts))
        claim_id = cur.lastrowid
        for name in evidence:
            db.execute("INSERT INTO claim_evidence (claim_id, filename, uploaded_by, created_at) VALUES (?, ?, ?, ?)",
                       (claim_id, name, user["id"], ts))
        others = db.execute("SELECT COUNT(*) AS c FROM claims WHERE item_id = ? AND id != ? AND status IN "
                            "('pending','needs_more_info','under_admin_review')", (item_id, claim_id)).fetchone()["c"]
        if others:
            for c in db.execute("SELECT * FROM claims WHERE item_id = ? AND status IN ('pending','needs_more_info')",
                                (item_id,)).fetchall():
                _move_claim(db, c, "under_admin_review", handler="admin",
                            escalation="Several people claimed this item; staff must decide.")
                if c["id"] != claim_id:
                    notify(db, c["claimer_id"], f"Another claim was made on {_label(item)}, so staff will decide.",
                           _link(item), "review")
            status = "under_admin_review"
        # The finder only learns that a claim exists; never who made it or what it says.
        if status == "pending":
            notify(db, item["user_id"], f"A new ownership claim needs your review on {_label(item)}.",
                   _link(item), "claim")
        else:
            notify(db, item["user_id"], f"A claim on {_label(item)} is being handled by college staff.",
                   _link(item), "claim")
            notify_reviewers(db, f"A claim on {_label(item)} needs staff review.", _link(item), "review",
                             exclude=(item["user_id"], user["id"]))
        audit(db, user["id"], "claim_submitted", f"{item['ref_code']} claim {claim_id}", "claim", claim_id)
    return claim_id


def respond_info(db, user, claim_id, text, evidence=()):
    text = _clean(text, 5, MAX_CLAIM_LEN, "Your answer")
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if claim["claimer_id"] != user["id"]:
            raise WorkflowError("This is not your claim.", 403)
        if claim["status"] != "needs_more_info":
            raise WorkflowError("No additional information was requested for this claim.", 409)
        have = db.execute("SELECT COUNT(*) AS c FROM claim_evidence WHERE claim_id = ?", (claim_id,)).fetchone()["c"]
        if have + len(evidence) > MAX_EVIDENCE_PER_CLAIM:
            raise WorkflowError("You can attach at most %d photos to a claim." % MAX_EVIDENCE_PER_CLAIM)
        merged = (claim["message"] + "\n\n[Additional information " + _now()[:10] + "]\n" + text)[:MAX_CLAIM_LEN * 2]
        back = "pending" if claim["handler"] == "finder" else "under_admin_review"
        _move_claim(db, claim, back, message=merged)
        for name in evidence:
            db.execute("INSERT INTO claim_evidence (claim_id, filename, uploaded_by, created_at) VALUES (?, ?, ?, ?)",
                       (claim_id, name, user["id"], _now()))
        if claim["handler"] == "finder":
            notify(db, item["user_id"], f"The claimant answered your question on {_label(item)}.", _link(item), "info")
        else:
            notify_reviewers(db, f"A claimant answered the question on {_label(item)}.", _link(item), "info")


def withdraw_claim(db, user, claim_id):
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if claim["claimer_id"] != user["id"]:
            raise WorkflowError("This is not your claim.", 403)
        if item["status"] == "returned":
            raise WorkflowError("The item was already returned.", 409)
        _move_claim(db, claim, "withdrawn", reason="Withdrawn by the claimant.", decided=True)
        if claim["status"] == "approved":
            _release_approval(db, item)
            notify(db, item["user_id"], f"The approved claim on {_label(item)} was withdrawn. The item is open again.",
                   _link(item), "decision")
        audit(db, user["id"], "claim_withdrawn", f"{item['ref_code']} claim {claim_id}", "claim", claim_id)


def appeal_claim(db, user, claim_id, text):
    """One explicit appeal for a claim the *finder* rejected. Admin decisions are final unless an admin reopens."""
    text = _clean(text, 10, 500, "Your reason for the appeal")
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if claim["claimer_id"] != user["id"]:
            raise WorkflowError("This is not your claim.", 403)
        if claim["status"] != "rejected":
            raise WorkflowError("Only a rejected claim can be appealed.", 409)
        if claim["handler"] != "finder":
            raise WorkflowError("A staff decision can only be reopened by an administrator.", 409)
        if claim["appeal_used"]:
            raise WorkflowError("This claim was already appealed once.", 409)
        if item["status"] != "active" or item["approved_claim_id"] or item["custody_status"] in ("returned", "disputed"):
            raise WorkflowError("This item is no longer available, so the claim cannot be reopened.", 409)
        _move_claim(db, claim, "under_admin_review", handler="admin", escalation="Appeal: " + text,
                    appeal_used=1)
        notify_reviewers(db, f"A rejected claim on {_label(item)} was appealed.", _link(item), "review")
        notify(db, item["user_id"], f"A rejected claim on {_label(item)} was sent to staff for another look.",
               _link(item), "review")
        audit(db, user["id"], "claim_appealed", f"{item['ref_code']} claim {claim_id}", "claim", claim_id)


def finder_can_verify(db, item, claim):
    """Risk policy for letting the finder (not staff) verify ownership. Returns (ok, why_not)."""
    if item["risk_level"] == "high" or item["unsafe"]:
        return False, "High-risk items must be verified by college staff."
    if item["custody_status"] in ("with_admin", "admin_handover_requested") or item["handover_pref"] == "admin":
        return False, "Staff verify claims for items handled by the college office."
    others = db.execute("SELECT COUNT(*) AS c FROM claims WHERE item_id = ? AND id != ? AND status IN "
                        "('pending','needs_more_info','under_admin_review')", (item["id"], claim["id"])).fetchone()["c"]
    if others:
        return False, "More than one person claimed this item, so staff must decide."
    if item["risk_level"] == "medium":
        has_photo = db.execute("SELECT 1 FROM claim_evidence WHERE claim_id = ?", (claim["id"],)).fetchone()
        if not has_photo and len(claim["message"]) < MEDIUM_RISK_MIN_DETAIL:
            return False, "For this kind of item the claim needs a photo or a more detailed description."
    return True, ""


def finder_review(db, user, claim_id, action, note=""):
    """The finder's options under the verification policy: verify, request_info, escalate, reject."""
    if action not in ("verify", "request_info", "escalate", "reject"):
        raise WorkflowError("Unknown action.")
    note = (note or "").strip()
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if item["user_id"] != user["id"]:
            raise WorkflowError("Only the finder who reported this item can do that.", 403)
        if claim["claimer_id"] == user["id"]:
            raise WorkflowError("You cannot decide your own claim.", 409)
        if claim["handler"] != "finder":
            raise WorkflowError("College staff are handling this claim.", 409)
        if action == "verify":
            if claim["status"] != "pending":
                raise WorkflowError("This claim is not waiting for your decision.", 409)
            ok, why = finder_can_verify(db, item, claim)
            if not ok:
                raise WorkflowError(why + " Please send it to staff.", 409)
            _approve(db, claim, item, user, note or "Verified by the finder.")
        elif action == "request_info":
            note = _clean(note, 5, 500, "Your question")
            if claim["status"] != "pending":
                raise WorkflowError("This claim is not waiting for your decision.", 409)
            _move_claim(db, claim, "needs_more_info", info_request=note)
            notify(db, claim["claimer_id"], f"The finder needs more information about your claim on {_label(item)}.",
                   _link(item), "info")
        elif action == "escalate":
            note = _clean(note, 3, 500, "A short reason")
            if claim["status"] not in ("pending", "needs_more_info"):
                raise WorkflowError("This claim cannot be escalated now.", 409)
            _move_claim(db, claim, "under_admin_review", handler="admin", escalation="Finder: " + note)
            notify(db, claim["claimer_id"], f"Your claim on {_label(item)} was sent to college staff for review.",
                   _link(item), "review")
            notify_reviewers(db, f"The finder asked staff to review a claim on {_label(item)}.", _link(item), "review")
            audit(db, user["id"], "claim_escalated", f"{item['ref_code']} claim {claim_id}", "claim", claim_id)
        else:  # reject
            note = _clean(note, 3, 500, "A reason")
            if claim["status"] not in ("pending", "needs_more_info"):
                raise WorkflowError("This claim cannot be rejected now.", 409)
            _move_claim(db, claim, "rejected", reviewer_id=user["id"], reason=note, decided=True)
            notify(db, claim["claimer_id"], f"Your claim on {_label(item)} was not approved. Open it to see the reason.",
                   _link(item), "decision")
            audit(db, user["id"], "claim_rejected", f"{item['ref_code']} claim {claim_id} by finder", "claim", claim_id)


def staff_review(db, actor, claim_id, action, reason):
    """Authorized staff decision: approve, reject or request_info. A reason is always recorded."""
    require_reviewer(actor)
    if action not in ("approve", "reject", "request_info"):
        raise WorkflowError("Unknown action.")
    reason = _clean(reason, 3, 500, "A reason")
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if claim["claimer_id"] == actor["id"]:
            raise WorkflowError("You cannot decide your own claim.", 409)
        if action == "approve":
            _approve(db, claim, item, actor, reason)
        elif action == "reject":
            if claim["status"] not in OPEN_CLAIM:
                raise WorkflowError("This claim is not open for a decision.", 409)
            _move_claim(db, claim, "rejected", reviewer_id=actor["id"], reason=reason, decided=True)
            notify(db, claim["claimer_id"], f"Your claim on {_label(item)} was not approved. Open it to see the reason.",
                   _link(item), "decision")
            notify(db, item["user_id"], f"A claim on {_label(item)} was rejected by staff.", _link(item), "decision")
            audit(db, actor["id"], "claim_rejected", f"{item['ref_code']} claim {claim_id}: {reason}", "claim", claim_id)
        else:
            if claim["status"] not in ("pending", "under_admin_review"):
                raise WorkflowError("More information can only be requested on a claim under review.", 409)
            _move_claim(db, claim, "needs_more_info", reviewer_id=actor["id"], handler="admin", info_request=reason)
            notify(db, claim["claimer_id"], f"College staff need more information about your claim on {_label(item)}.",
                   _link(item), "info")
            audit(db, actor["id"], "claim_info_requested", f"{item['ref_code']} claim {claim_id}", "claim", claim_id)


def _approve(db, claim, item, reviewer, reason):
    """The one place where a claim becomes APPROVED. Re-checks everything inside the caller's transaction."""
    claim = _claim(db, claim["id"])
    item = _item(db, item["id"])
    if claim["status"] not in ("pending", "under_admin_review"):
        raise WorkflowError("This claim can no longer be approved.", 409)
    if item["type"] != "found" or item["status"] != "active":
        raise WorkflowError("This item is not available for return.", 409)
    if item["custody_status"] in ("returned", "disputed"):
        raise WorkflowError("This item is already returned or under a dispute.", 409)
    if db.execute("SELECT 1 FROM claims WHERE item_id = ? AND status = 'approved'", (item["id"],)).fetchone() \
            or item["approved_claim_id"]:
        raise WorkflowError("Another claim was already approved for this item.", 409)
    if reviewer["id"] == claim["claimer_id"] or item["user_id"] == claim["claimer_id"]:
        raise WorkflowError("Nobody can approve their own claim.", 409)
    claimer = _user(db, claim["claimer_id"])
    if claimer is None or claimer["status"] != "active":
        raise WorkflowError("The claimant's account is not active.", 409)
    try:
        _move_claim(db, claim, "approved", reviewer_id=reviewer["id"], reason=reason, decided=True)
    except sqlite3.IntegrityError:
        raise WorkflowError("Another claim was already approved for this item.", 409)
    for other in db.execute("SELECT * FROM claims WHERE item_id = ? AND id != ? AND status IN "
                            "('pending','needs_more_info','under_admin_review')", (item["id"], claim["id"])).fetchall():
        _move_claim(db, other, "rejected", reason="Another claim was approved for this item.", decided=True)
        notify(db, other["claimer_id"], f"Your claim on {_label(item)} was not approved. Open it to see the reason.",
               _link(item), "decision")
    mode = handover_mode(item)
    if mode == "direct":
        _set_custody(db, item, "direct_handover_pending", custody_prior=item["custody_status"],
                     approved_claim_id=claim["id"])
        notify(db, claim["claimer_id"], f"Your claim on {_label(item)} was verified. Arrange a safe handover with the "
               "finder from the report page.", _link(item), "handover")
        notify(db, item["user_id"], f"The owner of {_label(item)} was verified. Arrange a safe handover; both of you "
               "confirm it afterwards.", _link(item), "handover")
    else:
        db.execute("UPDATE items SET approved_claim_id = ?, updated_at = ? WHERE id = ?",
                   (claim["id"], _now(), item["id"]))
        notify(db, claim["claimer_id"], f"Your claim on {_label(item)} was approved. College staff will arrange "
               "collection; bring your college ID.", _link(item), "handover")
        if item["custody_status"] == "with_admin":
            notify(db, item["user_id"], f"The owner of {_label(item)} was verified. Staff will return the item.",
                   _link(item), "handover")
        else:
            notify(db, item["user_id"], f"The owner of {_label(item)} was verified. Please take the item to the "
                   "college office so staff can return it.", _link(item), "handover")
    audit(db, reviewer["id"], "claim_approved",
          f"{item['ref_code']} claim {claim['id']} ({mode} handover, {item['risk_level']} risk): {reason}",
          "claim", claim["id"])


def _release_approval(db, item):
    """Undo the effects of an approval (withdrawal, revert, suspension): free the item for new claims."""
    item = _item(db, item["id"])
    extra = {"approved_claim_id": None}
    if item["custody_status"] in ("direct_handover_pending", "disputed"):
        target = item["custody_prior"] if item["custody_prior"] in ("with_finder", "admin_handover_requested") \
            else "with_finder"
        _set_custody(db, item, target, custody_prior=None, **extra)
    else:
        db.execute("UPDATE items SET approved_claim_id = NULL, updated_at = ? WHERE id = ?", (_now(), item["id"]))


def revert_approval(db, actor, claim_id, reason):
    """Staff undo an approval whose handover never happened. The claim becomes rejected; the item reopens."""
    require_reviewer(actor)
    reason = _clean(reason, 3, 500, "A reason")
    with transaction(db):
        claim = _claim(db, claim_id)
        item = _item(db, claim["item_id"])
        if claim["status"] != "approved" or item["status"] != "active":
            raise WorkflowError("Only an approved claim on a not-yet-returned item can be reverted.", 409)
        _move_claim(db, claim, "rejected", reviewer_id=actor["id"], reason="Approval reverted: " + reason, decided=True)
        _release_approval(db, item)
        for uid in (claim["claimer_id"], item["user_id"]):
            notify(db, uid, f"The approval on {_label(item)} was reverted by staff because the handover did not "
                   "happen. The item is open again.", _link(item), "decision")
        audit(db, actor["id"], "approval_reverted", f"{item['ref_code']} claim {claim_id}: {reason}", "claim", claim_id)


# --------------------------------------------------------------------------- handover
def _complete_return(db, item, claim, handover_type, released_by, received_by, staff_id, notes):
    cur = db.execute("UPDATE items SET status = 'returned', custody_status = 'returned', returned_at = ?, "
                     "updated_at = ?, custody_prior = NULL WHERE id = ? AND status = 'active' "
                     "AND custody_status = ? AND approved_claim_id = ?",
                     (_now(), _now(), item["id"], item["custody_status"], claim["id"]))
    if cur.rowcount != 1:
        raise WorkflowError("This return was already completed or is no longer possible.", 409)
    db.execute("INSERT INTO custody_events (item_id, from_state, to_state, handover_type, released_by, received_by, "
               "staff_id, claim_id, confirmed, notes, created_at) VALUES (?, ?, 'returned', ?, ?, ?, ?, ?, 1, ?, ?)",
               (item["id"], item["custody_status"], handover_type, released_by, received_by, staff_id,
                claim["id"], notes[:300], _now()))
    notify(db, claim["claimer_id"], f"The return of {_label(item)} is recorded. Thank you!", _link(item), "return")
    notify(db, item["user_id"], f"The return of {_label(item)} is recorded. Thank you for helping!",
           _link(item), "return")


def release_to_claimant(db, actor, item_id, acknowledged, note=""):
    """Admin route, final step: staff hand the item to the verified claimant and record the claimant's
    acknowledgment. Requires WITH_ADMIN, so college custody must have been confirmed first."""
    require_reviewer(actor)
    if not acknowledged:
        raise WorkflowError("Tick the box to confirm the claimant acknowledged receipt.")
    with transaction(db):
        item = _item(db, item_id)
        if item["status"] != "active" or not item["approved_claim_id"]:
            raise WorkflowError("There is no approved claim waiting for collection on this report.", 409)
        claim = _claim(db, item["approved_claim_id"])
        if claim["status"] != "approved":
            raise WorkflowError("The claim is no longer approved.", 409)
        if item["custody_status"] == "disputed":
            raise WorkflowError("This item is disputed and cannot be released until the dispute is resolved.", 409)
        if item["custody_status"] != "with_admin":
            raise WorkflowError("Staff must first confirm they physically received the item.", 409)
        if actor["id"] == claim["claimer_id"]:
            raise WorkflowError("You cannot release an item to yourself.", 409)
        claimer = _user(db, claim["claimer_id"])
        if claimer is None or claimer["status"] != "active":
            raise WorkflowError("The claimant's account is not active.", 409)
        note = (note or "").strip()[:300]
        _complete_return(db, item, claim, "ADMIN_RELEASE", actor["id"], claim["claimer_id"], actor["id"], note)
        ts = _now()
        db.execute("INSERT OR IGNORE INTO handover_confirmations (item_id, claim_id, user_id, party, outcome, note, "
                   "recorded_by, created_at) VALUES (?, ?, ?, 'staff', 'confirmed', ?, ?, ?)",
                   (item_id, claim["id"], actor["id"], note, actor["id"], ts))
        db.execute("INSERT OR IGNORE INTO handover_confirmations (item_id, claim_id, user_id, party, outcome, note, "
                   "recorded_by, created_at) VALUES (?, ?, ?, 'claimant', 'confirmed', 'Acknowledged in person', ?, ?)",
                   (item_id, claim["id"], claim["claimer_id"], actor["id"], ts))
        audit(db, actor["id"], "custody_released", f"{item['ref_code']} released to claim {claim['id']}. {note}",
              "item", item_id)


def confirm_direct_handover(db, user, item_id, outcome, note=""):
    """Direct route: finder and claimant each confirm the exchange. Returns one of
    'recorded' (waiting for the other party), 'completed', 'disputed' or 'duplicate' (safe retry)."""
    if outcome not in ("confirmed", "disputed"):
        raise WorkflowError("Unknown outcome.")
    note = (note or "").strip()[:300]
    if outcome == "disputed":
        note = _clean(note, 5, 300, "A short explanation")
    with transaction(db):
        item = _item(db, item_id)
        claim = _claim(db, item["approved_claim_id"]) if item["approved_claim_id"] else None
        if claim is None or claim["status"] != "approved":
            raise WorkflowError("There is no verified claim waiting for a handover on this report.", 409)
        if user["id"] == item["user_id"]:
            party = "finder"
        elif user["id"] == claim["claimer_id"]:
            party = "claimant"
        else:
            raise WorkflowError("Only the finder and the verified claimant can confirm a handover.", 403)
        if item["status"] == "returned":
            return "duplicate"
        if item["status"] != "active" or handover_mode(item) != "direct":
            raise WorkflowError("This handover is arranged through college staff.", 409)
        if item["custody_status"] == "disputed":
            raise WorkflowError("This handover is disputed and waiting for an admin.", 409)
        if item["custody_status"] != "direct_handover_pending":
            raise WorkflowError("This item is not waiting for a direct handover.", 409)
        prior = db.execute("SELECT outcome FROM handover_confirmations WHERE claim_id = ? AND user_id = ?",
                           (claim["id"], user["id"])).fetchone()
        if prior:
            if prior["outcome"] == outcome:
                return "duplicate"          # network retry or double click: no second record
            raise WorkflowError("You already recorded a different answer. Contact staff if something changed.", 409)
        db.execute("INSERT INTO handover_confirmations (item_id, claim_id, user_id, party, outcome, note, "
                   "recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (item_id, claim["id"], user["id"], party, outcome, note, user["id"], _now()))
        other_id = claim["claimer_id"] if party == "finder" else item["user_id"]
        if outcome == "disputed":
            _set_custody(db, item, "disputed", custody_prior="direct_handover_pending")
            notify(db, other_id, f"The handover of {_label(item)} was reported as not completed. Staff will review it.",
                   _link(item), "dispute")
            notify_reviewers(db, f"A direct handover of {_label(item)} is disputed.", _link(item), "dispute")
            audit(db, user["id"], "handover_disputed", f"{item['ref_code']} by {party}", "item", item_id)
            return "disputed"
        both = db.execute("SELECT COUNT(*) AS c FROM handover_confirmations WHERE claim_id = ? AND outcome = "
                          "'confirmed' AND party IN ('finder','claimant')", (claim["id"],)).fetchone()["c"]
        if both >= 2:
            _complete_return(db, item, claim, "DIRECT_RETURN", item["user_id"], claim["claimer_id"], None,
                             "Confirmed by finder and claimant")
            audit(db, user["id"], "handover_completed", f"{item['ref_code']} direct return, both confirmed",
                  "item", item_id)
            return "completed"
        notify(db, other_id, f"The other person confirmed the handover of {_label(item)}. Please confirm it too once you "
               "have exchanged the item.", _link(item), "handover")
        return "recorded"


def arrange_meeting(db, user, item_id, text):
    text = _clean(text, 5, 200, "The meeting place and time")
    with transaction(db):
        item = _item(db, item_id)
        claim = _claim(db, item["approved_claim_id"]) if item["approved_claim_id"] else None
        if claim is None or item["status"] != "active" or item["custody_status"] != "direct_handover_pending":
            raise WorkflowError("There is no direct handover to arrange.", 409)
        if user["id"] not in (item["user_id"], claim["claimer_id"]):
            raise WorkflowError("Only the finder and the verified claimant can arrange the handover.", 403)
        db.execute("UPDATE items SET meeting_note = ?, meeting_by = ?, updated_at = ? WHERE id = ?",
                   (text, user["id"], _now(), item_id))
        other = claim["claimer_id"] if user["id"] == item["user_id"] else item["user_id"]
        notify(db, other, f"A meeting place was suggested for {_label(item)}. Open the report to read it.",
               _link(item), "handover")


def resolve_dispute(db, actor, item_id, resolution, reason):
    """Staff close a disputed handover: 'complete' records the return, 'reopen' rejects the claim and reopens."""
    require_reviewer(actor)
    if resolution not in ("complete", "reopen"):
        raise WorkflowError("Unknown resolution.")
    reason = _clean(reason, 5, 500, "A reason")
    with transaction(db):
        item = _item(db, item_id)
        if item["custody_status"] != "disputed" or item["status"] != "active":
            raise WorkflowError("This report has no open dispute.", 409)
        claim = _claim(db, item["approved_claim_id"]) if item["approved_claim_id"] else None
        if claim is not None and actor["id"] in (claim["claimer_id"], item["user_id"]):
            raise WorkflowError("You are part of this handover, so another staff member must resolve it.", 409)
        if resolution == "complete":
            if claim is None:
                raise WorkflowError("There is no approved claim to complete.", 409)
            _complete_return(db, item, claim, "DISPUTE_RESOLUTION", item["user_id"], claim["claimer_id"], actor["id"],
                             "Dispute resolved: " + reason)
        else:
            if claim is not None:
                _move_claim(db, claim, "rejected", reviewer_id=actor["id"], reason="Dispute resolved: " + reason,
                            decided=True)
            _release_approval(db, item)
            for uid in {item["user_id"], claim["claimer_id"] if claim else item["user_id"]}:
                notify(db, uid, f"The dispute on {_label(item)} was resolved by staff. The item is open again.",
                       _link(item), "dispute")
        audit(db, actor["id"], "dispute_resolved", f"{item['ref_code']} {resolution}: {reason}", "item", item_id)


def correct_return(db, actor, item_id, reason):
    """Explicit, audited admin correction: RETURNED is otherwise terminal."""
    if actor["role"] != "admin":
        raise WorkflowError("Only an administrator can correct a recorded return.", 403)
    reason = _clean(reason, 5, 500, "A reason")
    with transaction(db):
        item = _item(db, item_id)
        if item["status"] != "returned":
            raise WorkflowError("Only a returned report can be corrected.", 409)
        last = db.execute("SELECT handover_type FROM custody_events WHERE item_id = ? AND to_state = 'returned' "
                          "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        target = "with_admin" if last and last["handover_type"] in ("ADMIN_RELEASE", "DISPUTE_RESOLUTION") else "with_finder"
        if item["handover_pref"] == "admin" and target == "with_finder":
            target = "admin_handover_requested"
        db.execute("UPDATE items SET status = 'active', custody_status = ?, returned_at = NULL, "
                   "approved_claim_id = NULL, custody_prior = NULL, updated_at = ? WHERE id = ?",
                   (target, _now(), item_id))
        if item["approved_claim_id"]:
            db.execute("UPDATE claims SET status = 'rejected', review_reason = ?, updated_at = ? WHERE id = ?",
                       ("Return corrected by an administrator.", _now(), item["approved_claim_id"]))
        db.execute("INSERT INTO custody_events (item_id, from_state, to_state, handover_type, staff_id, confirmed, "
                   "notes, created_at) VALUES (?, 'returned', ?, 'CORRECTION', ?, 1, ?, ?)",
                   (item_id, target, actor["id"], reason[:300], _now()))
        notify(db, item["user_id"], f"The recorded return of {_label(item)} was corrected by an administrator.",
               _link(item), "dispute")
        audit(db, actor["id"], "return_corrected", f"{item['ref_code']}: {reason}", "item", item_id)


# --------------------------------------------------------------------------- accounts
def handle_user_suspended(db, user_id, actor_id):
    """A suspended member cannot keep a claim or a handover hostage. Their open claims are withdrawn,
    approvals released, claims waiting on them as finder go to staff, and direct handovers go to dispute review."""
    with transaction(db):
        for c in db.execute("SELECT * FROM claims WHERE claimer_id = ? AND status IN "
                            "('pending','needs_more_info','under_admin_review','approved')", (user_id,)).fetchall():
            item = _item(db, c["item_id"])
            was_approved = c["status"] == "approved"
            _move_claim(db, c, "withdrawn", reason="The claimant's account was suspended.", decided=True)
            if was_approved and item["status"] == "active":
                _release_approval(db, item)
                notify(db, item["user_id"], f"The approved claim on {_label(item)} ended. The item is open again.",
                       _link(item), "decision")
        for item in db.execute("SELECT * FROM items WHERE user_id = ? AND type = 'found' AND status = 'active'",
                               (user_id,)).fetchall():
            escalate_open_claims(db, item, "The finder's account was suspended.", actor_id)
            if item["custody_status"] == "direct_handover_pending":
                _set_custody(db, item, "disputed", custody_prior="direct_handover_pending")
                notify_reviewers(db, f"The finder of {_label(item)} was suspended during a handover; please resolve it.",
                                 _link(item), "dispute")
        audit(db, actor_id, "suspension_effects_applied", f"user {user_id}", "user", user_id)


def set_category_risk(db, actor, category, risk):
    if actor["role"] != "admin":
        raise WorkflowError("Only an administrator can change the verification policy.", 403)
    if risk not in RISK_LEVELS or category not in DEFAULT_RISK:
        raise WorkflowError("Unknown category or risk level.")
    with transaction(db):
        old = risk_for(db, category)
        db.execute("INSERT INTO category_policy (category, risk_level, updated_at) VALUES (?, ?, ?) "
                   "ON CONFLICT(category) DO UPDATE SET risk_level = excluded.risk_level, updated_at = excluded.updated_at",
                   (category, risk, _now()))
        if old != risk:
            audit(db, actor["id"], "policy_changed", f"{category}: {old} -> {risk}", "policy", None)


# ---------------------------------------------------------------- "I found this" (direct return, no found report)
def offer_found(db, user, item_id, message, contact):
    """Someone who found a lost item tells its owner, with contact details, so they can meet and hand it back."""
    message = _clean(message, 10, 500, "A short note about where you found it")
    problem = contact_problem(contact)
    if problem:
        raise WorkflowError(problem)
    contact = contact.strip()
    with transaction(db):
        item = _item(db, item_id)
        if item["type"] != "lost":
            raise WorkflowError("This is a found report. Use “Is this yours?” if the item is yours.", 409)
        if item["status"] != "active":
            raise WorkflowError("This report is not open right now.", 409)
        if item["user_id"] == user["id"]:
            raise WorkflowError("This is your own report.", 403)
        if db.execute("SELECT 1 FROM found_offers WHERE item_id = ? AND finder_id = ?", (item_id, user["id"])).fetchone():
            raise WorkflowError("You already told the owner about this item.", 409)
        ts = _now()
        cur = db.execute("INSERT INTO found_offers (item_id, finder_id, message, contact, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (item_id, user["id"], message, contact, ts, ts))
        notify(db, item["user_id"], f"Someone says they found your lost item {_label(item)}. "
               "Open it to see their message and how to reach them.", _link(item), "match")
        audit(db, user["id"], "found_offer_sent", f"{item['ref_code']}", "item", item_id)
    return cur.lastrowid


def decide_offer(db, user, offer_id, decision):
    """The owner says the item came back to them (closes the report) or that it is not theirs."""
    if decision not in ("returned", "not_mine"):
        raise WorkflowError("Unknown choice.")
    with transaction(db):
        offer = db.execute("SELECT * FROM found_offers WHERE id = ?", (offer_id,)).fetchone()
        if offer is None:
            raise WorkflowError("Not found.", 404)
        item = _item(db, offer["item_id"])
        if item["user_id"] != user["id"]:
            raise WorkflowError("Only the owner of the report can do this.", 403)
        if offer["status"] != "open" or item["status"] != "active":
            raise WorkflowError("This was already answered.", 409)
        ts = _now()
        if decision == "not_mine":
            db.execute("UPDATE found_offers SET status = 'not_mine', updated_at = ? WHERE id = ?", (ts, offer_id))
            notify(db, offer["finder_id"], f"The owner of {_label(item)} says this is not their item. Thank you for trying.",
                   _link(item), "decision")
            return
        _set_report_status(db, item, "returned", returned_at=ts)
        db.execute("UPDATE found_offers SET status = 'returned', updated_at = ? WHERE id = ?", (ts, offer_id))
        for o in db.execute("SELECT * FROM found_offers WHERE item_id = ? AND id != ? AND status = 'open'",
                            (item["id"], offer_id)).fetchall():
            db.execute("UPDATE found_offers SET status = 'closed', updated_at = ? WHERE id = ?", (ts, o["id"]))
            notify(db, o["finder_id"], f"{_label(item)} is back with its owner. Thank you.", _link(item), "decision")
        notify(db, offer["finder_id"], f"The owner confirmed they got {_label(item)} back. Thank you for helping!",
               _link(item), "decision")
        audit(db, user["id"], "lost_item_returned", f"{item['ref_code']} returned directly by a finder", "item", item["id"])
