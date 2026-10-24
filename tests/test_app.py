"""Accounts, admin console and UI tests. Run all tests from the project root:  python -m unittest"""
import io
import os
import re
import tempfile
import unittest
import warnings
from unittest import mock

import app as appmod

warnings.simplefilter("ignore", ResourceWarning)

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
PW = "Secret123"


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        appmod.DB_PATH = self.path
        self.item_dir = tempfile.mkdtemp()
        self.card_dir = tempfile.mkdtemp()
        self.evidence_dir = tempfile.mkdtemp()
        appmod.ITEM_DIR, appmod.CARD_DIR, appmod.EVIDENCE_DIR = self.item_dir, self.card_dir, self.evidence_dir
        appmod.services.REPORTS_PER_HOUR = 1000
        appmod.MODERATION_REQUIRED = False   # most tests publish immediately; workflow tests switch it on
        appmod.init_db()
        appmod.app.config["TESTING"] = True
        self.mails = []
        self._orig_mail = appmod.send_mail
        appmod.send_mail = lambda to, subject, body: (self.mails.append((to, subject, body)) or "console")
        self.admin = appmod.app.test_client()
        self.alice = appmod.app.test_client()   # student S2301
        self.bob = appmod.app.test_client()     # student S2302
        self.staff = appmod.app.test_client()   # staff E1003
        self.outsider = appmod.app.test_client()

    def tearDown(self):
        appmod.send_mail = self._orig_mail
        if os.path.exists(self.path):
            os.remove(self.path)

    # ---- helpers
    def token(self, client, url="/login"):
        html = client.get(url).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)

    def last_code(self):
        return re.search(r"code is (\d{6})", self.mails[-1][2]).group(1)

    def register(self, client, role, id_number, email, password=PW, verify=True):
        t = self.token(client, "/register")
        r = client.post("/register", data={"csrf_token": t, "role": role, "id_number": id_number,
                                           "email": email, "phone": "9876543210", "password": password,
                                           "confirm_password": password, "department": "Computer Science",
                                           "dob": "2004-05-17"},
                        follow_redirects=True)
        if verify and "Verify your email" in r.get_data(as_text=True):
            t = self.token(client, "/verify")
            r = client.post("/verify", data={"csrf_token": t, "code": self.last_code()}, follow_redirects=True)
        return r

    def login(self, client, email, password):
        t = self.token(client, "/login")
        return client.post("/login", data={"csrf_token": t, "email": email, "password": password},
                           follow_redirects=True)

    def login_admin(self):
        return self.login(self.admin, "admin@campus.edu", "Admin@123")

    def setup_users(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu")
        self.register(self.bob, "student", "S2302", "diya.nair@campus.edu")
        self.register(self.staff, "staff", "E1003", "imran.khan@campus.edu")

    def post_item(self, client, **over):
        t = self.token(client, "/post")
        data = {"csrf_token": t, "type": "found", "title": "Black wallet", "category": "Wallets",
                "description": "Leather wallet with a metro card", "location": "Canteen",
                "event_date": "2026-10-01", "handover": "direct", "contact": "9876543210", "photo": (io.BytesIO(PNG), "w.png")}
        data.update(over)
        return client.post("/post", data=data, content_type="multipart/form-data", follow_redirects=True)

    def post(self, client, url, page="/", **data):
        data["csrf_token"] = self.token(client, page)
        return client.post(url, data=data, follow_redirects=True)


class RegistrationTests(Base):
    def test_landing_for_visitors_and_members_only(self):
        html = self.outsider.get("/").get_data(as_text=True)
        self.assertIn("Members only", html)
        self.assertEqual(self.outsider.get("/item/1").status_code, 302)
        self.assertEqual(self.outsider.get("/media/x.png").status_code, 302)
        self.assertEqual(self.outsider.get("/dashboard").status_code, 302)

    def test_roster_registration_with_email_code(self):
        r = self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu")
        html = r.get_data(as_text=True)
        self.assertIn("Email verified", html)
        self.assertIn("What are you looking for", html)
        self.assertIn('aria-label="Signed in as Aarav Shetty, student,', html)
        self.assertEqual(self.mails[0][0], "aarav.shetty@campus.edu")
        self.assertIn("Aarav Shetty", self.alice.get("/dashboard").get_data(as_text=True))

    def test_outsider_cannot_register(self):
        r = self.register(self.outsider, "student", "S9999", "random@gmail.com")
        self.assertIn("do not match the campus records", r.get_data(as_text=True))
        self.assertIn("manual verification", r.get_data(as_text=True))
        self.assertEqual(self.mails, [])

    def test_wrong_email_or_wrong_role_for_real_id(self):
        r = self.register(self.outsider, "student", "S2301", "someone.else@campus.edu")
        self.assertIn("do not match", r.get_data(as_text=True))
        r = self.register(self.outsider, "staff", "S2301", "aarav.shetty@campus.edu")
        self.assertIn("do not match", r.get_data(as_text=True))

    def test_wrong_otp_and_lockout(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu", verify=False)
        real = self.last_code()
        wrong = "000000" if real != "000000" else "111111"
        for i in range(4):
            r = self.post(self.alice, "/verify", "/verify", code=wrong)
            self.assertIn("tries left", r.get_data(as_text=True))
        r = self.post(self.alice, "/verify", "/verify", code=wrong)
        self.assertIn("Too many wrong attempts", r.get_data(as_text=True))
        r = self.post(self.alice, "/verify", "/verify", code=real)
        self.assertIn("no active code", r.get_data(as_text=True).lower())

    def test_resend_cooldown(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu", verify=False)
        r = self.post(self.alice, "/verify/resend", "/verify")
        self.assertIn("Please wait", r.get_data(as_text=True))

    def test_registration_delivery_failure_is_reported_and_resend_is_limited(self):
        appmod.send_mail = lambda to, subject, body: "failed"
        r = self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu", verify=False)
        self.assertIn("could not send the email", r.get_data(as_text=True).lower())
        with appmod.app.app_context():
            self.assertIsNotNone(appmod.q1("SELECT 1 FROM otp_codes"))

        r = self.post(self.alice, "/verify/resend", "/verify")
        self.assertIn("Please wait", r.get_data(as_text=True))

    def test_unverified_attempt_cannot_block_real_owner(self):
        # attacker knows an ID and email but cannot read the inbox
        self.register(self.outsider, "student", "S2301", "aarav.shetty@campus.edu", password="Attacker123",
                      verify=False)
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu")
        self.assertIn("Dashboard", self.alice.get("/dashboard").get_data(as_text=True))
        # attacker's password no longer works
        r = self.login(self.outsider, "aarav.shetty@campus.edu", "Attacker123")
        self.assertIn("incorrect", r.get_data(as_text=True))

    def test_duplicate_registration_blocked(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu")
        r = self.register(self.outsider, "student", "S2301", "aarav.shetty@campus.edu")
        self.assertIn("already exists", r.get_data(as_text=True))

    def test_weak_password_rejected(self):
        r = self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu", password="abcdefgh")
        self.assertIn("at least 8 characters", r.get_data(as_text=True))

    def test_unverified_login_goes_to_verify(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu", verify=False)
        r = self.login(self.bob, "aarav.shetty@campus.edu", PW)
        self.assertIn("Verify your email", r.get_data(as_text=True))

    def test_registration_requires_phone_dob_department_and_matching_password(self):
        good = {"role": "student", "id_number": "S2301", "email": "aarav.shetty@campus.edu",
                "phone": "9876543210", "dob": "2004-05-17", "department": "Computer Science",
                "password": PW, "confirm_password": PW}
        cases = [
            ({"phone": ""}, "Enter your phone number"),
            ({"phone": "abc"}, "valid phone number"),
            ({"dob": ""}, "date of birth"),
            ({"dob": "2999-01-01"}, "real date of birth"),
            ({"department": " "}, "department"),
            ({"confirm_password": "Different123"}, "do not match"),
            ({"confirm_password": ""}, "do not match"),
        ]
        for change, expected in cases:
            t = self.token(self.outsider, "/register")
            r = self.outsider.post("/register", data=dict(good, csrf_token=t, **change), follow_redirects=True)
            self.assertIn(expected, r.get_data(as_text=True), change)
        # none of the rejected attempts created an account
        with appmod.app.app_context():
            self.assertIsNone(appmod.q1("SELECT 1 FROM users WHERE id_number = 'S2301'"))
        t = self.token(self.outsider, "/register")
        r = self.outsider.post("/register", data=dict(good, csrf_token=t), follow_redirects=True)
        self.assertIn("Verify your email", r.get_data(as_text=True))
        with appmod.app.app_context():
            row = appmod.q1("SELECT date_of_birth, department FROM users WHERE id_number = 'S2301'")
            self.assertEqual((row["date_of_birth"], row["department"]), ("2004-05-17", "Computer Science"))

    def test_department_dropdown_and_other(self):
        page = self.outsider.get("/register").get_data(as_text=True)
        for dept in ("Computer Science", "Commerce &amp; Management", "Library", "Accounts &amp; Finance"):
            self.assertIn(f'<option value="{dept}"', page)
        for old_course in ("BCA", "MCA", "BCom"):
            self.assertNotIn(f'<option value="{old_course}"', page)
        good = {"role": "student", "id_number": "S2301", "email": "aarav.shetty@campus.edu",
                "phone": "9876543210", "dob": "2004-05-17", "department": "Computer Science",
                "password": PW, "confirm_password": PW}
        t = self.token(self.outsider, "/register")
        r = self.outsider.post("/register", data=dict(good, csrf_token=t, department="__other__",
                                                      department_other=""), follow_redirects=True)
        self.assertIn("Choose your department", r.get_data(as_text=True))
        t = self.token(self.outsider, "/register")
        r = self.outsider.post("/register", data=dict(good, csrf_token=t, department="__other__",
                                                      department_other="Fine Arts"), follow_redirects=True)
        self.assertIn("Verify your email", r.get_data(as_text=True))
        with appmod.app.app_context():
            self.assertEqual(appmod.q1("SELECT department FROM users WHERE id_number = 'S2301'")["department"],
                             "Fine Arts")

    def test_student_cannot_pick_staff_department(self):
        good = {"role": "student", "id_number": "S2301", "email": "aarav.shetty@campus.edu",
                "phone": "9876543210", "dob": "2004-05-17", "department": "Library",
                "password": PW, "confirm_password": PW}
        t = self.token(self.outsider, "/register")
        r = self.outsider.post("/register", data=dict(good, csrf_token=t), follow_redirects=True)
        self.assertIn("for staff", r.get_data(as_text=True))

    def test_manual_request_admin_approval(self):
        t = self.token(self.outsider, "/register/manual")
        data = {"csrf_token": t, "role": "student", "name": "New Joiner", "id_number": "n77",
                "department": "Law", "email": "joiner@gmail.com", "phone": "9876543210", "password": PW,
                "confirm_password": PW, "dob": "2003-02-11",
                "id_card": (io.BytesIO(PNG), "card.png")}
        r = self.outsider.post("/register/manual", data=data, content_type="multipart/form-data",
                               follow_redirects=True)
        self.assertIn("Request sent", r.get_data(as_text=True))
        r = self.login(self.outsider, "joiner@gmail.com", PW)
        self.assertIn("waiting for admin approval", r.get_data(as_text=True))
        self.login_admin()
        page = self.admin.get("/admin/requests").get_data(as_text=True)
        self.assertIn("New Joiner", page)
        uid = re.search(r"/admin/idcard/(\d+)", page).group(1)
        self.assertEqual(self.admin.get(f"/admin/idcard/{uid}").status_code, 200)
        self.assertEqual(self.outsider.get(f"/admin/idcard/{uid}").status_code, 302)
        r = self.post(self.admin, f"/admin/requests/{uid}/approve", "/admin/requests")
        self.assertIn("approved", r.get_data(as_text=True))
        r = self.login(self.outsider, "joiner@gmail.com", PW)
        self.assertIn("Welcome back", r.get_data(as_text=True))

    def test_manual_request_rejection(self):
        t = self.token(self.outsider, "/register/manual")
        self.outsider.post("/register/manual", data={
            "csrf_token": t, "role": "staff", "name": "Fake Person", "id_number": "x1", "department": "None",
            "email": "fake@mail.com", "phone": "9876543210", "password": PW, "confirm_password": PW,
            "dob": "1990-01-01", "id_card": (io.BytesIO(PNG), "c.png")},
            content_type="multipart/form-data")
        self.login_admin()
        page = self.admin.get("/admin/requests").get_data(as_text=True)
        uid = re.search(r"/admin/idcard/(\d+)", page).group(1)
        self.post(self.admin, f"/admin/requests/{uid}/reject", "/admin/requests", note="ID is not readable")
        r = self.login(self.outsider, "fake@mail.com", PW)
        self.assertIn("not approved", r.get_data(as_text=True))
        self.assertIn("ID is not readable", r.get_data(as_text=True))

    def test_manual_request_needs_real_image(self):
        t = self.token(self.outsider, "/register/manual")
        r = self.outsider.post("/register/manual", data={
            "csrf_token": t, "role": "student", "name": "Bad Upload", "id_number": "b1", "department": "X1",
            "email": "bad@mail.com", "phone": "9876543210", "password": PW, "confirm_password": PW,
            "dob": "2002-03-04", "id_card": (io.BytesIO(b"<?php evil ?>"), "card.png")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("not a valid image", r.get_data(as_text=True))
        self.assertEqual(os.listdir(self.card_dir), [])

    def test_login_rate_limit(self):
        self.setup_users()
        for _ in range(5):
            self.login(self.outsider, "aarav.shetty@campus.edu", "wrongpass1")
        r = self.login(self.outsider, "aarav.shetty@campus.edu", PW)
        self.assertIn("Too many failed attempts", r.get_data(as_text=True))


class AdminTests(Base):
    def test_admin_only_pages(self):
        self.setup_users()
        for url in ("/admin", "/admin/roster", "/admin/users", "/admin/audit", "/admin/requests"):
            self.assertEqual(self.alice.get(url).status_code, 403, url)
            self.assertEqual(self.outsider.get(url).status_code, 302, url)
        self.login_admin()
        for url in ("/admin", "/admin/roster", "/admin/users", "/admin/audit", "/admin/requests",
                    "/admin/roster/template.csv"):
            self.assertEqual(self.admin.get(url).status_code, 200, url)

    def test_roster_csv_import_enables_registration(self):
        self.login_admin()
        csv_text = ("id_number,full_name,email,role,department\n"
                    "n501,New Student,new.student@college.edu,student,Law\n"
                    "bad row without enough,,,,\n"
                    "S2301,Aarav S Updated,aarav.shetty@campus.edu,student,CSE\n")
        t = self.token(self.admin, "/admin/roster")
        r = self.admin.post("/admin/roster/upload", data={
            "csrf_token": t, "csv": (io.BytesIO(csv_text.encode()), "r.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("1 added, 1 updated, 1 skipped", r.get_data(as_text=True))
        r = self.register(self.outsider, "student", "N501", "new.student@college.edu")
        self.assertIn("Email verified", r.get_data(as_text=True))
        r = self.post(self.admin, "/admin/roster/upload", "/admin/roster")
        self.assertIn("Choose a CSV file", r.get_data(as_text=True))

    def test_bad_csv_rejected(self):
        self.login_admin()
        t = self.token(self.admin, "/admin/roster")
        r = self.admin.post("/admin/roster/upload", data={
            "csrf_token": t, "csv": (io.BytesIO(b"a,b\n1,2\n"), "r.csv")},
            content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("needs these columns", r.get_data(as_text=True))

    def test_roster_add_and_delete(self):
        self.login_admin()
        r = self.post(self.admin, "/admin/roster/add", "/admin/roster", id_number="z9", full_name="Zed Zee",
                      email="zed@college.edu", role="staff", department="Lab")
        self.assertIn("Roster entry saved", r.get_data(as_text=True))
        r = self.post(self.admin, "/admin/roster/add", "/admin/roster", id_number="", full_name="X",
                      email="bad", role="staff")
        self.assertIn("valid ID number", r.get_data(as_text=True))
        page = self.admin.get("/admin/roster?q=Zed").get_data(as_text=True)
        rid = re.search(r"/admin/roster/(\d+)/delete", page).group(1)
        r = self.post(self.admin, f"/admin/roster/{rid}/delete", "/admin/roster")
        self.assertIn("Roster entry removed", r.get_data(as_text=True))

    def test_suspend_and_reactivate(self):
        self.setup_users()
        self.login_admin()
        uid = appmod.sqlite3.connect(self.path).execute(
            "SELECT id FROM users WHERE id_number = 'S2301'").fetchone()[0]
        r = self.post(self.admin, f"/admin/users/{uid}/suspend", "/admin/users")
        self.assertIn("suspended", r.get_data(as_text=True))
        self.assertEqual(self.alice.get("/dashboard").status_code, 302)  # session no longer valid
        r = self.login(self.alice, "aarav.shetty@campus.edu", PW)
        self.assertIn("suspended", r.get_data(as_text=True))
        self.post(self.admin, f"/admin/users/{uid}/reactivate", "/admin/users")
        r = self.login(self.alice, "aarav.shetty@campus.edu", PW)
        self.assertIn("Welcome back", r.get_data(as_text=True))
        self.assertIn("User suspended", self.admin.get("/admin/audit").get_data(as_text=True))


class CourseEndAndRemovalTests(Base):
    """Students are closed automatically after their course end date; admins can extend, disable or remove."""

    def uid(self, id_number):
        return appmod.sqlite3.connect(self.path).execute(
            "SELECT id FROM users WHERE id_number = ?", (id_number,)).fetchone()[0]

    def set_end_directly(self, id_number, end):
        con = appmod.sqlite3.connect(self.path)
        con.execute("UPDATE users SET academic_end = ? WHERE id_number = ?", (end, id_number))
        con.commit()
        con.close()

    def status(self, id_number):
        return appmod.sqlite3.connect(self.path).execute(
            "SELECT status, review_note FROM users WHERE id_number = ?", (id_number,)).fetchone()

    def test_registration_copies_end_date_and_blocks_ended_courses(self):
        self.register(self.alice, "student", "S2301", "aarav.shetty@campus.edu")
        row = appmod.sqlite3.connect(self.path).execute(
            "SELECT academic_end FROM users WHERE id_number = 'S2301'").fetchone()
        self.assertEqual(row[0], "2029-05-20")
        con = appmod.sqlite3.connect(self.path)
        con.execute("UPDATE roster SET academic_end = '2020-01-01' WHERE id_number = 'S2302'")
        con.commit()
        con.close()
        r = self.register(self.bob, "student", "S2302", "diya.nair@campus.edu")
        self.assertIn("course period ended", r.get_data(as_text=True))

    def test_student_disabled_after_end_date_but_not_on_the_day(self):
        self.setup_users()
        today = appmod.datetime.now().strftime("%Y-%m-%d")
        self.set_end_directly("S2301", today)           # last day: still allowed
        self.assertEqual(self.alice.get("/dashboard").status_code, 200)
        self.assertEqual(self.status("S2301")[0], "active")
        self.set_end_directly("S2301", "2020-01-01")     # ended long ago: closed on the next request
        self.assertEqual(self.alice.get("/dashboard").status_code, 302)
        status, note = self.status("S2301")
        self.assertEqual(status, "suspended")
        self.assertTrue(note.startswith("Course ended on 2020-01-01"))
        r = self.login(self.alice, "aarav.shetty@campus.edu", PW)
        self.assertIn("course period has ended", r.get_data(as_text=True))
        self.login_admin()
        self.assertIn("auto disabled", self.admin.get("/admin/audit").get_data(as_text=True).lower().replace("_", " "))

    def test_sweep_disables_students_without_them_logging_in(self):
        self.setup_users()
        self.set_end_directly("S2302", "2020-01-01")
        with appmod.app.app_context():
            self.assertEqual(appmod.expire_students(), 1)
        self.assertEqual(self.status("S2302")[0], "suspended")
        self.assertEqual(self.status("S2301")[0], "active")
        self.assertEqual(self.status("E1003")[0], "active")  # staff never expire

    def test_cannot_reactivate_until_end_date_is_extended(self):
        self.setup_users()
        self.set_end_directly("S2301", "2020-01-01")
        self.alice.get("/dashboard")
        self.login_admin()
        uid = self.uid("S2301")
        r = self.post(self.admin, f"/admin/users/{uid}/reactivate", "/admin/users")
        self.assertIn("has passed", r.get_data(as_text=True))
        self.assertEqual(self.status("S2301")[0], "suspended")
        r = self.post(self.admin, f"/admin/users/{uid}/set_end", "/admin/users", end_date="2019-01-01")
        self.assertIn("cannot be in the past", r.get_data(as_text=True))
        r = self.post(self.admin, f"/admin/users/{uid}/set_end", "/admin/users", end_date="2030-06-30")
        self.assertIn("reactivated", r.get_data(as_text=True))
        self.assertEqual(self.status("S2301")[0], "active")
        roster = appmod.sqlite3.connect(self.path).execute(
            "SELECT academic_end FROM roster WHERE id_number = 'S2301'").fetchone()[0]
        self.assertEqual(roster, "2030-06-30")
        self.assertIn("Welcome back", self.login(self.alice, "aarav.shetty@campus.edu", PW).get_data(as_text=True))

    def test_manual_suspension_is_not_lifted_by_a_new_end_date(self):
        self.setup_users()
        self.login_admin()
        uid = self.uid("S2301")
        self.post(self.admin, f"/admin/users/{uid}/suspend", "/admin/users", reason="misuse")
        self.post(self.admin, f"/admin/users/{uid}/set_end", "/admin/users", end_date="2031-01-01")
        self.assertEqual(self.status("S2301")[0], "suspended")

    def test_remove_completely(self):
        self.setup_users()
        self.post_item(self.alice)                       # alice posts a found item, bob claims nothing yet
        self.login_admin()
        uid = self.uid("S2301")
        con = appmod.sqlite3.connect(self.path)
        con.execute("UPDATE users SET id_card = 'card.png' WHERE id = ?", (uid,))
        con.commit()
        con.close()
        open(os.path.join(self.card_dir, "card.png"), "wb").write(PNG)
        r = self.post(self.admin, f"/admin/users/{uid}/remove", "/admin/users", confirm_text="no")
        self.assertIn("Type REMOVE", r.get_data(as_text=True))
        self.assertIsNotNone(self.status("S2301"))
        r = self.post(self.admin, f"/admin/users/{uid}/remove", "/admin/users", confirm_text="REMOVE")
        self.assertIn("removed completely", r.get_data(as_text=True))
        con = appmod.sqlite3.connect(self.path)
        self.assertIsNone(con.execute("SELECT 1 FROM users WHERE id_number = 'S2301'").fetchone())
        self.assertIsNone(con.execute("SELECT 1 FROM roster WHERE id_number = 'S2301'").fetchone())
        self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        self.assertEqual(os.listdir(self.item_dir), [])
        self.assertEqual(os.listdir(self.card_dir), [])
        self.assertIn("removed completely", self.admin.get("/admin/audit").get_data(as_text=True))
        r = self.register(self.outsider, "student", "S2301", "aarav.shetty@campus.edu")
        self.assertIn("do not match", r.get_data(as_text=True))   # no longer on the campus list

    def test_cannot_remove_someone_whose_item_is_in_college_custody(self):
        self.setup_users()
        self.post_item(self.alice)
        con = appmod.sqlite3.connect(self.path)
        con.execute("UPDATE items SET custody_status = 'with_admin'")
        con.commit()
        con.close()
        self.login_admin()
        r = self.post(self.admin, f"/admin/users/{self.uid('S2301')}/remove", "/admin/users", confirm_text="REMOVE")
        self.assertIn("college custody", r.get_data(as_text=True))
        self.assertIsNotNone(self.status("S2301"))

    def test_add_person_validation(self):
        self.login_admin()
        base = dict(id_number="S3001", full_name="Neha Rao", email="neha.rao@campus.edu", role="student",
                    department="Computer Science", academic_end="2030-05-20")
        cases = [
            (dict(base, academic_end=""), "course end date"),
            (dict(base, academic_end="2001-01-01"), "cannot be in the past"),
            (dict(base, academic_end="not-a-date"), "valid course end date"),
            (dict(base, full_name="Neha123"), "valid full name"),
            (dict(base, full_name="N"), "valid full name"),
            (dict(base, id_number="S 30!"), "valid ID number"),
            (dict(base, email="neha@@campus"), "valid email"),
            (dict(base, email="a..b@campus.edu"), "valid email"),
            (dict(base, role="teacher"), "student or a staff"),
        ]
        for data, expected in cases:
            r = self.post(self.admin, "/admin/roster/add", "/admin/roster", **data)
            self.assertIn(expected, r.get_data(as_text=True), data)
        r = self.post(self.admin, "/admin/roster/add", "/admin/roster", **base)
        self.assertIn("saved", r.get_data(as_text=True))
        staff = dict(base, id_number="E3001", role="staff", academic_end="")
        r = self.post(self.admin, "/admin/roster/add", "/admin/roster", **staff)
        self.assertIn("saved", r.get_data(as_text=True))

    def test_csv_import_checks_every_row(self):
        con = appmod.sqlite3.connect(self.path)
        con.row_factory = appmod.sqlite3.Row
        con.close()
        text = ("id_number,full_name,email,role,department,end_date\n"
                "S4001,Good Student,good@campus.edu,student,Computer Science,2030-05-20\n"
                "S4002,Bad Date,bad1@campus.edu,student,Computer Science,31/12/2030\n"
                "S4003,Digits 99,bad2@campus.edu,student,Computer Science,2030-05-20\n"
                "S4004,Past Date,bad3@campus.edu,student,Computer Science,2001-01-01\n"
                "E4001,Good Staff,staff@campus.edu,staff,Library,\n")
        with appmod.app.app_context():
            added, updated, skipped = appmod.import_roster(appmod.get_db(), text)
        self.assertEqual((added, updated, skipped), (2, 0, 3))

    def test_login_and_register_input_validation(self):
        r = self.login(self.outsider, "not-an-email", PW)
        self.assertIn("valid email", r.get_data(as_text=True))
        r = self.login(self.outsider, "someone@campus.edu", "x" * 200)
        self.assertIn("valid email address and your password", r.get_data(as_text=True))
        r = self.register(self.outsider, "student", "S 23!01", "aarav.shetty@campus.edu")
        self.assertIn("valid ID number", r.get_data(as_text=True))
        r = self.register(self.outsider, "student", "S2301", "aarav@@campus.edu")
        self.assertIn("email address registered", r.get_data(as_text=True))
        r = self.register(self.outsider, "student", "S2301", "aarav.shetty@campus.edu", password="a1" * 70)
        self.assertIn("at most 128", r.get_data(as_text=True))
        t = self.token(self.outsider, "/register/manual")
        data = {"csrf_token": t, "role": "student", "name": "Rahul 007", "id_number": "n77", "department": "Law",
                "email": "r@gmail.com", "phone": "9876543210", "password": PW, "confirm_password": PW,
                "dob": "2003-02-11", "id_card": (io.BytesIO(PNG), "card.png")}
        r = self.outsider.post("/register/manual", data=data, content_type="multipart/form-data",
                               follow_redirects=True)
        self.assertIn("valid full name", r.get_data(as_text=True))



class ItemFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.setup_users()

    def test_post_validation(self):
        r = self.post_item(self.alice, event_date="2099-01-01")
        self.assertIn("cannot be in the future", r.get_data(as_text=True))
        r = self.post_item(self.alice, photo=(io.BytesIO(b"x"), "evil.exe"))
        self.assertIn("must be a PNG", r.get_data(as_text=True))
        r = self.post_item(self.alice, photo=(io.BytesIO(b"not an image at all"), "fake.png"))
        self.assertIn("not a valid image", r.get_data(as_text=True))
        r = self.post_item(self.alice, title="ab")
        self.assertIn("short title", r.get_data(as_text=True))
        r = self.post_item(self.alice, handover="somewhere")
        self.assertIn("how you want to hand over", r.get_data(as_text=True))

    def test_admin_can_delete_any_listing(self):
        self.post_item(self.alice)
        self.login_admin()
        r = self.post(self.admin, "/admin/listings/1/delete", "/item/1")
        self.assertIn("deleted", r.get_data(as_text=True).lower())
        self.assertEqual(self.alice.get("/item/1").status_code, 404)

    def test_matching_suggestions(self):
        self.post_item(self.alice, type="found", title="Black leather wallet", category="Wallets",
                       description="Has a metro card inside", location="Canteen")
        self.post_item(self.bob, type="lost", title="Lost black wallet", category="Wallets",
                       description="Leather wallet with metro card", location="Canteen area")
        self.post_item(self.bob, type="found", title="Blue umbrella", category="Other",
                       description="Large umbrella left outside", location="Gate")
        page = self.bob.get("/item/2").get_data(as_text=True)
        self.assertIn("Items that may match", page)
        self.assertIn("Black leather wallet", page)
        self.assertNotIn("Blue umbrella", page)

    def test_pagination(self):
        for i in range(14):
            self.post_item(self.alice, title=f"Item number {i}", photo=(io.BytesIO(PNG), "p.png"))
        self.assertIn("Page 1 of 2", self.bob.get("/").get_data(as_text=True))
        self.assertIn("Page 2 of 2", self.bob.get("/?page=2").get_data(as_text=True))
        self.assertEqual(self.bob.get("/?page=99").status_code, 200)

    def test_csrf_and_xss(self):
        r = self.alice.post("/post", data={"title": "x"})
        self.assertEqual(r.status_code, 400)
        self.post_item(self.alice, title="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", self.bob.get("/").get_data(as_text=True))

    def test_safe_next_redirect(self):
        t = self.token(self.outsider, "/login?next=//evil.com")
        r = self.outsider.post("/login?next=//evil.com", data={
            "csrf_token": t, "email": "aarav.shetty@campus.edu", "password": PW})
        self.assertNotIn("evil.com", r.headers.get("Location", ""))

    def test_security_headers(self):
        r = self.outsider.get("/")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")


class UserInterfaceTests(Base):
    """Behaviour added with the user-side redesign: sorting, duplicate guard, inline errors, UI states."""

    def setUp(self):
        super().setUp()
        self.setup_users()

    def titles_in_order(self, html, titles):
        return sorted(titles, key=lambda t: html.index(t))

    def test_sort_options_and_invalid_sort_fallback(self):
        self.post_item(self.alice, title="Alpha scarf", event_date="2026-10-05", category="Clothing", photo=(io.BytesIO(PNG), "a.png"))
        self.post_item(self.alice, title="Bravo keys", event_date="2026-10-01", category="Keys", photo=(io.BytesIO(PNG), "b.png"))
        self.post_item(self.alice, title="Charlie bag", event_date="2026-10-03", category="Bags", photo=(io.BytesIO(PNG), "c.png"))
        names = ["Alpha scarf", "Bravo keys", "Charlie bag"]
        newest = self.bob.get("/?sort=newest").get_data(as_text=True)
        self.assertEqual(self.titles_in_order(newest, names), ["Charlie bag", "Bravo keys", "Alpha scarf"])
        oldest = self.bob.get("/?sort=oldest").get_data(as_text=True)
        self.assertEqual(self.titles_in_order(oldest, names), ["Alpha scarf", "Bravo keys", "Charlie bag"])
        by_date = self.bob.get("/?sort=date").get_data(as_text=True)
        self.assertEqual(self.titles_in_order(by_date, names), ["Alpha scarf", "Charlie bag", "Bravo keys"])
        r = self.bob.get("/?sort=1%3BDROP+TABLE+items")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Charlie bag", r.get_data(as_text=True))

    def test_duplicate_submission_creates_one_listing(self):
        self.post_item(self.alice)
        r = self.post_item(self.alice)
        self.assertIn("did not create a duplicate", r.get_data(as_text=True))
        with appmod.app.app_context():
            self.assertEqual(appmod.q1("SELECT COUNT(*) AS c FROM items")["c"], 1)
        self.assertEqual(len(os.listdir(self.item_dir)), 1)
        self.post_item(self.alice, title="Different wallet")
        with appmod.app.app_context():
            self.assertEqual(appmod.q1("SELECT COUNT(*) AS c FROM items")["c"], 2)

    def test_validation_errors_are_attached_to_the_field(self):
        html = self.post_item(self.alice, title="ab").get_data(as_text=True)
        self.assertIn('id="err-title"', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn("short title", html)
        self.assertIn('value="ab"', html)  # entered values are kept
        html = self.post_item(self.alice, event_date="2099-01-01").get_data(as_text=True)
        self.assertIn('id="err-event_date"', html)

    def test_report_form_preselects_type(self):
        found = self.alice.get("/post?type=found").get_data(as_text=True)
        self.assertRegex(found, r'value="found"\s+checked')
        junk = self.alice.get("/post?type=bogus").get_data(as_text=True)
        self.assertRegex(junk, r'value="lost"\s+checked')
        self.assertIn("Report a found item", found)

    def test_public_landing_shows_totals_but_no_listings(self):
        self.post_item(self.alice)
        html = self.outsider.get("/").get_data(as_text=True)
        self.assertIn("Members only", html)
        self.assertIn("still lost", html)
        self.assertNotIn("Black wallet", html)
        self.assertNotIn("/item/1", html)

    def test_filter_chips_and_empty_states(self):
        self.assertIn("No listings yet", self.bob.get("/").get_data(as_text=True))
        self.post_item(self.alice)
        html = self.bob.get("/?q=wallet&type=found&category=Wallets").get_data(as_text=True)
        self.assertIn("Clear all", html)
        self.assertIn("Remove category filter", html)
        self.assertIn("No listings match", self.bob.get("/?q=zzzz").get_data(as_text=True))

    def test_item_page_actions_depend_on_permissions(self):
        self.post_item(self.alice)
        owner = self.alice.get("/item/1").get_data(as_text=True)
        self.assertIn("Close report", owner)
        self.assertNotIn("Submit claim", owner)
        other = self.bob.get("/item/1").get_data(as_text=True)
        self.assertNotIn("Close report", other)
        self.assertIn("Submit claim", other)
        self.assertNotIn("aarav.shetty@campus.edu", other)

    def test_dashboard_shows_pending_claims_and_sent_claims(self):
        self.post_item(self.alice, category="Other")
        self.post(self.bob, "/item/1/claim", "/item/1", message="It has a metro card with my name on it")
        owner = self.alice.get("/dashboard").get_data(as_text=True)
        self.assertIn("1 pending claim", owner)
        self.assertIn("Claims to review", owner)
        sender = self.bob.get("/dashboard").get_data(as_text=True)
        self.assertIn("Black wallet", sender)
        self.assertIn("Waiting for the finder", sender)

    def test_static_assets_are_served(self):
        for path in ("/static/css/style.css", "/static/js/app.js"):
            self.assertEqual(self.outsider.get(path).status_code, 200)

    def test_pages_render_for_every_user_route(self):
        self.post_item(self.alice)
        for path in ("/", "/post", "/post?type=found", "/item/1", "/dashboard", "/notifications"):
            self.assertEqual(self.bob.get(path).status_code, 200, path)
        for path in ("/login", "/register", "/register/manual"):
            self.assertEqual(self.outsider.get(path).status_code, 200, path)
        self.assertEqual(self.bob.get("/item/999").status_code, 404)


class MailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.original_app_env = appmod.app.config["APP_ENV"]
        appmod.app.config["APP_ENV"] = "production"

    def tearDown(self):
        appmod.app.config["APP_ENV"] = self.original_app_env

    def test_smtp_delivery_uses_starttls_and_configured_sender(self):
        smtp = mock.Mock()
        smtp.__enter__ = mock.Mock(return_value=smtp)
        smtp.__exit__ = mock.Mock(return_value=False)
        settings = {
            "SMTP_HOST": "smtp.example.edu",
            "SMTP_PORT": "587",
            "SMTP_USER": "portal@example.edu",
            "SMTP_PASS": "mail-password",
            "MAIL_FROM": "Campus Portal <portal@example.edu>",
        }
        with mock.patch.dict(os.environ, settings), \
                mock.patch.object(appmod.smtplib, "SMTP", return_value=smtp) as smtp_factory:
            result = appmod.send_mail("student@example.edu", "Verification", "Your code is 123456.")

        self.assertEqual(result, "email")
        smtp_factory.assert_called_once_with("smtp.example.edu", 587, timeout=15)
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("portal@example.edu", "mail-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["From"], "Campus Portal <portal@example.edu>")
        self.assertEqual(message["To"], "student@example.edu")
        self.assertIn("123456", message.get_content())

    def test_production_smtp_errors_do_not_log_otp_or_server_response(self):
        smtp = mock.Mock()
        smtp.__enter__ = mock.Mock(return_value=smtp)
        smtp.__exit__ = mock.Mock(return_value=False)
        smtp.send_message.side_effect = RuntimeError("Remote server echoed OTP 654321.")
        settings = {
            "SMTP_HOST": "smtp.example.edu",
            "SMTP_PORT": "587",
            "SMTP_USER": "portal@example.edu",
            "SMTP_PASS": "mail-password",
        }
        with mock.patch.dict(os.environ, settings), \
                mock.patch.object(appmod.smtplib, "SMTP", return_value=smtp):
            with self.assertLogs(appmod.app.logger, level="ERROR") as captured:
                result = appmod.send_mail("student@example.edu", "Verification", "Your code is 654321.")

        self.assertEqual(result, "failed")
        logs = "\n".join(captured.output)
        self.assertNotIn("654321", logs)
        self.assertNotIn("Remote server echoed", logs)

    def test_production_requires_smtp_host_without_printing_message(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": ""}), \
                mock.patch.object(appmod.smtplib, "SMTP") as smtp:
            with self.assertLogs(appmod.app.logger, level="ERROR") as captured:
                result = appmod.send_mail("student@example.edu", "Verification", "Your code is 987654.")

        self.assertEqual(result, "failed")
        self.assertNotIn("987654", "\n".join(captured.output))
        smtp.assert_not_called()


class AdminConsoleTests(Base):
    def test_new_admin_pages_and_access(self):
        self.setup_users()
        urls = ("/admin/listings", "/admin/users/1", "/admin/users/export.csv", "/admin/listings/export.csv",
                "/admin/roster/export.csv", "/admin/audit/export.csv")
        for url in urls:
            self.assertEqual(self.alice.get(url).status_code, 403, url)
            self.assertEqual(self.outsider.get(url).status_code, 302, url)
        self.login_admin()
        self.assertEqual(self.admin.get("/admin/users/1").status_code, 404)  # admin accounts are not listed
        uid = appmod.sqlite3.connect(self.path).execute(
            "SELECT id FROM users WHERE id_number = 'S2301'").fetchone()[0]
        for url in ("/admin/listings", f"/admin/users/{uid}", "/admin/users/export.csv",
                    "/admin/listings/export.csv", "/admin/roster/export.csv", "/admin/audit/export.csv",
                    "/admin/users?q=aarav&role=student&status=active", "/admin/audit?action=user_suspended",
                    "/admin/roster?role=staff&reg=yes", "/admin/listings?type=found&status=open&category=Wallets"):
            self.assertEqual(self.admin.get(url).status_code, 200, url)

    def test_dashboard_counts_and_filters(self):
        self.setup_users()
        self.post_item(self.alice, title="Blue umbrella", category="Other", type="lost")
        self.post_item(self.bob, title="Black wallet")
        self.login_admin()
        page = self.admin.get("/admin").get_data(as_text=True)
        self.assertIn("To do", page)
        self.assertIn("Where do you want to go", page)
        listings = self.admin.get("/admin/listings?type=lost").get_data(as_text=True)
        self.assertIn("Blue umbrella", listings)
        self.assertNotIn("Black wallet", listings)
        self.assertIn("Black wallet", self.admin.get("/admin/listings?q=wallet").get_data(as_text=True))
        csv_text = self.admin.get("/admin/listings/export.csv").get_data(as_text=True)
        self.assertTrue(csv_text.startswith("id,type,title"))
        self.assertIn("Blue umbrella", csv_text)

    def test_admin_deletes_listing_with_reason_and_notifies_poster(self):
        self.setup_users()
        self.post_item(self.alice, title="Spam listing")
        self.login_admin()
        r = self.post(self.admin, "/admin/listings/1/delete", "/admin/listings", reason="Not a real item")
        self.assertIn("Listing deleted", r.get_data(as_text=True))
        self.assertNotIn("Spam listing", self.admin.get("/admin/listings").get_data(as_text=True))
        alerts = self.alice.get("/notifications").get_data(as_text=True)
        self.assertIn("removed by an admin", alerts)
        self.assertIn("Not a real item", alerts)
        self.assertIn("Item deleted", self.admin.get("/admin/audit").get_data(as_text=True))
        self.assertEqual(os.listdir(self.item_dir), [])

    def test_roster_edit_and_filters(self):
        self.login_admin()
        self.post(self.admin, "/admin/roster/add", "/admin/roster", id_number="z9", full_name="Zed Zee",
                  email="zed@campus.edu", role="student", department="Law", academic_end="2030-05-20")
        page = self.admin.get("/admin/roster?q=Zed").get_data(as_text=True)
        rid = re.search(r"/admin/roster/(\d+)/edit", page).group(1)
        r = self.post(self.admin, f"/admin/roster/{rid}/edit", "/admin/roster", full_name="Zed Zeta",
                      email="zed@campus.edu", role="staff", department="Law", next="/admin/roster?q=Zed")
        self.assertIn("Zed Zeta", r.get_data(as_text=True))
        bad = self.post(self.admin, f"/admin/roster/{rid}/edit", "/admin/roster", full_name="Z",
                        email="nope", role="staff", department="")
        self.assertIn("valid full name", bad.get_data(as_text=True))
        csv_text = self.admin.get("/admin/roster/export.csv?q=Zed").get_data(as_text=True)
        self.assertIn("Zed Zeta", csv_text)

    def test_suspend_from_detail_page_returns_there_and_blocks_open_redirect(self):
        self.setup_users()
        self.login_admin()
        uid = appmod.sqlite3.connect(self.path).execute(
            "SELECT id FROM users WHERE id_number = 'S2301'").fetchone()[0]
        r = self.post(self.admin, f"/admin/users/{uid}/suspend", f"/admin/users/{uid}",
                      reason="Spamming", next="//evil.example")
        self.assertIn("suspended", r.get_data(as_text=True))
        self.assertIn("Spamming", self.admin.get("/admin/audit").get_data(as_text=True))
        r = self.post(self.admin, f"/admin/users/{uid}/reactivate", f"/admin/users/{uid}",
                      next=f"/admin/users/{uid}")
        self.assertIn("reactivated", r.get_data(as_text=True))

    def test_global_search_and_admin_landing(self):
        self.setup_users()
        self.post_item(self.alice, title="Blue umbrella", category="Other", type="lost")
        r = self.login_admin()
        self.assertIn("Good", r.get_data(as_text=True))          # admins land on the console home
        self.assertIn("To do", r.get_data(as_text=True))
        page = self.admin.get("/admin/search?q=umbrella").get_data(as_text=True)
        self.assertIn("Blue umbrella", page)
        page = self.admin.get("/admin/search?q=aarav").get_data(as_text=True)
        self.assertIn("Aarav Shetty", page)
        self.assertIn("Nothing found", self.admin.get("/admin/search?q=zzzzqqq").get_data(as_text=True))
        self.assertEqual(self.outsider.get("/admin/search?q=x").status_code, 302)
        self.assertEqual(self.alice.get("/admin/search?q=x").status_code, 403)

    def test_listing_flags_filter(self):
        self.setup_users()
        self.post_item(self.alice, title="Old umbrella")
        self.login_admin()
        con = appmod.sqlite3.connect(self.path)
        con.execute("UPDATE items SET created_at = '2020-01-01 10:00:00'"); con.commit(); con.close()
        self.assertIn("Old umbrella", self.admin.get("/admin/listings?flag=stale").get_data(as_text=True))
        self.assertNotIn("Old umbrella", self.admin.get("/admin/listings?flag=locked").get_data(as_text=True))
        self.assertIn("older than 30 days", self.admin.get("/admin/listings?flag=stale").get_data(as_text=True))

    def test_csv_export_neutralises_formulas(self):
        resp = None
        with appmod.app.test_request_context("/"):
            resp = appmod.csv_response("x.csv", ["a"], [("=HYPERLINK(1)",), ("ok",)])
        body = resp.get_data(as_text=True)
        self.assertIn("'=HYPERLINK(1)", body)
        self.assertIn("ok", body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
