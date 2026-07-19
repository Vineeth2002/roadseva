"""
permissions.py — Single source of truth for role-based access control.

WHY THIS FILE EXISTS
---------------------
main.py and database.py each independently define ROLE_HOME and ROLE_LABELS.
They have already diverged: main.py includes an "officer" role in both
dicts; database.py's copies do not. That means any code path that reads
database.py's ROLE_HOME["officer"] or ROLE_LABELS["officer"] will silently
fall through to a .get() default (or KeyError on plain [] access) instead
of getting the value main.py's routes assume exists.

TODO — CONFIRM BEFORE MERGING:
  Is "officer" a live role or a legacy alias for "grievance_officer" / "ae"?
  main.py uses staff["role"] in ("officer", "ae") and
  staff["role"] == "officer" in several places (update_report_status,
  assign flows). database.py's CREATABLE_ROLES / ROLE_LABELS / ROLE_HOME
  never mention "officer" at all. Until this is confirmed, "officer" is
  kept below with its main.py-derived home/label, flagged explicitly, so
  nothing is silently dropped or silently invented.

MIGRATION PLAN (do this before touching main.py/database.py structure):
  1. Drop this file in as C:\\RoadSeva\\permissions.py
  2. In main.py and database.py, DELETE the local ROLE_HOME, ROLE_LABELS,
     COMMISSIONER_ROLES, STAFF_ROLES, FIELD_ROLES, TRIAGE_ROLES,
     CREATABLE_ROLES, ROLE_CAN_CREATE definitions.
  3. Replace with: from permissions import (ROLE_HOME, ROLE_LABELS,
     COMMISSIONER_ROLES, STAFF_ROLES, FIELD_ROLES, TRIAGE_ROLES,
     ROLE_CAN_CREATE, require_role)
  4. Run the app locally, log in as each of the 9 roles, hit every
     dashboard once. This is your characterization test — you don't have
     automated coverage for role-scoped routes, so this manual pass is
     the only thing standing between you and a silent access regression.
  5. Only after that passes clean: start replacing individual
     `if staff["role"] not in (...): return RedirectResponse(...)` blocks
     with `require_role(...)` one route at a time, testing each as you go.
     Do NOT batch-replace all 58 sites in one commit — you will not be
     able to tell which failure came from which change.
"""

from functools import wraps
from fastapi.responses import RedirectResponse

# ---------------------------------------------------------------------------
# Canonical role groups (reconciled from main.py + database.py)
# ---------------------------------------------------------------------------

COMMISSIONER_ROLES = {"commissioner", "admin", "zonal_commissioner"}
STAFF_ROLES         = {"officer", "ae", "viewer"}
FIELD_ROLES          = {"field_engineer", "was"}
TRIAGE_ROLES         = {"grievance_officer", "triage_officer"}

# ---------------------------------------------------------------------------
# ROLE_HOME / ROLE_LABELS — reconciled. "officer" kept per main.py behavior;
# flagged above as needing your confirmation, not silently resolved.
# ---------------------------------------------------------------------------

ROLE_HOME = {
    "admin":              "/commissioner",
    "commissioner":       "/commissioner",
    "zonal_commissioner": "/commissioner",
    "ae":                 "/staff",
    "officer":            "/staff",   # TODO: confirm — see note above
    "grievance_officer":  "/triage",
    "triage_officer":     "/triage",
    "was":                "/field",
    "field_engineer":     "/field",
    "viewer":             "/staff",
}

ROLE_LABELS = {
    "admin":              "IT Administrator",
    "commissioner":       "Commissioner",
    "zonal_commissioner": "Zonal Commissioner",
    "ae":                 "Assistant Engineer",
    "officer":            "Grievance Officer",  # TODO: confirm — see note above
    "grievance_officer":  "Grievance Officer",
    "triage_officer":     "Triage Officer",
    "was":                "Ward Amenities Secretary",
    "field_engineer":     "Field Engineer",
    "viewer":             "Viewer / Corporator",
}

# Who can create which roles (hierarchical) — from database.py, unchanged.
CREATABLE_ROLES = {
    "admin": [
        "admin", "commissioner", "zonal_commissioner",
        "ae", "grievance_officer", "triage_officer",
        "was", "field_engineer", "viewer",
    ],
    "commissioner": [
        "zonal_commissioner", "ae", "grievance_officer",
        "triage_officer", "was", "field_engineer", "viewer",
    ],
    "zonal_commissioner": ["ae", "was", "field_engineer", "viewer"],
    "ae":                 ["was", "field_engineer"],
}

VALID_ROLES     = set(ROLE_LABELS.keys())
ROLE_CAN_CREATE = {k: set(v) for k, v in CREATABLE_ROLES.items()}


def can_manage_user(creator_role: str, target_role: str) -> bool:
    return target_role in ROLE_CAN_CREATE.get(creator_role, set())


def home_for(role: str) -> str:
    """Where to redirect a role after login / on access-denied."""
    return ROLE_HOME.get(role, "/staff")


def require_role(*allowed_roles):
    """
    Decorator for FastAPI route handlers that take `staff` (the session
    dict) as an argument. Replaces the repeated pattern:

        if staff["role"] not in (...):
            return RedirectResponse(ROLE_HOME.get(staff["role"], "/staff"), status_code=302)

    Usage:
        @app.get("/triage")
        @require_role(*TRIAGE_ROLES, "admin", "commissioner")
        def triage_page(staff=Depends(get_current_staff)):
            ...

    NOTE: this only replaces the *shape* of the check. Each of the 58
    existing call sites has its own allowed-role tuple — some overlapping,
    some not. Migrate them one at a time (see MIGRATION PLAN above);
    do not assume two visually-similar tuples were meant to be identical
    without checking against the route's actual behavior.
    """
    allowed = set(allowed_roles)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            staff = kwargs.get("staff")
            if staff is None:
                for a in args:
                    if isinstance(a, dict) and "role" in a:
                        staff = a
                        break
            if staff is None or staff.get("role") not in allowed:
                role = staff.get("role") if staff else None
                return RedirectResponse(home_for(role), status_code=302)
            return fn(*args, **kwargs)
        return wrapper
    return decorator