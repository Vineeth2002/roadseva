"""
tests/test_complaint_story.py — GET /complaint-story/{report_id} IDOR fix
============================================================================
Proves complaint-story now enforces authz.can_access_report() instead of
only checking that someone is logged in.

Run with: pytest tests/test_complaint_story.py -v
"""

import os
import re
import sys

import pytest

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import authz


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_complaint_story.db")
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


def make_staff(role, username, **kw):
    ok, result = database.add_staff(
        name=f"Test {role.title()}", username=username, password="TestPass@2024!",
        role=role, created_by="test_suite", must_change=False,
        zone=kw.get("zone", ""), division=kw.get("division", ""),
        ward_list=kw.get("ward_list", ""),
    )
    assert ok, f"setup failed: {result}"
    return database.get_staff_by_username(username)


def make_report(ward, assigned_to=""):
    rid = database.add_report(
        city="GVMC", ward=ward, damage_type="Pothole", description="test",
        photo_path="", citizen_name="Citizen", citizen_phone="9999999999",
        citizen_email="", latitude=17.0, longitude=83.0, photo_data="",
    )
    if assigned_to:
        database.assign_report(rid, assigned_to, "test_suite")
    return rid


WARD_A = "Ward 1 - Kondapeta / Wilsonpeta"
WARD_B = "Ward 4 - Pedda Uppada / Chepaluppada"


# ═══════════════════════════════════════════════════════════════════════════
# DB-level: authz.can_access_report() directly against real report/staff rows
# ═══════════════════════════════════════════════════════════════════════════

class TestCanAccessReportForComplaintStory:

    def test_admin_can_access_any_ward(self):
        admin = make_staff("admin", "admin1")
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(admin, report) is True

    def test_commissioner_can_access_any_ward(self):
        comm = make_staff("commissioner", "comm1")
        rid = make_report(WARD_B)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(comm, report) is True

    def test_zonal_commissioner_inside_zone_allowed(self):
        from wards import get_wards_for_zone, ZONE_DIVISION_MAP
        # Skip gracefully if zone data isn't populated (pre-existing gap,
        # flagged separately, not something this fix depends on)
        zone_wards = get_wards_for_zone(next(iter(ZONE_DIVISION_MAP), ""))
        if not zone_wards:
            pytest.skip("ZONE_DIVISION_MAP is empty -- pre-existing wards.py gap, unrelated to this fix")
        zc = make_staff("zonal_commissioner", "zc1", zone=next(iter(ZONE_DIVISION_MAP)))
        rid = make_report(zone_wards[0])
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(zc, report) is True

    def test_zonal_commissioner_outside_zone_denied(self):
        zc = make_staff("zonal_commissioner", "zc1", zone="Zone 1")
        rid = make_report("A ward definitely outside Zone 1")
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(zc, report) is False

    def test_ae_inside_division_allowed(self):
        from wards import get_wards_for_division
        division_wards = get_wards_for_division("Bheemunipatnam")
        assert division_wards, "Bheemunipatnam division must have wards for this test to be meaningful"
        ae = make_staff("ae", "ae1", division="Bheemunipatnam")
        rid = make_report(division_wards[0])
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(ae, report) is True

    def test_ae_outside_division_denied(self):
        ae = make_staff("ae", "ae1", division="Bheemunipatnam")
        rid = make_report("A ward definitely outside Bheemunipatnam")
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(ae, report) is False

    def test_was_outside_ward_list_denied(self):
        was = make_staff("was", "was1", ward_list=WARD_A)
        rid = make_report(WARD_B)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(was, report) is False

    def test_was_inside_ward_list_allowed(self):
        was = make_staff("was", "was1", ward_list=WARD_A)
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(was, report) is True

    def test_field_engineer_outside_assignment_denied(self):
        fe1 = make_staff("field_engineer", "fe1")
        make_staff("field_engineer", "fe2")
        rid = make_report(WARD_A, assigned_to="Test Field_Engineer")  # assigned to fe2's display name pattern differs; assign explicitly below
        database.assign_report(rid, fe1["name"], "test_suite")
        # Now reassign to someone else entirely so fe1 is NOT the assignee
        rid2 = make_report(WARD_A)
        database.assign_report(rid2, "Someone Else Entirely", "test_suite")
        report2 = database.get_report_by_id(rid2)
        assert authz.can_access_report(fe1, report2) is False

    def test_field_engineer_own_assignment_allowed(self):
        fe1 = make_staff("field_engineer", "fe1")
        rid = make_report(WARD_A)
        database.assign_report(rid, fe1["name"], "test_suite")
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(fe1, report) is True

    def test_viewer_can_access_report_outside_ward_list_city_wide(self):
        """POLICY UPDATE: viewer is city-wide read-only -- this is the
        selected product policy, not an IDOR. A viewer's configured
        ward_list (if any) no longer restricts complaint-story access."""
        viewer = make_staff("viewer", "viewer1", ward_list=WARD_A)
        rid = make_report(WARD_B)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(viewer, report) is True

    def test_viewer_inside_ward_list_still_allowed(self):
        viewer = make_staff("viewer", "viewer1", ward_list=WARD_A)
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(viewer, report) is True

    def test_unknown_role_denied(self):
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report({"role": "made_up_role", "name": "X"}, report) is False

    def test_viewer_empty_ward_list_still_unrestricted(self):
        """Confirms the fail-closed principle now applies correctly to
        viewer's NEW policy: an empty ward_list must NOT deny access,
        because ward_list is no longer what determines viewer's scope."""
        viewer = make_staff("viewer", "viewer_empty", ward_list="")
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(viewer, report) is True

    def test_was_empty_scope_still_denied_not_unrestricted(self):
        """The general fail-closed principle, verified against the role
        it actually still applies to (was), now that viewer has moved to
        the unrestricted policy above. add_staff() itself blocks creating
        a real WAS account with no ward, so this exercises authz directly
        against a synthetic staff dict, matching tests/test_authz.py's
        pattern, rather than the DB layer."""
        was_staff = {"role": "was", "name": "was_empty", "ward_list": ""}
        rid = make_report(WARD_A)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(was_staff, report) is False

    def test_grievance_officer_matches_existing_unscoped_list_behavior(self):
        """Deliberate consistency choice, not a new grant: these roles
        already have unrestricted list-level access via
        get_reports_for_role(); complaint-story must not become MORE
        restrictive than that existing (separately-flagged) behavior."""
        go = make_staff("grievance_officer", "go1")
        rid = make_report(WARD_B)
        report = database.get_report_by_id(rid)
        assert authz.can_access_report(go, report) is True


