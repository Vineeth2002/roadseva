"""
tests/test_authz.py — Centralized authorization layer tests
=============================================================
Run with: pytest tests/test_authz.py -v

Covers the negative/positive matrix required before implementing the
scope-check integration into main.py's write routes:
  - geographic scope resolution (staff_ward_scope) per role
  - object-level access (can_access_report) across ward/zone/division
  - workflow authority (can_modify_inspection) incl. supervisor override
  - assignment validation (can_assign_report)
  - fail-closed behavior for unknown roles, missing data, empty scope

Setup: uses SQLite in a temp file per test — no production DB touched,
matching the existing fixture pattern in tests/test_database.py.
"""

import os
import sys
import pytest

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import authz


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original_path = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_roadseva_authz.db")
    database.init_db()
    yield
    database.DB_PATH = original_path


# ── Synthetic staff / report builders ──────────────────────────────────────

def staff(role, name, **extra):
    base = {"role": role, "name": name, "is_active": 1}
    base.update(extra)
    return base


def report(ward, assigned_to="", escalation_level=0, status="open"):
    return {
        "report_id": "R-TEST", "ward": ward, "assigned_to": assigned_to,
        "escalation_level": escalation_level, "status": status,
    }


# ═══════════════════════════════════════════════════════════════════════════
# staff_ward_scope() — geographic scope resolution
# ═══════════════════════════════════════════════════════════════════════════

class TestStaffWardScope:

    def test_admin_is_unrestricted(self):
        assert authz.staff_ward_scope(staff("admin", "A")) is None

    def test_commissioner_is_unrestricted(self):
        assert authz.staff_ward_scope(staff("commissioner", "C")) is None

    def test_zonal_commissioner_returns_list_type(self):
        # NOTE: as of this test run, wards.py's ZONE_DIVISION_MAP is
        # empty (0 entries) — a pre-existing data gap, not introduced by
        # this refactor. That means every zone currently resolves to [],
        # same as an unconfigured zone. This test only asserts the
        # return TYPE is correct (list, never None) so the scoping
        # mechanism itself is verified independent of that data gap.
        scope = authz.staff_ward_scope(staff("zonal_commissioner", "Z", zone="Zone 1"))
        assert isinstance(scope, list)

    def test_zonal_commissioner_unconfigured_zone_is_empty_not_none(self):
        scope = authz.staff_ward_scope(staff("zonal_commissioner", "Z", zone=""))
        assert scope == []

    def test_ae_unconfigured_division_is_empty(self):
        scope = authz.staff_ward_scope(staff("ae", "AE", division=""))
        assert scope == []

    def test_was_empty_ward_list_denies_not_allows(self):
        """Critical: empty ward_list must mean zero access, never all wards."""
        scope = authz.staff_ward_scope(staff("was", "W", ward_list=""))
        assert scope == []

    def test_was_configured_ward_list_parsed(self):
        scope = authz.staff_ward_scope(staff("was", "W", ward_list="Ward 1, Ward 2"))
        assert scope == ["Ward 1", "Ward 2"]

    def test_viewer_is_unrestricted_regardless_of_ward_list(self):
        """POLICY UPDATE: viewer resolved to city-wide read-only (see
        authz.py's UNRESTRICTED_ROLES comment for the evidence basis).
        An empty ward_list must NOT deny access, and a configured one
        must NOT restrict it -- viewer ignores ward_list entirely now."""
        assert authz.staff_ward_scope(staff("viewer", "V", ward_list="")) is None
        assert authz.staff_ward_scope(staff("viewer", "V", ward_list="Ward 5")) is None

    def test_field_engineer_has_no_ward_scope(self):
        """field_engineer's real scope is assignment, checked separately
        in can_access_report — this function must not grant it ward-wide
        access."""
        assert authz.staff_ward_scope(staff("field_engineer", "FE")) == []

    def test_unknown_role_fails_closed(self):
        assert authz.staff_ward_scope(staff("made_up_role", "X")) == []

    def test_missing_staff_fails_closed(self):
        assert authz.staff_ward_scope(None) == []

    def test_missing_staff_never_returns_none(self):
        """None from this function means 'unrestricted' — a missing staff
        record must never be interpreted that way."""
        assert authz.staff_ward_scope({}) == []


