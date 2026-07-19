"""
pre_demo_check.py — RoadSeva Pre-Deploy Health Check
=====================================================
Run before every demo or Render deployment.
Catches broken imports, missing DB columns, wrong passwords,
routing failures, and role mismatches before they embarrass you.

Command:
  D:/Anaconda/python.exe pre_demo_check.py

Exit code 0 = all green, safe to demo.
Exit code 1 = failures found, fix before presenting.
"""

import sys
import os

failures  = []
warnings  = []
passed    = []

def ok(msg):    passed.append(msg);   print(f"  ✅ {msg}")
def warn(msg):  warnings.append(msg); print(f"  ⚠️  {msg}")
def fail(msg):  failures.append(msg); print(f"  ❌ {msg}")


print("\n" + "="*60)
print("  RoadSeva — Pre-Demo Health Check")
print("="*60)

# ── 1. Core imports ───────────────────────────────────────────────────────────
print("\n[1] Core Imports")

try:
    import database
    ok("database.py imports cleanly")
except Exception as e:
    fail(f"database.py import failed: {e}")

try:
    import wards
    ok("wards.py imports cleanly")
except Exception as e:
    fail(f"wards.py import failed: {e}")

try:
    import watchdog
    ok("watchdog.py imports cleanly")
except Exception as e:
    fail(f"watchdog.py import failed: {e}")

try:
    import security
    ok("security.py imports cleanly")
except Exception as e:
    fail(f"security.py import failed: {e}")

