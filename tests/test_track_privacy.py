"""
tests/test_track_privacy.py — /track public exposure fix
============================================================
Proves exact GPS coordinates, the raw complaint photo, and the assigned
staff member's name are no longer rendered to unauthenticated visitors,
while status, ward, damage type, and the citizen-facing tracking flow
remain fully public and functional.

Run with: pytest tests/test_track_privacy.py -v
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_track.db")
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


EXACT_LAT = 17.712345
EXACT_LNG = 83.318765
FAKE_PHOTO_B64 = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/thisisnotarealimagebutlooksliketagcontent"


def make_report(assigned_to="", status="assigned"):
    rid = database.add_report(
        city="GVMC", ward="Ward 1 - Kondapeta / Wilsonpeta", damage_type="Pothole",
        description="A pothole near the bus stop", photo_path="",
        citizen_name="Real Citizen Name", citizen_phone="9876543210",
        citizen_email="citizen@example.com",
        latitude=EXACT_LAT, longitude=EXACT_LNG, photo_data=FAKE_PHOTO_B64,
    )
    if assigned_to:
        database.assign_report(rid, assigned_to, "test_suite")
    if status != "open":
        database.update_report_status(rid, status, "test_suite")
    return rid


def make_staff(role, username, **kw):
    ok, result = database.add_staff(
        name=f"Test {role.title()}", username=username, password="TestPass@2024!",
        role=role, created_by="test_suite", must_change=False,
        zone=kw.get("zone", ""), division=kw.get("division", ""),
        ward_list=kw.get("ward_list", ""),
    )
    assert ok, f"setup failed: {result}"
    return database.get_staff_by_username(username)


def get(client, report_id):
    return client.get(f"/track?report_id={report_id}")


class TestUnauthenticatedTrackExposure:

    def test_track_remains_publicly_accessible(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert resp.status_code == 200

    def test_status_still_shown_publicly(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report(status="assigned")
        resp = get(client, rid)
        assert "assigned" in resp.text.lower() or "field engineer has been assigned" in resp.text.lower()

    def test_ward_still_shown_publicly(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "Ward 1 - Kondapeta" in resp.text

    def test_damage_type_still_shown_publicly(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "Pothole" in resp.text

    def test_exact_latitude_not_rendered(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert str(EXACT_LAT) not in resp.text

    def test_exact_longitude_not_rendered(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert str(EXACT_LNG) not in resp.text

    def test_google_maps_link_with_coordinates_absent(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "google.com/maps" not in resp.text

    def test_raw_photo_not_rendered(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert FAKE_PHOTO_B64 not in resp.text
        assert '<img class="photo-img"' not in resp.text

    def test_phone_not_exposed(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "9876543210" not in resp.text

    def test_email_not_exposed(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "citizen@example.com" not in resp.text

    def test_citizen_name_still_gated(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report()
        resp = get(client, rid)
        assert "Real Citizen Name" not in resp.text

    def test_assigned_staff_name_not_exposed(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report(assigned_to="Test Field_Engineer", status="assigned")
        resp = get(client, rid)
        assert "Test Field_Engineer" not in resp.text
        assert "field engineer has been assigned" in resp.text.lower()

    def test_assigned_staff_name_not_exposed_while_inspecting(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report(assigned_to="Test Field_Engineer", status="inspecting")
        resp = get(client, rid)
        assert "Test Field_Engineer" not in resp.text

    def test_nonexistent_report_behavior_unchanged(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = get(client, "GVMC-9999-NOTREAL")
        assert resp.status_code == 200
        assert "not found" in resp.text.lower() or "Complaint not found" in resp.text


class TestOriginalExposureReproduction:

    def test_unauthenticated_visitor_with_valid_id_gets_no_gps_or_photo(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        rid = make_report(assigned_to="Some Real Engineer Name")
        resp = get(client, rid)

        assert resp.status_code == 200
        assert str(EXACT_LAT) not in resp.text
        assert str(EXACT_LNG) not in resp.text
        assert FAKE_PHOTO_B64 not in resp.text
        assert "Some Real Engineer Name" not in resp.text
        assert "Pothole" in resp.text


class TestAuthenticatedStaffStillSeeFullDetail:

    def test_logged_in_staff_sees_exact_gps(self):
        from main import app
        staff = make_staff("admin", "admin_track")
        token = database.create_session(staff["id"])
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)

        rid = make_report()
        resp = get(client, rid)
        assert str(EXACT_LAT) in resp.text

    def test_logged_in_staff_sees_photo(self):
        from main import app
        staff = make_staff("admin", "admin_track2")
        token = database.create_session(staff["id"])
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)

        rid = make_report()
        resp = get(client, rid)
        assert FAKE_PHOTO_B64 in resp.text

    def test_logged_in_staff_sees_assigned_engineer_name(self):
        from main import app
        staff = make_staff("admin", "admin_track3")
        token = database.create_session(staff["id"])
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)

        rid = make_report(assigned_to="Visible To Staff Only")
        resp = get(client, rid)
        assert "Visible To Staff Only" in resp.text

    def test_logged_in_staff_still_sees_citizen_name(self):
        from main import app
        staff = make_staff("admin", "admin_track4")
        token = database.create_session(staff["id"])
        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set("session_token", token)

        rid = make_report()
        resp = get(client, rid)
        assert "Real Citizen Name" in resp.text


class TestStorageUnaffected:

    def test_report_record_still_has_real_coordinates_in_db(self):
        rid = make_report()
        report = database.get_report_by_id(rid)
        assert report["latitude"] == EXACT_LAT
        assert report["longitude"] == EXACT_LNG
        assert report["photo_data"] == FAKE_PHOTO_B64