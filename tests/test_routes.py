"""
tests/test_routes.py — RoadSeva Route Integration Tests
=========================================================
Run with: pytest tests/test_routes.py -v

Tests cover:
  1. GET /track?ref=ID — auto-search via query param
  2. GET /track — empty form when no ref param
  3. POST /submit — rate limit applied
  4. GET /health and /health/ready
  5. GET /citizen-review/ID — 404 if not resolved
  6. POST /track — manual search works
  7. GET / — home page loads

Setup: TestClient with SQLite in-memory, no real Twilio/Cloudinary called.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

os.environ.pop("DATABASE_URL", None)
os.environ["ENVIRONMENT"] = "test"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test.db")
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """security._limiter is process-global, in-memory, and real-time
    (30 req/sec/IP burst window) — every test in this file shares one
    bucket under TestClient's fixed 'testclient' IP. Test-only reset,
    does not touch security.py's production rate-limiting logic. Without
    this, test execution speed (e.g. a faster app-startup path) can trip
    the shared burst limit and fail unrelated tests with 429s — a
    pre-existing fragility in the limiter's design, not a route bug."""
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


@pytest.fixture
def client(fresh_db):
    from main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def sample_report_id(fresh_db):
    """Creates a real complaint in the test DB and returns its ID."""
    rid = database.add_report(
        city="GVMC",
        ward="Ward 1 - Kondapeta / Wilsonpeta",
        damage_type="Pothole",
        description="Test pothole near bus stop",
        photo_path="",
        citizen_name="Test Citizen",
        citizen_phone="9848012345",
        citizen_email="",
        latitude=17.7226,
        longitude=83.3182,
        severity="unknown",
        photo_data="",
    )
    return rid

@pytest.fixture
def authed_client(fresh_db):
    """
    Factory fixture: call authed_client(role) to get a TestClient
    logged in as a real staff account with that role.
    Creates the account fresh in the test DB, logs in via create_session()
    (same mechanism main.py's real /login route uses), and sets the
    session cookie — mirrors an actual browser session.
    """
    from main import app

    def _make(role: str, **kwargs):
        username = f"test_{role}"
        ok, result = database.add_staff(
            name=f"Test {role.title()}",
            username=username,
            password="TestPass@2024!",
            role=role,
            created_by="test_suite",
            zone=kwargs.get("zone", "Zone 1" if role == "zonal_commissioner" else ""),
            division=kwargs.get("division", "Bheemunipatnam" if role == "ae" else ""),
            ward_list=kwargs.get("ward_list", "Ward 1 - Kondapeta / Wilsonpeta" if role == "was" else ""),
        )
        assert ok, f"Failed to create test {role} account: {result}"

        staff = database.get_staff_by_username(username)
        token = database.create_session(staff["id"])

        c = TestClient(app, raise_server_exceptions=False)
        c.cookies.set("session_token", token)
        return c

    return _make

# ═══════════════════════════════════════════════════════════════════════════════
# /track GET — auto-search via ?ref= param
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrackGet:

    def test_track_with_valid_ref_shows_complaint(self, client, sample_report_id):
        """
        GET /track?ref=GVMC-XXXX-XXXXXX should auto-search and
        render the complaint card — not an empty search form.
        """
        response = client.get(f"/track?ref={sample_report_id}")
        assert response.status_code == 200
        assert sample_report_id in response.text

    def test_track_with_invalid_ref_shows_not_found(self, client):
        """GET /track?ref=GVMC-9999-XXXXXX should show not-found message."""
        response = client.get("/track?ref=GVMC-9999-XXXXXX")
        assert response.status_code == 200
        assert "not found" in response.text.lower() or "No Complaint" in response.text

    def test_track_without_ref_shows_empty_form(self, client):
        """GET /track with no params should show the search form, not search results."""
        response = client.get("/track")
        assert response.status_code == 200
        assert "Grievance Number" in response.text or "grievance" in response.text.lower()

    def test_track_ref_is_case_insensitive_after_upper(self, client, sample_report_id):
        """?ref= param is uppercased in the route — lowercase input should still work."""
        response = client.get(f"/track?ref={sample_report_id.lower()}")
        assert response.status_code == 200
        assert sample_report_id in response.text


