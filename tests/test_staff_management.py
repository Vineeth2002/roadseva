"""
tests/test_staff_management.py — /team/reset-password and /team/toggle
=========================================================================
Proves the P0 fix: both routes now enforce can_manage_user(creator_role,
target_role) via database functions, instead of only requiring login.

Two layers of tests:
  - DB-level: toggle_staff_active() / reset_password_with_log() directly,
    fast and precise (also proves "cannot bypass through direct database
    function invocation" -- the check lives in the DB layer itself, not
    only at the route).
  - HTTP-level: full request through TestClient with valid CSRF, proving
    the route wiring is correct end-to-end, not just the DB function.

Run with: pytest tests/test_staff_management.py -v
"""

import os
import re
import sys

import pytest

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_staff_mgmt.db")
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


def make_staff(role, username=None, **kw):
    username = username or f"test_{role}_{kw.get('suffix','1')}"
    ok, result = database.add_staff(
        name=f"Test {role.title()}", username=username, password="TestPass@2024!",
        role=role, created_by="test_suite",
        zone=kw.get("zone", "Zone 1" if role == "zonal_commissioner" else ""),
        division=kw.get("division", "Bheemunipatnam" if role == "ae" else ""),
        ward_list=kw.get("ward_list", "Ward 1 - Kondapeta / Wilsonpeta" if role == "was" else ""),
    )
    assert ok, f"setup failed: {result}"
    return database.get_staff_by_username(username)


# ═══════════════════════════════════════════════════════════════════════════
# toggle_staff_active() — DB-level authorization
# ═══════════════════════════════════════════════════════════════════════════

class TestToggleStaffActiveAuthorization:

    def test_viewer_cannot_toggle_admin(self):
        """viewer -> toggle admin = DENY."""
        admin = make_staff("admin", username="admin1")
        make_staff("viewer", username="viewer1")
        ok, msg = database.toggle_staff_active(admin["id"], "viewer", "viewer1")
        assert ok is False
        admin_after = database.get_staff_by_username("admin1")
        assert admin_after["is_active"] == 1

    def test_viewer_cannot_toggle_commissioner(self):
        """viewer -> toggle commissioner = DENY."""
        comm = make_staff("commissioner", username="comm1")
        ok, msg = database.toggle_staff_active(comm["id"], "viewer", "viewer1")
        assert ok is False

    def test_ae_cannot_toggle_privileged_account(self):
        """AE -> privileged target = DENY."""
        admin = make_staff("admin", username="admin1")
        ok, msg = database.toggle_staff_active(admin["id"], "ae", "ae1")
        assert ok is False

    def test_unauthorized_role_cannot_toggle_anyone(self):
        """unauthorized role -> target = DENY (was/field/grievance/triage
        have no ROLE_CAN_CREATE entry at all)."""
        was_target = make_staff("was", username="was_target")
        for role in ("was", "field_engineer", "grievance_officer", "triage_officer"):
            ok, msg = database.toggle_staff_active(was_target["id"], role, f"{role}_actor")
            assert ok is False, f"{role} should not be able to toggle anyone"

    def test_admin_can_toggle_permitted_target(self):
        """authorized admin -> permitted target = ALLOW."""
        fe = make_staff("field_engineer", username="fe1")
        ok, msg = database.toggle_staff_active(fe["id"], "admin", "admin_actor")
        assert ok is True
        assert msg == "deactivated"

    def test_commissioner_cannot_toggle_admin(self):
        """Per ROLE_CAN_CREATE, commissioner may not manage admin --
        confirms existing hierarchy is preserved, not invented."""
        admin = make_staff("admin", username="admin1")
        ok, msg = database.toggle_staff_active(admin["id"], "commissioner", "comm_actor")
        assert ok is False

    def test_zonal_commissioner_can_toggle_permitted_target(self):
        was = make_staff("was", username="was1")
        ok, msg = database.toggle_staff_active(was["id"], "zonal_commissioner", "zc_actor")
        assert ok is True

    def test_zonal_commissioner_cannot_toggle_admin(self):
        admin = make_staff("admin", username="admin1")
        ok, msg = database.toggle_staff_active(admin["id"], "zonal_commissioner", "zc_actor")
        assert ok is False

    def test_nonexistent_target_safe_failure(self):
        ok, msg = database.toggle_staff_active(999999, "admin", "admin_actor")
        assert ok is False
        assert "not found" in msg.lower()

    def test_cannot_bypass_by_changing_staff_id_to_self(self):
        """Confirms self-deactivation policy independently of the
        general manage-permission check."""
        admin = make_staff("admin", username="admin1")
        ok, msg = database.toggle_staff_active(admin["id"], "admin", "Test Admin")
        assert ok is False
        assert "own account" in msg.lower()


