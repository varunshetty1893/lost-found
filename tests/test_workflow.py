"""Tests for the lost-and-found business workflow (report / custody / claim lifecycles,
both handover methods, risk-based verification, authorization, concurrency, migration).

Run everything:   python -m unittest discover -p "test_*.py"
Run only these:   python -m unittest tests.test_workflow
"""
import io
import os
import sqlite3
import tempfile
import threading
import unittest

import app as appmod
import matching
import services
from tests.test_app import Base, PNG

LONG = "It is a black leather one with a torn left corner and my library card S2302 inside."


class WF(Base):
    """Base for workflow tests: four members (alice S2301, bob S2302, carol S2303, staff E1003)."""

    def setUp(self):
        super().setUp()
        self.setup_users()
        self.carol = appmod.app.test_client()
        self.register(self.carol, "student", "S2303", "rohan.kulkarni@campus.edu")
        self.login_admin()

    # ---- db helpers
    def rows(self, sql, *args):
        c = sqlite3.connect(appmod.DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def one(self, sql, *args):
        r = self.rows(sql, *args)
        return r[0] if r else None

    def item(self, item_id):
        return self.one("SELECT * FROM items WHERE id = ?", item_id)

    def claim_row(self, claim_id):
        return self.one("SELECT * FROM claims WHERE id = ?", claim_id)

    def user_id(self, id_number):
        return self.one("SELECT id FROM users WHERE id_number = ?", id_number)["id"]

    def user_row(self, id_number):
        return self.one("SELECT * FROM users WHERE id_number = ?", id_number)

    def audit_actions(self):
        return [r["action"] for r in self.rows("SELECT action FROM audit_log")]

    def notes(self, client):
        return client.get("/notifications").get_data(as_text=True)

    # ---- action helpers
    def make_officer(self):
        self.post(self.admin, f"/admin/users/{self.user_id('E1003')}/officer", "/dashboard", grant="1")

    def found(self, client=None, **over):
        """Post a found report; defaults to a low-risk item handed over directly."""
        over.setdefault("category", "Other")
        over.setdefault("title", "Blue water bottle")
        over.setdefault("description", "A blue steel bottle with a dent")
        self.post_item(client or self.alice, **over)
        return self.one("SELECT MAX(id) AS m FROM items")["m"]

    def claim(self, client, item_id, message=LONG):
        return self.post(client, f"/item/{item_id}/claim", f"/item/{item_id}", message=message)

    def claim_id(self, item_id, id_number):
        return self.one("SELECT id FROM claims WHERE item_id = ? AND claimer_id = ?",
                        item_id, self.user_id(id_number))["id"]

    def finder_act(self, client, claim_id, action, item_id, note="Looks right to me"):
        return self.post(client, f"/claim/{claim_id}/finder/{action}", f"/item/{item_id}", note=note)

    def staff_act(self, client, claim_id, action, item_id, reason="Checked the details"):
        return self.post(client, f"/claim/{claim_id}/staff/{action}", f"/item/{item_id}", reason=reason)

    def text(self, response):
        return response.get_data(as_text=True)


class ReportAndModerationTests(WF):
    def test_report_gets_reference_and_separate_statuses(self):
        direct = self.found(handover="direct")
        admin = self.found(handover="admin", title="Silver keys ring")
        lost = self.found(type="lost", title="Lost umbrella", handover="direct")
        d, a, l = self.item(direct), self.item(admin), self.item(lost)
        self.assertRegex(d["ref_code"], r"^LF-\d{4}-\d{5}$")
        self.assertEqual((d["status"], d["custody_status"], d["handover_pref"]), ("active", "with_finder", "direct"))
        # choosing the admin option is a preference, never proof of deposit
        self.assertEqual((a["status"], a["custody_status"]), ("active", "admin_handover_requested"))
        self.assertEqual((l["custody_status"], l["handover_pref"]), (None, None))
        self.assertEqual(len({d["ref_code"], a["ref_code"], l["ref_code"]}), 3)

    def test_client_cannot_inject_status_or_custody(self):
        i = self.found(handover="admin", status="returned", custody_status="with_admin", risk_level="low",
                       approved_claim_id="1")
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"], row["approved_claim_id"]),
                         ("active", "admin_handover_requested", None))

    def test_pending_reports_are_hidden_until_staff_publish(self):
        appmod.MODERATION_REQUIRED = True
        i = self.found()
        self.assertEqual(self.item(i)["status"], "pending_review")
        self.assertNotIn("Blue water bottle", self.text(self.bob.get("/")))
        self.assertEqual(self.bob.get(f"/item/{i}").status_code, 404)
        photo = self.item(i)["photo"]
        self.assertEqual(self.bob.get(f"/media/{photo}").status_code, 404)
        self.assertEqual(self.alice.get(f"/item/{i}").status_code, 200)
        # students and plain staff cannot moderate
        t = self.token(self.bob, "/dashboard")
        self.assertEqual(self.bob.post(f"/item/{i}/moderate/approve", data={"csrf_token": t}).status_code, 403)
        t = self.token(self.staff, "/dashboard")
        self.assertEqual(self.staff.post(f"/item/{i}/moderate/approve", data={"csrf_token": t}).status_code, 403)
        # rejection needs a reason
        r = self.post(self.admin, f"/item/{i}/moderate/reject", f"/item/{i}", reason="")
        self.assertIn("reason", self.text(r).lower())
        self.assertEqual(self.item(i)["status"], "pending_review")
        self.post(self.admin, f"/item/{i}/moderate/approve", f"/item/{i}")
        self.assertEqual(self.item(i)["status"], "active")
        self.assertIn("Blue water bottle", self.text(self.bob.get("/")))
        self.assertIn("published", self.notes(self.alice))
        self.assertIn("report_approved", self.audit_actions())

    def test_rejected_report_stays_private_and_terminal(self):
        appmod.MODERATION_REQUIRED = True
        i = self.found()
        self.post(self.admin, f"/item/{i}/moderate/reject", f"/item/{i}", reason="Looks like spam")
        self.assertEqual(self.item(i)["status"], "rejected")
        self.assertIn("Looks like spam", self.text(self.alice.get(f"/item/{i}")))
        self.assertEqual(self.bob.get(f"/item/{i}").status_code, 404)
        r = self.post(self.admin, f"/item/{i}/moderate/approve", f"/item/{i}")
        self.assertIn("not waiting for review", self.text(r))
        self.assertEqual(self.item(i)["status"], "rejected")

    def test_edit_permissions_and_claim_lock(self):
        i = self.found()
        self.assertEqual(self.bob.get(f"/item/{i}/edit").status_code, 403)
        r = self.post(self.alice, f"/item/{i}/edit", f"/item/{i}/edit", type="found", title="Blue bottle edited",
                      category="Other", description="A blue steel bottle, small dent", location="Canteen",
                      event_date="2026-10-02", handover="direct", private_details="Sticker inside lid")
        self.assertIn("Report updated", self.text(r))
        self.assertEqual(self.item(i)["title"], "Blue bottle edited")
        self.claim(self.bob, i)
        r = self.alice.get(f"/item/{i}/edit", follow_redirects=True)
        self.assertIn("can no longer be edited", self.text(r))

    def test_owner_close_report_ends_claims_and_notifies(self):
        i = self.found()
        self.claim(self.bob, i)
        self.assertEqual(self.bob.post(f"/item/{i}/close", data={"csrf_token": self.token(self.bob, "/dashboard"),
                                                                  "reason": "mine"}).status_code, 403)
        r = self.post(self.alice, f"/item/{i}/close", f"/item/{i}", reason="")
        self.assertIn("at least 3 characters", self.text(r))
        self.post(self.alice, f"/item/{i}/close", f"/item/{i}", reason="Owner collected it already")
        row = self.item(i)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(self.claim_row(self.claim_id(i, "S2302"))["status"], "rejected")
        self.assertIn("was closed", self.notes(self.bob))
        self.assertNotIn(i, [r["id"] for r in self.rows("SELECT id FROM items WHERE status = 'active'")])
        r = self.claim(self.carol, i)
        self.assertIn("not open for claims", self.text(r))

    def test_unsafe_item_is_forced_to_the_college_office(self):
        i = self.found(handover="direct", unsafe="1")
        row = self.item(i)
        self.assertEqual((row["handover_pref"], row["risk_level"], row["unsafe"]), ("admin", "high", 1))
        self.assertIn("Refer to security", self.text(self.admin.get("/staff/queue/awaiting_receipt")))

    def test_post_rate_limit(self):
        services.REPORTS_PER_HOUR = 2
        for n in range(2):
            self.found(title=f"Item {n} bottle")
        r = self.post_item(self.alice, title="Third bottle", category="Other")
        self.assertIn("a lot of reports", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM items")["c"], 2)
        self.assertEqual(len(os.listdir(self.item_dir)), 2)   # rejected upload was not kept


class AdminHandoverTests(WF):
    """Method A: finder hands the item to the college office."""

    def test_full_admin_handover(self):
        self.make_officer()
        i = self.found(handover="admin")
        ref = self.item(i)["ref_code"]
        # selecting the option is not custody; the item page says so
        self.assertIn("not in custody until staff confirm", self.text(self.bob.get(f"/item/{i}")))
        self.assertIn(ref, self.text(self.admin.get("/staff/queue/awaiting_receipt")))
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")

        # a claim on an office-handled item goes to staff, never to the finder
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("under_admin_review", "admin"))
        self.assertIn("staff", self.notes(self.alice))
        self.assertNotIn("Diya", self.notes(self.alice))

        # pending claim is not auto-approved; the finder cannot decide
        self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.claim_row(cid)["status"], "under_admin_review")

        # approval before the item reached the office is allowed, but release is not
        self.staff_act(self.staff, cid, "approve", i, reason="ID and description match")
        self.assertEqual(self.claim_row(cid)["status"], "approved")
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")
        r = self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1")
        self.assertIn("must first confirm they physically received", self.text(r))
        self.assertEqual(self.item(i)["status"], "active")

        # staff confirm physical receipt -> custody WITH_ADMIN with a record
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1", notes="Shelf B4")
        self.assertIn("Receipt recorded", self.text(r))
        row = self.item(i)
        self.assertEqual(row["custody_status"], "with_admin")
        ev = self.one("SELECT * FROM custody_events WHERE item_id = ?", i)
        self.assertEqual((ev["handover_type"], ev["staff_id"], ev["from_state"], ev["to_state"]),
                         ("ADMIN_RECEIPT", self.user_id("E1003"), "admin_handover_requested", "with_admin"))
        self.assertIn("received", self.notes(self.alice))

        # release requires the acknowledgment box and an authorized member
        r = self.post(self.staff, f"/item/{i}/release", f"/item/{i}")
        self.assertIn("claimant acknowledged", self.text(r))
        self.assertEqual(self.bob.post(f"/item/{i}/release", data={"csrf_token": self.token(self.bob, "/dashboard"),
                                                                   "acknowledged": "1"}).status_code, 403)
        self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1", notes="Showed college ID")
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"]), ("returned", "returned"))
        self.assertIsNotNone(row["returned_at"])
        self.assertEqual([r["handover_type"] for r in self.rows("SELECT * FROM custody_events ORDER BY id")],
                         ["ADMIN_RECEIPT", "ADMIN_RELEASE"])
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM handover_confirmations")["c"], 2)
        self.assertIn("recorded", self.notes(self.alice))
        self.assertIn("recorded", self.notes(self.bob))
        for action in ("custody_accepted", "claim_approved", "custody_released"):
            self.assertIn(action, self.audit_actions())
        a = self.one("SELECT * FROM audit_log WHERE action = 'custody_released'")
        self.assertEqual((a["actor_id"], a["record_type"], a["record_id"]), (self.user_id("E1003"), "item", i))

        # a second release attempt (retry) cannot create a second return
        r = self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1")
        self.assertIn("approved claim waiting", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM custody_events WHERE to_state = 'returned'")["c"], 1)

    def test_only_authorized_staff_can_confirm_receipt(self):
        i = self.found(handover="admin")
        for client in (self.alice, self.bob):
            t = self.token(client, "/dashboard")
            self.assertEqual(client.post(f"/item/{i}/receive", data={"csrf_token": t, "confirm": "1"}).status_code, 403)
        # a plain staff member is not a custody officer until an admin authorizes them
        t = self.token(self.staff, "/dashboard")
        self.assertEqual(self.staff.post(f"/item/{i}/receive", data={"csrf_token": t, "confirm": "1"}).status_code, 403)
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")
        self.make_officer()
        self.assertIn("officer_granted", self.audit_actions())
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}")        # checkbox missing
        self.assertIn("physically hold", self.text(r))
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertEqual(self.item(i)["custody_status"], "with_admin")
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertIn("already recorded", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM custody_events")["c"], 1)

    def test_receipt_needs_a_published_open_report(self):
        self.make_officer()
        appmod.MODERATION_REQUIRED = True
        i = self.found(handover="admin")
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertIn("publish this report first", self.text(r))
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")

    def test_late_receipt_date_is_recorded_and_validated(self):
        self.make_officer()
        i = self.found(handover="admin")
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1", received_on="2099-01-01")
        self.assertIn("between the report date and today", self.text(r))
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1", received_on=appmod.now()[:10])
        self.assertIn(appmod.now()[:10], self.one("SELECT notes FROM custody_events")["notes"])

    def test_receiving_item_moves_finder_claims_to_staff(self):
        self.make_officer()
        i = self.found(handover="direct")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.assertEqual(self.claim_row(cid)["handler"], "finder")
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("under_admin_review", "admin"))

    def test_admin_can_revert_an_approval_whose_handover_never_happened(self):
        self.make_officer()
        i = self.found(handover="admin")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.staff_act(self.staff, cid, "approve", i)
        r = self.post(self.staff, f"/claim/{cid}/revert", f"/item/{i}", reason="")
        self.assertIn("at least 3", self.text(r))
        self.post(self.staff, f"/claim/{cid}/revert", f"/item/{i}", reason="Claimant never came")
        self.assertEqual(self.claim_row(cid)["status"], "rejected")
        self.assertIsNone(self.item(i)["approved_claim_id"])
        self.assertIn("approval_reverted", self.audit_actions())
        self.claim(self.carol, i)              # open for a new claim again
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE item_id = ?", i)["c"], 2)


