"""
tests/test_inspections.py — Scheduled Inspection Workflow Tests
===============================================================
Run with: pytest tests/test_inspections.py -v

Tests cover:
  1. create_scheduled_inspections()
       - uses contract policy if exists, else default
       - idempotent — second call skips
       - repair not found → returns []
       - no policies → returns []
  2. submit_inspection_result()
       - valid scores 1–5 accepted
       - score out of range rejected
       - already completed → rejected
       - score 1–2 → breach candidate flagged in recurrence_events
       - score 3–5 → no breach candidate
       - audit log written
  3. get_pending_inspections_for_officer()
       - filters to officer's assignments + unassigned
       - overdue flag set correctly
  4. Full chain: link → inspections auto-created → submit → breach candidate
  5. _flag_breach_candidate DLP boundary (within vs outside)
"""

import os, sys
import pytest
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import linkage


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_inspect.db")
    database.init_db()
    database.init_contract_intelligence_schema()
    database.seed_demo_contract_data()
    yield
    database.DB_PATH = original


def _make_report(report_id="GVMC-TEST-0001", ward="Ward 9"):
    conn = database.get_conn(); c = conn.cursor()
    c.execute(database._q("""
        INSERT OR IGNORE INTO reports
            (report_id, city, ward, damage_type, status, submitted_at, latitude, longitude)
        VALUES (?, 'Visakhapatnam', ?, 'pothole', 'resolved', ?, 17.7236, 83.3309)
    """), (report_id, ward, database.now()))
    conn.commit(); conn.close()


def _make_repair(report_id="GVMC-TEST-0001",
                 contractor_name="Megha Engineering",
                 lat=17.7236, lng=83.3309,
                 repaired_at="2024-06-15 10:00:00"):
    _make_report(report_id)
    conn = database.get_conn(); c = conn.cursor()
    c.execute(database._q("""
        INSERT INTO repair_records
            (report_id, contractor_name, repair_lat, repair_lng,
             warranty_months, repaired_at, recorded_by)
        VALUES (?,?,?,?,12,?,'test')
    """), (report_id, contractor_name, lat, lng, repaired_at))
    conn.commit()
    c.execute(database._q("SELECT last_insert_rowid() AS id"))
    rid = dict(c.fetchone())["id"]
    conn.close()
    return rid