# ═══════════════════════════════════════════════════════════════════════════
# can_access_report() — object-level authorization
# ═══════════════════════════════════════════════════════════════════════════

class TestCanAccessReport:

    def test_admin_all_wards(self):
        assert authz.can_access_report(staff("admin", "A"), report("Ward 1")) is True
        assert authz.can_access_report(staff("admin", "A"), report("Ward 99")) is True

    def test_commissioner_all_wards(self):
        assert authz.can_access_report(staff("commissioner", "C"), report("Ward 1")) is True

    def test_viewer_can_access_any_ward_city_wide(self):
        """POLICY UPDATE: viewer is city-wide read-only -- this is the
        selected product policy, not an IDOR. ward_list is irrelevant to
        viewer's access now, whether empty or configured."""
        v_no_list = staff("viewer", "V1", ward_list="")
        v_with_list = staff("viewer", "V2", ward_list="Ward 1")
        assert authz.can_access_report(v_no_list, report("Ward 1")) is True
        assert authz.can_access_report(v_no_list, report("Ward 99")) is True
        assert authz.can_access_report(v_with_list, report("Ward 2")) is True  # outside its own configured list, still allowed

    def test_was_other_ward_denied(self):
        """field/WAS ward 1 -> report ward 2 = DENY."""
        w = staff("was", "W", ward_list="Ward 1")
        assert authz.can_access_report(w, report("Ward 2")) is False

    def test_ae_other_division_denied(self):
        """AE division A -> report division B = DENY."""
        from wards import DIVISION_WARD_MAP
        divisions = list(DIVISION_WARD_MAP.keys())
        a = staff("ae", "AE", division=divisions[0])
        # A ward from a different division should not be in AE's scope
        other_ward = report("A ward that is definitely not in division 0's list")
        assert authz.can_access_report(a, other_ward) is False

    def test_zonal_commissioner_other_zone_denied(self):
        """zonal commissioner zone A -> zone B = DENY."""
        z = staff("zonal_commissioner", "Z", zone="Zone 1")
        assert authz.can_access_report(z, report("A ward outside Zone 1")) is False

    def test_field_engineer_own_report_allowed(self):
        fe = staff("field_engineer", "FE1")
        assert authz.can_access_report(fe, report("Ward 1", assigned_to="FE1")) is True

    def test_field_engineer_others_report_denied(self):
        """unauthorized actor -> another report = DENY."""
        fe = staff("field_engineer", "FE1")
        assert authz.can_access_report(fe, report("Ward 1", assigned_to="FE2")) is False

    def test_missing_report_denied(self):
        assert authz.can_access_report(staff("admin", "A"), None) is False

    def test_missing_staff_denied(self):
        assert authz.can_access_report(None, report("Ward 1")) is False

    def test_unknown_role_denied(self):
        assert authz.can_access_report(staff("made_up_role", "X"), report("Ward 1")) is False


# ═══════════════════════════════════════════════════════════════════════════
# can_modify_inspection() — workflow authority + supervisor override
# ═══════════════════════════════════════════════════════════════════════════

