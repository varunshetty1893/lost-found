"""Reset the demo data to a small, clean set.

    python scripts/seed_minimal.py          asks for confirmation first
    python scripts/seed_minimal.py --yes    no prompt
    add --keep-roster to leave the Campus list as it is

DELETES: every member account except admins, every Campus list entry except the two seeded people, and every item with its claims, handover records,
alerts, uploaded photos and ID-card/evidence files.
         The Campus list is trimmed to just the two people below (unless --keep-roster is given).
KEEPS:   the admin account, the category policy and the activity log.

CREATES: 1 student (S2301) and 1 staff member (E1003, a custody officer), both with the
password  Demo@1234,  plus 3 items and 4 transactions:

  Brown leather wallet  found by staff   in office custody, student's claim APPROVED (awaiting handover)
  Black HP laptop bag   lost by student  open listing, no claims yet
  Grey zip hoodie       found by student claimed by staff, handed back directly (RETURNED)

  Transactions: office receipt of the wallet, approved claim on the wallet,
                approved claim on the hoodie, direct return of the hoodie.
"""
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, so "import app" works

import app as portal  # noqa: E402
import services  # noqa: E402

PASSWORD = "Demo@1234"
STUDENT, STAFF = "S2301", "E1003"


def stamp(days=0, hours=0):
    return (datetime.now() - timedelta(days=days, hours=hours)).strftime(portal.TS)


def clear(db):
    for table in ("handover_confirmations", "custody_events", "claim_evidence", "claims", "items",
                  "notifications", "otp_codes", "login_attempts"):
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM users WHERE role != 'admin'")
    db.execute("DELETE FROM sqlite_sequence WHERE name IN ('items', 'claims', 'custody_events', "
               "'handover_confirmations', 'notifications', 'claim_evidence')")
    for folder in (portal.ITEM_DIR, portal.CARD_DIR, portal.EVIDENCE_DIR):
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if name != ".gitkeep" and os.path.isfile(path):
                    os.remove(path)


def trim_roster(db):
    db.execute("DELETE FROM roster WHERE id_number NOT IN (?, ?)", (STUDENT, STAFF))


def make_user(db, id_number, is_officer=0):
    row = db.execute("SELECT * FROM roster WHERE id_number = ?", (id_number,)).fetchone()
    if row is None:
        raise SystemExit(f"{id_number} is not on the Campus list. Import data/sample_roster.csv first.")
    cur = db.execute(
        "INSERT INTO users (name, email, phone, password_hash, role, status, id_number, department, "
        "verification, is_custody_officer, date_of_birth, created_at) VALUES (?, ?, '9876543210', ?, ?, 'active', ?, ?, 'roster', ?, ?, ?)",
        (row["full_name"], row["email"], generate_password_hash(PASSWORD), row["role"], id_number,
         row["department"], is_officer, "1985-03-12" if is_officer else "2004-05-17", stamp(10)))
    return cur.lastrowid


def make_item(db, owner, kind, title, cat, desc, private, loc, days, custody=None, pref=None, status="active"):
    when = stamp(days)
    cur = db.execute(
        "INSERT INTO items (user_id, type, title, category, description, private_details, location, event_date, "
        "status, custody_status, handover_pref, risk_level, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (owner, kind, title, cat, desc, private, loc, when[:10], status, custody, pref,
         services.risk_for(db, cat), when, when))
    item_id = cur.lastrowid
    db.execute("UPDATE items SET ref_code = ? WHERE id = ?", (f"LF-{when[:4]}-{item_id:05d}", item_id))
    return item_id


def make_claim(db, item_id, claimer, message, status, handler, reviewer, days):
    when = stamp(days)
    cur = db.execute(
        "INSERT INTO claims (item_id, claimer_id, message, status, handler, reviewer_id, review_reason, "
        "decided_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, claimer, message, status, handler, reviewer,
         "Details matched." if status == "approved" else "", when if status == "approved" else None, when, when))
    return cur.lastrowid