def _get_repair(repair_id):
    conn = database.get_conn(); c = conn.cursor()
    c.execute(database._q("SELECT * FROM repair_records WHERE id=?"), (repair_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None


def _link_repair(repair_id, road_asset_id=1):
    """Link repair to contract chain and return result."""
    return linkage.link_repair_to_contract(repair_id, road_asset_id, "AE Prasad")


def _count_inspections(repair_id):
    conn = database.get_conn(); c = conn.cursor()
    c.execute(database._q(
        "SELECT COUNT(*) as cnt FROM scheduled_inspections WHERE repair_record_id=?"
    ), (repair_id,))
    cnt = dict(c.fetchone())["cnt"]; conn.close()
    return cnt


def _count_breach_candidates():
    conn = database.get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM recurrence_events WHERE verification_status='candidate'")
    cnt = dict(c.fetchone())["cnt"]; conn.close()
    return cnt


def _get_first_inspection(repair_id):
    conn = database.get_conn(); c = conn.cursor()
    c.execute(database._q("""
        SELECT * FROM scheduled_inspections WHERE repair_record_id=?
        ORDER BY due_date ASC LIMIT 1
    """), (repair_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# 1. create_scheduled_inspections()
# ─────────────────────────────────────────────────────────────────────────────

class TestCreateScheduledInspections:

    def test_creates_inspections_from_default_policy(self):
        """
        Repair with no contract → falls back to default inspection policies.
        DEFAULT_INSPECTION_SCHEDULE has 5 entries (30/60/90/180/365 days).
        """
        repair_id = _make_repair()
        created = database.create_scheduled_inspections(repair_id, contract_id=None)
        assert len(created) == 5, f"Expected 5 default inspections, got {len(created)}"

    def test_inspection_due_dates_correct(self):
        """Due dates are calculated from repaired_at, not today."""
        repair_id = _make_repair(repaired_at="2024-06-15 10:00:00")
        database.create_scheduled_inspections(repair_id, contract_id=None)

        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("""
            SELECT due_date FROM scheduled_inspections
            WHERE repair_record_id=? ORDER BY due_date ASC
        """), (repair_id,))
        dates = [dict(r)["due_date"] for r in c.fetchall()]
        conn.close()

        # 30-day check from 2024-06-15 → 2024-07-15
        assert dates[0] == "2024-07-15", f"Expected 2024-07-15, got {dates[0]}"
        # 365-day check → 2025-06-15
        assert dates[-1] == "2025-06-15", f"Expected 2025-06-15, got {dates[-1]}"

    def test_idempotent_second_call_skips(self):
        """Calling twice for the same repair does not create duplicates."""
        repair_id = _make_repair()
        created1 = database.create_scheduled_inspections(repair_id)
        created2 = database.create_scheduled_inspections(repair_id)
        assert len(created1) == 5
        assert len(created2) == 0, "Second call should return [] (already exists)"
        assert _count_inspections(repair_id) == 5

    def test_invalid_repair_id_returns_empty(self):
        """Non-existent repair_record_id → returns []."""
        created = database.create_scheduled_inspections(99999)
        assert created == []

    def test_inspections_start_as_pending(self):
        """All created inspections start with status='pending'."""
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q(
            "SELECT DISTINCT status FROM scheduled_inspections WHERE repair_record_id=?"
        ), (repair_id,))
        statuses = [dict(r)["status"] for r in c.fetchall()]
        conn.close()
        assert statuses == ["pending"]

    def test_uses_contract_policy_when_available(self):
        """
        If a contract-specific policy exists, it should be used instead of defaults.
        Insert a custom policy for contract_id=1 and verify it's picked up.
        """
        # Insert a contract-specific policy (2 inspections only)
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("""
            INSERT INTO inspection_schedule_policies
                (policy_name, contract_id, inspection_type, due_after_days,
                 is_default, notes, created_at, created_by)
            VALUES (?, 1, 'routine', 45, 0, 'Custom', ?, 'test')
        """), ("45-Day Custom", database.now()))
        c.execute(database._q("""
            INSERT INTO inspection_schedule_policies
                (policy_name, contract_id, inspection_type, due_after_days,
                 is_default, notes, created_at, created_by)
            VALUES (?, 1, 'final_dlp', 180, 0, 'Custom final', ?, 'test')
        """), ("180-Day Custom", database.now()))
        conn.commit(); conn.close()

        repair_id = _make_repair()
        created = database.create_scheduled_inspections(repair_id, contract_id=1)
        # Should use the 2 contract-specific policies, not the 5 defaults
        assert len(created) == 2, (
            f"Expected 2 contract-specific inspections, got {len(created)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. submit_inspection_result()
# ─────────────────────────────────────────────────────────────────────────────

class TestSubmitInspectionResult:

    def _setup(self):
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)
        insp = _get_first_inspection(repair_id)
        return repair_id, insp["id"]

    def test_valid_score_5_accepted(self):
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 5, "Excellent condition", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is True
        assert result["breach_candidate"] is False

    def test_valid_score_3_accepted_no_breach(self):
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 3, "Fair condition, minor wear", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is True
        assert result["breach_candidate"] is False
        assert _count_breach_candidates() == 0

    def test_score_2_flags_breach_candidate(self):
        """Score of 2 (poor) → breach candidate created in recurrence_events."""
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 2, "Road deteriorating rapidly", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is True
        assert result["breach_candidate"] is True
        assert _count_breach_candidates() == 1

    def test_score_1_flags_breach_candidate(self):
        """Score of 1 (failed) → breach candidate created."""
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 1, "Complete failure — potholes returned", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is True
        assert result["breach_candidate"] is True
        assert _count_breach_candidates() == 1

    def test_score_3_no_breach_candidate(self):
        """Score of 3 → no breach candidate."""
        _, iid = self._setup()
        database.submit_inspection_result(iid, 3, "Fair", "", None, None, "WAS Kumar")
        assert _count_breach_candidates() == 0

    def test_score_out_of_range_rejected(self):
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 6, "Invalid score", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is False
        assert "1–5" in result["error"] or "1-5" in result["error"]

    def test_score_zero_rejected(self):
        _, iid = self._setup()
        result = database.submit_inspection_result(
            iid, 0, "Invalid score", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is False

    def test_inspection_marked_completed_after_submit(self):
        _, iid = self._setup()
        database.submit_inspection_result(iid, 4, "Good", "", None, None, "WAS Kumar")
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("SELECT status FROM scheduled_inspections WHERE id=?"), (iid,))
        status = dict(c.fetchone())["status"]; conn.close()
        assert status == "completed"

    def test_already_completed_rejected(self):
        _, iid = self._setup()
        database.submit_inspection_result(iid, 4, "Good", "", None, None, "WAS Kumar")
        result = database.submit_inspection_result(
            iid, 4, "Duplicate submit", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is False
        assert "already completed" in result["error"].lower()

    def test_invalid_inspection_id_rejected(self):
        result = database.submit_inspection_result(
            99999, 4, "Notes", "", None, None, "WAS Kumar"
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_audit_log_written(self):
        _, iid = self._setup()
        database.submit_inspection_result(iid, 4, "Good", "", None, None, "WAS Kumar")
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q(
            "SELECT * FROM audit_log WHERE action='inspection_completed' ORDER BY id DESC LIMIT 1"
        ))
        row = c.fetchone(); conn.close()
        assert row is not None
        row = dict(row)
        assert row["done_by"] == "WAS Kumar"
        assert row["action"] == "inspection_completed"

    def test_photo_data_stored(self):
        """Photo data (base64 string) is persisted in inspection_results."""
        _, iid = self._setup()
        fake_photo = "data:image/jpeg;base64,/9j/fakedata"
        result = database.submit_inspection_result(
            iid, 4, "Good", fake_photo, None, None, "WAS Kumar"
        )
        assert result["ok"] is True
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q(
            "SELECT photo_data FROM inspection_results WHERE inspection_id=?"
        ), (iid,))
        row = dict(c.fetchone()); conn.close()
        assert row["photo_data"] == fake_photo

    def test_gps_coordinates_stored(self):
        """GPS coordinates from the field officer are persisted."""
        _, iid = self._setup()
        database.submit_inspection_result(
            iid, 4, "Good", "", 17.7236, 83.3309, "WAS Kumar"
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q(
            "SELECT inspect_lat, inspect_lng FROM inspection_results WHERE inspection_id=?"
        ), (iid,))
        row = dict(c.fetchone()); conn.close()
        assert abs(row["inspect_lat"] - 17.7236) < 0.0001
        assert abs(row["inspect_lng"] - 83.3309) < 0.0001


# ─────────────────────────────────────────────────────────────────────────────
# 3. get_pending_inspections_for_officer()
# ─────────────────────────────────────────────────────────────────────────────

class TestGetPendingInspections:

    def test_returns_all_pending_for_no_filter(self):
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)
        result = database.get_pending_inspections_for_officer("")
        assert len(result) == 5

    def test_filters_by_assigned_officer(self):
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)

        # Assign first inspection to specific officer
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("""
            UPDATE scheduled_inspections SET assigned_to='WAS Kumar'
            WHERE repair_record_id=? AND id=(
                SELECT id FROM scheduled_inspections WHERE repair_record_id=? LIMIT 1
            )
        """), (repair_id, repair_id))
        conn.commit(); conn.close()

        # WAS Kumar gets assigned + unassigned
        result = database.get_pending_inspections_for_officer("WAS Kumar")
        assert len(result) == 5  # 1 assigned + 4 unassigned

        # Different officer gets only unassigned
        result2 = database.get_pending_inspections_for_officer("Other Officer")
        assert len(result2) == 4  # only unassigned

    def test_overdue_flag_set_for_past_due_date(self):
        """
        Inspection due in the past → is_overdue=1.
        We create an inspection and manually set its due_date to a past date.
        """
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)

        # Set all due dates to past
        past = "2020-01-01"
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q(
            "UPDATE scheduled_inspections SET due_date=? WHERE repair_record_id=?"
        ), (past, repair_id))
        conn.commit(); conn.close()

        result = database.get_pending_inspections_for_officer("")
        assert all(r["is_overdue"] == 1 for r in result), (
            "All past-due inspections should be flagged overdue"
        )

    def test_future_due_dates_not_overdue(self):
        repair_id = _make_repair(repaired_at=database.now()[:10] + " 00:00:00")
        database.create_scheduled_inspections(repair_id)
        result = database.get_pending_inspections_for_officer("")
        # All due dates are in the future
        assert all(r["is_overdue"] == 0 for r in result)

    def test_completed_inspections_excluded(self):
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)
        insp = _get_first_inspection(repair_id)
        database.submit_inspection_result(
            insp["id"], 4, "Good", "", None, None, "WAS Kumar"
        )
        result = database.get_pending_inspections_for_officer("")
        assert len(result) == 4  # one completed, four pending

    def test_result_has_required_fields(self):
        repair_id = _make_repair()
        database.create_scheduled_inspections(repair_id)
        result = database.get_pending_inspections_for_officer("")
        required = {"inspection_id", "repair_record_id", "due_date",
                    "inspection_type", "status", "is_overdue", "report_id"}
        for row in result:
            missing = required - set(row.keys())
            assert not missing, f"Missing fields: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. FULL CHAIN: link → inspections auto-created → submit → breach candidate