# ═══════════════════════════════════════════════════════════════════════════════
# /track POST — manual search
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrackPost:

    def test_post_track_finds_existing_complaint(self, client, sample_report_id):
        response = client.post("/track", data={
            "report_id": sample_report_id,
            "search_type": "id",
        })
        assert response.status_code == 200
        assert sample_report_id in response.text

    def test_post_track_not_found_shows_message(self, client):
        response = client.post("/track", data={
            "report_id": "GVMC-9999-XXXXXX",
            "search_type": "id",
        })
        assert response.status_code == 200
        assert "not found" in response.text.lower() or "No Complaint" in response.text

    def test_post_track_empty_id_shows_error(self, client):
        response = client.post("/track", data={
            "report_id": "",
            "search_type": "id",
        })
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# /health endpoints
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealth:

    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_live_returns_ok(self, client):
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_ready_returns_ok_with_db(self, client):
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


# ═══════════════════════════════════════════════════════════════════════════════
# /citizen-review
# ═══════════════════════════════════════════════════════════════════════════════

class TestCitizenReview:

    def test_review_page_404_for_open_complaint(self, client, sample_report_id):
        """
        Citizen review page must return 404 if complaint is not yet resolved.
        Prevents citizens from reviewing before work is done.
        """
        response = client.get(f"/citizen-review/{sample_report_id}")
        assert response.status_code == 404

    def test_review_page_loads_for_resolved_complaint(self, client, sample_report_id):
        """Review page should load once complaint is resolved AND a valid token is used."""
        database.update_report_status(sample_report_id, "resolved", "Test Engineer")
        token = database.create_citizen_review_token(sample_report_id)
        response = client.get(f"/citizen-review/{sample_report_id}?token={token}")
        assert response.status_code == 200

    def test_review_page_404_without_token_even_if_resolved(self, client, sample_report_id):
        """Confirms the security fix: resolved status alone is not enough — token is required."""
        database.update_report_status(sample_report_id, "resolved", "Test Engineer")
        response = client.get(f"/citizen-review/{sample_report_id}")
        assert response.status_code == 404

    def test_review_page_404_with_wrong_token(self, client, sample_report_id):
        """A token that doesn't match this report_id must not grant access."""
        database.update_report_status(sample_report_id, "resolved", "Test Engineer")
        response = client.get(f"/citizen-review/{sample_report_id}?token=not-a-real-token")
        assert response.status_code == 404

    def test_review_page_404_for_nonexistent_id(self, client):
        response = client.get("/citizen-review/GVMC-9999-XXXXXX")
        assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# / home page
# ═══════════════════════════════════════════════════════════════════════════════

