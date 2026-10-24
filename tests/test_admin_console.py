"""Admin console reorganization: six-entry sidebar, People tabs, permissions, roster import report,
registration decisions, Items closure and the Staff Desk additions.

Run only these:   python -m unittest tests.test_admin_console
"""
import io
import re
import unittest

import app as appmod
from tests.test_app import PNG, PW
from tests.test_workflow import WF

SIDEBAR = ["Home", "Staff Desk", "Items", "People", "Verification Policy", "Activity Log"]
PEOPLE_TABS = ["Registered Users", "Registration Verification", "Campus Roster"]


def sidebar(html):
    nav = re.search(r'<nav class="adm-nav".*?</nav>', html, re.S).group(0)
    return re.findall(r"<span>([^<]+)</span>", nav), nav


class ConsoleBase(WF):
    def pending_request(self, name="New Joiner", id_number="n77", email="joiner@gmail.com"):
        t = self.token(self.outsider, "/register/manual")
        self.outsider.post("/register/manual", data={
            "csrf_token": t, "role": "student", "name": name, "id_number": id_number, "department": "Law",
            "email": email, "phone": "9876543210", "password": PW, "confirm_password": PW, "dob": "2003-02-11",
            "id_card": (io.BytesIO(PNG), "card.png")}, content_type="multipart/form-data")
        return self.user_id(id_number.upper())

    def upload_csv(self, text):
        t = self.token(self.admin, "/admin/roster")
        return self.admin.post("/admin/roster/upload", data={
            "csrf_token": t, "csv": (io.BytesIO(text.encode()), "r.csv")},
            content_type="multipart/form-data", follow_redirects=True)


class SidebarTests(ConsoleBase):
    def test_six_entries_in_order_on_every_admin_page(self):
        uid = self.user_id("S2301")
        for url in ("/admin", "/admin/users", "/admin/requests", "/admin/roster", "/admin/listings",
                    "/admin/policy", "/admin/audit", "/admin/search?q=aarav", f"/admin/users/{uid}",
                    "/staff/queue/pending_reports", "/staff/queue/needs_info"):
            labels, nav = sidebar(self.text(self.admin.get(url)))
            self.assertEqual(labels, SIDEBAR, url)
            self.assertEqual(nav.count("<a "), 6, url)
            for old in ("Approvals", "Campus list", "Search Results", "Member Profile"):
                self.assertNotIn(old, nav, url)

    def test_active_entry_is_highlighted(self):
        def active(url):
            _, nav = sidebar(self.text(self.admin.get(url)))
            return re.findall(r'aria-current="page"[^>]*>(?:<svg.*?</svg>)?<span>([^<]+)</span>', nav, re.S)
        self.assertEqual(active("/admin"), ["Home"])
        self.assertEqual(active("/staff/queue/disputes"), ["Staff Desk"])
        self.assertEqual(active("/admin/listings"), ["Items"])
        for url in ("/admin/users", "/admin/requests", "/admin/roster", f"/admin/users/{self.user_id('S2301')}"):
            self.assertEqual(active(url), ["People"], url)
        self.assertEqual(active("/admin/policy"), ["Verification Policy"])
        self.assertEqual(active("/admin/audit"), ["Activity Log"])
        self.assertEqual(active("/admin/search?q=aarav"), [])   # search is reached from the top bar only

    def test_staff_desk_keeps_badges_for_admin(self):
        appmod.MODERATION_REQUIRED = True
        self.found(title="Waiting report")
        labels, nav = sidebar(self.text(self.admin.get("/staff/queue/pending_reports")))
        self.assertRegex(nav, r"Staff Desk</span><b class=\"adm-count\">[1-9]")

    def test_global_search_is_still_in_the_top_bar(self):
        html = self.text(self.admin.get("/admin"))
        self.assertIn('action="/admin/search"', html)
        r = self.admin.get("/admin/search?q=Aarav")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/admin/users/{self.user_id('S2301')}", self.text(r))