# ─────────────────────────────────────────────────────────────────────────────

class TestFullInspectionChain:

    def test_link_auto_creates_inspections(self):
        """
        Linking a repair to a contract via link_repair_to_contract()
        should automatically trigger create_scheduled_inspections().
        """
        repair_id = _make_repair(repaired_at="2024-06-15 10:00:00")
        result = _link_repair(repair_id, road_asset_id=1)
        assert result["ok"] is True

        # Inspections should be auto-created
        count = _count_inspections(repair_id)
        assert count > 0, (
            "link_repair_to_contract should auto-create scheduled inspections"
        )
        assert result.get("inspections_created", 0) > 0

    def test_failed_inspection_becomes_breach_candidate(self):
        """
        Full chain: repair closed → linked → inspection due → submitted with
        score 1 → recurrence_events candidate created with
        verification_status='candidate' (NOT 'verified_recurrence').
        """
        repair_id = _make_repair(repaired_at="2024-06-15 10:00:00")
        _link_repair(repair_id, road_asset_id=1)

        insp = _get_first_inspection(repair_id)
        assert insp is not None

        result = database.submit_inspection_result(
            insp["id"], 1, "Road completely failed — back to potholes",
            "", 17.7236, 83.3309, "WAS Kumar"
        )
        assert result["ok"] is True
        assert result["breach_candidate"] is True

        # Check recurrence_event is a CANDIDATE, not a verdict
        conn = database.get_conn(); c = conn.cursor()
        c.execute("""
            SELECT verification_status, match_method
            FROM recurrence_events
            ORDER BY id DESC LIMIT 1
        """)
        row = dict(c.fetchone()); conn.close()
        assert row["verification_status"] == "candidate", (
            "Failed inspection must create 'candidate' not 'verified_recurrence' — "
            "human review required before breach verdict"
        )
        assert row["match_method"] == "inspection_failed", (
            "Recurrence event from failed inspection should have match_method='inspection_failed'"
        )

    def test_good_inspection_no_breach_candidate(self):
        """Score 4 after link → no breach candidate created."""
        repair_id = _make_repair(repaired_at="2024-06-15 10:00:00")
        _link_repair(repair_id, road_asset_id=1)
        insp = _get_first_inspection(repair_id)
        database.submit_inspection_result(
            insp["id"], 4, "Road holding up well", "", None, None, "WAS Kumar"
        )
        assert _count_breach_candidates() == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. DLP BOUNDARY — breach candidate within_dlp flag