class DirectHandoverTests(WF):
    """Method B: finder returns the item directly to a verified owner."""

    def test_full_direct_handover_low_risk(self):
        i = self.found(private_details="Sticker of a cat inside the lid")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("pending", "finder"))

        # the finder is told only that a claim exists
        note = self.notes(self.alice)
        self.assertIn("needs your review", note)
        self.assertNotIn("Diya", note)
        self.assertNotIn("S2302", note)
        page = self.text(self.alice.get(f"/item/{i}"))
        self.assertIn(LONG, page)                           # evidence needed to evaluate
        self.assertNotIn("Diya Nair", page)                 # no claimant identity
        self.assertNotIn("diya.nair@campus.edu", page)
        self.assertNotIn("9876543210", page)                # no phone numbers anywhere
        # the claimant cannot see the finder's private details
        self.assertNotIn("Sticker of a cat", self.text(self.bob.get(f"/item/{i}")))
        # students other than the finder cannot decide
        t = self.token(self.carol, "/dashboard")
        self.assertEqual(self.carol.post(f"/claim/{cid}/finder/verify", data={"csrf_token": t}).status_code, 403)
        self.assertEqual(self.carol.post(f"/claim/{cid}/staff/approve", data={"csrf_token": t, "reason": "x y z"}).status_code, 403)

        self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.claim_row(cid)["status"], "approved")
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"], row["approved_claim_id"]),
                         ("active", "direct_handover_pending", cid))
        # not returned yet, and no custody event has been written for a non-transfer
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM custody_events")["c"], 0)
        # parties can now coordinate; each sees the other's name and campus email only
        self.assertIn("diya.nair@campus.edu", self.text(self.alice.get(f"/item/{i}")))
        self.assertIn("aarav.shetty@campus.edu", self.text(self.bob.get(f"/item/{i}")))
        self.assertNotIn("diya.nair@campus.edu", self.text(self.carol.get(f"/item/{i}")))
        r = self.post(self.bob, f"/item/{i}/meeting", f"/item/{i}", meeting="Library entrance, 4 pm")
        self.assertIn("Library entrance", self.text(self.alice.get(f"/item/{i}")))

        # one confirmation is not enough
        r = self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertIn("waiting for the other person", self.text(r))
        self.assertIn("Handover confirmation pending", self.text(self.bob.get(f"/item/{i}")))
        self.assertEqual(self.item(i)["status"], "active")
        # retrying (double click / network retry) does not duplicate anything
        r = self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertIn("already recorded", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM handover_confirmations")["c"], 1)
        # strangers cannot confirm
        t = self.token(self.carol, "/dashboard")
        self.assertEqual(self.carol.post(f"/item/{i}/handover", data={"csrf_token": t, "outcome": "confirmed"}).status_code, 403)
        # second party completes
        r = self.post(self.bob, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertIn("marked as returned", self.text(r))
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"]), ("returned", "returned"))
        ev = self.one("SELECT * FROM custody_events")
        self.assertEqual((ev["handover_type"], ev["released_by"], ev["received_by"]),
                         ("DIRECT_RETURN", self.user_id("S2301"), self.user_id("S2302")))
        self.assertIn("recorded", self.notes(self.alice))
        self.assertIn("recorded", self.notes(self.bob))
        self.assertIn("handover_completed", self.audit_actions())
        # a late retry after completion is harmless
        r = self.post(self.bob, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertIn("already recorded", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM custody_events")["c"], 1)

    def test_finder_cannot_approve_their_own_claim(self):
        i = self.found()
        now = appmod.now()
        con = sqlite3.connect(appmod.DB_PATH)
        con.execute("INSERT INTO claims (item_id, claimer_id, message, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (i, self.user_id("S2301"), LONG, now, now))
        con.commit(); con.close()
        cid = self.claim_id(i, "S2301")
        db = appmod.sqlite3.connect(appmod.DB_PATH); db.row_factory = sqlite3.Row
        alice = db.execute("SELECT * FROM users WHERE id_number = 'S2301'").fetchone()
        with self.assertRaises(services.WorkflowError):
            services.finder_review(db, alice, cid, "verify")
        self.assertEqual(self.claim_row(cid)["status"], "pending")
        # and a normal claim attempt on one's own report is refused with a message
        r = self.claim(self.alice, i)
        self.assertIn("cannot claim an item you reported", self.text(r))

    def test_high_risk_item_always_needs_staff(self):
        self.make_officer()
        i = self.found(category="Wallets", title="Black wallet", handover="direct")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        c = self.claim_row(cid)
        self.assertEqual((c["status"], c["handler"]), ("under_admin_review", "admin"))
        r = self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.claim_row(cid)["status"], "under_admin_review")
        self.assertIn("College staff are handling", self.text(r))
        # the finder no longer sees the private evidence once staff own the claim
        self.assertNotIn(LONG, self.text(self.alice.get(f"/item/{i}")))
        self.assertIn(LONG, self.text(self.staff.get(f"/item/{i}")))
        self.assertIn("Diya Nair", self.text(self.staff.get(f"/item/{i}")))     # staff see identity to check the ID
        # staff approve; direct handover is NOT used for high-risk items
        self.staff_act(self.staff, cid, "approve", i)
        self.assertEqual(self.item(i)["custody_status"], "with_finder")
        self.assertEqual(self.item(i)["approved_claim_id"], cid)
        r = self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertIn("arranged through college staff", self.text(r))
        self.assertIn("college office", self.notes(self.alice))
        # secure custody path completes it
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1")
        self.assertEqual(self.item(i)["status"], "returned")

    def test_medium_risk_needs_photo_or_detailed_claim(self):
        i = self.found(category="Bags", title="Grey backpack")
        self.claim(self.bob, i, "It is my grey bag with a red zip")
        cid = self.claim_id(i, "S2302")
        r = self.finder_act(self.alice, cid, "verify", i)
        self.assertIn("photo or a more detailed description", self.text(r))
        self.assertEqual(self.claim_row(cid)["status"], "pending")
        # a detailed description is enough
        j = self.found(category="Bags", title="Green backpack")
        self.claim(self.bob, j, LONG + " It also has a keychain shaped like a fox on the front pocket.")
        self.finder_act(self.alice, self.claim_id(j, "S2302"), "verify", j)
        self.assertEqual(self.claim_row(self.claim_id(j, "S2302"))["status"], "approved")

    def test_finder_can_ask_questions_then_escalate(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        r = self.post(self.bob, f"/claim/{cid}/respond", f"/item/{i}", message="No question was asked")
        self.assertIn("No additional information was requested", self.text(r))
        self.finder_act(self.alice, cid, "request_info", i, note="What colour is the lid?")
        self.assertEqual(self.claim_row(cid)["status"], "needs_more_info")
        self.assertIn("What colour is the lid?", self.text(self.bob.get(f"/item/{i}")))
        self.assertIn("more information", self.notes(self.bob))
        # a claimant in this state cannot be approved until they answer
        r = self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.claim_row(cid)["status"], "needs_more_info")
        self.post(self.bob, f"/claim/{cid}/respond", f"/item/{i}", message="The lid is white with a green ring")
        c = self.claim_row(cid)
        self.assertEqual(c["status"], "pending")
        self.assertIn("white with a green ring", c["message"])
        self.assertIn("answered", self.notes(self.alice))
        # not sure -> send to staff; the finder cannot then see or decide it
        self.finder_act(self.alice, cid, "escalate", i, note="I cannot tell")
        self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("under_admin_review", "admin"))
        self.assertNotIn("white with a green ring", self.text(self.alice.get(f"/item/{i}")))
        self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.claim_row(cid)["status"], "under_admin_review")

    def test_direct_handover_dispute_goes_to_staff(self):
        self.make_officer()
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        r = self.post(self.bob, f"/item/{i}/handover", f"/item/{i}", outcome="disputed", note="")
        self.assertIn("short explanation", self.text(r))
        self.post(self.bob, f"/item/{i}/handover", f"/item/{i}", outcome="disputed", note="Never received it")
        self.assertEqual(self.item(i)["custody_status"], "disputed")
        self.assertEqual(self.item(i)["status"], "active")
        self.assertIn("not completed", self.notes(self.alice))
        self.assertIn(str(i), self.text(self.staff.get("/staff/queue/disputes")))
        # while disputed: no release, no new claims, no more confirmations, not a match
        self.assertIn("disputed", self.text(self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1")).lower())
        self.assertIn("dispute", self.text(self.claim(self.carol, i)).lower())
        # participants cannot resolve; only an uninvolved reviewer can
        t = self.token(self.alice, "/dashboard")
        self.assertEqual(self.alice.post(f"/item/{i}/dispute", data={"csrf_token": t, "resolution": "complete", "reason": "fine ok"}).status_code, 403)
        self.post(self.staff, f"/item/{i}/dispute", f"/item/{i}", resolution="reopen", reason="Bob never got it")
        row = self.item(i)
        self.assertEqual((row["custody_status"], row["approved_claim_id"]), ("with_finder", None))
        self.assertEqual(self.claim_row(cid)["status"], "rejected")
        self.assertIn("dispute_resolved", self.audit_actions())

    def test_dispute_can_be_resolved_as_completed(self):
        self.make_officer()
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="disputed", note="He says he has not got it")
        self.post(self.staff, f"/item/{i}/dispute", f"/item/{i}", resolution="complete", reason="Bob confirmed in person")
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"]), ("returned", "returned"))
        self.assertEqual(self.one("SELECT handover_type FROM custody_events")["handover_type"], "DISPUTE_RESOLUTION")

    def test_direct_approval_can_be_reverted_when_nobody_meets(self):
        self.make_officer()
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.item(i)["custody_status"], "direct_handover_pending")
        self.post(self.staff, f"/claim/{cid}/revert", f"/item/{i}", reason="Nobody answered for two weeks")
        row = self.item(i)
        self.assertEqual((row["custody_status"], row["approved_claim_id"]), ("with_finder", None))
        self.assertEqual(self.claim_row(cid)["status"], "rejected")

    def test_claimant_can_withdraw_even_after_approval(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.post(self.bob, f"/claim/{cid}/withdraw", f"/item/{i}")
        self.assertEqual(self.claim_row(cid)["status"], "withdrawn")
        self.assertEqual(self.item(i)["custody_status"], "with_finder")
        # a withdrawn claim can never be approved again
        with self.assertRaises(services.WorkflowError):
            self.admin_service(services.staff_review, cid, "approve", "Trying again")
        t = self.token(self.carol, "/dashboard")
        self.assertEqual(self.carol.post(f"/claim/{cid}/withdraw", data={"csrf_token": t}).status_code, 403)

    def admin_service(self, fn, *args):
        db = sqlite3.connect(appmod.DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            admin = db.execute("SELECT * FROM users WHERE role = 'admin'").fetchone()
            return fn(db, admin, *args)
        finally:
            db.close()


class ClaimRulesTests(WF):
    def test_competing_claims_go_to_staff_and_only_one_wins(self):
        self.make_officer()
        i = self.found()
        self.claim(self.bob, i)
        first = self.claim_id(i, "S2302")
        self.assertEqual(self.claim_row(first)["handler"], "finder")
        self.claim(self.carol, i, "It has a cracked base and a sticker of a fox on the side, mine for 2 years")
        second = self.claim_id(i, "S2303")
        for cid in (first, second):
            self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("under_admin_review", "admin"))
        self.assertIn("Another claim", self.notes(self.bob))
        # the earliest claim is NOT approved automatically
        self.assertNotEqual(self.claim_row(first)["status"], "approved")
        self.finder_act(self.alice, first, "verify", i)
        self.assertEqual(self.claim_row(first)["status"], "under_admin_review")
        # staff pick the better evidence: the second one
        self.staff_act(self.staff, second, "approve", i, reason="Sticker and base crack match")
        self.assertEqual(self.claim_row(second)["status"], "approved")
        self.assertEqual(self.claim_row(first)["status"], "rejected")
        self.assertIn("Another claim was approved", self.claim_row(first)["review_reason"])
        # the rejected one cannot be approved afterwards by a crafted request
        r = self.staff_act(self.staff, first, "approve", i)
        self.assertIn("can no longer be approved", self.text(r))
        self.assertEqual(self.claim_row(first)["status"], "rejected")
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE status = 'approved'")["c"], 1)
        # new claims are refused while a handover is in progress
        dave = appmod.app.test_client()
        self.register(dave, "student", "S2304", "meera.iyer@campus.edu")
        self.assertIn("already been approved", self.text(self.claim(dave, i)))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE item_id = ?", i)["c"], 2)

    def test_database_allows_only_one_approved_claim_per_item(self):
        i = self.found()
        self.claim(self.bob, i); self.claim(self.carol, i)
        con = sqlite3.connect(appmod.DB_PATH)
        con.execute("UPDATE claims SET status = 'approved' WHERE claimer_id = ?", (self.user_id("S2302"),))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("UPDATE claims SET status = 'approved' WHERE claimer_id = ?", (self.user_id("S2303"),))
        con.close()

    def test_concurrent_approvals_only_one_succeeds(self):
        i = self.found()
        self.claim(self.bob, i); self.claim(self.carol, i)
        ids = [self.claim_id(i, "S2302"), self.claim_id(i, "S2303")]
        barrier, results = threading.Barrier(2), []

        def worker(cid):
            db = sqlite3.connect(appmod.DB_PATH, timeout=20)
            db.row_factory = sqlite3.Row
            admin = db.execute("SELECT * FROM users WHERE role = 'admin'").fetchone()
            barrier.wait()
            try:
                services.staff_review(db, admin, cid, "approve", "Concurrent approval")
                results.append(("ok", cid))
            except services.WorkflowError as exc:
                results.append(("refused", cid))
            finally:
                db.close()
        threads = [threading.Thread(target=worker, args=(c,)) for c in ids]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(r[0] for r in results), ["ok", "refused"])
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE status = 'approved'")["c"], 1)
        winner = [r[1] for r in results if r[0] == "ok"][0]
        self.assertEqual(self.item(i)["approved_claim_id"], winner)

    def test_concurrent_handover_confirmations_return_once(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        barrier, results = threading.Barrier(4), []

        def worker(id_number):
            db = sqlite3.connect(appmod.DB_PATH, timeout=20)
            db.row_factory = sqlite3.Row
            user = db.execute("SELECT * FROM users WHERE id_number = ?", (id_number,)).fetchone()
            barrier.wait()
            try:
                results.append(services.confirm_direct_handover(db, user, i, "confirmed"))
            finally:
                db.close()
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("S2301", "S2301", "S2302", "S2302")]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(results.count("completed"), 1)
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM custody_events WHERE to_state = 'returned'")["c"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM handover_confirmations")["c"], 2)
        self.assertEqual(self.item(i)["status"], "returned")

    def test_reject_withdraw_and_appeal(self):
        self.make_officer()
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        r = self.finder_act(self.alice, cid, "reject", i, note="")
        self.assertIn("at least 3", self.text(r))
        self.finder_act(self.alice, cid, "reject", i, note="Description does not match")
        self.assertEqual(self.claim_row(cid)["status"], "rejected")
        page = self.text(self.bob.get(f"/item/{i}"))
        self.assertIn("Description does not match", page)                  # decision reason is shown
        # a rejected claim cannot be forced to approved
        self.assertIn("not waiting for your decision", self.text(self.finder_act(self.alice, cid, "verify", i)))
        self.assertEqual(self.claim_row(cid)["status"], "rejected")
        # explicit, one-time appeal goes to staff
        r = self.post(self.bob, f"/claim/{cid}/appeal", f"/item/{i}", message="short")
        self.assertIn("at least 10", self.text(r))
        self.post(self.bob, f"/claim/{cid}/appeal", f"/item/{i}", message="I can describe the inside as well")
        c = self.claim_row(cid)
        self.assertEqual((c["status"], c["handler"], c["appeal_used"]), ("under_admin_review", "admin", 1))
        self.staff_act(self.staff, cid, "reject", i, reason="Still not convincing")
        r = self.post(self.bob, f"/claim/{cid}/appeal", f"/item/{i}", message="Please look again once more")
        self.assertIn("only be reopened by an administrator", self.text(r))
        self.assertEqual(self.claim_row(cid)["status"], "rejected")

    def test_staff_decisions_require_reasons_and_authority(self):
        self.make_officer()
        i = self.found(category="Electronics", title="Phone")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        r = self.staff_act(self.staff, cid, "approve", i, reason="")
        self.assertIn("at least 3", self.text(r))
        self.assertEqual(self.claim_row(cid)["status"], "under_admin_review")
        self.staff_act(self.staff, cid, "request_info", i, reason="What is the lock screen picture?")
        self.assertEqual(self.claim_row(cid)["status"], "needs_more_info")
        self.post(self.bob, f"/claim/{cid}/respond", f"/item/{i}", message="A photo of a beach")
        self.assertEqual(self.claim_row(cid)["status"], "under_admin_review")
        self.staff_act(self.staff, cid, "approve", i, reason="Matches")
        c = self.claim_row(cid)
        self.assertEqual((c["status"], c["reviewer_id"], c["review_reason"]), ("approved", self.user_id("E1003"), "Matches"))
        self.assertIsNotNone(c["decided_at"])
        # reviewers can never decide their own claim
        j = self.found(client=self.bob, category="Electronics", title="Tablet")
        self.claim(self.staff, j)
        scid = self.claim_id(j, "E1003")
        r = self.staff_act(self.staff, scid, "approve", j)
        self.assertIn("cannot decide your own claim", self.text(r))
        self.assertEqual(self.claim_row(scid)["status"], "under_admin_review")

    def test_returned_items_are_terminal(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.post(self.alice, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.post(self.bob, f"/item/{i}/handover", f"/item/{i}", outcome="confirmed")
        self.assertEqual(self.item(i)["status"], "returned")
        r = self.claim(self.carol, i)
        self.assertIn("not open for claims", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE item_id = ?", i)["c"], 1)
        r = self.post(self.alice, f"/item/{i}/close", f"/item/{i}", reason="trying to close")
        self.assertIn("already finished", self.text(r))
        self.assertIn(self.one("SELECT status FROM items WHERE id = ?", i)["status"], ("returned",))
        # returned items are not matches
        self.assertEqual(matching.find_matches(self._db(), self.item(i)), [])
        # students cannot correct; admin can, with a reason and an audit record
        t = self.token(self.alice, "/dashboard")
        self.assertEqual(self.alice.post(f"/item/{i}/correct-return", data={"csrf_token": t, "reason": "please undo"}).status_code, 403)
        self.post(self.admin, f"/item/{i}/correct-return", f"/item/{i}", reason="Recorded by mistake")
        row = self.item(i)
        self.assertEqual((row["status"], row["custody_status"], row["approved_claim_id"]), ("active", "with_finder", None))
        self.assertIn("return_corrected", self.audit_actions())
        self.assertEqual(self.one("SELECT handover_type FROM custody_events ORDER BY id DESC")["handover_type"], "CORRECTION")

    def _db(self):
        con = sqlite3.connect(appmod.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    def test_claim_validation_and_rate_limit(self):
        i = self.found()
        r = self.claim(self.bob, i, "too short")
        self.assertIn("at least 15", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims")["c"], 0)
        lost = self.found(type="lost", title="Lost scarf", handover="direct")
        r = self.claim(self.bob, lost)
        self.assertIn("only be made on found-item reports", self.text(r))
        services.CLAIMS_PER_DAY = 2
        a = self.found(title="Bottle A"); b = self.found(title="Bottle B")
        self.claim(self.bob, a); self.claim(self.bob, b)
        c = self.found(title="Bottle C")
        self.assertIn("many claims today", self.text(self.claim(self.bob, c)))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims WHERE item_id = ?", c)["c"], 0)

    def test_suspension_during_active_claim(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "verify", i)
        self.assertEqual(self.item(i)["custody_status"], "direct_handover_pending")
        # suspend the claimant: approval is released and the item opens again
        self.post(self.admin, f"/admin/users/{self.user_id('S2302')}/suspend", "/admin", reason="Misuse")
        self.assertEqual(self.claim_row(cid)["status"], "withdrawn")
        self.assertEqual((self.item(i)["custody_status"], self.item(i)["approved_claim_id"]), ("with_finder", None))
        # suspend a finder in the middle of a handover: staff must resolve it
        j = self.found(client=self.carol, title="Green flask")
        self.claim(self.alice, j)
        acid = self.claim_id(j, "S2301")
        self.finder_act(self.carol, acid, "verify", j)
        self.post(self.admin, f"/admin/users/{self.user_id('S2303')}/suspend", "/admin", reason="Misuse")
        self.assertEqual(self.item(j)["custody_status"], "disputed")
        self.assertIn("suspension_effects_applied", self.audit_actions())

    def test_finder_suspended_with_pending_claim_goes_to_staff(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.post(self.admin, f"/admin/users/{self.user_id('S2301')}/suspend", "/admin", reason="Misuse")
        self.assertEqual((self.claim_row(cid)["status"], self.claim_row(cid)["handler"]), ("under_admin_review", "admin"))


class PrivacyAndSecurityTests(WF):
    def claim_with_photo(self, client, item_id, message=LONG, photo=None):
        data = {"csrf_token": self.token(client, f"/item/{item_id}"), "message": message}
        data["evidence"] = photo if photo is not None else (io.BytesIO(PNG), "proof.png")
        return client.post(f"/item/{item_id}/claim", data=data, content_type="multipart/form-data", follow_redirects=True)

    def test_evidence_downloads_are_authorized(self):
        self.make_officer()
        i = self.found()
        self.claim_with_photo(self.bob, i)
        ev = self.one("SELECT * FROM claim_evidence")
        url = f"/evidence/{ev['id']}"
        self.assertEqual(len(os.listdir(self.evidence_dir)), 1)
        self.assertNotEqual(ev["filename"], "proof.png")                 # generated name
        self.assertEqual(self.outsider.get(url).status_code, 302)        # not logged in
        self.assertEqual(self.bob.get(url).status_code, 200)             # the claimant
        self.assertEqual(self.alice.get(url).status_code, 200)           # finder while reviewing
        self.assertEqual(self.carol.get(url).status_code, 404)           # unrelated student
        self.assertEqual(self.staff.get(url).status_code, 200)           # authorized staff
        self.assertIn("no-store", self.bob.get(url).headers["Cache-Control"])
        # once staff own the claim the finder loses access
        cid = self.claim_id(i, "S2302")
        self.finder_act(self.alice, cid, "escalate", i, note="Not sure")
        self.assertEqual(self.alice.get(url).status_code, 404)
        self.assertEqual(self.bob.get(url).status_code, 200)
        # the private evidence is not reachable through the public media route either
        self.assertEqual(self.carol.get(f"/media/{ev['filename']}").status_code, 404)

    def test_other_students_never_see_private_claim_content(self):
        i = self.found(private_details="Hidden serial 12345")
        self.claim(self.bob, i)
        for client in (self.carol, self.bob):
            page = self.text(client.get(f"/item/{i}"))
            self.assertNotIn("Hidden serial 12345", page)
        carol_page = self.text(self.carol.get(f"/item/{i}"))
        self.assertNotIn(LONG, carol_page)
        self.assertNotIn("Claims (", carol_page)
        search = self.text(self.carol.get("/?q=Hidden"))
        self.assertNotIn("Blue water bottle", search)                    # private details are not searchable

    def test_evidence_upload_validation(self):
        i = self.found()
        r = self.claim_with_photo(self.bob, i, photo=(io.BytesIO(b"MZ not an image"), "run.exe"))
        self.assertIn("must be a PNG", self.text(r))
        r = self.claim_with_photo(self.bob, i, photo=(io.BytesIO(b"definitely not png"), "fake.png"))
        self.assertIn("not a valid image", self.text(r))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims")["c"], 0)
        self.assertEqual(os.listdir(self.evidence_dir), [])
        r = self.claim_with_photo(self.bob, i)
        self.assertIn("Claim sent", self.text(r))
        self.assertEqual(len(os.listdir(self.evidence_dir)), 1)
        self.assertTrue(os.listdir(self.evidence_dir)[0].endswith(".png"))

    def test_refused_claim_removes_uploaded_evidence(self):
        i = self.found()
        self.claim_with_photo(self.alice, i)       # own item: refused after the file was saved
        self.assertEqual(os.listdir(self.evidence_dir), [])

    def test_csrf_is_required_on_workflow_posts(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        for url in (f"/claim/{cid}/finder/verify", f"/claim/{cid}/withdraw", f"/item/{i}/handover",
                    f"/item/{i}/receive", f"/item/{i}/close"):
            client = self.alice if "finder" in url or "close" in url else self.bob
            self.assertIn(client.post(url, data={}).status_code, (400, 403))
        self.assertEqual(self.claim_row(cid)["status"], "pending")

    def test_unknown_workflow_actions_are_404(self):
        i = self.found()
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        t = self.token(self.alice, "/dashboard")
        for url in (f"/claim/{cid}/finder/approve_everything", f"/claim/{cid}/staff/delete", f"/item/{i}/moderate/maybe"):
            self.assertEqual(self.alice.post(url, data={"csrf_token": t}).status_code, 404)

    def test_notifications_do_not_leak_evidence_or_identity(self):
        self.make_officer()
        i = self.found(category="Electronics", title="Phone")
        self.claim(self.bob, i, "Lock screen is a picture of my dog Bruno and the IMEI ends 4321")
        self.staff_act(self.staff, self.claim_id(i, "S2302"), "approve", i, reason="IMEI matched on the box")
        everything = " ".join(r["message"] for r in self.rows("SELECT message FROM notifications"))
        for secret in ("Bruno", "4321", "IMEI", "Diya", "diya.nair", "S2302", "9876543210"):
            self.assertNotIn(secret, everything)
        kinds = {r["kind"] for r in self.rows("SELECT kind FROM notifications")}
        self.assertTrue({"claim", "review", "handover"} <= kinds)

    def test_audit_log_contents(self):
        self.make_officer()
        i = self.found(handover="admin")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.staff_act(self.staff, cid, "approve", i, reason="Verified in person")
        a = self.one("SELECT * FROM audit_log WHERE action = 'claim_approved'")
        self.assertEqual((a["actor_id"], a["record_type"], a["record_id"]), (self.user_id("E1003"), "claim", cid))
        self.assertIn("Verified in person", a["detail"])
        blob = " ".join(f"{r['detail']} {r['action']}" for r in self.rows("SELECT * FROM audit_log"))
        self.assertNotIn(LONG, blob)                                    # claim evidence is never logged
        self.assertNotIn("Secret123", blob)
        self.assertIn("claim_submitted", blob)

    def test_matching_is_only_a_suggestion(self):
        lost = self.found(client=self.bob, type="lost", category="Other", title="Blue steel water bottle",
                          description="Blue steel bottle with a dent, lost near canteen", location="Canteen",
                          private_details="Engraved with my initials DN")
        found = self.found(title="Blue water bottle", location="Canteen area", private_details="Engraved DN inside")
        # the lost report owner gets a notification and a suggestion, nothing else changes
        self.assertIn("may match", self.notes(self.bob))
        page = self.text(self.bob.get(f"/item/{lost}"))
        self.assertIn("Possible matches", page)
        self.assertIn("Blue water bottle", page)
        self.assertIn("Same category", page)
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM claims")["c"], 0)
        self.assertEqual(self.item(found)["custody_status"], "with_finder")
        self.assertNotIn("Possible matches", self.text(self.carol.get(f"/item/{lost}")))
        # private details never influence a score
        a = dict(self.item(lost)); b = dict(self.item(found))
        base_score = matching.score_pair(a, b)[0]
        a["private_details"], b["private_details"] = "totally different", "words entirely"
        self.assertEqual(base_score, matching.score_pair(a, b)[0])
        self.assertEqual(matching.DEFAULT_WEIGHTS, {"category": 30, "name": 25, "location": 20, "date": 15, "description": 10})
        # once an approved claim exists the found item is no longer an actionable match
        self.claim(self.carol, found)
        self.finder_act(self.alice, self.claim_id(found, "S2303"), "verify", found)
        con = sqlite3.connect(appmod.DB_PATH); con.row_factory = sqlite3.Row
        self.assertEqual(matching.find_matches(con, con.execute("SELECT * FROM items WHERE id = ?", (lost,)).fetchone()), [])
        con.close()


class StaffDeskAndPolicyTests(WF):
    def test_queues_and_authorization(self):
        appmod.MODERATION_REQUIRED = True
        pending = self.found(title="Pending thing", handover="admin")
        self.post(self.admin, f"/item/{pending}/moderate/approve", f"/item/{pending}")
        waiting = self.found(title="Another thing", handover="admin")
        self.post(self.admin, f"/item/{waiting}/moderate/approve", f"/item/{waiting}")
        third = self.found(title="Third thing")                          # stays pending
        for name in ("pending_reports", "awaiting_receipt", "claims_review", "high_risk", "disputes",
                     "awaiting_collection", "completed"):
            self.assertEqual(self.admin.get(f"/staff/queue/{name}").status_code, 200, name)
        self.assertIn("Third thing", self.text(self.admin.get("/staff/queue/pending_reports")))
        q = self.text(self.admin.get("/staff/queue/awaiting_receipt"))
        self.assertIn("Pending thing", q)
        self.assertIn(self.item(pending)["ref_code"], q)
        # lookup by reference
        r = self.admin.get(f"/staff/lookup?ref={self.item(waiting)['ref_code'].lower()}")
        self.assertEqual(r.status_code, 302)
        r = self.admin.get("/staff/lookup?ref=NOPE", follow_redirects=True)
        self.assertIn("No report has that reference", self.text(r))
        # students and plain staff are blocked; an authorized officer gets the plain-layout desk
        self.assertEqual(self.alice.get("/staff/queue/pending_reports").status_code, 403)
        self.assertEqual(self.staff.get("/staff/queue/pending_reports").status_code, 403)
        self.assertEqual(self.outsider.get("/staff/queue/pending_reports").status_code, 302)
        self.make_officer()
        self.assertEqual(self.staff.get("/staff/queue/pending_reports").status_code, 200)
        self.assertEqual(self.admin.get("/staff/queue/nonsense").status_code, 404)
        self.assertIn("Staff desk", self.text(self.staff.get("/dashboard")))
        self.assertNotIn("Staff desk", self.text(self.alice.get("/dashboard")))

    def test_high_risk_and_collection_queues(self):
        self.make_officer()
        i = self.found(category="Electronics", title="Laptop", handover="admin")
        self.claim(self.bob, i)
        self.assertIn("Laptop", self.text(self.staff.get("/staff/queue/high_risk")))
        self.assertIn("Laptop", self.text(self.staff.get("/staff/queue/claims_review")))
        self.staff_act(self.staff, self.claim_id(i, "S2302"), "approve", i)
        self.assertIn("Laptop", self.text(self.staff.get("/staff/queue/awaiting_collection")))
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.post(self.staff, f"/item/{i}/release", f"/item/{i}", acknowledged="1")
        self.assertIn("Laptop", self.text(self.staff.get("/staff/queue/completed")))
        self.assertNotIn("Laptop", self.text(self.staff.get("/staff/queue/awaiting_collection")))

    def test_policy_page_changes_risk_and_is_audited(self):
        self.assertEqual(self.alice.get("/admin/policy").status_code, 403)
        self.assertEqual(self.admin.get("/admin/policy").status_code, 200)
        field = "risk_Electronics"
        self.post(self.admin, "/admin/policy", "/admin/policy", **{field: "medium"})
        self.assertEqual(self.one("SELECT risk_level FROM category_policy WHERE category = 'Electronics'")["risk_level"], "medium")
        self.assertIn("policy_changed", self.audit_actions())
        i = self.found(category="Electronics", title="Calculator")
        self.assertEqual(self.item(i)["risk_level"], "medium")
        self.post(self.admin, "/admin/policy", "/admin/policy", **{field: "bogus"})
        self.assertEqual(self.one("SELECT risk_level FROM category_policy WHERE category = 'Electronics'")["risk_level"], "medium")

    def test_only_admin_can_authorize_custody_officers(self):
        uid = self.user_id("E1003")
        t = self.token(self.alice, "/dashboard")
        self.assertEqual(self.alice.post(f"/admin/users/{uid}/officer", data={"csrf_token": t, "grant": "1"}).status_code, 403)
        self.make_officer()
        self.assertEqual(self.user_row("E1003")["is_custody_officer"], 1)
        # students cannot be made officers
        r = self.post(self.admin, f"/admin/users/{self.user_id('S2301')}/officer", "/dashboard", grant="1")
        self.assertIn("Only active staff", self.text(r))
        self.assertEqual(self.user_row("S2301")["is_custody_officer"], 0)
        self.post(self.admin, f"/admin/users/{uid}/officer", "/dashboard", grant="0")
        self.assertEqual(self.user_row("E1003")["is_custody_officer"], 0)
        self.assertIn("officer_revoked", self.audit_actions())

    def test_admin_delete_removes_evidence_files(self):
        i = self.found()
        data = {"csrf_token": self.token(self.bob, f"/item/{i}"), "message": LONG, "evidence": (io.BytesIO(PNG), "p.png")}
        self.bob.post(f"/item/{i}/claim", data=data, content_type="multipart/form-data")
        self.assertEqual(len(os.listdir(self.evidence_dir)), 1)
        self.post(self.admin, f"/admin/listings/{i}/delete", "/admin", reason="spam")
        self.assertEqual(os.listdir(self.evidence_dir), [])
        self.assertEqual(os.listdir(self.item_dir), [])
        self.assertIn("item_deleted", self.audit_actions())


class StateMachineTests(unittest.TestCase):
    def test_transition_tables_are_consistent(self):
        self.assertEqual(set(services.REPORT_TRANSITIONS), set(services.REPORT_STATUSES))
        self.assertEqual(set(services.CUSTODY_TRANSITIONS), set(services.CUSTODY_STATUSES))
        self.assertEqual(set(services.CLAIM_TRANSITIONS), set(services.CLAIM_STATUSES))
        for table in (services.REPORT_TRANSITIONS, services.CUSTODY_TRANSITIONS, services.CLAIM_TRANSITIONS):
            for src, dests in table.items():
                self.assertNotIn(src, dests)
        # terminal and one-way states
        self.assertEqual(services.REPORT_TRANSITIONS["returned"], set())
        self.assertEqual(services.CUSTODY_TRANSITIONS["returned"], set())
        self.assertEqual(services.CLAIM_TRANSITIONS["withdrawn"], set())
        self.assertNotIn("approved", services.CLAIM_TRANSITIONS["rejected"])
        self.assertNotIn("approved", services.CLAIM_TRANSITIONS["withdrawn"])
        self.assertNotIn("with_admin", services.CUSTODY_TRANSITIONS["returned"])
        self.assertEqual(services.CLAIM_TRANSITIONS["pending"] & {"needs_more_info", "under_admin_review"},
                         {"needs_more_info", "under_admin_review"})
        self.assertIn("pending", services.CLAIM_TRANSITIONS["needs_more_info"])

    def test_handover_mode_policy(self):
        base = {"handover_pref": "direct", "risk_level": "low", "unsafe": 0, "custody_status": "with_finder"}
        self.assertEqual(services.handover_mode(base), "direct")
        self.assertEqual(services.handover_mode({**base, "handover_pref": "admin"}), "admin")
        self.assertEqual(services.handover_mode({**base, "risk_level": "high"}), "admin")
        self.assertEqual(services.handover_mode({**base, "unsafe": 1}), "admin")
        self.assertEqual(services.handover_mode({**base, "custody_status": "with_admin"}), "admin")


class MigrationTests(unittest.TestCase):
    """The first release stored custody as a form value; the migration must not turn that into proof."""

    LEGACY = """
    CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, type TEXT NOT NULL,
        title TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL, location TEXT NOT NULL,
        event_date TEXT NOT NULL, photo TEXT, custody TEXT, status TEXT NOT NULL DEFAULT 'open',
        handover_code TEXT, handover_tries INTEGER NOT NULL DEFAULT 0, claimed_by INTEGER,
        created_at TEXT NOT NULL, returned_at TEXT);
    CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, claimer_id INTEGER NOT NULL,
        message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        UNIQUE (item_id, claimer_id));
    CREATE INDEX idx_items_status_type_created ON items(status, type, created_at);
    """

    def test_legacy_database_is_upgraded_safely(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd); os.remove(path)
        old_path, appmod.DB_PATH = appmod.DB_PATH, path
        try:
            con = sqlite3.connect(path)
            users_ddl = appmod.SCHEMA.split("CREATE TABLE IF NOT EXISTS users", 1)[1].split(");", 1)[0]
            users_ddl = users_ddl.replace("    is_custody_officer INTEGER NOT NULL DEFAULT 0,\n", "")
            con.executescript("CREATE TABLE users" + users_ddl + ");" + self.LEGACY)
            for n in (1, 2):
                con.execute("INSERT INTO users (name, email, phone, password_hash, role, status, created_at) "
                            "VALUES (?, ?, '1', 'x', 'student', 'active', '2026-01-01 00:00:00')", (f"U{n}", f"u{n}@x.edu"))
            rows = [
                (1, "found", "Wallet", "Wallets", "d", "Canteen", "2026-09-01", "security", "open", "2026-09-01 10:00:00"),
                (1, "found", "Bottle", "Other", "d", "Gym", "2026-09-02", "finder", "claimed", "2026-09-02 10:00:00"),
                (2, "lost", "Umbrella", "Other", "d", "Lab", "2026-09-03", None, "open", "2026-09-03 10:00:00"),
                (1, "found", "Hoodie", "Clothing", "d", "Hall", "2026-09-04", "finder", "returned", "2026-09-04 10:00:00"),
            ]
            for r in rows:
                con.execute("INSERT INTO items (user_id, type, title, category, description, location, event_date, custody, "
                            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
            con.execute("INSERT INTO claims (item_id, claimer_id, message, status, created_at) VALUES (2, 2, 'it is mine ok', 'approved', '2026-09-02 11:00:00')")
            con.execute("INSERT INTO claims (item_id, claimer_id, message, status, created_at) VALUES (3, 1, 'found your umbrella', 'pending', '2026-09-03 11:00:00')")
            con.commit(); con.close()

            appmod.init_db()
            appmod.init_db()                                   # idempotent
            db = sqlite3.connect(path); db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            it = {r["title"]: r for r in db.execute("SELECT * FROM items")}
            # an old "handed to security" answer was never confirmed by staff
            self.assertEqual((it["Wallet"]["custody_status"], it["Wallet"]["handover_pref"]), ("admin_handover_requested", "admin"))
            self.assertNotEqual(it["Wallet"]["custody_status"], "with_admin")
            self.assertEqual(it["Wallet"]["risk_level"], "high")
            self.assertEqual((it["Bottle"]["status"], it["Bottle"]["custody_status"]), ("active", "direct_handover_pending"))
            self.assertEqual(it["Bottle"]["approved_claim_id"], 1)
            self.assertEqual((it["Hoodie"]["status"], it["Hoodie"]["custody_status"]), ("returned", "returned"))
            self.assertEqual((it["Umbrella"]["custody_status"], it["Umbrella"]["handover_pref"]), (None, None))
            self.assertEqual(len({r["ref_code"] for r in it.values()}), 4)
            claims = {r["id"]: r for r in db.execute("SELECT * FROM claims")}
            self.assertEqual(claims[1]["status"], "approved")
            self.assertEqual(claims[2]["status"], "rejected")        # claims on lost reports are closed
            self.assertIn("no longer supported", claims[2]["review_reason"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'schema_migrated'").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.close()
        finally:
            appmod.DB_PATH = old_path
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=1)
