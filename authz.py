"""
authz.py — Object-level, geographic, and workflow authorization.

Layered on top of permissions.py, which only answers "is this ROLE
allowed to perform this CLASS of action" (role authorization). This
module answers the questions permissions.py cannot:

  2. GEOGRAPHIC SCOPE     -- staff_ward_scope(staff)
  3. OBJECT AUTHORIZATION -- can_access_report(staff, report)
  4. WORKFLOW AUTHORITY   -- can_modify_inspection(...), can_assign_report(...)
  5. SPECIAL OVERRIDE     -- handled inside can_modify_inspection()

Every route must still call permissions.check_role() first. This module
does not replace that check — it adds the object-level check that was
previously missing after the role check passed.

FAIL CLOSED, always:
  - Unknown/unrecognized role  -> no access, never "allow."
  - Missing/empty ward_list    -> no access, never "all wards."
  - Missing report/staff       -> no access.
Never write `if scope is missing: return all_reports` anywhere in this
file. That exact anti-pattern is what created the vulnerabilities this
module fixes.
"""

from typing import Optional, Tuple

import database
from wards import get_wards_for_zone, get_wards_for_division


# Roles with genuinely unrestricted, city-wide authority. Deliberately a
# short, explicit list — NOT "every role we haven't scoped yet." Adding a
# role here is a product decision, not a default.
#
# POLICY DECISION (viewer): resolved to city-wide read-only. Evidence:
# templates/staff.html's own viewer-facing banner states "You can see
# all grievances" — first-party product copy, not an inference. The
# database.py role documentation also explicitly describes geographic
# scope for zonal_commissioner/ae/was but deliberately does not for
# viewer. An earlier pass had scoped viewer to ward_list based on the
# "Viewer / Corporator" label alone; that was a reasonable but
# insufficiently-evidenced guess, corrected here. Viewer remains
# read-only — this only changes WHAT it can read, not whether it can
# write (it still can't; see permissions.py's route-level checks).
UNRESTRICTED_ROLES = {"admin", "commissioner", "viewer"}

# Roles whose scope is a configured ward_list column. viewer was
# previously here; moved to UNRESTRICTED_ROLES per the policy decision
# above. Only "was" remains ward_list-scoped now.
WARD_LIST_ROLES = {"was"}


def staff_ward_scope(staff: dict) -> Optional[list]:
    """
    Returns the list of ward-name strings this staff member may operate
    within, or None meaning "genuinely unrestricted" (admin, commissioner,
    and viewer — see UNRESTRICTED_ROLES above for why viewer is included).

    An empty list ([]) means "scoped, but nothing configured or resolvable"
    — this is NOT the same as None. Callers must treat [] as "touches
    nothing." That distinction is the entire reason this function exists
    instead of just returning a boolean.

    This is the single implementation of ward-scope resolution. It
    replaces three previously-separate inline implementations inside
    database.get_reports_for_role() (one per role branch) — that function
    now calls this one instead of recomputing the same logic three ways.
    """
    if not staff:
        return []

    role = staff.get("role")

    if role in UNRESTRICTED_ROLES:
        return None

    if role == "zonal_commissioner":
        zone = staff.get("zone", "")
        return get_wards_for_zone(zone) if zone else []

    if role == "ae":
        division = staff.get("division", "")
        return get_wards_for_division(division) if division else []

    if role in WARD_LIST_ROLES:
        ward_str = staff.get("ward_list", "") or ""
        return [w.strip() for w in ward_str.split(",") if w.strip()]

    if role in ("grievance_officer", "triage_officer"):
        # Matches database.get_reports_for_role()'s existing, deliberate
        # choice to leave these two roles unscoped (see the comment
        # there) -- not a new grant of privilege. Without this branch,
        # these roles would fall through to "unknown role -> []" here
        # while still having unrestricted list-level access elsewhere,
        # which would make complaint-story MORE restrictive than the
        # /staff list these same roles already see. That inconsistency,
        # not a deliberate policy decision, is what this branch avoids.
        # Whether these two roles SHOULD be ward-scoped is a real open
        # product question -- flagged, not resolved, here or there.
        return None

    if role == "field_engineer":
        # field_engineer's real scope is per-report assignment, not ward
        # membership — see can_access_report() below, which checks
        # assigned_to directly for this role. Returning [] here (rather
        # than, say, treating it as unrestricted) means this function
        # never accidentally grants ward-wide access to a role whose
        # actual authority is "only reports assigned to me."
        return []

    # Unknown / unsupported role (grievance_officer, triage_officer, or
    # anything future) — fail closed. Callers that need those roles
    # scoped will get an explicit [] here rather than silent full access;
    # if/when those roles need real ward scoping, that's a deliberate
    # addition to this function, not something to infer.
    return []


def can_access_report(staff: dict, report: dict) -> bool:
    """
    Object-level check: does this staff member's scope cover this
    specific report? This is the check that was missing from
    complaint-story, assign-ward, flag-incorrect-ward, approve-ward-flag,
    resolve-dispute, update-status, and add-comment — all of which had a
    role check but nothing after it.

    Does NOT check workflow authority (assigned-engineer / supervisor
    override) — see can_modify_inspection() for actions that need that
    additional layer.
    """
    if not staff or not report:
        return False

    role = staff.get("role")

    if role == "field_engineer":
        return report.get("assigned_to") == staff.get("name")

    scope = staff_ward_scope(staff)
    if scope is None:
        return True
    return report.get("ward") in scope