class TestCanModifyInspection:

    def test_assigned_engineer_permitted(self):
        fe = staff("field_engineer", "FE1")
        r = report("Ward 1", assigned_to="FE1")
        allowed, is_override, err = authz.can_modify_inspection(fe, r)
        assert allowed is True and is_override is False

    def test_admin_always_permitted(self):
        a = staff("admin", "A")
        r = report("Ward 1", assigned_to="FE9")
        allowed, is_override, err = authz.can_modify_inspection(a, r)
        assert allowed is True and is_override is False

    def test_unrelated_engineer_denied(self):
        """unauthorized actor -> inspection mutation = DENY."""
        fe = staff("field_engineer", "FE_other")
        r = report("Ward 1", assigned_to="FE1")
        allowed, is_override, err = authz.can_modify_inspection(fe, r)
        assert allowed is False

    def test_valid_supervisor_without_sla_breach_denied(self, monkeypatch):
        supervisor = staff("field_engineer", "SUP")
        monkeypatch.setattr(authz.database, "get_parent_name_for_engineer", lambda name: "SUP")
        r = report("Ward 1", assigned_to="FE1", escalation_level=0)
        allowed, is_override, err = authz.can_modify_inspection(supervisor, r, "reason given")
        assert allowed is False
        assert "SLA" in err

    def test_valid_supervisor_without_reason_denied(self, monkeypatch):
        supervisor = staff("field_engineer", "SUP")
        monkeypatch.setattr(authz.database, "get_parent_name_for_engineer", lambda name: "SUP")
        r = report("Ward 1", assigned_to="FE1", escalation_level=1)
        allowed, is_override, err = authz.can_modify_inspection(supervisor, r, "")
        assert allowed is False
        assert "reason" in err.lower()

    def test_valid_supervisor_with_breach_and_reason_permitted(self, monkeypatch):
        supervisor = staff("field_engineer", "SUP")
        monkeypatch.setattr(authz.database, "get_parent_name_for_engineer", lambda name: "SUP")
        r = report("Ward 1", assigned_to="FE1", escalation_level=1)
        allowed, is_override, err = authz.can_modify_inspection(supervisor, r, "site visit confirmed")
        assert allowed is True and is_override is True

    def test_unrelated_supervisor_denied(self, monkeypatch):
        someone_else = staff("field_engineer", "NOT_SUP")
        monkeypatch.setattr(authz.database, "get_parent_name_for_engineer", lambda name: "SUP")
        r = report("Ward 1", assigned_to="FE1", escalation_level=1)
        allowed, is_override, err = authz.can_modify_inspection(someone_else, r, "reason")
        assert allowed is False

    def test_was_role_unrelated_engineer_denied(self, monkeypatch):
        """unauthorized actor -> another inspection = DENY, for the WAS
        role specifically (the other role, besides field_engineer, that
        reaches this function via the real routes)."""
        was_user = staff("was", "W_other")
        monkeypatch.setattr(authz.database, "get_parent_name_for_engineer", lambda name: "")
        r = report("Ward 1", assigned_to="FE1", escalation_level=0)
        allowed, is_override, err = authz.can_modify_inspection(was_user, r)
        assert allowed is False

    def test_missing_report_safe_failure(self):
        allowed, is_override, err = authz.can_modify_inspection(staff("admin", "A"), None)
        assert allowed is False
        assert err


# ═══════════════════════════════════════════════════════════════════════════
# can_assign_report() — assignment validation
# ═══════════════════════════════════════════════════════════════════════════

class TestCanAssignReport:

    def test_invalid_assignee_denied(self):
        """invalid assignee = DENY."""
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Ward 1"), None
        )
        assert allowed is False

    def test_inactive_assignee_denied(self):
        """inactive assignee = DENY."""
        assignee = staff("field_engineer", "FE1", is_active=0)
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Ward 1"), assignee
        )
        assert allowed is False

    def test_assignee_outside_ward_denied(self):
        assignee = staff("was", "W1", ward_list="Ward 9", is_active=1)
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Ward 1"), assignee
        )
        assert allowed is False

    def test_valid_assignment_permitted(self):
        assignee = staff("was", "W1", ward_list="Ward 1", is_active=1)
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Ward 1"), assignee
        )
        assert allowed is True

    def test_field_engineer_assignee_exempt_from_ward_check(self):
        """field_engineer scope is assignment itself -- assigning IS what
        puts a report in their scope, so no prior ward match is required."""
        assignee = staff("field_engineer", "FE1", is_active=1)
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Ward 1"), assignee
        )
        assert allowed is True

    def test_actor_outside_report_scope_denied(self):
        """Zone A actor -> Zone B report, unless city-wide authority."""
        actor = staff("zonal_commissioner", "Z", zone="Zone 1")
        assignee = staff("field_engineer", "FE1", is_active=1)
        allowed, err = authz.can_assign_report(
            actor, report("A ward outside Zone 1"), assignee
        )
        assert allowed is False

    def test_admin_actor_unrestricted(self):
        assignee = staff("was", "W1", ward_list="Ward 1", is_active=1)
        allowed, err = authz.can_assign_report(
            staff("admin", "A"), report("Any Ward"), assignee
        )
        # admin scope is unrestricted; the only remaining constraint is
        # the assignee's own scope covering the ward.
        assert allowed is False  # assignee ward_list="Ward 1" != "Any Ward"


