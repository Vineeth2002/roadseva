"""
permissions.py — Single source of truth for role-based access control.

WHY THIS FILE EXISTS
---------------------
ROLE_HOME and ROLE_LABELS were previously defined independently in both
main.py and database.py. That divergence caused a real bug (the "officer"
role existed in main.py's copy but not database.py's) that silently broke
part of the app for hours before being found. This file does NOT redefine
those — it imports them from database.py, which is the schema owner and
the copy that's been verified correct all session. main.py should import
role data from HERE, not define its own.

MIGRATION PLAN — do this one route group at a time, not all at once:
  1. In main.py, delete the local ROLE_HOME, ROLE_LABELS, COMMISSIONER_ROLES,
     STAFF_ROLES, FIELD_ROLES, TRIAGE_ROLES definitions.
  2. Add: from permissions import (ROLE_HOME, ROLE_LABELS, COMMISSIONER_ROLES,
     STAFF_ROLES, FIELD_ROLES, TRIAGE_ROLES, check_role, redirect_home)
  3. Pick ONE route. Replace its inline role-check tuple with check_role().
     Restart, test that specific route across 2-3 roles. Confirm no regression.
  4. Repeat for the next route. Do not batch-convert all 50+ sites at once —
     that's exactly the mistake that made this file unused for months.
"""

from fastapi.responses import RedirectResponse
import database

# ── Canonical role data — imported, not redefined ─────────────────────────────
ROLE_HOME       = database.ROLE_HOME
ROLE_LABELS     = database.ROLE_LABELS
VALID_ROLES     = database.VALID_ROLES
ROLE_CAN_CREATE = database.ROLE_CAN_CREATE

# ── Role groups — previously duplicated in main.py, now defined once ──────────
COMMISSIONER_ROLES = {"commissioner", "admin", "zonal_commissioner"}
STAFF_ROLES         = {"ae", "viewer"}
FIELD_ROLES          = {"field_engineer", "was"}
TRIAGE_ROLES         = {"grievance_officer", "triage_officer"}


def home_for(role: str) -> str:
    """Where to redirect a role after login / on access-denied."""
    return ROLE_HOME.get(role, "/staff")


def redirect_home(staff: dict) -> RedirectResponse:
    """Shorthand for the extremely common 'not allowed, send them home' pattern."""
    role = staff.get("role") if staff else None
    return RedirectResponse(home_for(role), status_code=302)


def check_role(staff: dict, *allowed_roles) -> bool:
    """
    Returns True if staff's role is in allowed_roles (or any of the passed
    role-group sets, since sets can be unpacked with * too).

    Usage — replaces the existing inline pattern:
        OLD:
            if staff["role"] not in ("commissioner","admin","zonal_commissioner","ae"):
                return RedirectResponse(ROLE_HOME.get(staff["role"],"/staff"), status_code=302)
        NEW:
            if not check_role(staff, *COMMISSIONER_ROLES, "ae"):
                return redirect_home(staff)
    """
    if not staff:
        return False
    return staff.get("role") in allowed_roles