class TestSelfAndLastAdminProtection:

    def test_self_deactivation_denied(self):
        """user deactivating own account = DENY."""
        staff = make_staff("field_engineer", username="fe1")
        ok, msg = database.toggle_staff_active(staff["id"], "admin", staff["name"])
        assert ok is False

    def test_last_admin_cannot_be_deactivated(self):
        """Prevents locking out every administrator."""
        admin = make_staff("admin", username="only_admin")
        ok, msg = database.toggle_staff_active(admin["id"], "admin", "different_admin_actor")
        assert ok is False
        assert "last active" in msg.lower()

    def test_last_admin_protection_ignores_commissioner_count(self):
        """The last-admin check only counts admin/commissioner tier
        together as 'privileged' -- deactivating the only admin while a
        commissioner still exists is allowed, since the system isn't
        fully locked out."""
        admin = make_staff("admin", username="only_admin")
        make_staff("commissioner", username="a_commissioner")
        ok, msg = database.toggle_staff_active(admin["id"], "admin", "different_actor")
        assert ok is True

    def test_second_admin_can_be_deactivated(self):
        admin1 = make_staff("admin", username="admin1")
        make_staff("admin", username="admin2")
        ok, msg = database.toggle_staff_active(admin1["id"], "admin", "admin2")
        assert ok is True

    def test_non_privileged_target_never_blocked_by_last_admin_rule(self):
        fe = make_staff("field_engineer", username="fe1")
        ok, msg = database.toggle_staff_active(fe["id"], "admin", "admin_actor")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# reset_password_with_log() — DB-level authorization
# ═══════════════════════════════════════════════════════════════════════════

class TestResetPasswordAuthorization:

    def test_viewer_cannot_reset_admin(self):
        """viewer -> reset admin = DENY."""
        admin = make_staff("admin", username="admin1")
        ok, msg = database.reset_password_with_log(admin["id"], "viewer", "viewer1")
        assert ok is False

    def test_viewer_cannot_reset_commissioner(self):
        """viewer -> reset commissioner = DENY."""
        comm = make_staff("commissioner", username="comm1")
        ok, msg = database.reset_password_with_log(comm["id"], "viewer", "viewer1")
        assert ok is False

    def test_ae_cannot_reset_privileged_target(self):
        """AE -> privileged target = DENY."""
        admin = make_staff("admin", username="admin1")
        ok, msg = database.reset_password_with_log(admin["id"], "ae", "ae1")
        assert ok is False

    def test_unauthorized_role_cannot_reset_anyone(self):
        target = make_staff("was", username="was_target")
        for role in ("was", "field_engineer", "grievance_officer", "triage_officer"):
            ok, msg = database.reset_password_with_log(target["id"], role, f"{role}_actor")
            assert ok is False

    def test_admin_can_reset_permitted_target(self):
        """authorized admin -> permitted target = ALLOW."""
        fe = make_staff("field_engineer", username="fe1")
        before_hash = fe["password_hash"]
        ok, new_pass = database.reset_password_with_log(fe["id"], "admin", "admin_actor")
        assert ok is True
        assert isinstance(new_pass, str) and len(new_pass) > 0
        after = database.get_staff_by_username("fe1")
        assert after["password_hash"] != before_hash
        assert after["must_change_password"] == 1

    def test_commissioner_reset_follows_existing_policy(self):
        """commissioner -> permitted target = per existing
        ROLE_CAN_CREATE policy (commissioner may manage everything except
        admin/another commissioner)."""
        was = make_staff("was", username="was1")
        ok, _ = database.reset_password_with_log(was["id"], "commissioner", "comm_actor")
        assert ok is True

        admin = make_staff("admin", username="admin1")
        ok2, _ = database.reset_password_with_log(admin["id"], "commissioner", "comm_actor")
        assert ok2 is False

    def test_nonexistent_target_safe_failure(self):
        ok, msg = database.reset_password_with_log(999999, "admin", "admin_actor")
        assert ok is False
        assert "not found" in msg.lower()

    def test_inactive_target_reset_still_scoped_by_role(self):
        """Deactivated accounts follow the same role-permission check --
        no special bypass for inactive targets."""
        fe = make_staff("field_engineer", username="fe1")
        database.toggle_staff_active(fe["id"], "admin", "admin_actor")  # deactivate
        ok, _ = database.reset_password_with_log(fe["id"], "viewer", "viewer1")
        assert ok is False

    def test_audit_log_records_reset_without_password(self):
        fe = make_staff("field_engineer", username="fe1")
        ok, new_pass = database.reset_password_with_log(fe["id"], "admin", "admin_actor")
        assert ok is True
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT * FROM staff_audit_log WHERE target_username='fe1' ORDER BY id DESC LIMIT 1")
        row = dict(c.fetchone())
        conn.close()
        assert row["action"] == "password_reset"
        assert new_pass not in str(row)  # plaintext password never in the log row


