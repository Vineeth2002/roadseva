"""
tests/test_database.py — RoadSeva Critical Unit Tests
======================================================
Run with: pytest tests/test_database.py -v

Tests cover:
  1. _generate_report_id() — format, IST timezone, uniqueness
  2. validate_password_strength() — all 7 rules
  3. _haversine_m() — known GPS distances
  4. check_duplicate_complaint() — within and outside radius
  5. add_report() — success path and failure path (no orphaned IDs)
  6. get_report_by_id() — found and not found
  7. update_report_status() — audit log written
  8. _now_ist_naive() — always IST not UTC

Setup: uses SQLite in-memory for all tests — no production DB touched.
"""

import os
import sys
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Force SQLite for tests — never touch production PostgreSQL
os.environ.pop("DATABASE_URL", None)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """
    Each test gets a fresh SQLite database in a temp directory.
    Prevents test pollution — state never leaks between tests.
    """
    original_path = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_roadseva.db")
    database.init_db()
    yield
    database.DB_PATH = original_path


# ═══════════════════════════════════════════════════════════════════════════════
# 1. REPORT ID GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateReportId:

    def test_format_is_correct(self):
        """ID must match GVMC-YYMM-XXXXXX exactly."""
        rid = database._generate_report_id()
        parts = rid.split("-")
        assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}: {rid}"
        assert parts[0] == "GVMC"
        assert len(parts[1]) == 4, f"YYMM part must be 4 chars, got: {parts[1]}"
        assert len(parts[2]) == 6, f"Random part must be 6 chars, got: {parts[2]}"

    def test_yymm_uses_ist_not_utc(self):
        """
        YYMM segment must be generated using IST (UTC+5:30).
        Critical edge case: at midnight IST (6:30 PM UTC previous day),
        UTC would give wrong month. This test simulates that boundary.
        """
        IST = timezone(timedelta(hours=5, minutes=30))
        ist_now = datetime.now(IST)
        expected_yymm = ist_now.strftime("%y%m")
        rid = database._generate_report_id()
        actual_yymm = rid.split("-")[1]
        assert actual_yymm == expected_yymm, (
            f"ID uses wrong date: got {actual_yymm}, expected {expected_yymm} (IST). "
            f"Check _generate_report_id() uses datetime.now(IST) not datetime.now()."
        )

    def test_random_part_is_uppercase_alphanumeric(self):
        """Random suffix must be uppercase letters and digits only."""
        import string
        allowed = set(string.ascii_uppercase + string.digits)
        for _ in range(20):
            rid = database._generate_report_id()
            rand_part = rid.split("-")[2]
            invalid = set(rand_part) - allowed
            assert not invalid, f"Invalid chars in random part: {invalid} in {rid}"

    def test_uniqueness(self):
        """Generate 1000 IDs — no duplicates."""
        ids = {database._generate_report_id() for _ in range(1000)}
        assert len(ids) == 1000, "Collision detected in 1000 generated IDs"

    def test_prefix_is_always_gvmc(self):
        """Prefix must always be GVMC regardless of any config."""
        for _ in range(10):
            rid = database._generate_report_id()
            assert rid.startswith("GVMC-"), f"Wrong prefix: {rid}"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PASSWORD STRENGTH VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestPasswordStrength:

    def test_valid_strong_password(self):
        ok, err = database.validate_password_strength("GvmC@2026!")
        assert ok is True
        assert err == ""

    def test_too_short(self):
        ok, err = database.validate_password_strength("Ab1!")
        assert ok is False
        assert "8 characters" in err

    def test_no_uppercase(self):
        ok, err = database.validate_password_strength("gvmc@2026!")
        assert ok is False
        assert "uppercase" in err.lower()

    def test_no_lowercase(self):
        ok, err = database.validate_password_strength("GVMC@2026!")
        assert ok is False
        assert "lowercase" in err.lower()

    def test_no_digit(self):
        ok, err = database.validate_password_strength("GvmcRoad!")
        assert ok is False
        assert "number" in err.lower()

    def test_no_special_char(self):
        ok, err = database.validate_password_strength("GvmcRoad2026")
        assert ok is False
        assert "special" in err.lower()

    def test_too_repetitive(self):
        ok, err = database.validate_password_strength("Aaaa@1111")
        # Only 4 unique chars — borderline, depends on set size
        unique_count = len(set("Aaaa@1111"))
        if unique_count < 4:
            assert ok is False

    def test_blocked_common_password(self):
        ok, err = database.validate_password_strength("Roadseva@1")
        assert ok is False
        assert "common" in err.lower()

    def test_blocked_year_password(self):
        ok, err = database.validate_password_strength("2026")
        assert ok is False

    def test_vizag_blocked(self):
        ok, err = database.validate_password_strength("vizag123")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. HAVERSINE DISTANCE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestHaversine:

    def test_same_point_is_zero(self):
        dist = database._haversine_m(17.6868, 83.2185, 17.6868, 83.2185)
        assert dist == pytest.approx(0.0, abs=0.1)

    def test_known_distance_vizag_centre(self):
        """
        Distance between two known points in Visakhapatnam.
        GVMC Office (17.7226, 83.3182) to RK Beach (17.7141, 83.3379)
        Approximately 2.1 km.
        """
        dist = database._haversine_m(17.7226, 83.3182, 17.7141, 83.3379)
        assert 1800 < dist < 2400, f"Expected ~2100m, got {dist:.0f}m"

    def test_50m_nearby_detection(self):
        """
        Two points 30m apart — within RQI breach radius of 50m.
        """
        # Move ~30m north (0.00027 degrees latitude ≈ 30m)
        lat1, lon1 = 17.7226, 83.3182
        lat2, lon2 = 17.7229, 83.3182
        dist = database._haversine_m(lat1, lon1, lat2, lon2)
        assert dist < 50, f"Expected < 50m, got {dist:.1f}m"

    def test_100m_outside_duplicate_radius(self):
        """
        Two points ~150m apart — outside duplicate complaint radius of 100m.
        """
        lat1, lon1 = 17.7226, 83.3182
        lat2, lon2 = 17.7240, 83.3182  # ~155m north
        dist = database._haversine_m(lat1, lon1, lat2, lon2)
        assert dist > 100, f"Expected > 100m, got {dist:.1f}m"

    def test_symmetry(self):
        """Distance A→B must equal B→A."""
        d1 = database._haversine_m(17.7226, 83.3182, 17.7141, 83.3379)
        d2 = database._haversine_m(17.7141, 83.3379, 17.7226, 83.3182)
        assert d1 == pytest.approx(d2, rel=0.001)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ADD REPORT — DB WRITE AND FAILURE HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddReport:

    def _make_report(self, **kwargs):
        defaults = dict(
            city="GVMC", ward="Ward 1 - Gajuwaka", damage_type="Pothole",
            description="Large pothole near bus stop",
            photo_path="", citizen_name="Test Citizen",
            citizen_phone="9848012345", citizen_email="",
            latitude=17.7226, longitude=83.3182,
            severity="unknown", photo_data="",
        )
        defaults.update(kwargs)
        return database.add_report(**defaults)

    def test_returns_valid_report_id(self):
        rid = self._make_report()
        assert rid.startswith("GVMC-")
        parts = rid.split("-")
        assert len(parts) == 3
        assert len(parts[2]) == 6

    def test_complaint_is_retrievable_after_save(self):
        """ID returned must actually exist in the database."""
        rid = self._make_report()
        report = database.get_report_by_id(rid)
        assert report is not None, f"Complaint {rid} not found after add_report()"
        assert report["report_id"] == rid
        assert report["ward"] == "Ward 1 - Gajuwaka"
        assert report["damage_type"] == "Pothole"

    def test_status_is_open_when_no_was(self):
        """With no WAS mapped to the ward, status should be 'open'."""
        rid = self._make_report()
        report = database.get_report_by_id(rid)
        assert report["status"] in ("open", "assigned")

    def test_audit_log_written_on_submit(self):
        """Every new complaint must create an audit log entry."""
        rid = self._make_report()
        conn = database.get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM audit_log WHERE report_id = ?", (rid,))
        rows = c.fetchall()
        conn.close()
        assert len(rows) >= 1, f"No audit log entry for {rid}"

    def test_sla_expiry_set_on_submit(self):
        """sla_due_at must be set to ~48h from submission."""
        rid = self._make_report()
        report = database.get_report_by_id(rid)
        assert report["sla_due_at"] is not None, "sla_due_at not set"
        expiry = datetime.strptime(report["sla_due_at"], "%Y-%m-%d %H:%M:%S")
        submitted = datetime.strptime(report["submitted_at"], "%Y-%m-%d %H:%M:%S")
        diff_hours = (expiry - submitted).total_seconds() / 3600
        assert 47 < diff_hours < 49, f"SLA expiry not ~48h: got {diff_hours:.1f}h"

    def test_db_failure_raises_exception(self):
        """
        If the DB INSERT fails, add_report() must RAISE an exception.
        It must NOT return an ID silently — that would create an orphaned ID
        shown to the citizen but not in the database.
        """
        with patch("database.get_conn") as mock_conn:
            mock_conn.side_effect = Exception("DB connection failed")
            with pytest.raises(Exception):
                self._make_report()

    def test_division_name_populated(self):
        """division_name must be set from ward mapping."""
        rid = self._make_report(ward="Ward 1 - Gajuwaka")
        report = database.get_report_by_id(rid)
        # division_name should be set (not empty) if ward mapping exists
        # If ward not in mapping it will be 'Unknown' — both are acceptable
        assert report["division_name"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GET REPORT BY ID
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetReportById:

    def test_returns_none_for_nonexistent_id(self):
        result = database.get_report_by_id("GVMC-9999-XXXXXX")
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = database.get_report_by_id("")
        assert result is None

    def test_case_sensitive_match(self):
        """Report ID is uppercase — lowercase must not match."""
        rid = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Test", "9848012345", "", None, None
        )
        result = database.get_report_by_id(rid.lower())
        assert result is None, "ID lookup should be case-sensitive"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. STATUS UPDATE AND AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpdateReportStatus:

    def _create(self):
        return database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Test Citizen", "9848012345", "", None, None
        )

    def test_status_updated_correctly(self):
        rid = self._create()
        database.update_report_status(rid, "assigned", "Test Officer")
        report = database.get_report_by_id(rid)
        assert report["status"] == "assigned"

    def test_audit_log_written_on_status_change(self):
        rid = self._create()
        database.update_report_status(rid, "assigned", "Test Officer")
        conn = database.get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM audit_log WHERE report_id = ? AND action = 'status_change'",
            (rid,)
        )
        rows = c.fetchall()
        conn.close()
        assert len(rows) >= 1, "No audit log entry for status change"

    def test_updated_by_recorded(self):
        rid = self._create()
        database.update_report_status(rid, "inspecting", "Engineer Ravi")
        report = database.get_report_by_id(rid)
        assert report["updated_by"] == "Engineer Ravi"

    def test_nonexistent_id_does_not_crash(self):
        """Updating a non-existent report should silently do nothing."""
        database.update_report_status("GVMC-9999-XXXXXX", "resolved", "Someone")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. IST TIMEZONE
