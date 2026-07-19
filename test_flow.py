"""
test_flow.py — RoadSeva end-to-end runtime walkthrough
=========================================================
Run this from C:\\RoadSeva with: D:\\Anaconda\\python.exe test_flow.py

Exercises the full citizen -> triage -> WAS -> AE -> citizen review loop
directly against database.py functions (no HTTP/FastAPI layer involved),
so any failure here is a real logic/schema bug, not a routing issue.

Safe to run multiple times — uses fresh report IDs each run.
Does NOT delete any data. You can inspect roadseva.db afterwards.
"""

import sys
import database

PASS = "  [PASS]"
FAIL = "  [FAIL]"
INFO = "  [INFO]"

failures = []

def check(label, condition, detail=""):
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}  {detail}")
        failures.append(label)

def section(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


# ─────────────────────────────────────────────────────────────────────────
section("SETUP — ensure schema is current")
database.init_db()
try:
    from watchdog import init_watchdog_schema
    init_watchdog_schema()
except Exception as e:
    print(f"{INFO} watchdog schema init skipped: {e}")


# ─────────────────────────────────────────────────────────────────────────
section("STEP 1 — Citizen submits complaint WITH GPS (ward known)")

report_id_gps = database.add_report(
    city="GVMC", ward="Ward 41 - Maddilapalem", damage_type="Pothole",
    description="Large pothole near bus stand", photo_path="",
    citizen_name="Test Citizen GPS", citizen_phone="9876543210",
    citizen_email="testgps@example.com",
    latitude=17.7231, longitude=83.3018,
    photo_data="", location_text="",
)
print(f"{INFO} report_id = {report_id_gps}")

r1 = database.get_report_by_id(report_id_gps)
check("Report created and retrievable", r1 is not None)
check("Status is 'open' (ward was provided)", r1 and r1.get("status") == "open", f"got: {r1.get('status') if r1 else None}")
check("division_name was persisted (FIX 10)", r1 and r1.get("division_name"), f"got: {r1.get('division_name') if r1 else None!r}")
check("sla_due_at was set", r1 and r1.get("sla_due_at"), f"got: {r1.get('sla_due_at') if r1 else None!r}")
check("escalation_level starts at 0", r1 and r1.get("escalation_level") == 0)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 2 — Citizen submits complaint WITHOUT GPS, WITHOUT ward (needs triage)")

report_id_notriage = database.add_report(
    city="GVMC", ward="", damage_type="Road Damage",
    description="", photo_path="",
    citizen_name="Test Citizen NoGPS", citizen_phone="9876543211",
    citizen_email="",
    latitude=None, longitude=None,
    photo_data="", location_text="Near Asilmetta junction, opposite the old cinema hall",
)
print(f"{INFO} report_id = {report_id_notriage}")

r2 = database.get_report_by_id(report_id_notriage)
check("Report created", r2 is not None)
check("Status is 'pending_triage' (no ward)", r2 and r2.get("status") == "pending_triage", f"got: {r2.get('status') if r2 else None}")
check("location_text was saved", r2 and r2.get("location_text") == "Near Asilmetta junction, opposite the old cinema hall")
check("division_name is blank (ward unknown yet)", r2 and r2.get("division_name") == "", f"got: {r2.get('division_name') if r2 else None!r}")

pending = database.get_pending_triage_reports(limit=100)
pending_ids = [p["report_id"] for p in pending]
check("New report appears in triage queue", report_id_notriage in pending_ids)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 3 — Triage officer assigns ward to the GPS-failed complaint")

ok, msg = database.assign_ward_from_triage(
    report_id_notriage, "Ward 12 - Asilmetta", "Test Triage Officer"
)
check("assign_ward_from_triage() succeeded", ok, msg)

r2b = database.get_report_by_id(report_id_notriage)
check("Status moved to 'open'", r2b and r2b.get("status") == "open", f"got: {r2b.get('status') if r2b else None}")
check("Ward was set", r2b and r2b.get("ward") == "Ward 12 - Asilmetta")
check("division_name backfilled after triage (FIX 10)", r2b and r2b.get("division_name"), f"got: {r2b.get('division_name') if r2b else None!r}")
check("sla_due_at set after triage assignment", r2b and r2b.get("sla_due_at"))

pending_after = database.get_pending_triage_reports(limit=100)
pending_ids_after = [p["report_id"] for p in pending_after]
check("Report no longer in triage queue", report_id_notriage not in pending_ids_after)

# ─────────────────────────────────────────────────────────────────────────
section("STEP 3b — AE assigns the triaged complaint to a WAS (assign_report)")

result = database.assign_report(
    report_id_notriage, "Test WAS User", "Test AE User", assigned_officer="Test AE User"
)
check("assign_report() returns a tuple, not None", result is not None, f"got: {result!r}")
if result is not None:
    ok, msg = result
    check("assign_report() succeeded", ok, msg)

r2e = database.get_report_by_id(report_id_notriage)
check("Status moved to 'assigned'", r2e and r2e.get("status") == "assigned", f"got: {r2e.get('status') if r2e else None}")
check("assigned_to was set", r2e and r2e.get("assigned_to") == "Test WAS User")

# ─────────────────────────────────────────────────────────────────────────
section("STEP 4 — WAS flags incorrect ward, AE approves reassignment")

ok, msg = database.flag_incorrect_ward(
    report_id_gps, "Ward 12 - Asilmetta", "Actually closer to Asilmetta junction", "Test WAS User"
)
check("flag_incorrect_ward() succeeded", ok, msg)

r1b = database.get_report_by_id(report_id_gps)
check("ward_flag_status is 'pending'", r1b and r1b.get("ward_flag_status") == "pending", f"got: {r1b.get('ward_flag_status') if r1b else None}")

flags = database.get_pending_ward_flags()
flag_report_ids = [f["report_id"] for f in flags]
check("Flag appears in pending list", report_id_gps in flag_report_ids)

ok, msg = database.approve_ward_reassignment(
    report_id_gps, approved=True, reviewed_by="Test AE User", reason="Confirmed via site knowledge"
)
check("approve_ward_reassignment() succeeded", ok, msg)

r1c = database.get_report_by_id(report_id_gps)
check("Ward was changed to requested ward", r1c and r1c.get("ward") == "Ward 12 - Asilmetta", f"got: {r1c.get('ward') if r1c else None}")
check("division_name recomputed after approval (FIX 10)", r1c and r1c.get("division_name"), f"got: {r1c.get('division_name') if r1c else None!r}")
check("ward_flag_status is 'approved'", r1c and r1c.get("ward_flag_status") == "approved")


# ─────────────────────────────────────────────────────────────────────────
section("STEP 5 — WAS marks work done, citizen review token created")

database.update_report_status(report_id_gps, "assigned", "Test WAS User")
database.mark_work_done(report_id_gps, "data:image/jpeg;base64,FAKEDATA", "Test WAS User")
database.update_report_status(report_id_gps, "resolved", "Test WAS User")

r1d = database.get_report_by_id(report_id_gps)
check("work_done_photo was saved", r1d and r1d.get("work_done_photo"), f"got: {bool(r1d.get('work_done_photo')) if r1d else None}")
check("Status is 'resolved'", r1d and r1d.get("status") == "resolved")

review_token = database.create_citizen_review_token(report_id_gps)
check("Review token generated", bool(review_token) and len(review_token) > 10)

review_data = database.get_review_by_token(review_token)
check("Token resolves back to the report", review_data and review_data.get("rid") == report_id_gps)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 6 — Citizen confirms satisfied -> complaint closes")

ok, result = database.submit_citizen_review(review_token, satisfied=True, comment="Looks good, thank you")
check("submit_citizen_review() succeeded", ok, result)
check("Result is 'closed'", result == "closed", f"got: {result}")

r1e = database.get_report_by_id(report_id_gps)
check("Report status is 'closed'", r1e and r1e.get("status") == "closed", f"got: {r1e.get('status') if r1e else None}")
check("citizen_satisfied is 1", r1e and r1e.get("citizen_satisfied") == 1)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 7 — Second complaint: work done -> citizen DISPUTES -> AE site visit required")

database.update_report_status(report_id_notriage, "assigned", "Test WAS User 2")
database.mark_work_done(report_id_notriage, "data:image/jpeg;base64,FAKEDATA2", "Test WAS User 2")
database.update_report_status(report_id_notriage, "resolved", "Test WAS User 2")

review_token_2 = database.create_citizen_review_token(report_id_notriage)
ok, result = database.submit_citizen_review(review_token_2, satisfied=False, comment="Pothole still there!")
check("submit_citizen_review() succeeded for dispute", ok, result)
check("Result is 'disputed_ae_required'", result == "disputed_ae_required", f"got: {result}")

r2c = database.get_report_by_id(report_id_notriage)
check("Status is 'disputed'", r2c and r2c.get("status") == "disputed", f"got: {r2c.get('status') if r2c else None}")
check("ae_site_visit_required flag is set", r2c and r2c.get("ae_site_visit_required") == 1)
check("dispute_count incremented", r2c and r2c.get("dispute_count") == 1, f"got: {r2c.get('dispute_count') if r2c else None}")

disputed_division = r2c.get("division_name", "") if r2c else ""
ae_queue = database.get_disputed_reports_for_ae(division=disputed_division)
ae_queue_ids = [d["report_id"] for d in ae_queue]
check("Disputed report appears in AE queue (scoped by division)", report_id_notriage in ae_queue_ids,
      f"queue division={disputed_division!r}, found_ids={ae_queue_ids}")

ae_queue_all = database.get_disputed_reports_for_ae()
ae_queue_all_ids = [d["report_id"] for d in ae_queue_all]
check("Disputed report appears in unscoped AE queue too", report_id_notriage in ae_queue_all_ids)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 8 — AE resolves the dispute (force close after site visit)")

ok, msg = database.ae_resolve_disputed(
    report_id_notriage, decision="force_close", ae_name="Test AE User", ae_notes="Verified, work was actually fine"
)
check("ae_resolve_disputed() succeeded", ok, msg)

r2d = database.get_report_by_id(report_id_notriage)
check("Status is 'closed'", r2d and r2d.get("status") == "closed", f"got: {r2d.get('status') if r2d else None}")
check("ae_site_visit_done is 1", r2d and r2d.get("ae_site_visit_done") == 1)


# ─────────────────────────────────────────────────────────────────────────
section("STEP 9 — SLA escalation lookup functions (no live breach expected yet, just checking they run)")

try:
    breached_0 = database.get_sla_breached_reports(0)
    check("get_sla_breached_reports(0) runs without error", True)
    print(f"{INFO} currently {len(breached_0)} report(s) breached at level 0")
except Exception as e:
    check("get_sla_breached_reports(0) runs without error", False, str(e))

try:
    warning_0 = database.get_sla_warning_reports(0, warning_hours=999999)
    check("get_sla_warning_reports(0) runs without error", True)
    print(f"{INFO} {len(warning_0)} report(s) within (very wide) warning window")
except Exception as e:
    check("get_sla_warning_reports(0) runs without error", False, str(e))

try:
    staff_lvl0 = database.get_staff_for_escalation_level(0, ward="Ward 12 - Asilmetta")
    check("get_staff_for_escalation_level(0, ward=...) runs", True)
    print(f"{INFO} {len(staff_lvl0)} WAS staff found for that ward")
except Exception as e:
    check("get_staff_for_escalation_level(0, ward=...) runs", False, str(e))

try:
    staff_lvl1 = database.get_staff_for_escalation_level(1, zone="Test Division")
    check("get_staff_for_escalation_level(1, zone=...) runs (FIX 2)", True)
    print(f"{INFO} {len(staff_lvl1)} AE staff found for zone='Test Division'")
except Exception as e:
    check("get_staff_for_escalation_level(1, zone=...) runs (FIX 2)", False, str(e))


# ─────────────────────────────────────────────────────────────────────────
section("STEP 10 — notifications.py wiring sanity check (no real SMS/email sent, just no-crash check)")

try:
    import notifications
    result = notifications.notify_citizen(
        event="submitted", report_id=report_id_gps, name="Test Citizen",
        phone="9876543210", email="test@example.com", ward="Ward 12 - Asilmetta",
    )
    check("notify_citizen() runs without error", isinstance(result, dict), f"got: {result}")
    print(f"{INFO} notify result: {result} (False/False expected if MSG91/RESEND keys not set)")
except Exception as e:
    check("notify_citizen() runs without error", False, str(e))


# ─────────────────────────────────────────────────────────────────────────
section("SUMMARY")

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:\n")
    for f in failures:
        print(f"  - {f}")
    print("\nPaste this full output back so we can fix the failures.")
    sys.exit(1)
else:
    print("\nALL CHECKS PASSED. The full citizen -> triage -> WAS -> AE -> citizen review")
    print("loop is verified working end-to-end against your real database.py.")
    print(f"\nTest report IDs created (safe to leave in DB, or delete manually):")
    print(f"  {report_id_gps}")
    print(f"  {report_id_notriage}")
    sys.exit(0)