# ─────────────────────────────────────────────────────────────────────────────

class TestDLPBoundary:

    def test_within_dlp_flagged_correctly(self):
        """
        within_dlp is computed from days_since_repair vs contract dlp_months.
        To get within_dlp=1, the repair must have a linked contract AND
        days_since_repair <= dlp_months * 30.

        We insert a contract active during 2024-06-15 (CONT-2024-EAST-01,
        dlp_months=24) and a repair on that date. The inspection is submitted
        ~today — days_since is ~730+ for a 2024 repair. So within_dlp depends
        on the DLP period relative to days elapsed.

        More testable: verify the logic directly via a short-DLP contract.
        Insert a contract with dlp_months=1 (30 days) and a repair from yesterday.
        days_since=1 <= 30 → within_dlp=1.
        """
        from datetime import datetime, timedelta

        # Insert a short-DLP contract (1 month) active yesterday
        yesterday_str  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow_str   = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        yesterday_dt   = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")

        conn = database.get_conn(); c = conn.cursor()
        # Get contractor id 1 (MEIL)
        c.execute("SELECT id FROM contractors LIMIT 1")
        cont_id = dict(c.fetchone())["id"]

        c.execute(database._q("""
            INSERT INTO contracts
                (contract_code, tender_number, contractor_id, division, ward_scope,
                 scope_description, contract_value_lakh, work_start_date, work_end_date,
                 dlp_months, status, responsible_ae, created_at, created_by)
            VALUES ('CONT-TEST-DLP1', 'TEST/001', ?, 'East', 'Ward 9',
                    'Test contract DLP 1 month', 10.0, ?, ?, 1, 'active', 'AE Test', ?, 'test')
        """), (cont_id, yesterday_str, tomorrow_str, database.now()))
        conn.commit()
        c.execute(database._q("SELECT id FROM contracts WHERE contract_code='CONT-TEST-DLP1'"))
        short_contract_id = dict(c.fetchone())["id"]

        # Link road asset 1 to this contract
        c.execute(database._q("""
            INSERT INTO contract_segments (contract_id, road_asset_id, created_at, created_by)
            VALUES (?, 1, ?, 'test')
        """), (short_contract_id, database.now()))
        conn.commit(); conn.close()

        repair_id = _make_repair(repaired_at=yesterday_dt, report_id="GVMC-DLP-001")
        _link_repair(repair_id, road_asset_id=1)
        insp = _get_first_inspection(repair_id)
        assert insp is not None, "Inspection should be created for linked repair"

        database.submit_inspection_result(
            insp["id"], 1, "Failure within DLP", "", None, None, "WAS Kumar"
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("""
            SELECT within_dlp, days_since_repair
            FROM recurrence_events ORDER BY id DESC LIMIT 1
        """)
        row = dict(c.fetchone()); conn.close()

        assert row["days_since_repair"] <= 3, (
            f"Yesterday repair should be 1 day old, got {row['days_since_repair']}"
        )
        assert row["within_dlp"] == 1, (
            f"1 day since repair on a 1-month DLP contract should be within_dlp=1, "
            f"got {row['within_dlp']} (days_since={row['days_since_repair']})"
        )

    def test_outside_dlp_flagged_correctly(self):
        """
        Repair from 2020 → days_since_repair >> 730 days.
        No active contract for 2020 (all demo contracts start 2024) → within_dlp=0.
        days_since_repair is still computed and stored correctly.
        """
        repair_id = _make_repair(repaired_at="2020-01-01 10:00:00",
                                  report_id="GVMC-DLP-002")
        _link_repair(repair_id, road_asset_id=1)
        insp = _get_first_inspection(repair_id)
        assert insp is not None, "Inspection should be created even without contract"

        database.submit_inspection_result(
            insp["id"], 1, "Failure outside DLP", "", None, None, "WAS Kumar"
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("""
            SELECT within_dlp, days_since_repair
            FROM recurrence_events ORDER BY id DESC LIMIT 1
        """)
        row = dict(c.fetchone()); conn.close()
        assert row["days_since_repair"] > 730, (
            f"2020 repair should be > 730 days old, got {row['days_since_repair']}"
        )
        assert row["within_dlp"] == 0, (
            "2020 repair with no linked contract should be within_dlp=0"
        )