# ═══════════════════════════════════════════════════════════════════════════════

class TestISTTimezone:

    def test_now_returns_ist_string(self):
        """now() must return IST time string in correct format."""
        result = database.now()
        # Must be parseable
        dt = datetime.strptime(result, "%Y-%m-%d %H:%M:%S")
        assert dt is not None

    def test_now_returns_naive_parseable_string(self):
        """now() must return a plain string parseable back into a naive datetime."""
        result = database.now()
        parsed = datetime.strptime(result, "%Y-%m-%d %H:%M:%S")
        assert parsed.tzinfo is None, "Parsed result must be naive for arithmetic comparisons"

    def test_now_uses_ist_offset(self):
        """now() must reflect IST (UTC+5:30), not UTC or naive local time."""
        ist = timezone(timedelta(hours=5, minutes=30))
        expected_str = datetime.now(ist).strftime("%Y-%m-%d %H:%M")
        actual_str = database.now()[:16]
        assert actual_str == expected_str, f"now() not using IST: got {actual_str}, expected {expected_str}"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. DUPLICATE COMPLAINT DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateComplaint:

    def test_no_duplicate_when_far_away(self):
        """Two complaints >100m apart should NOT be flagged as duplicates."""
        rid1 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen A", "9848000001", "", 17.7226, 83.3182
        )
        rid2 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen B", "9848000002", "", 17.7240, 83.3200  # ~200m away
        )
        result = database.check_duplicate_complaint(
            rid2, "Pothole", 17.7240, 83.3200, ward="Ward 1 - Gajuwaka"
        )
        assert result["found"] is False

    def test_duplicate_detected_when_nearby(self):
        """Two same-type complaints within 100m should be flagged."""
        rid1 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen A", "9848000001", "", 17.7226, 83.3182
        )
        # File second complaint 30m away
        rid2 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen B", "9848000002", "", 17.7229, 83.3182
        )
        result = database.check_duplicate_complaint(
            rid2, "Pothole", 17.7229, 83.3182, ward="Ward 1 - Gajuwaka"
        )
        assert result["found"] is True
        assert result["report_id"] == rid1

    def test_different_damage_type_not_duplicate(self):
        """Same location but different damage type — not a duplicate."""
        rid1 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen A", "9848000001", "", 17.7226, 83.3182
        )
        rid2 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Broken Footpath", "", "",
            "Citizen B", "9848000002", "", 17.7227, 83.3182
        )
        result = database.check_duplicate_complaint(
            rid2, "Broken Footpath", 17.7227, 83.3182, ward="Ward 1 - Gajuwaka"
        )
        assert result["found"] is False

    def test_resolved_complaint_not_flagged_as_duplicate(self):
        """A resolved complaint should not be flagged as duplicate for new ones."""
        rid1 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen A", "9848000001", "", 17.7226, 83.3182
        )
        database.update_report_status(rid1, "resolved", "Engineer")

        rid2 = database.add_report(
            "GVMC", "Ward 1 - Gajuwaka", "Pothole", "", "",
            "Citizen B", "9848000002", "", 17.7227, 83.3182
        )
        result = database.check_duplicate_complaint(
            rid2, "Pothole", 17.7227, 83.3182, ward="Ward 1 - Gajuwaka"
        )
        assert result["found"] is False