try:
    # Check main.py exists and has key symbols without importing the full app
    import ast, pathlib
    main_src = pathlib.Path("main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    required_in_main = {"lifespan", "submit", "login_post", "commissioner", "api_stats"}
    missing_fns = required_in_main - names
    if missing_fns:
        fail(f"main.py missing routes/functions: {missing_fns}")
    else:
        ok("main.py syntax valid, key routes present")
except FileNotFoundError:
    fail("main.py not found in project directory")
except SyntaxError as e:
    fail(f"main.py has syntax error: {e}")
except Exception as e:
    warn(f"main.py check skipped: {e}")

# ── 2. Ward routing brain ─────────────────────────────────────────────────────
print("\n[2] Ward → Division Routing")

try:
    from wards import DIVISION_WARD_MAP, WARD_DIVISION_MAP, get_division_for_ward_name

    # All 98 wards covered
    total_mapped = len(WARD_DIVISION_MAP)
    if total_mapped == 98:
        ok(f"All 98 wards mapped to divisions")
    else:
        missing = set(range(1, 99)) - set(WARD_DIVISION_MAP.keys())
        fail(f"Only {total_mapped}/98 wards mapped — missing: {sorted(missing)[:10]}")

    # All 10 divisions present
    expected_divisions = {
        "Bheemunipatnam","Madhurawada","East","North","South",
        "West","Pendurthi","Gajuwaka","Aganampudi","Anakapalli"
    }
    actual_divisions = set(DIVISION_WARD_MAP.keys())
    if actual_divisions == expected_divisions:
        ok("All 10 GVMC divisions present")
    else:
        missing_div = expected_divisions - actual_divisions
        extra_div   = actual_divisions - expected_divisions
        if missing_div: fail(f"Missing divisions: {missing_div}")
        if extra_div:   warn(f"Extra divisions: {extra_div}")

    # Spot check key wards
    routing_tests = [
        ("Ward 1 - Kondapeta / Wilsonpeta",             "Bheemunipatnam"),
        ("Ward 6 - Madhurawada / Bakkannapalem",         "Madhurawada"),
        ("Ward 22 - Sivajipalem / AU Campus",            "East"),
        ("Ward 14 - Seethammadhara / BS Layout",         "North"),
        ("Ward 27 - Srinagar / Dondaparthi",             "South"),
        ("Ward 40 - Malkapuram / Dolphin Hills",         "West"),
        ("Ward 64 - Pedagantyada / Yarada / Gangavaram", "Gajuwaka"),
        ("Ward 77 - Pittavanipalem / Steel Plant",       "Aganampudi"),
        ("Ward 80 - Anakapalli / Gavarapalem",           "Anakapalli"),
        ("Ward 88 - Narava / Duvvada",                   "Pendurthi"),
        ("Ward 98 - Simhachalam / Kommadi / Kapuluppada","Madhurawada"),
        ("Ward 85 - Aganampudi / Lankelapalem",          "Aganampudi"),
    ]
    route_fails = []
    for ward_name, expected in routing_tests:
        got = get_division_for_ward_name(ward_name)
        if got != expected:
            route_fails.append(f"{ward_name} → {got} (expected {expected})")
    if route_fails:
        for rf in route_fails: fail(f"Routing wrong: {rf}")
    else:
        ok(f"All {len(routing_tests)} routing spot checks passed")

except Exception as e:
    fail(f"Routing brain check failed: {e}")

# ── 3. Database init ──────────────────────────────────────────────────────────
print("\n[3] Database Init")

try:
    database.init_db()
    ok("init_db() completed without errors")
except Exception as e:
    fail(f"init_db() failed: {e}")

# ── 4. Schema columns check ───────────────────────────────────────────────────
print("\n[4] Schema Columns")

try:
    conn = database.get_conn()
    c    = conn.cursor()

    # Reports table
    c.execute("PRAGMA table_info(reports)")
    report_cols = {row[1] if isinstance(row, tuple) else dict(row)["name"]
                   for row in c.fetchall()}

    required_report_cols = {
        "report_id", "ward", "damage_type", "status", "assigned_to",
        "division_name", "sla_expiry_time", "escalation_count",
        "sla_warning", "training_label", "citizen_review_status",
        "work_done_photo", "latitude", "longitude",
        "photo_data", "severity", "verified_damage_type",
    }
    missing_report = required_report_cols - report_cols
    if missing_report:
        fail(f"reports table missing columns: {missing_report}")
    else:
        ok(f"reports table has all required columns ({len(report_cols)} total)")

    # Staff table
    c.execute("PRAGMA table_info(staff)")
    staff_cols = {row[1] if isinstance(row, tuple) else dict(row)["name"]
                  for row in c.fetchall()}

    required_staff_cols = {
        "id", "name", "username", "password_hash", "role",
        "is_active", "must_change_password", "zone", "ward_list",
        "parent_id",
    }
    missing_staff = required_staff_cols - staff_cols
    if missing_staff:
        # supervised_by is backward-compatible fallback
        still_missing = missing_staff - {"parent_id"}
        if still_missing:
            fail(f"staff table missing columns: {still_missing}")
        else:
            warn("staff.parent_id missing — using supervised_by (backward compatible)")
    else:
        ok(f"staff table has all required columns ({len(staff_cols)} total)")

    conn.close()

except Exception as e:
    fail(f"Schema check failed: {e}")

# ── 5. Role system ────────────────────────────────────────────────────────────
print("\n[5] Role System")

try:
    expected_roles = {
        "admin", "commissioner", "zonal_commissioner",
        "ae", "was", "field_engineer", "viewer"
    }
    actual_roles = set(database.VALID_ROLES)
    missing_roles = expected_roles - actual_roles
    if missing_roles:
        fail(f"VALID_ROLES missing: {missing_roles}")
    else:
        ok(f"All 7 GVMC hierarchy roles present in VALID_ROLES")

    # ROLE_HOME mapping
    if hasattr(database, "ROLE_HOME"):
        missing_home = expected_roles - set(database.ROLE_HOME.keys())
        if missing_home:
            warn(f"ROLE_HOME missing entries for: {missing_home}")
        else:
            ok("ROLE_HOME has entries for all 7 roles")
    else:
        warn("ROLE_HOME not defined in database.py")

    # CREATABLE_ROLES hierarchy
    if "was" in database.CREATABLE_ROLES.get("commissioner", []):
        ok("Commissioner can create WAS accounts")
    else:
        warn("Commissioner cannot create WAS accounts — check CREATABLE_ROLES")

except Exception as e:
    fail(f"Role system check failed: {e}")

# ── 6. Admin account ──────────────────────────────────────────────────────────
print("\n[6] Admin Account")

try:
    staff, err = database.authenticate_staff("admin", "Admin@2024!" , "127.0.0.1")
    if staff:
        ok(f"admin login works — role: {staff['role']}")
    else:
        fail(f"admin login failed: {err}")
except Exception as e:
    fail(f"Admin auth check failed: {e}")

# ── 7. Demo accounts ──────────────────────────────────────────────────────────
print("\n[7] Demo Accounts")

demo_logins = [
    ("commissioner1", "Comm@2024!",    "commissioner"),
    ("ae_east",       "AeEast@2024!",  "ae"),
    ("was_ward22",    "Was22@2024!",   "was"),
    ("engineer1",     "Field@2024!",   "field_engineer"),
    ("viewer1",       "View@2024!",    "viewer"),
]

for username, password, expected_role in demo_logins:
    try:
        staff, err = database.authenticate_staff(username, password, "127.0.0.1")
        if staff:
            if staff["role"] == expected_role:
                ok(f"{username} ({expected_role})")
            else:
                warn(f"{username} — role is '{staff['role']}', expected '{expected_role}'")
        else:
            warn(f"{username} — not found (run create_demo_accounts.py first)")
    except Exception as e:
        fail(f"{username} check failed: {e}")

# ── 8. Key database functions ─────────────────────────────────────────────────
print("\n[8] Key Database Functions")

required_functions = [
    "get_division_by_ward",
    "get_was_for_ward",
    "get_ae_for_division",
    "get_zonal_commissioner_for_division",
    "get_parent_name",
    "add_report",
    "get_reports_for_role",
    "get_zone_performance",
    "get_live_stats_for_role",
    "get_commissioner_data",
    "save_work_done_photo",
    "save_citizen_review",
    "get_disputed_reports_for_review",
    "log_training_label",
    "save_training_sample",
    "get_training_stats",
]

for fn_name in required_functions:
    if hasattr(database, fn_name) and callable(getattr(database, fn_name)):
        ok(fn_name)
    else:
        fail(f"database.{fn_name}() — MISSING")

# ── 9. WAS auto-assignment ────────────────────────────────────────────────────
print("\n[9] WAS Auto-Assignment")

try:
    was22  = database.get_was_for_ward("Ward 22 - Sivajipalem / AU Campus")
    was64  = database.get_was_for_ward("Ward 64 - Pedagantyada / Yarada / Gangavaram")
    was999 = database.get_was_for_ward("Ward 99 - Nonexistent Ward")

    if was22:
        ok(f"Ward 22 WAS found: {was22}")
    else:
        warn("Ward 22 — no WAS mapped yet (run create_demo_accounts.py)")

    if was64:
        ok(f"Ward 64 WAS found: {was64}")
    else:
        warn("Ward 64 — no WAS mapped yet (run create_demo_accounts.py)")

    if was999 is None:
        ok("Non-existent ward returns None (correct)")
    else:
        warn(f"Non-existent ward returned: {was999}")

except Exception as e:
    fail(f"WAS auto-assignment check failed: {e}")

# ── 10. Zone performance query ────────────────────────────────────────────────
print("\n[10] Zone Performance Query")

try:
    zone_data = database.get_zone_performance()
    if isinstance(zone_data, list):
        ok(f"get_zone_performance() returns list ({len(zone_data)} divisions in DB)")
    else:
        fail(f"get_zone_performance() returned unexpected type: {type(zone_data)}")
except Exception as e:
    fail(f"get_zone_performance() failed: {e}")

# ── 11. SLA watchdog ──────────────────────────────────────────────────────────
print("\n[11] SLA Watchdog")

try:
    from watchdog import get_sla_dashboard_data
    sla_data = get_sla_dashboard_data()
    required_keys = {"sla_breached_now", "sla_warning_now", "total_escalations", "recent_breaches"}
    missing_keys  = required_keys - set(sla_data.keys())
    if missing_keys:
        fail(f"get_sla_dashboard_data() missing keys: {missing_keys}")
    else:
        ok(f"get_sla_dashboard_data() OK — breached_now={sla_data['sla_breached_now']}")
except Exception as e:
    fail(f"Watchdog check failed: {e}")

try:
    from watchdog import start_watchdog, stop_watchdog
    ok("start_watchdog / stop_watchdog importable")
except Exception as e:
    fail(f"Watchdog start/stop not importable: {e}")

# ── 12. Security layer ────────────────────────────────────────────────────────
print("\n[12] Security Layer")

try:
    from security import (
        deep_inspect_photo, generate_csrf_token, verify_csrf_token,
        check_rate_limit, sanitize_input, validate_status, validate_role,
        is_safe_redirect, VALID_ROLES as SEC_VALID_ROLES,
        SecurityGatewayMiddleware, SecurityHeadersMiddleware,
    )
    ok("All security functions importable")

    # VALID_ROLES in security.py should include new hierarchy roles
    new_roles = {"was", "ae", "zonal_commissioner"}
    if new_roles.issubset(SEC_VALID_ROLES):
        ok(f"security.VALID_ROLES includes new hierarchy roles")
    else:
        missing_in_sec = new_roles - SEC_VALID_ROLES
        fail(f"security.VALID_ROLES missing: {missing_in_sec}")

    # _is_private_ip operator precedence fix
    from security import _is_private_ip
    tests = [
        ("10.0.0.1",     True),
        ("192.168.1.1",  True),
        ("172.16.0.1",   True),
        ("172.31.255.255",True),
        ("172.32.0.1",   False),  # just outside range
        ("127.0.0.1",    True),
        ("8.8.8.8",      False),
    ]
    ip_test_pass = True
    for ip, expected in tests:
        got = _is_private_ip(ip)
        if got != expected:
            fail(f"_is_private_ip({ip}) = {got}, expected {expected}")
            ip_test_pass = False
    if ip_test_pass:
        ok("_is_private_ip() all edge cases correct (BUG1 fixed)")

    # CSRF token roundtrip
    token = generate_csrf_token("test_session_token_123")
    if verify_csrf_token("test_session_token_123", token):
        ok("CSRF token generate/verify roundtrip works")
    else:
        fail("CSRF token roundtrip FAILED")

    # Status validation
    if validate_status("resolved") and not validate_status("hack_attempt"):
        ok("validate_status() correctly accepts/rejects values")
    else:
        fail("validate_status() validation broken")

    # Safe redirect
    if is_safe_redirect("/staff") and not is_safe_redirect("http://evil.com"):
        ok("is_safe_redirect() correctly rejects external URLs")
    else:
        fail("is_safe_redirect() broken")

except Exception as e:
    fail(f"Security layer check failed: {e}")

# ── 13. Training data ─────────────────────────────────────────────────────────
print("\n[13] Training Data")

try:
    stats = database.get_training_stats()
    ok(f"Training CSV readable — {stats['total']} samples, {stats.get('corrections',0)} corrections")

    if hasattr(database, "TRAINING_CSV_HEADERS"):
        headers = database.TRAINING_CSV_HEADERS
        required_headers = {"ward_number", "division", "label_correct"}
        present = set(headers)
        missing_h = required_headers - present
        if missing_h:
            warn(f"Training CSV missing extended headers: {missing_h}")
        else:
            ok("Training CSV has extended headers (ward_number, division, label_correct)")

except Exception as e:
    fail(f"Training data check failed: {e}")

# ── 14. Environment ───────────────────────────────────────────────────────────
print("\n[14] Environment")

db_url = os.getenv("DATABASE_URL", "")
if db_url:
    ok(f"PostgreSQL mode (DATABASE_URL set)")
else:
    ok("SQLite mode (local/Render free tier)")

secret = os.getenv("SECRET_KEY", "")
if secret and len(secret) >= 32:
    ok("SECRET_KEY set and strong")
elif secret:
    warn("SECRET_KEY set but short — use 32+ random chars in production")
else:
    warn("SECRET_KEY not set — using random key (sessions won't survive restart)")

groq_key = os.getenv("GROQ_API_KEY", "")
if groq_key:
    ok("GROQ_API_KEY set")
else:
    warn("GROQ_API_KEY not set — AI severity analysis will be disabled")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
total   = len(passed) + len(warnings) + len(failures)
print(f"  Results: {len(passed)} passed  {len(warnings)} warnings  {len(failures)} failed")
print("="*60)

if failures:
    print("\n  ❌ FAILED CHECKS (fix before demo):")
    for f in failures:
        print(f"     → {f}")

if warnings:
    print("\n  ⚠️  WARNINGS (review before demo):")
    for w in warnings:
        print(f"     → {w}")

if not failures:
    print("\n  ✅ All critical checks passed. System is demo-ready.")
    print()
    sys.exit(0)
else:
    print(f"\n  Fix {len(failures)} failure(s) before running the demo.")
    print()
    sys.exit(1)