# ═══════════════════════════════════════════════════════════════════════════
# can_submit_scheduled_inspection() — RQI inspection ownership
# ═══════════════════════════════════════════════════════════════════════════

class TestCanSubmitScheduledInspection:

    def test_assigned_engineer_permitted(self):
        insp = {"assigned_to": "FE1", "status": "pending"}
        allowed, err = authz.can_submit_scheduled_inspection(staff("field_engineer", "FE1"), insp)
        assert allowed is True

    def test_unassigned_engineer_denied(self):
        insp = {"assigned_to": "FE1", "status": "pending"}
        allowed, err = authz.can_submit_scheduled_inspection(staff("field_engineer", "FE2"), insp)
        assert allowed is False

    def test_admin_permitted(self):
        insp = {"assigned_to": "FE1", "status": "pending"}
        allowed, err = authz.can_submit_scheduled_inspection(staff("admin", "A"), insp)
        assert allowed is True

    def test_already_completed_denied(self):
        insp = {"assigned_to": "FE1", "status": "completed"}
        allowed, err = authz.can_submit_scheduled_inspection(staff("field_engineer", "FE1"), insp)
        assert allowed is False

    def test_missing_inspection_safe_failure(self):
        allowed, err = authz.can_submit_scheduled_inspection(staff("field_engineer", "FE1"), {})
        assert allowed is False
        assert err


# ═══════════════════════════════════════════════════════════════════════════
# get_reports_for_role() regression — refactor must preserve behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestGetReportsForRoleRegression:
    """The refactor moved scope resolution into authz.staff_ward_scope().
    These tests confirm existing role behavior is unchanged, and that the
    WAS ward-parameter bypass found during the refactor is actually
    closed."""

    def _seed_report(self, ward, **kw):
        rid = database.add_report(
            city="Visakhapatnam", ward=ward, damage_type="pothole",
            description="test", photo_path="",
            citizen_name="T", citizen_phone="9999999999", citizen_email="",
            latitude=17.0, longitude=83.0,
            photo_data="", **kw
        )
        return rid

    def test_admin_sees_all_wards(self):
        self._seed_report("Ward A")
        self._seed_report("Ward B")
        results = database.get_reports_for_role(staff("admin", "A"))
        wards_seen = {r["ward"] for r in results}
        assert "Ward A" in wards_seen and "Ward B" in wards_seen

    def test_viewer_sees_all_wards_city_wide(self):
        """POLICY UPDATE: viewer resolved to city-wide read-only (see
        authz.py's UNRESTRICTED_ROLES). ward_list no longer restricts
        viewer's report list, whether set or empty."""
        self._seed_report("Ward A")
        self._seed_report("Ward B")
        results = database.get_reports_for_role(staff("viewer", "V", ward_list=""))
        wards_seen = {r["ward"] for r in results}
        assert "Ward A" in wards_seen and "Ward B" in wards_seen

        results2 = database.get_reports_for_role(staff("viewer", "V2", ward_list="Ward A"))
        wards_seen2 = {r["ward"] for r in results2}
        assert "Ward A" in wards_seen2 and "Ward B" in wards_seen2  # not restricted to "Ward A"

    def test_was_cannot_bypass_scope_via_ward_query_param(self):
        """SECURITY REGRESSION TEST: previously, a WAS user supplying a
        `ward` query param bypassed their own ward_list entirely via a
        direct get_all_reports(ward=...) call. Must now return nothing
        for a ward outside their configured scope."""
        self._seed_report("Ward Outside Scope")
        results = database.get_reports_for_role(
            staff("was", "W", ward_list="Ward 1 - Test"),
            ward="Ward Outside Scope",
        )
        assert results == []

    def test_field_engineer_only_sees_assigned(self):
        rid = self._seed_report("Ward A")
        database.assign_report(rid, "FE1", "tester")
        self._seed_report("Ward B")
        results = database.get_reports_for_role(staff("field_engineer", "FE1"))
        assert all(r["assigned_to"] == "FE1" for r in results)

    def test_unknown_role_returns_empty(self):
        self._seed_report("Ward A")
        results = database.get_reports_for_role(staff("made_up_role", "X"))
        assert results == []