class TestHomePage:

    def test_home_loads(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_citizen_page_loads(self, client):
        response = client.get("/citizen")
        assert response.status_code == 200
        assert "complaint" in response.text.lower() or "grievance" in response.text.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Security headers
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecurityHeaders:

    def test_xframe_options_deny(self, client):
        response = client.get("/")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_x_content_type_nosniff(self, client):
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_csp_present(self, client):
        response = client.get("/")
        assert "Content-Security-Policy" in response.headers

# ═══════════════════════════════════════════════════════════════════════════════
# Role-based access — routes that previously had zero coverage
# (this exact gap let the ROLE_HOME/ROLE_LABELS import bug ship silently
#  through 6 commits before CI's ruff check caught it)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoleBasedAccess:

    def test_login_redirects_admin_to_correct_home(self, client):
        database.add_staff(name="Admin Test", username="logintest_admin",
            password="TestPass@2024!", role="admin", created_by="test_suite")
        response = client.post("/login", data={
            "username": "logintest_admin", "password": "TestPass@2024!"
        }, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] in ("/staff", "/commissioner", "/change-password?forced=1")

    def test_login_wrong_password_shows_error(self, client):
        database.add_staff(name="Admin Test", username="logintest_admin2",
            password="TestPass@2024!", role="admin", created_by="test_suite")
        response = client.post("/login", data={
            "username": "logintest_admin2", "password": "WrongPassword123!"
        })
        assert response.status_code == 200

    def test_admin_route_loads_for_admin_no_crash(self, authed_client):
        c = authed_client("admin")
        response = c.get("/admin")
        assert response.status_code == 200, f"Expected 200, got {response.status_code} — check for NameError/500"

    def test_admin_route_denies_non_admin(self, authed_client):
        c = authed_client("viewer")
        response = c.get("/admin", follow_redirects=False)
        assert response.status_code == 302
        assert "/admin" not in response.headers.get("location", "")

    def test_credential_card_loads_no_crash(self, authed_client):
        c = authed_client("admin")
        staff = database.get_staff_by_username("test_admin")
        response = c.get(f"/credential-card/{staff['id']}")
        assert response.status_code == 200, f"Expected 200, got {response.status_code} — check for NameError/500"

    def test_team_page_loads_for_ae_no_crash(self, authed_client):
        c = authed_client("ae")
        response = c.get("/team")
        assert response.status_code == 200, f"Expected 200, got {response.status_code} — check for NameError/500"

    def test_field_dashboard_loads_for_was_no_crash(self, authed_client):
        c = authed_client("was")
        response = c.get("/field")
        assert response.status_code == 200, f"Expected 200, got {response.status_code} — check for NameError/500"

    def test_staff_dashboard_loads_for_ae_no_crash(self, authed_client):
        c = authed_client("ae")
        response = c.get("/staff")
        assert response.status_code == 200, f"Expected 200, got {response.status_code} — check for NameError/500"

    def test_add_comment_route_no_crash(self, authed_client, sample_report_id):
        c = authed_client("ae")
        response = c.post("/add-comment", data={
            "report_id": sample_report_id,
            "comment": "test comment",
            "csrf": "test",
        }, follow_redirects=False)
        assert response.status_code in (302, 200), f"Expected redirect/200, got {response.status_code} — check for NameError/500"

    def test_add_comment_with_real_csrf_succeeds(self, authed_client, sample_report_id):
        """Full success path: real CSRF token → comment actually gets added."""
        c = authed_client("ae")
        get_response = c.get("/staff")
        assert get_response.status_code == 200

        import re
        match = re.search(r'name="csrf" value="([^"]+)"', get_response.text)
        assert match, "Could not find CSRF token on /staff page"
        real_csrf = match.group(1)

        response = c.post("/add-comment", data={
            "report_id": sample_report_id,
            "comment": "Real CSRF test comment",
            "csrf": real_csrf,
        }, follow_redirects=False)
        assert response.status_code == 302

        comments = database.get_comments(sample_report_id)
        assert any("Real CSRF test comment" in cm["comment"] for cm in comments), \
            "Comment was not actually saved to the database"

    def test_change_password_redirects_correctly(self, authed_client):
        c = authed_client("ae")
        # Get a real CSRF token first — GET renders it into the page via generate_csrf_token()
        get_response = c.get("/change-password")
        assert get_response.status_code == 200

        import re
        match = re.search(r'name="csrf" value="([^"]+)"', get_response.text)
        assert match, "Could not find CSRF token in change-password page"
        real_csrf = match.group(1)

        response = c.post("/change-password", data={
            "current_password": "TestPass@2024!",
            "new_password": "NewPass@2024!",
            "confirm_password": "NewPass@2024!",
            "csrf": real_csrf,
        }, follow_redirects=False)
        assert response.status_code == 302, f"Expected 302, got {response.status_code} — check for NameError/500"