"""
tests/test_bootstrap.py — Bootstrap credential security tests
================================================================
Proves the _seed_admin() removal fixed the P0 finding: a fresh
database no longer contains a published default credential, the
proper setup_first_commissioner() flow is the sole way to create the
first privileged account, and it cannot be reused or bypassed.

Run with: pytest tests/test_bootstrap.py -v
"""

import os
import sys
import io
import contextlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture
def fresh_db_path(tmp_path):
    original_path = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_bootstrap.db")
    yield database.DB_PATH
    database.DB_PATH = original_path


class TestNoDefaultAdmin:

    def test_fresh_db_has_no_admin_username(self, fresh_db_path):
        database.init_db()
        assert database.get_staff_by_username("admin") is None

    def test_fresh_db_has_zero_staff_accounts(self, fresh_db_path):
        database.init_db()
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM staff")
        count = dict(c.fetchone())["n"]
        conn.close()
        assert count == 0

    def test_default_password_does_not_authenticate(self, fresh_db_path):
        """Even if someone tries the previously-published credential
        against a fresh instance, no account exists to match it."""
        database.init_db()
        staff = database.get_staff_by_username("admin")
        assert staff is None  # nothing to check a password against

    def test_seed_admin_function_no_longer_exists(self):
        """The function itself is removed, not merely uncalled -- proves
        it can't be re-invoked from anywhere else in the codebase."""
        assert not hasattr(database, "_seed_admin")

    def test_init_db_does_not_print_plaintext_credential(self, fresh_db_path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            database.init_db()
        output = buf.getvalue()
        assert "Admin@2024" not in output
        assert "password" not in output.lower() or "no staff" in output.lower()


class TestRestartSafety:

    def test_restart_does_not_recreate_admin(self, fresh_db_path):
        database.init_db()
        database.init_db()  # simulate a second app startup against the same DB
        database.init_db()  # and a third, for good measure
        assert database.get_staff_by_username("admin") is None

    def test_restart_after_setup_preserves_commissioner_and_creates_no_admin(self, fresh_db_path):
        database.init_db()
        ok, _ = database.setup_first_commissioner(
            "First Commissioner", "commissioner1", "StrongPass!2026", "GVMC"
        )
        assert ok

        database.init_db()  # restart
        commissioner = database.get_staff_by_username("commissioner1")
        assert commissioner is not None
        assert database.get_staff_by_username("admin") is None


class TestSetupFlowIsAuthoritative:

    def test_first_setup_creates_commissioner_account(self, fresh_db_path):
        database.init_db()
        ok, msg = database.setup_first_commissioner(
            "First Commissioner", "commissioner1", "StrongPass!2026", "GVMC"
        )
        assert ok, msg
        staff = database.get_staff_by_username("commissioner1")
        assert staff is not None
        assert staff["role"] == "commissioner"

    def test_setup_marks_complete(self, fresh_db_path):
        database.init_db()
        assert database.is_setup_complete() is False
        database.setup_first_commissioner("A", "commissioner1", "StrongPass!2026", "GVMC")
        assert database.is_setup_complete() is True

    def test_completed_setup_rejects_second_privileged_account(self, fresh_db_path):
        database.init_db()
        database.setup_first_commissioner("A", "commissioner1", "StrongPass!2026", "GVMC")
        ok, msg = database.setup_first_commissioner("B", "commissioner2", "AnotherPass!99", "GVMC")
        assert ok is False
        assert "already" in msg.lower()
        assert database.get_staff_by_username("commissioner2") is None

    def test_weak_password_rejected_on_open_setup(self, fresh_db_path):
        database.init_db()
        ok, msg = database.setup_first_commissioner("A", "weakuser", "weak", "GVMC")
        assert ok is False
        assert database.get_staff_by_username("weakuser") is None

    def test_common_password_rejected(self, fresh_db_path):
        database.init_db()
        ok, msg = database.setup_first_commissioner("A", "commonuser", "Admin@123", "GVMC")
        assert ok is False

    def test_no_implicit_fallback_account_exists_before_setup(self, fresh_db_path):
        """No privileged account of ANY role should exist until setup
        runs -- not just 'admin' specifically."""
        database.init_db()
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM staff WHERE role IN ('admin','commissioner')")
        count = dict(c.fetchone())["n"]
        conn.close()
        assert count == 0


class TestExistingStaffUnaffected:

    def test_existing_legitimate_staff_survive_init_db_rerun(self, fresh_db_path):
        database.init_db()
        database.setup_first_commissioner("A", "commissioner1", "StrongPass!2026", "GVMC")
        ok, _ = database.add_staff(
            name="Field Engineer One", username="fe1", password="FieldPass!2026",
            role="field_engineer", created_by="commissioner1",
        )
        assert ok
        before = database.get_staff_by_username("fe1")
        assert before is not None

        database.init_db()  # restart again

        after = database.get_staff_by_username("fe1")
        assert after is not None
        assert after["password_hash"] == before["password_hash"]  # untouched


class TestDemoDataUnaffectedByBootstrapFix:
    """seed_demo_contract_data() (road assets / contracts / contractors)
    does not create staff accounts and is unrelated to this fix -- these
    tests confirm the bootstrap removal didn't accidentally touch it."""

    def test_demo_contract_seeding_still_runs_independently(self, fresh_db_path):
        database.init_db()
        database.init_contract_intelligence_schema()
        database.seed_demo_contract_data()  # should not raise, no staff dependency
        assert database.get_staff_by_username("admin") is None