# ═══════════════════════════════════════════════════════════════════════════
# HTTP-level: full route, real session, IDOR reproduction
# ═══════════════════════════════════════════════════════════════════════════

class TestComplaintStoryRouteEndToEnd:

    def _login(self, role, username, **kw):
        from fastapi.testclient import TestClient
        from main import app
        staff = make_staff(role, username, **kw)
        token = database.create_session(staff["id"])
        c = TestClient(app, raise_server_exceptions=False)
        c.cookies.set("session_token", token)
        return c, staff

    def test_unauthenticated_request_rejected(self):
        from fastapi.testclient import TestClient
        from main import app
        rid = make_report(WARD_A)
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get(f"/complaint-story/{rid}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers.get("location") == "/login"

    def test_original_idor_reproduction_is_now_blocked(self):
        """THE regression test: a low-privilege staff account (was),
        scoped to Ward A, changes report_id in the URL to a report that
        actually belongs to Ward B. Before this fix, this succeeded --
        any authenticated staff, any role, could view any report_id."""
        client, was_staff = self._login("was", "was_attacker", ward_list=WARD_A)
        other_ward_report_id = make_report(WARD_B)

        resp = client.get(f"/complaint-story/{other_ward_report_id}", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers.get("location") != f"/complaint-story/{other_ward_report_id}"
        assert "staff" in resp.headers.get("location", "")

    def test_was_can_still_view_own_ward_report(self):
        """Confirms the fix isn't a blanket denial -- legitimate access
        still works."""
        client, was_staff = self._login("was", "was_legit", ward_list=WARD_A)
        own_ward_report_id = make_report(WARD_A)

        resp = client.get(f"/complaint-story/{own_ward_report_id}", follow_redirects=False)

        assert resp.status_code == 200

    def test_admin_can_view_any_report_via_http(self):
        client, admin = self._login("admin", "admin_http")
        rid = make_report(WARD_B)
        resp = client.get(f"/complaint-story/{rid}", follow_redirects=False)
        assert resp.status_code == 200

    def test_nonexistent_report_unchanged_response(self):
        """Existing behavior for a missing report_id (redirect to /staff)
        must be unaffected by this fix."""
        client, admin = self._login("admin", "admin_http2")
        resp = client.get("/complaint-story/GVMC-9999-NOTREAL", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers.get("location") == "/staff"

    def test_viewer_assigned_ward_a_can_view_ward_b_report_via_http(self):
        """THE selected-policy test: viewer is city-wide read-only. A
        viewer configured with Ward A's ward_list requesting a Ward B
        report succeeds -- intentionally, per Policy B, not an IDOR."""
        client, viewer = self._login("viewer", "viewer_http", ward_list=WARD_A)
        rid = make_report(WARD_B)
        resp = client.get(f"/complaint-story/{rid}", follow_redirects=False)
        assert resp.status_code == 200