class PeopleTabTests(ConsoleBase):
    def test_three_tabs_on_each_people_page(self):
        for url, current in (("/admin/users", "Registered Users"), ("/admin/requests", "Registration Verification"),
                             ("/admin/roster", "Campus Roster")):
            html = self.text(self.admin.get(url))
            tabs = re.search(r'<nav class="q-tabs people-tabs".*?</nav>', html, re.S).group(0)
            for label in PEOPLE_TABS:
                self.assertIn(label, tabs, url)
            self.assertRegex(tabs, r'aria-current="page">' + current, url)

    def test_old_urls_still_work_and_people_entry_redirects(self):
        for url in ("/admin/users", "/admin/requests", "/admin/roster", "/admin/roster/template.csv",
                    "/admin/roster/export.csv", "/admin/users/export.csv"):
            self.assertEqual(self.admin.get(url).status_code, 200, url)
        r = self.admin.get("/admin/people")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/admin/users"))
        r = self.admin.get("/admin/people?tab=verification")
        self.assertEqual(r.headers["Location"], "/admin/requests")
        r = self.admin.get("/admin/people?tab=roster&q=S23&role=student")
        self.assertEqual(r.headers["Location"], "/admin/roster?q=S23&role=student")
        r = self.admin.get("/admin/people?tab=//evil.example")
        self.assertEqual(r.headers["Location"], "/admin/users")

    def test_member_profile_is_a_detail_page_from_registered_users(self):
        uid = self.user_id("S2301")
        self.assertIn(f"/admin/users/{uid}", self.text(self.admin.get("/admin/users")))
        html = self.text(self.admin.get(f"/admin/users/{uid}"))
        self.assertIn("Registered Users", html)
        self.assertIn("Claims made", html)

    def test_tab_badge_shows_pending_registrations(self):
        self.pending_request()
        html = self.text(self.admin.get("/admin/roster"))
        tabs = re.search(r'<nav class="q-tabs people-tabs".*?</nav>', html, re.S).group(0)
        self.assertRegex(tabs, r"Registration Verification<b>1</b>")
        self.assertRegex(sidebar(html)[1], r"People</span><b class=\"adm-count\">1")


class PermissionTests(ConsoleBase):
    ADMIN_GETS = ("/admin", "/admin/users", "/admin/requests", "/admin/roster", "/admin/people", "/admin/listings",
                  "/admin/policy", "/admin/audit", "/admin/search?q=a", "/admin/roster/export.csv",
                  "/admin/users/export.csv", "/admin/listings/export.csv", "/admin/audit/export.csv")

    def test_students_and_visitors_are_blocked(self):
        for url in self.ADMIN_GETS:
            self.assertEqual(self.alice.get(url).status_code, 403, url)
            self.assertEqual(self.outsider.get(url).status_code, 302, url)

    def test_custody_officer_reaches_staff_desk_but_not_admin_pages(self):
        self.make_officer()
        self.assertEqual(self.staff.get("/staff/queue/needs_info").status_code, 200)
        for url in self.ADMIN_GETS:
            self.assertEqual(self.staff.get(url).status_code, 403, url)
        uid = self.user_id("S2301")
        t = self.token(self.staff, "/dashboard")
        for url in (f"/admin/users/{uid}/suspend", f"/admin/users/{uid}/officer", "/admin/roster/upload",
                    "/admin/roster/add", "/admin/policy", f"/admin/requests/{uid}/approve",
                    "/admin/listings/1/close", "/admin/listings/1/delete"):
            self.assertEqual(self.staff.post(url, data={"csrf_token": t}).status_code, 403, url)
        self.assertEqual(self.user_row("S2301")["status"], "active")

    def test_plain_staff_cannot_use_staff_desk(self):
        self.assertEqual(self.staff.get("/staff/queue/needs_info").status_code, 403)

    def test_posts_without_csrf_token_are_refused(self):
        uid = self.user_id("S2301")
        r = self.admin.post(f"/admin/users/{uid}/suspend", data={})
        self.assertIn(r.status_code, (400, 403))
        self.assertEqual(self.user_row("S2301")["status"], "active")