# ═══════════════════════════════════════════════════════════════════════════
# HTTP-level: full route, valid CSRF, session-based auth
# ═══════════════════════════════════════════════════════════════════════════

class TestRoutesEndToEnd:

    def _login(self, client_factory, role, username):
        """Mirrors tests/test_routes.py's authed_client pattern locally
        so this file has no cross-file fixture dependency."""
        from fastapi.testclient import TestClient
        from main import app
        staff = make_staff(role, username=username)
        token = database.create_session(staff["id"])
        c = TestClient(app, raise_server_exceptions=False)
        c.cookies.set("session_token", token)
        return c, staff

    def _csrf(self, client, page="/team"):
        resp = client.get(page)
        match = re.search(r'name="csrf" value="([^"]+)"', resp.text)
        assert match, f"No CSRF token found on {page}"
        return match.group(1)

    def test_viewer_cannot_toggle_admin_via_http(self):
        target_client, admin = self._login(None, "admin", "http_admin1")
        viewer_client, viewer = self._login(None, "viewer", "http_viewer1")
        csrf = self._csrf(viewer_client)
        resp = viewer_client.post("/team/toggle",
            data={"staff_id": admin["id"], "csrf": csrf}, follow_redirects=False)
        # viewer isn't role-permitted onto /team at all -> redirected home,
        # never reaches the DB layer
        admin_after = database.get_staff_by_username("http_admin1")
        assert admin_after["is_active"] == 1

    def test_ae_cannot_reset_admin_password_via_http(self):
        _, admin = self._login(None, "admin", "http_admin2")
        ae_client, ae = self._login(None, "ae", "http_ae1")
        csrf = self._csrf(ae_client)
        resp = ae_client.post("/team/reset-password",
            data={"staff_id": admin["id"], "csrf": csrf}, follow_redirects=False)
        assert resp.status_code == 302
        assert "error" in resp.headers.get("location", "")

    def test_admin_can_reset_field_engineer_via_http(self):
        admin_client, admin = self._login(None, "admin", "http_admin3")
        fe = make_staff("field_engineer", username="http_fe1")
        csrf = self._csrf(admin_client)
        resp = admin_client.post("/team/reset-password",
            data={"staff_id": fe["id"], "csrf": csrf}, follow_redirects=False)
        # UPDATED by the password-in-URL fix: success now renders
        # credential_card.html directly (200) instead of redirecting with
        # the password in the URL. See tests/test_password_in_url.py for
        # the dedicated coverage of that change.
        assert resp.status_code == 200

    def test_invalid_csrf_denied_before_authorization_matters(self):
        admin_client, admin = self._login(None, "admin", "http_admin4")
        fe = make_staff("field_engineer", username="http_fe2")
        resp = admin_client.post("/team/reset-password",
            data={"staff_id": fe["id"], "csrf": "not-a-real-token"}, follow_redirects=False)
        assert resp.status_code == 302
        assert "expired" in resp.headers.get("location", "").lower() or "error" in resp.headers.get("location","")
        fe_after = database.get_staff_by_username("http_fe2")
        assert fe_after["password_hash"] == fe["password_hash"]  # unchanged