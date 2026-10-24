"""Optional demo data. Run once from the project root:  python scripts/seed_demo.py

Creates verified demo members and sample listings so the app looks alive for
screenshots and demos. All demo members use the password  Demo@1234
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, so "import app" works
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import app as portal  # noqa: E402 (needs the sys.path line above)
import services  # noqa: E402

PASSWORD = "Demo@1234"
MEMBERS = ["S2301", "S2302", "S2303", "S2304", "E1003"]

ITEMS = [
    # owner, type, title, category, description, location, days_ago, handover (finder=direct, security=admin office), status
    ("S2301", "lost", "Black HP laptop bag", "Bags", "Black backpack with a red zip and a HP sticker. Contains a charger.", "Library, 2nd floor", 1, None, "active"),
    ("S2302", "found", "Blue steel water bottle", "Other", "Blue bottle with a dent near the bottom, found on a bench.", "Main canteen", 2, "finder", "active"),
    ("S2303", "lost", "Casio scientific calculator", "Electronics", "Casio fx-991 with my initials scratched on the back.", "Exam hall B", 3, None, "active"),
    ("E1003", "found", "Brown leather wallet", "Wallets", "Wallet with cards inside. Handed in at the Security Office.", "Parking area", 1, "security", "active"),
    ("S2304", "found", "Set of keys with a green tag", "Keys", "Three keys on a ring with a green tag.", "Sports ground", 4, "finder", "active"),
    ("S2301", "lost", "Silver wired earphones", "Accessories", "Silver earphones in a small grey case.", "Seminar hall", 5, None, "active"),
    ("S2302", "found", "Grey hoodie", "Clothing", "Grey zip hoodie, size M, left on a chair.", "Lab block", 6, "finder", "returned"),
    ("S2303", "found", "Operating Systems textbook", "Books & Notes", "Hardcover OS textbook with sticky notes inside.", "Library reading room", 2, "finder", "active"),
    ("S2304", "lost", "Samsung phone with blue case", "Electronics", "Blue silicone case, cracked corner on the screen.", "Canteen", 1, None, "active"),
    ("E1003", "found", "Student ID card", "ID & Cards", "ID card found near the main gate. Kept at the Security Office.", "Main gate", 1, "security", "active"),
    ("S2301", "lost", "Black umbrella", "Other", "Black folding umbrella with a wooden handle.", "Bus stop", 3, None, "active"),
    ("S2302", "lost", "Silver wrist watch", "Accessories", "Silver analog watch with a brown strap.", "Gymnasium", 4, None, "active"),
]


def main():
    db = sqlite3.connect(portal.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    ids = {}
    for roll in MEMBERS:
        row = db.execute("SELECT * FROM roster WHERE id_number = ?", (roll,)).fetchone()
        if row is None:
            print(f"Skipping {roll}: not in the roster.")
            continue
        user = db.execute("SELECT id FROM users WHERE id_number = ?", (roll,)).fetchone()
        if user is None:
            cur = db.execute(
                "INSERT INTO users (name, email, phone, password_hash, role, status, id_number, department, "
                "verification, created_at) VALUES (?, ?, '9876543210', ?, ?, 'active', ?, ?, 'roster', ?)",
                (row["full_name"], row["email"], generate_password_hash(PASSWORD), row["role"], roll,
                 row["department"], portal.now()))
            ids[roll] = cur.lastrowid
        else:
            ids[roll] = user["id"]
    if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        for owner, kind, title, cat, desc, loc, days, custody, status in ITEMS:
            if owner not in ids:
                continue
            when = datetime.now() - timedelta(days=days)
            pref = None if kind == "lost" else ("admin" if custody == "security" else "direct")
            state = None if kind == "lost" else ("returned" if status == "returned" else
                                                 ("admin_handover_requested" if pref == "admin" else "with_finder"))
            risk = services.risk_for(db, cat)
            cur = db.execute(
                "INSERT INTO items (user_id, type, title, category, description, location, event_date, "
                "status, custody_status, handover_pref, risk_level, returned_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ids[owner], kind, title, cat, desc, loc, when.strftime("%Y-%m-%d"), status, state, pref, risk,
                 portal.now() if status == "returned" else None, when.strftime(portal.TS), when.strftime(portal.TS)))
            db.execute("UPDATE items SET ref_code = ? WHERE id = ?", (f"LF-{when.year}-{cur.lastrowid:05d}", cur.lastrowid))
    db.commit()
    db.close()
    print("Demo data ready. Log in with any of these (password " + PASSWORD + "):")
    for roll in MEMBERS:
        row = portal.sqlite3.connect(portal.DB_PATH).execute(
            "SELECT email FROM users WHERE id_number = ?", (roll,)).fetchone()
        if row:
            print("  ", row[0])


if __name__ == "__main__":
    main()
