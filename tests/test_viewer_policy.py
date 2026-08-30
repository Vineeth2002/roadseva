"""
tests/test_viewer_policy.py -- Policy B: viewer = city-wide read-only
========================================================================
Proves the viewer policy resolution (city-wide READ, per
templates/staff.html's own "You can see all grievances" banner) did not
accidentally grant any write capability. Viewer's route-level
restrictions (permissions.py's role checks, unchanged by this policy
decision) are what actually block writes -- these tests confirm that
boundary held.

Run with: pytest tests/test_viewer_policy.py -v
"""

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import authz


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_viewer_policy.db")
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


def login(role, username, **kw):
    from main import app
    staff = make_staff(role, username, **kw)
    token = database.create_session(staff["id"])
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("session_token", token)
    return c, staff


def get_csrf(client, page=None):
    """viewer's /staff view has no write forms (nothing for it to do
    there), so scraping a CSRF token from a page isn't reliable for this
    role -- generate one directly from the session cookie instead,
    matching security.generate_csrf_token()'s own signature."""
    from security import generate_csrf_token
    session_token = list(client.cookies.jar)[0].value
    return generate_csrf_token(session_token)


def make_report(ward="Ward 1 - Kondapeta / Wilsonpeta"):
    return database.add_report(
        city="GVMC", ward=ward, damage_type="Pothole", description="test",
        photo_path="", citizen_name="Citizen", citizen_phone="9999999999",
        citizen_email="", latitude=17.0, longitude=83.0, photo_data="",
    )


class TestViewerCityWideRead:

    def test_viewer_sees_reports_from_every_ward(self):
        make_staff("viewer", "v1", ward_list="")
        make_report("Ward 1 - Kondapeta / Wilsonpeta")
        make_report("Ward 4 - Pedda Uppada / Chepaluppada")
        results = database.get_reports_for_role(database.get_staff_by_username("v1"))
        wards = {r["ward"] for r in results}
        assert len(wards) == 2

    def test_viewer_can_open_complaint_story_for_any_ward(self):
        client, viewer = login("viewer", "v2")
        rid = make_report("Ward 4 - Pedda Uppada / Chepaluppada")
        resp = client.get(f"/complaint-story/{rid}", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_behavior_unaffected(self):
        client, admin = login("admin", "admin1")
        rid = make_report()
        resp = client.get(f"/complaint-story/{rid}", follow_redirects=False)
        assert resp.status_code == 200

    def test_ae_scope_unaffected(self):
        ae = {"role": "ae", "name": "AE1", "division": "Bheemunipatnam"}
        outside_division = "A ward definitely outside Bheemunipatnam"
        report = {"report_id": "R", "ward": outside_division, "assigned_to": ""}
        assert authz.can_access_report(ae, report) is False

    def test_was_scope_unaffected(self):
        was = {"role": "was", "name": "W1", "ward_list": "Ward 1"}
        report = {"report_id": "R", "ward": "Ward 2", "assigned_to": ""}
        assert authz.can_access_report(was, report) is False

    def test_zonal_commissioner_scope_unaffected(self):
        zc = {"role": "zonal_commissioner", "name": "Z1", "zone": "Zone 1"}
        report = {"report_id": "R", "ward": "A ward outside Zone 1", "assigned_to": ""}
        assert authz.can_access_report(zc, report) is False

    def test_unknown_role_still_fails_closed(self):
        assert authz.staff_ward_scope({"role": "made_up", "name": "X"}) == []
        report = {"report_id": "R", "ward": "Any Ward", "assigned_to": ""}
        assert authz.can_access_report({"role": "made_up", "name": "X"}, report) is False


class TestViewerCannotWrite:

    def test_viewer_cannot_update_status(self):
        client, viewer = login("viewer", "v3")
        rid = make_report()
        csrf = get_csrf(client)
        client.post("/update-status",
            data={"report_id": rid, "new_status": "closed", "csrf": csrf},
            follow_redirects=False)
        report = database.get_report_by_id(rid)
        assert report["status"] != "closed"

    def test_viewer_cannot_assign_report(self):
        client, viewer = login("viewer", "v4")
        fe = make_staff("field_engineer", "fe_target")
        rid = make_report()
        csrf = get_csrf(client)
        client.post("/assign-report",
            data={"report_id": rid, "assigned_to": fe["name"], "csrf": csrf},
            follow_redirects=False)
        report = database.get_report_by_id(rid)
        assert report.get("assigned_to") != fe["name"]

    def test_viewer_cannot_add_comment(self):
        client, viewer = login("viewer", "v5")
        rid = make_report()
        csrf = get_csrf(client)
        client.post("/add-comment",
            data={"report_id": rid, "comment": "sneaky comment", "csrf": csrf},
            follow_redirects=False)
        story = database.get_complaint_story(rid)
        comments = story.get("comments", []) if isinstance(story, dict) else []
        assert not any("sneaky comment" in str(c) for c in comments)

    def test_viewer_cannot_reset_staff_password(self):
        client, viewer = login("viewer", "v6")
        fe = make_staff("field_engineer", "fe_target2")
        before_hash = fe["password_hash"]
        csrf = get_csrf(client)
        client.post("/team/reset-password",
            data={"staff_id": fe["id"], "csrf": csrf}, follow_redirects=False)
        after = database.get_staff_by_username("fe_target2")
        assert after["password_hash"] == before_hash

    def test_viewer_cannot_toggle_staff(self):
        client, viewer = login("viewer", "v7")
        fe = make_staff("field_engineer", "fe_target3")
        csrf = get_csrf(client)
        client.post("/team/toggle",
            data={"staff_id": fe["id"], "csrf": csrf}, follow_redirects=False)
        after = database.get_staff_by_username("fe_target3")
        assert after["is_active"] == 1

    def test_viewer_cannot_add_staff_member(self):
        client, viewer = login("viewer", "v8")
        csrf = get_csrf(client)
        client.post("/team/add-member", data={
            "name": "Sneaky", "username": "sneaky_add", "role": "field_engineer",
            "temp_password": "Whatever@2026!", "csrf": csrf,
        }, follow_redirects=False)
        assert database.get_staff_by_username("sneaky_add") is None

    def test_viewer_cannot_verify_inspection(self):
        allowed, is_override, err = authz.can_modify_inspection(
            {"role": "viewer", "name": "v9"},
            {"report_id": "R", "ward": "Ward 1", "assigned_to": "someone else", "escalation_level": 0}
        )
        assert allowed is False


class TestViewerRedirectUnchanged:

    def test_viewer_denied_admin_only_route(self):
        client, viewer = login("viewer", "v10")
        resp = client.get("/admin", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers.get("location") != "/admin"