def can_modify_report(staff: dict, report: dict) -> bool:
    """
    Geographic/object gate for general report-mutation routes (assign-ward,
    flag-incorrect-ward, approve-ward-flag, resolve-dispute, update-status,
    add-comment). Role authorization for these routes is still
    permissions.check_role()'s job and must be called first — this
    function only answers the object-scope question that check leaves
    open.
    """
    return can_access_report(staff, report)


def can_modify_inspection(
    staff: dict, report: dict, override_reason: str = ""
) -> Tuple[bool, bool, str]:
    """
    Workflow-authority gate for inspection actions: start-inspection,
    was-verify-damage, complete-inspection, reject-inspection,
    submit-inspection, verify-inspection.

    Reuses, unchanged, the exact logic already proven correct in the
    original /verify-inspection route: the assigned engineer may always
    act; otherwise their direct supervisor may act, but only when the
    report is already SLA-breached (escalation_level >= 1) and a written
    override reason is supplied. Admin retains override authority as a
    city-wide role. This function does not weaken any of those four
    conditions — it generalizes them so the other five inspection routes
    get the same protection /verify-inspection already had, instead of
    each needing its own copy.

    Deliberately does NOT call can_access_report() as a pre-check: the
    field_engineer branch of can_access_report() only recognizes the
    assigned engineer, which would incorrectly reject a legitimate
    supervisor override before the supervisor logic below ever runs. The
    routes that call this (start/complete/reject/was-verify-damage/
    submit/verify-inspection) are already role-gated to FIELD_ROLES+admin
    at the route level, so a separate ward-scope gate here is redundant
    with, and would conflict with, the assignment/supervisor model this
    function implements instead.

    Returns (allowed, is_override, error_message). error_message is only
    meaningful when allowed is False, matching the existing redirect
    patterns in main.py.
    """
    if not staff or not report:
        return False, False, "Report not found"

    is_assigned_engineer = report.get("assigned_to") == staff.get("name")
    is_admin = staff.get("role") == "admin"
    if is_assigned_engineer or is_admin:
        return True, False, ""

    parent_name = database.get_parent_name_for_engineer(report.get("assigned_to", ""))
    if staff.get("name") != parent_name:
        return False, False, "Only the assigned engineer or their supervisor can submit this"
    if int(report.get("escalation_level", 0) or 0) < 1:
        return False, False, "SLA not yet breached — wait or contact the assigned engineer"
    if not (override_reason or "").strip():
        return False, False, "Override reason required"

    return True, True, ""


def can_submit_scheduled_inspection(staff: dict, inspection: dict) -> Tuple[bool, str]:
    """
    Separate from can_modify_inspection() on purpose: scheduled_inspections
    (the RQI/repair-durability warranty-check system) is a distinct object
    type from `reports` — it has its own `assigned_to` and no `ward` or
    `escalation_level` field, so it isn't shaped like a report and
    shouldn't be forced through can_modify_inspection(). Its ownership
    model is simpler: assigned engineer or admin only, no supervisor/SLA
    override exists for this workflow today.
    """
    if not staff or not inspection:
        return False, "Inspection not found"
    if inspection.get("status") == "completed":
        return False, "Inspection already completed"
    if staff.get("role") == "admin":
        return True, ""
    if inspection.get("assigned_to") == staff.get("name"):
        return True, ""
    return False, "This inspection is not assigned to you"


def can_assign_report(staff: dict, report: dict, assignee: Optional[dict]) -> Tuple[bool, str]:
    """
    Assignment-specific gate, used by /assign-report (and reusable by
    /assign-ward). Checks, in order:
      1. The acting staff member's own scope covers the report (so an AE
         in Zone A cannot reassign a Zone B report).
      2. The proposed assignee is a real, resolvable staff account — not
         an arbitrary form string (previously assigned_to was accepted
         with zero validation that it referred to anyone).
      3. The assignee's account is active.
      4. The assignee's own scope covers the report's ward, so a report
         can't be handed to a WAS/AE whose configured area doesn't
         include it. field_engineer is exempt from this specific check
         since that role's scope is assignment itself, not ward
         membership — assigning IS what puts it in their scope.

    Does not check whether the acting staff member's ROLE is allowed to
    assign at all — permissions.check_role() at the route is still
    responsible for that.
    """
    if not can_access_report(staff, report):
        return False, "Report is outside your assigned area"

    if not assignee:
        return False, "Assignee not found — enter an exact, existing staff name"
    if not assignee.get("is_active", 1):
        return False, "Assignee account is inactive"

    if assignee.get("role") != "field_engineer":
        assignee_scope = staff_ward_scope(assignee)
        if assignee_scope is not None and report.get("ward") not in assignee_scope:
            return False, "Assignee's configured area does not include this report's ward"

    return True, ""