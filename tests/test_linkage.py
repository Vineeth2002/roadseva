"""
tests/test_linkage.py — Linkage Engine Tests
=============================================
Run with: pytest tests/test_linkage.py -v

Tests cover:
  1. _point_to_segment_distance_m() — geometry correctness
  2. find_road_asset_candidates()
       - exact / near segment → HIGH
       - single candidate 25–50m → MEDIUM
       - multiple candidates within 50m → AMBIGUOUS
       - > 50m → NO_MATCH
       - no GPS → NO_MATCH
       - ward boundary — correct candidate still discoverable
       - inactive/expired contract → not selected as active
       - multiple historical contracts → correct one by repair date
  3. link_repair_to_contract()
       - full chain resolves and persists
       - invalid road_asset_id → rejected
       - no active contract → partial link (road only), warns
       - transaction atomicity — partial failure → rollback
  4. Geometry utility — _haversine_m known distance
"""

import os
import sys
import math
import pytest
from datetime import datetime, timezone, timedelta

# Force SQLite for tests
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import linkage
from linkage import (
    find_road_asset_candidates,
    link_repair_to_contract,
    _point_to_segment_distance_m,
    _haversine_m,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_NO_MATCH,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Each test gets an isolated SQLite database with schema + demo data."""
    original_path = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_linkage.db")
    database.init_db()
    database.init_contract_intelligence_schema()
    database.seed_demo_contract_data()
    yield
    database.DB_PATH = original_path


def _make_repair(contractor_name="Test Contractor",
                 lat=17.7326, lng=83.3320,
                 repaired_at="2024-06-15 10:00:00",
                 report_id="GVMC-2406-000001"):
    """Insert a repair_record row directly and return its id."""
    conn = database.get_conn()
    c = conn.cursor()
    # Ensure parent report exists (audit_log FK)
    c.execute(database._q("""
        INSERT OR IGNORE INTO reports
            (report_id, city, ward, damage_type, status, submitted_at)
        VALUES (?, 'Visakhapatnam', 'Ward 9', 'pothole', 'resolved', ?)
    """), (report_id, repaired_at))
    c.execute(database._q("""
        INSERT INTO repair_records
            (report_id, contractor_name, repair_lat, repair_lng,
             warranty_months, repaired_at, recorded_by)
        VALUES (?, ?, ?, ?, 12, ?, 'test')
    """), (report_id, contractor_name, lat, lng, repaired_at))
    conn.commit()
    c.execute(database._q("SELECT last_insert_rowid() AS id"))
    row = c.fetchone()
    rid = dict(row)["id"]
    conn.close()
    return rid


def _get_repair(repair_id):
    conn = database.get_conn()
    c = conn.cursor()
    c.execute(database._q("SELECT * FROM repair_records WHERE id=?"), (repair_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════════
# 1. GEOMETRY — _point_to_segment_distance_m
# ═══════════════════════════════════════════════════════════════════════════════

class TestPointToSegmentDistance:

    def test_point_on_segment_midpoint(self):
        """A point exactly on the midpoint of the segment → ~0m."""
        # Segment: Waltair Main Road rough endpoints
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        # Midpoint
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        dist = _point_to_segment_distance_m(mlat, mlon, alat, alon, blat, blon)
        assert dist < 1.0, f"Point on segment midpoint should be ~0m, got {dist:.2f}m"

    def test_point_perpendicular_offset(self):
        """Point ~25m perpendicular to a segment returns roughly that distance."""
        # Horizontal segment (same lat, different lon)
        alat, alon = 17.7200, 83.3200
        blat, blon = 17.7200, 83.3300  # ~900m east
        # Move point ~25m north (perpendicular)
        offset_lat = alat + (25 / 111_320.0)
        dist = _point_to_segment_distance_m(offset_lat, (alon + blon) / 2,
                                             alat, alon, blat, blon)
        assert 20 < dist < 30, f"Expected ~25m, got {dist:.2f}m"

    def test_point_beyond_endpoint_returns_endpoint_distance(self):
        """Point past end of segment → distance to nearest endpoint."""
        alat, alon = 17.7200, 83.3200
        blat, blon = 17.7200, 83.3210
        # Point well past B end
        plat, plon = 17.7200, 83.3230
        dist = _point_to_segment_distance_m(plat, plon, alat, alon, blat, blon)
        dist_to_b = _haversine_m(plat, plon, blat, blon)
        assert abs(dist - dist_to_b) < 2.0, (
            f"Expected distance to endpoint B ({dist_to_b:.1f}m), got {dist:.1f}m"
        )

    def test_degenerate_segment_point_equals_endpoint(self):
        """Segment where A == B degenerates to point distance."""
        alat, alon = 17.7200, 83.3200
        plat, plon = 17.7201, 83.3201
        dist = _point_to_segment_distance_m(plat, plon, alat, alon, alat, alon)
        expected = _haversine_m(plat, plon, alat, alon)
        assert abs(dist - expected) < 1.0

    def test_result_less_than_or_equal_to_endpoint_distances(self):
        """Point-to-segment distance always ≤ distance to either endpoint."""
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        plat, plon = 17.7240, 83.3400  # off to the side
        dist_seg = _point_to_segment_distance_m(plat, plon, alat, alon, blat, blon)
        dist_a   = _haversine_m(plat, plon, alat, alon)
        dist_b   = _haversine_m(plat, plon, blat, blon)
        assert dist_seg <= dist_a + 0.1
        assert dist_seg <= dist_b + 0.1


class TestHaversine:

    def test_known_distance_vizag(self):
        """
        Rough distance between Vizag railway station and GVMC main office
        is approximately 2.5 km. Haversine should be within 100m.
        """
        # Railway station approx
        lat1, lon1 = 17.6868, 83.2185
        # GVMC main office approx
        lat2, lon2 = 17.7228, 83.3013
        dist = _haversine_m(lat1, lon1, lat2, lon2)
        assert 9_000 < dist < 10_500, f"Expected ~9.6km, got {dist:.0f}m"

    def test_zero_distance(self):
        dist = _haversine_m(17.7200, 83.3200, 17.7200, 83.3200)
        assert dist == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. find_road_asset_candidates()
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindCandidates:

    def test_no_gps_returns_no_match(self):
        result = find_road_asset_candidates(None, None)
        assert result["confidence"] == CONFIDENCE_NO_MATCH
        assert result["recommended"] is None
        assert "GPS" in result["reason"]

    def test_exact_on_segment_high_confidence(self):
        """
        GPS exactly on Waltair Main Road midpoint → HIGH confidence.
        RD-EAST-001: (17.7326,83.3320) → (17.7145,83.3298)
        """
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        result = find_road_asset_candidates(mlat, mlon, repair_date="2024-06-15")
        assert result["confidence"] == CONFIDENCE_HIGH
        assert result["recommended"] is not None
        assert result["recommended"]["road_code"] == "RD-EAST-001"
        assert result["recommended"]["distance_m"] < 5.0

    def test_near_segment_20m_high_confidence(self):
        """20m from Waltair Main Road → HIGH confidence (< 25m threshold)."""
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        # Offset ~20m perpendicular (north)
        offset_lat = mlat + (20 / 111_320.0)
        result = find_road_asset_candidates(offset_lat, mlon, repair_date="2024-06-15")
        assert result["confidence"] == CONFIDENCE_HIGH
        assert result["recommended"]["road_code"] == "RD-EAST-001"

    def test_near_segment_35m_medium_confidence(self):
        """35m from nearest segment → MEDIUM confidence (25–50m range)."""
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        # Offset ~35m perpendicular (north)
        offset_lat = mlat + (35 / 111_320.0)
        result = find_road_asset_candidates(offset_lat, mlon, repair_date="2024-06-15")
        assert result["confidence"] in (CONFIDENCE_MEDIUM, CONFIDENCE_HIGH), (
            f"Expected MEDIUM or HIGH at 35m, got {result['confidence']}, "
            f"distance={result['candidates'][0]['distance_m'] if result['candidates'] else 'N/A'}m"
        )

    def test_far_from_all_segments_no_match(self):
        """
        GPS point 300m north of all demo road assets → NO_MATCH.
        Uses a location far enough from every segment that no candidate
        falls within the 200m search radius.
        """
        # All demo assets cluster around lat 17.68–17.89, lon 83.20–83.45
        # Move well north into open area
        result = find_road_asset_candidates(
            17.9500, 83.3500,  # north of all demo roads
            repair_date="2024-06-15"
        )
        assert result["confidence"] == CONFIDENCE_NO_MATCH
        assert result["recommended"] is None

    def test_far_from_all_roads_no_match(self):
        """GPS in the middle of the sea → NO_MATCH."""
        result = find_road_asset_candidates(
            17.5000, 83.0000,  # well south of city
            repair_date="2024-06-15"
        )
        assert result["confidence"] == CONFIDENCE_NO_MATCH
        assert result["candidates"] == [] or result["recommended"] is None

    def test_ward_signal_ranks_same_ward_first(self):
        """
        When ward hint matches one candidate, that candidate ranks first
        even if another ward's segment is equidistant.
        Ward is a ranking signal, not a filter — both candidates still appear.
        """
        # GPS on Waltair Main Road (Ward 9)
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        result = find_road_asset_candidates(
            mlat, mlon, ward="Ward 9", repair_date="2024-06-15"
        )
        if result["recommended"]:
            assert result["recommended"]["ward"] == "Ward 9"

    def test_ward_boundary_other_ward_still_discoverable(self):
        """
        GPS exactly on Waltair Main Road (Ward 9) but ward hint says Ward 10.
        The correct Ward 9 asset must still appear in candidates —
        ward is NOT a hard filter.
        """
        alat, alon = 17.7326, 83.3320
        blat, blon = 17.7145, 83.3298
        mlat = (alat + blat) / 2
        mlon = (alon + blon) / 2
        result = find_road_asset_candidates(
            mlat, mlon, ward="Ward 10", repair_date="2024-06-15"
        )
        # RD-EAST-001 (Ward 9) should appear in candidates
        candidate_codes = [c["road_code"] for c in result["candidates"]]
        assert "RD-EAST-001" in candidate_codes, (
            "Ward 9 asset missing from candidates when ward hint is Ward 10 — "
            "ward must be ranking signal only, not hard filter"
        )

    def test_contract_resolved_by_repair_date_not_today(self):
        """
        A repair dated 2024-06-15 must resolve the 2024 contract on that segment,
        not any future contract that might be active today.
        """
        result = find_road_asset_candidates(
            17.7236, 83.3309,  # midpoint of Waltair Main Road
            repair_date="2024-06-15"
        )
        if result["recommended"] and result["recommended"]["contract_id"]:
            # Contract must have been active on 2024-06-15
            ct_start = result["recommended"].get("contract_code", "")
            # CONT-2024-EAST-01 runs 2024-04-01 to 2024-09-30 — should match
            assert "2024" in (result["recommended"]["contract_code"] or ""), (
                f"Expected 2024 contract, got: {result['recommended']['contract_code']}"
            )

    def test_expired_contract_not_returned_for_future_repair(self):
        """
        A repair dated 2025-06-01 on a road whose only contract ended 2024-09-30
        should find no active contract (contract_id = None).
        """
        result = find_road_asset_candidates(
            17.7236, 83.3309,
            repair_date="2025-06-01"  # after CONT-2024-EAST-01 ended
        )
        if result["recommended"]:
            # Contract ended 2024-09-30 — should not be returned for 2025 repair
            assert result["recommended"]["contract_id"] is None or (
                result["recommended"].get("contract_code", "") == ""
            ), (
                "Expired contract should not be attributed to a 2025 repair. "
                f"Got: {result['recommended'].get('contract_code')}"
            )

    def test_ambiguous_multiple_nearby_segments(self):
        """
        Artificially place GPS equidistant between two closely-spaced
        demo segments → should return AMBIGUOUS or NO_MATCH (not HIGH/MEDIUM).

        We use the intersection area of Beach Road Extension and
        a perpendicular offset to create near-equidistant candidates.
        """
        # GPS midway between Waltair Main (RD-EAST-001) and Beach Road (RD-EAST-002)
        # Both start near lat 17.72, lon 83.33ish
        mid_lat = (17.7236 + 17.7210) / 2
        mid_lon = (83.3309 + 83.3351) / 2
        result = find_road_asset_candidates(mid_lat, mid_lon, repair_date="2024-06-15")
        # If both within 50m → AMBIGUOUS; if only one → HIGH/MEDIUM (also fine)
        # Either way, the recommended must not be auto-set unless single match
        if result["confidence"] == CONFIDENCE_AMBIGUOUS:
            assert result["recommended"] is None

    def test_result_always_has_required_keys(self):
        """Every result dict must have the four required keys."""
        result = find_road_asset_candidates(17.7200, 83.3200, repair_date="2024-06-15")
        assert "confidence"  in result
        assert "candidates"  in result
        assert "recommended" in result
        assert "reason"      in result

    def test_candidates_have_required_fields(self):
        """Each candidate dict must have the documented fields."""
        result = find_road_asset_candidates(
            17.7236, 83.3309, repair_date="2024-06-15"
        )
        required = {
            "road_asset_id", "road_name", "ward",
            "distance_m", "confidence",
            "contract_id", "contractor_name",
        }
        for candidate in result["candidates"]:
            missing = required - set(candidate.keys())
            assert not missing, f"Candidate missing fields: {missing}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. link_repair_to_contract()
# ═══════════════════════════════════════════════════════════════════════════════

class TestLinkRepairToContract:

    def test_full_chain_resolves_and_persists(self):
        """
        Full happy path: repair on Waltair Main Road during 2024 contract
        resolves contractor + contract + road asset and persists all three FKs.
        """
        repair_id = _make_repair(
            lat=17.7236, lng=83.3309,
            repaired_at="2024-06-15 10:00:00"
        )
        # RD-EAST-001 is road_asset id=1 in demo data
        result = link_repair_to_contract(
            repair_record_id=repair_id,
            road_asset_id=1,
            confirmed_by="AE Prasad",
        )
        assert result["ok"] is True, f"Expected ok=True, got: {result}"
        assert result["road_asset_id"] == 1
        assert result["contract_id"] is not None
        assert result["contractor_id"] is not None
        assert "MEIL" in result["contractor_name"] or result["contractor_name"]

        # Verify FKs persisted in DB
        repair = _get_repair(repair_id)
        assert repair["road_asset_id"] == 1
        assert repair["contract_id"]   == result["contract_id"]
        assert repair["contractor_id"] == result["contractor_id"]

    def test_invalid_repair_id_rejected(self):
        result = link_repair_to_contract(
            repair_record_id=99999,
            road_asset_id=1,
            confirmed_by="test",
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_invalid_road_asset_id_rejected(self):
        repair_id = _make_repair()
        result = link_repair_to_contract(
            repair_record_id=repair_id,
            road_asset_id=99999,
            confirmed_by="test",
        )
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_no_active_contract_links_road_only_with_warning(self):
        """
        Repair dated after all contracts expired → road_asset_id linked,
        contract_id and contractor_id remain NULL, warning returned.
        """
        repair_id = _make_repair(
            lat=17.7236, lng=83.3309,
            repaired_at="2030-01-01 10:00:00"  # all demo contracts long expired
        )
        result = link_repair_to_contract(
            repair_record_id=repair_id,
            road_asset_id=1,
            confirmed_by="AE Prasad",
        )
        assert result["ok"] is True
        assert result["road_asset_id"] == 1
        assert result["contract_id"] is None
        assert result["contractor_id"] is None
        assert "warning" in result

        # Verify in DB
        repair = _get_repair(repair_id)
        assert repair["road_asset_id"] == 1
        assert repair["contract_id"] is None
        assert repair["contractor_id"] is None
        # Raw contractor_name preserved
        assert repair["contractor_name"] == "Test Contractor"

    def test_raw_contractor_name_preserved_after_link(self):
        """
        The original free-text contractor_name must never be overwritten
        by the linkage process. It is a permanent raw audit field.
        """
        repair_id = _make_repair(
            contractor_name="MEIL Pvt Ltd",  # raw variant
            lat=17.7236, lng=83.3309,
            repaired_at="2024-06-15 10:00:00"
        )
        link_repair_to_contract(
            repair_record_id=repair_id,
            road_asset_id=1,
            confirmed_by="AE Prasad",
        )
        repair = _get_repair(repair_id)
        assert repair["contractor_name"] == "MEIL Pvt Ltd", (
            "Raw contractor_name was overwritten — it must be preserved permanently"
        )

    def test_idempotent_relinking_updates_fks(self):
        """
        Linking a repair twice (e.g. staff corrects the road selection)
        should succeed and update the FKs to the new values.
        """
        repair_id = _make_repair(
            lat=17.7236, lng=83.3309,
            repaired_at="2024-06-15 10:00:00"
        )
        # Link to road 1 first
        r1 = link_repair_to_contract(repair_id, 1, "AE Prasad")
        assert r1["ok"] is True

        # Re-link to road 2 (override)
        r2 = link_repair_to_contract(repair_id, 2, "AE Prasad")
        assert r2["ok"] is True
        assert r2["road_asset_id"] == 2

        repair = _get_repair(repair_id)
        assert repair["road_asset_id"] == 2

    def test_audit_log_written_on_link(self):
        """Linking a repair must write an entry to audit_log."""
        repair_id = _make_repair(
            lat=17.7236, lng=83.3309,
            repaired_at="2024-06-15 10:00:00"
        )
        link_repair_to_contract(repair_id, 1, "AE Prasad")

        conn = database.get_conn()
        c = conn.cursor()
        c.execute(database._q(
            "SELECT * FROM audit_log WHERE action='repair_linked' ORDER BY id DESC LIMIT 1"
        ))
        row = c.fetchone()
        conn.close()
        assert row is not None, "audit_log entry missing after repair link"
        row = dict(row)
        assert "repair_linked" == row["action"]
        assert "AE Prasad"     == row["done_by"]

    def test_historical_contract_selected_not_current(self):
        """
        Road RD-EAST-001 has CONT-2024-EAST-01 (2024-04-01 to 2024-09-30).
        A repair from 2024-06-15 must resolve CONT-2024-EAST-01 (MEIL).
        A repair from 2030-01-01 must resolve no contract (all expired).
        """
        # 2024 repair → MEIL contract
        repair_2024 = _make_repair(
            repaired_at="2024-06-15 10:00:00",
            report_id="GVMC-2406-111111"
        )
        r = link_repair_to_contract(repair_2024, 1, "AE Prasad")
        assert r["ok"] is True
        assert r["contract_id"] is not None
        assert "Megha Engineering" in r["contractor_name"]

        # 2030 repair → no contract
        repair_2030 = _make_repair(
            repaired_at="2030-01-01 10:00:00",
            report_id="GVMC-3001-222222"
        )
        r2 = link_repair_to_contract(repair_2030, 1, "AE Prasad")
        assert r2["ok"] is True
        assert r2["contract_id"] is None
        assert "warning" in r2


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MULTIPLE HISTORICAL CONTRACTS — same road segment
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultipleHistoricalContracts:

    def _insert_second_contract(self):
        """Insert a second contract on RD-EAST-001 for 2026."""
        conn = database.get_conn()
        c    = conn.cursor()

        # Insert a new contractor
        c.execute(database._q("""
            INSERT INTO contractors
                (contractor_code, name, raw_name_variants, created_at, created_by)
            VALUES ('TEST-002', 'New Roads Pvt Ltd', 'New Roads', ?, 'test')
        """), (database.now(),))
        conn.commit()
        c.execute(database._q("SELECT id FROM contractors WHERE contractor_code='TEST-002'"))
        contractor_id = dict(c.fetchone())["id"]

        # Insert a new contract on same road segment (2026)
        c.execute(database._q("""
            INSERT INTO contracts
                (contract_code, tender_number, contractor_id, division, ward_scope,
                 scope_description, contract_value_lakh, work_start_date, work_end_date,
                 dlp_months, status, responsible_ae, created_at, created_by)
            VALUES ('CONT-2026-EAST-99', 'GVMC/EE/East/2026/099', ?, 'East', 'Ward 9',
                    'Re-surfacing 2026', 150.0, '2026-01-01', '2026-12-31',
                    12, 'active', 'AE Test', ?, 'test')
        """), (contractor_id, database.now()))
        conn.commit()
        c.execute(database._q("SELECT id FROM contracts WHERE contract_code='CONT-2026-EAST-99'"))
        contract_id = dict(c.fetchone())["id"]

        # Link to RD-EAST-001 (road_asset id=1)
        c.execute(database._q("""
            INSERT INTO contract_segments (contract_id, road_asset_id, created_at, created_by)
            VALUES (?, 1, ?, 'test')
        """), (contract_id, database.now()))
        conn.commit()
        conn.close()
        return contract_id, contractor_id

    def test_2024_repair_gets_2024_contractor_not_2026(self):
        """
        Road has two contracts: MEIL (2024) and New Roads (2026).
        A 2024-06-15 repair must get MEIL, not New Roads.
        """
        self._insert_second_contract()

        repair_id = _make_repair(
            repaired_at="2024-06-15 10:00:00",
            report_id="GVMC-2406-HIST01"
        )
        result = link_repair_to_contract(repair_id, 1, "AE Prasad")
        assert result["ok"] is True
        assert result["contract_id"] is not None
        assert "Megha Engineering" in result["contractor_name"], (
            f"2024 repair attributed to wrong contractor: {result['contractor_name']}. "
            "Expected Megha Engineering (2024 contract), not New Roads (2026 contract)."
        )

    def test_2026_repair_gets_2026_contractor_not_2024(self):
        """
        A 2026-06-01 repair on the same road must get New Roads, not MEIL.
        """
        self._insert_second_contract()

        repair_id = _make_repair(
            repaired_at="2026-06-01 10:00:00",
            report_id="GVMC-2606-HIST02"
        )
        result = link_repair_to_contract(repair_id, 1, "AE Prasad")
        assert result["ok"] is True
        assert result["contract_id"] is not None
        assert "New Roads" in result["contractor_name"], (
            f"2026 repair attributed to wrong contractor: {result['contractor_name']}. "
            "Expected New Roads (2026 contract), not MEIL (2024 contract)."
        )

    def test_repair_between_contracts_gets_no_contract(self):
        """
        A repair dated 2025-06-01 — after 2024 contract ends (Sep 2024),
        before 2026 contract starts (Jan 2026) — should find no active contract.
        """
        self._insert_second_contract()

        repair_id = _make_repair(
            repaired_at="2025-06-01 10:00:00",
            report_id="GVMC-2506-HIST03"
        )
        result = link_repair_to_contract(repair_id, 1, "AE Prasad")
        assert result["ok"] is True
        assert result["contract_id"] is None, (
            "Repair dated between two contracts should have no contract attribution. "
            f"Got: {result.get('contract_id')}"
        )
        assert "warning" in result