class RegistrationVerificationTests(ConsoleBase):
    def test_id_card_is_admin_only_and_never_cached(self):
        uid = self.pending_request()
        r = self.admin.get(f"/admin/idcard/{uid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers["Cache-Control"])
        self.assertEqual(self.alice.get(f"/admin/idcard/{uid}").status_code, 403)

    def test_a_request_can_only_be_decided_once(self):
        uid = self.pending_request()
        self.post(self.admin, f"/admin/requests/{uid}/approve", "/admin/requests")
        self.assertEqual(self.user_row("N77")["status"], "active")
        r = self.post(self.admin, f"/admin/requests/{uid}/reject", "/admin/requests", note="too late")
        self.assertIn("already handled", self.text(r))
        self.assertEqual(self.user_row("N77")["status"], "active")
        r = self.post(self.admin, f"/admin/requests/{uid}/approve", "/admin/requests")
        self.assertIn("already handled", self.text(r))
        actions = self.audit_actions()
        self.assertEqual(actions.count("request_approved"), 1)
        self.assertNotIn("request_rejected", actions)

    def test_rejection_records_reason_and_audit(self):
        uid = self.pending_request(name="Fake Person", id_number="x1", email="fake@mail.com")
        self.post(self.admin, f"/admin/requests/{uid}/reject", "/admin/requests", note="ID is not readable")
        row = self.user_row("X1")
        self.assertEqual((row["status"], row["review_note"]), ("rejected", "ID is not readable"))
        self.assertIn("ID is not readable", self.text(self.admin.get("/admin/audit")))

    def test_approval_never_changes_the_role(self):
        uid = self.pending_request()
        self.post(self.admin, f"/admin/requests/{uid}/approve", "/admin/requests")
        row = self.user_row("N77")
        self.assertEqual((row["role"], row["is_custody_officer"]), ("student", 0))


class RosterImportTests(ConsoleBase):
    CSV = ("id_number,full_name,email,role,department\n"
           "n501,New Student,new.student@college.edu,student,Law\n"
           "bad row without enough,,,,\n"
           "S2301,Aarav S Updated,aarav.shetty@campus.edu,student,CSE\n")

    def test_skipped_rows_are_explained(self):
        r = self.upload_csv(self.CSV.replace("student,CSE", "student,CSE,"))   # keep original counts
        html = self.text(r)
        self.assertIn("1 added, 1 updated, 1 skipped", html)
        self.assertIn("1 row in the last import was skipped", html)
        # CSV line 3, ID shown capped at 20 characters, with the first problem found as the reason
        self.assertRegex(html, r"<td>3</td><td>BAD ROW WITHOUT ENOU</td><td>Choose whether the person is a student or a staff member\.</td>")
        self.assertNotIn("1 row in the last import", self.text(self.admin.get("/admin/roster")))   # shown once

    def test_duplicate_id_inside_a_file_is_imported_once(self):
        csv_text = ("id_number,full_name,email,role,department\n"
                    "S9001,First Copy,first@campus.edu,student,Law\n"
                    "s9001,Second Copy,second@campus.edu,student,Law\n")
        html = self.text(self.upload_csv(csv_text))
        self.assertIn("1 added, 0 updated, 1 skipped", html)
        self.assertIn("appears more than once", html)
        row = self.one("SELECT full_name, email FROM roster WHERE id_number = 'S9001'")
        self.assertEqual((row["full_name"], row["email"]), ("First Copy", "first@campus.edu"))
        self.assertEqual(self.one("SELECT COUNT(*) AS c FROM roster WHERE id_number = 'S9001'")["c"], 1)

    def test_error_list_is_capped_and_counted(self):
        bad = "".join(f"B{n},,x,student,\n" for n in range(25))
        html = self.text(self.upload_csv("id_number,full_name,email,role,department\n" + bad))
        self.assertIn("25 rows in the last import were skipped", html)
        self.assertIn("Showing the first 10 of 25", html)

    def test_missing_columns_and_empty_choice_are_reported(self):
        self.assertIn("needs these columns", self.text(self.upload_csv("a,b\n1,2\n")))
        r = self.post(self.admin, "/admin/roster/upload", "/admin/roster")
        self.assertIn("Choose a CSV file", self.text(r))

    def test_import_roster_signature_stays_compatible(self):
        with appmod.app.app_context():
            self.assertEqual(appmod.import_roster(appmod.get_db(), "id_number,full_name,email,role\n"), (0, 0, 0))
            problems = []
            out = appmod.import_roster(appmod.get_db(), "id_number,full_name,email,role\nQ1,,,\n", problems)
        self.assertEqual(out, (0, 0, 1))
        self.assertEqual(problems[0][0], 2)


class ItemsPageTests(ConsoleBase):
    def test_close_keeps_the_record_and_needs_a_reason(self):
        i = self.found(title="Duplicate umbrella")
        r = self.post(self.admin, f"/admin/listings/{i}/close", "/admin/listings", reason="")
        self.assertIn("at least 3", self.text(r))
        self.assertEqual(self.item(i)["status"], "active")
        r = self.post(self.admin, f"/admin/listings/{i}/close", "/admin/listings", reason="Duplicate report")
        self.assertIn("The report was closed", self.text(r))
        row = self.item(i)
        self.assertEqual((row["status"], row["closure_reason"]), ("closed", "Duplicate report"))
        self.assertIn("report_closed", self.audit_actions())
        self.assertIn("Duplicate umbrella", self.text(self.admin.get("/admin/listings?status=closed")))
        r = self.post(self.admin, f"/admin/listings/{i}/close", "/admin/listings", reason="Again")
        self.assertIn("already finished", self.text(r))

    def test_close_button_only_for_open_reports(self):
        i = self.found(title="Open thing")
        html = self.text(self.admin.get("/admin/listings"))
        self.assertIn(f"/admin/listings/{i}/close", html)
        self.post(self.admin, f"/admin/listings/{i}/close", "/admin/listings", reason="Not needed")
        self.assertNotIn(f"/admin/listings/{i}/close", self.text(self.admin.get("/admin/listings")))

    def test_filters_and_export_still_work(self):
        self.found(title="Alpha lost thing")
        self.post_item(self.bob, type="lost", title="Beta lost thing", category="Other", handover="")
        self.assertIn("Beta lost thing", self.text(self.admin.get("/admin/listings?type=lost")))
        self.assertNotIn("Alpha lost thing", self.text(self.admin.get("/admin/listings?type=lost")))
        csv_text = self.text(self.admin.get("/admin/listings/export.csv?type=found"))
        self.assertIn("Alpha lost thing", csv_text)
        self.assertNotIn("Beta lost thing", csv_text)

    def test_students_cannot_close_through_the_admin_route(self):
        i = self.found(title="Mine")
        t = self.token(self.alice, "/dashboard")
        self.assertEqual(self.alice.post(f"/admin/listings/{i}/close", data={"csrf_token": t, "reason": "x y z"}).status_code, 403)
        self.assertEqual(self.item(i)["status"], "active")


class StaffDeskTests(ConsoleBase):
    def test_awaiting_more_information_tab(self):
        self.make_officer()
        i = self.found(category="Electronics", title="Phone", handover="admin")
        self.claim(self.bob, i)
        cid = self.claim_id(i, "S2302")
        self.assertNotIn("Phone", self.text(self.staff.get("/staff/queue/needs_info")))
        self.staff_act(self.staff, cid, "request_info", i, reason="What is the lock screen picture?")
        page = self.text(self.staff.get("/staff/queue/needs_info"))
        self.assertIn("Phone", page)
        self.assertIn("Waiting for details", page)
        self.post(self.bob, f"/claim/{cid}/respond", f"/item/{i}", message="A photo of a beach")
        self.assertNotIn("Phone", self.text(self.staff.get("/staff/queue/needs_info")))

    def test_all_tabs_listed_with_counts_and_lookup_everywhere(self):
        html = self.text(self.admin.get("/staff/queue/completed"))
        for label in ("New reports", "Items to receive", "Claims to check",
                      "Waiting for details", "High-risk claims", "Handover problems",
                      "Ready to give back", "Given back"):
            self.assertIn(label, html)
        self.assertIn('action="/staff/lookup"', html)

    def test_custody_receipt_still_needs_an_authorized_officer(self):
        i = self.found(handover="admin")
        r = self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertEqual(self.item(i)["custody_status"], "admin_handover_requested")
        self.make_officer()
        self.post(self.staff, f"/item/{i}/receive", f"/item/{i}", confirm="1")
        self.assertEqual(self.item(i)["custody_status"], "with_admin")


class PolicyAndHomeTests(ConsoleBase):
    def test_policy_page_sections_and_honest_scope(self):
        html = self.text(self.admin.get("/admin/policy"))
        self.assertIn("College identity is not proof of ownership", html)
        for heading in ("Item categories and risk levels", "Who verifies", "Claim review and escalation",
                        "Abuse-prevention limits"):
            self.assertIn(heading, html)
        self.assertIn("cannot be edited from this page", html)
        self.assertNotIn("straight away", html)
        self.assertEqual(self.alice.get("/admin/policy").status_code, 403)

    def test_policy_change_affects_new_reports_only(self):
        before = self.found(category="Electronics", title="Old laptop")
        self.assertEqual(self.item(before)["risk_level"], "high")
        self.post(self.admin, "/admin/policy", "/admin/policy", risk_Electronics="low")
        after = self.found(category="Electronics", title="New laptop")
        self.assertEqual(self.item(after)["risk_level"], "low")
        self.assertEqual(self.item(before)["risk_level"], "high")

    def test_home_summarises_and_links_to_the_new_places(self):
        self.found(title="One found")
        self.post_item(self.bob, type="lost", title="One lost", category="Other", handover="")
        html = self.text(self.admin.get("/admin"))
        self.assertIn("Reports in total", html)
        self.assertRegex(html, r"1 lost · 1 found")
        for old in ("Campus list", "Approvals"):
            self.assertNotIn(old, html)
        for label in ("Staff Desk", "Items", "People", "Verification Policy", "Activity Log"):
            self.assertIn(f"<strong>{label}</strong>", html)

    def test_activity_log_search_filter_export(self):
        self.post(self.admin, f"/admin/users/{self.user_id('S2301')}/suspend", "/admin/users", reason="spam")
        self.assertIn("User suspended", self.text(self.admin.get("/admin/audit?action=user_suspended")))
        self.assertIn("spam", self.text(self.admin.get("/admin/audit?q=spam")))
        csv_text = self.text(self.admin.get("/admin/audit/export.csv?action=user_suspended"))
        self.assertIn("user_suspended", csv_text)
        self.assertNotIn("Secret123", csv_text)


class RouteReferenceTests(unittest.TestCase):
    def test_every_url_for_in_templates_and_code_exists(self):
        import pathlib
        endpoints = {r.endpoint for r in appmod.app.url_map.iter_rules()}
        root = pathlib.Path(appmod.__file__).parent
        sources = list((root / "templates").glob("*.html")) + [root / "app.py"]
        missing = set()
        for path in sources:
            for name in re.findall(r"url_for\(\s*['\"]([a-z_0-9]+)['\"]", path.read_text(encoding="utf-8")):
                if name not in endpoints:
                    missing.add((path.name, name))
        self.assertEqual(missing, set())


class RosterDepartmentTests(ConsoleBase):
    """Department is a required dropdown in the roster dialogs; 'Other' takes typed text."""

    def add(self, **over):
        data = dict(id_number="d1", full_name="Dee Dee", email="dee@college.edu", role="staff",
                    department="Library")
        data.update(over)
        return self.post(self.admin, "/admin/roster/add", "/admin/roster", **data).get_data(as_text=True)

    def _dept(self, id_number):
        with appmod.app.app_context():
            row = appmod.get_db().execute("SELECT department FROM roster WHERE id_number = ?",
                                          (id_number,)).fetchone()
        return row["department"] if row else None

    def test_dialogs_show_required_dropdown_with_other_and_placeholder(self):
        page = self.admin.get("/admin/roster").get_data(as_text=True)
        for prefix in ("ra", "re"):
            self.assertRegex(page, rf'<select id="{prefix}-dept" name="department" required data-roster-dept>')
            self.assertIn(f'id="{prefix}-dept-other"', page)
        self.assertIn('value="__other__"', page)
        self.assertIn('placeholder="Type department"', page)
        self.assertNotIn("(optional)", re.search(r'id="dlg-roster-add".*?</dialog>', page, re.S).group(0))

    def test_add_without_department_is_rejected(self):
        self.assertIn("Choose the department", self.add(department=""))
        self.assertIsNone(self._dept("D1"))

    def test_add_other_without_text_is_rejected(self):
        self.assertIn("Choose the department", self.add(department="__other__", department_other=" "))
        self.assertIsNone(self._dept("D1"))

    def test_add_other_with_text_is_saved(self):
        self.assertIn("Roster entry saved", self.add(department="__other__", department_other="Zoology"))
        self.assertEqual(self._dept("D1"), "Zoology")
        # and it now appears in the registration dropdown
        self.assertIn("Zoology", self.outsider.get("/register").get_data(as_text=True))

    def test_edit_to_other_and_blank(self):
        self.add()
        with appmod.app.app_context():
            rid = appmod.get_db().execute("SELECT id FROM roster WHERE id_number = 'D1'").fetchone()["id"]
        base = dict(full_name="Dee Dee", email="dee@college.edu", role="staff")
        r = self.post(self.admin, f"/admin/roster/{rid}/edit", "/admin/roster", department="", **base)
        self.assertIn("Choose the department", r.get_data(as_text=True))
        self.assertEqual(self._dept("D1"), "Library")
        self.post(self.admin, f"/admin/roster/{rid}/edit", "/admin/roster",
                  department="__other__", department_other="Botany", **base)
        self.assertEqual(self._dept("D1"), "Botany")

    def test_csv_row_without_department_is_skipped_with_reason(self):
        html = self.upload_csv("id_number,full_name,email,role,department\nQ1,Quin Quo,quin@college.edu,staff,\n"
                               ).get_data(as_text=True)
        self.assertIn("Choose the department", html)
        self.assertIsNone(self._dept("Q1"))


if __name__ == "__main__":
    unittest.main()