def event(db, item_id, frm, to, kind, released, received, staff, claim_id, notes, days):
    db.execute("INSERT INTO custody_events (item_id, from_state, to_state, handover_type, released_by, received_by, "
               "staff_id, claim_id, confirmed, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
               (item_id, frm, to, kind, released, received, staff, claim_id, notes, stamp(days)))


def seed(db):
    admin = db.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()["id"]
    student = make_user(db, STUDENT)
    staff = make_user(db, STAFF, is_officer=1)

    # 1. Wallet: found by staff, handed to the office, student's claim approved.
    wallet = make_item(db, staff, "found", "Brown leather wallet", "Wallets",
                       "Brown leather wallet with a few cards inside. Found near the parking area.",
                       "Library card in the left slot, a 100 rupee note folded in the coin pocket.",
                       "Parking area", 2, custody="with_admin", pref="admin")
    event(db, wallet, "admin_handover_requested", "with_admin", "ADMIN_RECEIPT", staff, admin, admin, None,
          "Received at the Security Office.", 2)                                            # transaction 1
    wallet_claim = make_claim(db, wallet, student,
                              "It is mine. Library card is in the left slot and a folded 100 rupee note is in the coin pocket.",
                              "approved", "admin", admin, 1)                                # transaction 2
    db.execute("UPDATE items SET approved_claim_id = ?, updated_at = ? WHERE id = ?", (wallet_claim, stamp(1), wallet))

    # 2. Laptop bag: lost by the student, still open.
    make_item(db, student, "lost", "Black HP laptop bag", "Bags",
              "Black backpack with a red zip and an HP sticker. Contains a charger.",
              "", "Library, 2nd floor", 1)

    # 3. Hoodie: found by the student, claimed by staff, returned directly.
    hoodie = make_item(db, student, "found", "Grey zip hoodie", "Clothing",
                       "Grey zip hoodie, size M, left on a chair in the lab block.",
                       "Small ink mark inside the left cuff.", "Lab block", 5,
                       custody="with_finder", pref="direct")
    hoodie_claim = make_claim(db, hoodie, staff, "It is my hoodie. There is a small ink mark inside the left cuff.",
                              "approved", "finder", student, 4)                            # transaction 3
    db.execute("UPDATE items SET approved_claim_id = ? WHERE id = ?", (hoodie_claim, hoodie))
    for uid, party in ((student, "finder"), (staff, "claimant")):
        db.execute("INSERT INTO handover_confirmations (item_id, claim_id, user_id, party, outcome, note, "
                   "recorded_by, created_at) VALUES (?, ?, ?, ?, 'confirmed', '', ?, ?)",
                   (hoodie, hoodie_claim, uid, party, uid, stamp(3)))
    event(db, hoodie, "with_finder", "returned", "DIRECT_RETURN", student, staff, None, hoodie_claim,
          "Both parties confirmed the handover.", 3)                                        # transaction 4
    db.execute("UPDATE items SET status = 'returned', custody_status = 'returned', returned_at = ?, updated_at = ? "
               "WHERE id = ?", (stamp(3), stamp(3), hoodie))


def main():
    if "--yes" not in sys.argv:
        keep = "--keep-roster" in sys.argv
        print(f"This will DELETE all members (except admins), all items"
              + ("" if keep else " and all Campus list entries except S2301 and E1003") + f" in {portal.DB_PATH}.")
        if input("Type YES to continue: ").strip() != "YES":
            raise SystemExit("Cancelled. Nothing was changed.")
    portal.init_db()
    db = sqlite3.connect(portal.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            clear(db)
            if "--keep-roster" not in sys.argv:
                trim_roster(db)
            seed(db)
    finally:
        db.close()
    print("Done. 1 student, 1 staff member, 3 items, 4 transactions.")
    print(f"  Student: aarav.shetty@campus.edu   Staff: imran.khan@campus.edu   password {PASSWORD}")


if __name__ == "__main__":
    main()
