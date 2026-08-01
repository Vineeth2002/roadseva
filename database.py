"""
database.py — RoadSeva
Complete database layer.
Dual SQLite/PostgreSQL, bcrypt passwords, full schema with 9-role hierarchy.

ROLES (production-grade, hard walls everywhere):
  admin               → full system access + admin panel
  commissioner        → city-wide dashboard, RQI, no individual assignment
  zonal_commissioner  → zone-scoped dashboard, can reassign within zone
  ae                  → division-scoped, approves ward flags, closes disputed tickets
  grievance_officer   → triage queue, assigns ward to GPS-failed complaints
  triage_officer      → alias for grievance_officer (same permissions)
  was                 → ward-level, first responder, flags incorrect wards
  field_engineer      → assigned complaints only (legacy / pilot compatibility)
  viewer              → read-only, no write access

SLA LADDER:
  Hour 0   → Complaint filed → WAS assigned
  Hour 36  → WAS warning SMS (12h before breach)
  Hour 48  → WAS breach → AE takes over
  Hour 60  → AE warning SMS (12h before breach)
  Hour 72  → AE breach → ZC takes over
  Hour 84  → ZC warning SMS (12h before breach)
  Hour 96  → ZC breach → Commissioner flagged
  Hour 108 → Commissioner warning SMS (12h before max)
  Hour 120 → Maximum escalation — daily reminders

SECOND DISPUTE:
  Citizen disputes resolution → AE site visit required
  AE physically verifies → can Force Close OR Reopen
  No auto-close on second dispute (prevents citizen frustration)

WARD FLAGS:
  WAS flags "Incorrect Ward" → AE approves/rejects
  ward_reassignment_log tracks full audit trail
  SLA clock continues through flag (no pause)
"""

import os, csv, base64, sqlite3, secrets, string
from datetime import datetime, timezone, timedelta
import bcrypt

DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)
DB_PATH      = "roadseva.db"

# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_ipv4(url: str) -> str:
    """Force IPv4 resolution to avoid Render/Neon IPv6 issues."""
    try:
        import socket, re
        host = re.search(r'@([^:/]+)', url)
        if not host:
            return ""
        hostname = host.group(1)
        results = socket.getaddrinfo(hostname, 5432, socket.AF_INET)
        if results:
            return results[0][4][0]
    except Exception:
        pass
    return ""

def get_conn():
    if USE_POSTGRES:
        import psycopg
        ipv4 = _resolve_ipv4(DATABASE_URL)
        conn = psycopg.connect(
            DATABASE_URL,
            row_factory=psycopg.rows.dict_row,
            **({"hostaddr": ipv4} if ipv4 else {})
        )
        return conn
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def now() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

def _q(sql):
    """Convert ? placeholders to %s for PostgreSQL."""
    return sql.replace("?", "%s") if USE_POSTGRES else sql

# ─────────────────────────────────────────────────────────────────────────────
# ROLE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

ROLE_LABELS = {
    "admin":               "IT Administrator",
    "commissioner":        "Commissioner",
    "zonal_commissioner":  "Zonal Commissioner",
    "ae":                  "Assistant Engineer",
    "grievance_officer":   "Grievance Officer",
    "triage_officer":      "Triage Officer",
    "was":                 "Ward Amenities Secretary",
    "field_engineer":      "Field Engineer",       # legacy / pilot
    "viewer":              "Viewer / Corporator",
}

# Who can log into which dashboard
ROLE_HOME = {
    "admin":              "/commissioner",
    "commissioner":       "/commissioner",
    "zonal_commissioner": "/commissioner",
    "ae":                 "/staff",
    "grievance_officer":  "/triage",
    "triage_officer":     "/triage",
    "was":                "/field",
    "field_engineer":     "/field",
    "viewer":             "/staff",
}

# Who can create which roles (hierarchical)
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

VALID_ROLES    = set(ROLE_LABELS.keys())
ROLE_CAN_CREATE = {k: set(v) for k, v in CREATABLE_ROLES.items()}

# Roles that can READ all reports in their scope
READ_ROLES = {
    "admin", "commissioner", "zonal_commissioner",
    "ae", "viewer", "grievance_officer", "triage_officer",
}

# Roles that can WRITE status changes
WRITE_ROLES = {
    "admin", "commissioner", "zonal_commissioner", "ae", "was", "field_engineer",
}

def can_manage_user(creator_role: str, target_role: str) -> bool:
    return target_role in ROLE_CAN_CREATE.get(creator_role, set())

# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD SECURITY
# ─────────────────────────────────────────────────────────────────────────────

BLOCKED_PASSWORDS = {
    "password","password1","password123","passw0rd","admin123","admin@123",
    "admin1234","welcome1","welcome@1","welcome123","letmein","letmein1",
    "test1234","123456","12345678","1234567890","qwerty","qwerty123",
    "qwerty@123","iloveyou","sunshine","monkey","123","@123",
    "2024","2025","2026","visakha1","vizag123","vizag@123",
    "andhra123","andhra@123","road@123","roads123","roadseva",
    "commissioner1","officer123","india123","india@123",
}

def validate_password_strength(password: str) -> tuple:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Must contain uppercase (A-Z)"
    if not any(c.islower() for c in password):
        return False, "Must contain lowercase (a-z)"
    if not any(c.isdigit() for c in password):
        return False, "Must contain a number (0-9)"
    specials = set("!@#$%^&*()_+-=[]{}|;:,.<>?/~`")
    if not any(c in specials for c in password):
        return False, "Must contain a special character (!@#$% etc.)"
    if len(set(password)) < 4:
        return False, "Password is too repetitive"
    pw_lower = password.lower()
    if pw_lower in BLOCKED_PASSWORDS:
        return False, "This password is too common"
    _BRAND_SUBSTRINGS = {"roadseva", "vizag", "visakha", "andhra"}
    if any(brand in pw_lower for brand in _BRAND_SUBSTRINGS):
        return False, "This password is too common"
    return True, ""

def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")

def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def generate_temp_password(length=12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        ok, _ = validate_password_strength(pwd)
        if ok:
            return pwd

# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA INIT
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs("data", exist_ok=True)
    os.makedirs("training_data", exist_ok=True)
    conn = get_conn()
    c    = conn.cursor()

    if not USE_POSTGRES:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE,
                city TEXT,
                ward TEXT,
                division_name TEXT DEFAULT "",
                damage_type TEXT,
                description TEXT,
                location_text TEXT DEFAULT "",
                photo_path TEXT DEFAULT "",
                photo_data TEXT DEFAULT "",
                status TEXT DEFAULT "open",
                submitted_at TEXT,
                updated_at TEXT,
                updated_by TEXT DEFAULT "",
                citizen_name TEXT DEFAULT "",
                citizen_phone TEXT DEFAULT "",
                citizen_email TEXT DEFAULT "",
                assigned_to TEXT DEFAULT "",
                assigned_officer TEXT DEFAULT "",
                latitude REAL,
                longitude REAL,
                severity TEXT DEFAULT "unknown",
                severity_details TEXT DEFAULT "",
                estimated_cost TEXT DEFAULT "",
                urgency TEXT DEFAULT "",
                accident_risk TEXT DEFAULT "",
                recommended_action TEXT DEFAULT "",
                estimated_size TEXT DEFAULT "",
                intake_channel TEXT DEFAULT "citizen_web",
                intake_ref TEXT DEFAULT "",
                rqi_flagged INTEGER DEFAULT 0,
                verified_damage_type TEXT DEFAULT "",
                site_condition TEXT DEFAULT "",
                site_photo_data TEXT DEFAULT "",
                verified_by TEXT DEFAULT "",
                verified_at TEXT DEFAULT "",
                ward_flag_reason TEXT DEFAULT "",
                ward_flag_status TEXT DEFAULT "",
                ward_flag_by TEXT DEFAULT "",
                ward_flag_at TEXT DEFAULT "",
                escalation_level INTEGER DEFAULT 0,
                escalation_count INTEGER DEFAULT 0,
                sla_due_at TEXT DEFAULT "",
                last_escalated_at TEXT DEFAULT "",
                ae_site_visit_required INTEGER DEFAULT 0,
                ae_site_visit_done INTEGER DEFAULT 0,
                dispute_count INTEGER DEFAULT 0,
                citizen_satisfied INTEGER DEFAULT NULL,
                satisfaction_reviewed_at TEXT DEFAULT "",
                work_done_photo TEXT DEFAULT "",
                work_done_at TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT "was",
                is_active INTEGER DEFAULT 1,
                must_change_password INTEGER DEFAULT 0,
                created_at TEXT,
                created_by TEXT DEFAULT "",
                last_login TEXT DEFAULT "",
                supervised_by INTEGER DEFAULT NULL,
                zone TEXT DEFAULT "",
                division TEXT DEFAULT "",
                ward_list TEXT DEFAULT "",
                phone TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                staff_id INTEGER,
                created_at TEXT,
                last_seen TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                action TEXT,
                old_value TEXT DEFAULT "",
                new_value TEXT DEFAULT "",
                done_by TEXT,
                done_at TEXT
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                comment TEXT,
                added_by TEXT,
                added_at TEXT
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                ip TEXT,
                attempted_at TEXT,
                success INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS staff_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT,
                target_username TEXT,
                done_by TEXT,
                done_at TEXT,
                details TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS repair_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                contractor_name TEXT DEFAULT "",
                repair_photo TEXT DEFAULT "",
                repair_lat REAL,
                repair_lng REAL,
                warranty_months INTEGER DEFAULT 6,
                repaired_at TEXT,
                recorded_by TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS rqi_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_report_id TEXT,
                new_report_id TEXT,
                contractor_name TEXT DEFAULT "",
                distance_m REAL,
                days_since_repair INTEGER,
                flagged_at TEXT,
                seen_by_commissioner INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ai_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT,
                original_ai_severity TEXT DEFAULT "",
                corrected_severity TEXT DEFAULT "",
                original_damage_type TEXT DEFAULT "",
                corrected_damage_type TEXT DEFAULT "",
                original_ai_correct INTEGER DEFAULT 0,
                corrected_by TEXT DEFAULT "",
                corrected_at TEXT DEFAULT "",
                photo_path TEXT DEFAULT "",
                ward TEXT DEFAULT "",
                notes TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS ward_reassignment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                original_ward TEXT DEFAULT "",
                requested_ward TEXT DEFAULT "",
                flagged_by TEXT DEFAULT "",
                flagged_at TEXT DEFAULT "",
                reviewed_by TEXT DEFAULT "",
                reviewed_at TEXT DEFAULT "",
                decision TEXT DEFAULT "",
                reason TEXT DEFAULT ""
            );

            CREATE TABLE IF NOT EXISTS sla_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                escalation_level INTEGER DEFAULT 0,
                target_role TEXT DEFAULT "",
                target_name TEXT DEFAULT "",
                message_sent TEXT DEFAULT "",
                triggered_at TEXT DEFAULT "",
                sms_sent INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS citizen_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT NOT NULL,
                satisfied INTEGER DEFAULT NULL,
                comment TEXT DEFAULT "",
                reviewed_at TEXT DEFAULT "",
                review_token TEXT DEFAULT "",
                dispute_number INTEGER DEFAULT 1,
                ae_review_required INTEGER DEFAULT 0,
                ae_reviewed_by TEXT DEFAULT "",
                ae_reviewed_at TEXT DEFAULT "",
                ae_decision TEXT DEFAULT ""
            );
        """)
    else:
        _init_postgres(c)

    _safe_add_columns(c, conn)
    _seed_admin(c, conn)
    conn.commit()
    conn.close()
    print("[db] init_db complete — default admin: admin / Admin@2024!")


def _init_postgres(c):
    """Create all tables for PostgreSQL (Render production)."""
    statements = [
        """CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            report_id TEXT UNIQUE,
            city TEXT,
            ward TEXT,
            division_name TEXT DEFAULT '',
            damage_type TEXT,
            description TEXT,
            location_text TEXT DEFAULT '',
            photo_path TEXT DEFAULT '',
            photo_data TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            submitted_at TEXT,
            updated_at TEXT,
            updated_by TEXT DEFAULT '',
            citizen_name TEXT DEFAULT '',
            citizen_phone TEXT DEFAULT '',
            citizen_email TEXT DEFAULT '',
            assigned_to TEXT DEFAULT '',
            assigned_officer TEXT DEFAULT '',
            latitude REAL,
            longitude REAL,
            severity TEXT DEFAULT 'unknown',
            severity_details TEXT DEFAULT '',
            estimated_cost TEXT DEFAULT '',
            urgency TEXT DEFAULT '',
            accident_risk TEXT DEFAULT '',
            recommended_action TEXT DEFAULT '',
            estimated_size TEXT DEFAULT '',
            intake_channel TEXT DEFAULT 'citizen_web',
            intake_ref TEXT DEFAULT '',
            rqi_flagged INTEGER DEFAULT 0,
            verified_damage_type TEXT DEFAULT '',
            site_condition TEXT DEFAULT '',
            site_photo_data TEXT DEFAULT '',
            verified_by TEXT DEFAULT '',
            verified_at TEXT DEFAULT '',
            ward_flag_reason TEXT DEFAULT '',
            ward_flag_status TEXT DEFAULT '',
            ward_flag_by TEXT DEFAULT '',
            ward_flag_at TEXT DEFAULT '',
            escalation_level INTEGER DEFAULT 0,
            escalation_count INTEGER DEFAULT 0,
            sla_due_at TEXT DEFAULT '',
            last_escalated_at TEXT DEFAULT '',
            ae_site_visit_required INTEGER DEFAULT 0,
            ae_site_visit_done INTEGER DEFAULT 0,
            dispute_count INTEGER DEFAULT 0,
            citizen_satisfied INTEGER DEFAULT NULL,
            satisfaction_reviewed_at TEXT DEFAULT '',
            work_done_photo TEXT DEFAULT '',
            work_done_at TEXT DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS staff (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'was',
            is_active INTEGER DEFAULT 1,
            must_change_password INTEGER DEFAULT 0,
            created_at TEXT,
            created_by TEXT DEFAULT '',
            last_login TEXT DEFAULT '',
            supervised_by INTEGER DEFAULT NULL,
            zone TEXT DEFAULT '',
            division TEXT DEFAULT '',
            ward_list TEXT DEFAULT '',
            phone TEXT DEFAULT ''
        )""",
        "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, staff_id INTEGER, created_at TEXT, last_seen TEXT)",
        "CREATE TABLE IF NOT EXISTS audit_log (id SERIAL PRIMARY KEY, report_id TEXT, action TEXT, old_value TEXT DEFAULT '', new_value TEXT DEFAULT '', done_by TEXT, done_at TEXT)",
        "CREATE TABLE IF NOT EXISTS comments (id SERIAL PRIMARY KEY, report_id TEXT, comment TEXT, added_by TEXT, added_at TEXT)",
        "CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS login_attempts (id SERIAL PRIMARY KEY, username TEXT, ip TEXT, attempted_at TEXT, success INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS staff_audit_log (id SERIAL PRIMARY KEY, action TEXT, target_username TEXT, done_by TEXT, done_at TEXT, details TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS repair_records (id SERIAL PRIMARY KEY, report_id TEXT, contractor_name TEXT DEFAULT '', repair_photo TEXT DEFAULT '', repair_lat REAL, repair_lng REAL, warranty_months INTEGER DEFAULT 6, repaired_at TEXT, recorded_by TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS rqi_events (id SERIAL PRIMARY KEY, original_report_id TEXT, new_report_id TEXT, contractor_name TEXT DEFAULT '', distance_m REAL, days_since_repair INTEGER, flagged_at TEXT, seen_by_commissioner INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS ai_corrections (id SERIAL PRIMARY KEY, report_id TEXT, original_ai_severity TEXT DEFAULT '', corrected_severity TEXT DEFAULT '', original_damage_type TEXT DEFAULT '', corrected_damage_type TEXT DEFAULT '', original_ai_correct INTEGER DEFAULT 0, corrected_by TEXT DEFAULT '', corrected_at TEXT DEFAULT '', photo_path TEXT DEFAULT '', ward TEXT DEFAULT '', notes TEXT DEFAULT '')",
        """CREATE TABLE IF NOT EXISTS ward_reassignment_log (
            id SERIAL PRIMARY KEY,
            report_id TEXT NOT NULL,
            original_ward TEXT DEFAULT '',
            requested_ward TEXT DEFAULT '',
            flagged_by TEXT DEFAULT '',
            flagged_at TEXT DEFAULT '',
            reviewed_by TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            decision TEXT DEFAULT '',
            reason TEXT DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS sla_events (
            id SERIAL PRIMARY KEY,
            report_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            escalation_level INTEGER DEFAULT 0,
            target_role TEXT DEFAULT '',
            target_name TEXT DEFAULT '',
            message_sent TEXT DEFAULT '',
            triggered_at TEXT DEFAULT '',
            sms_sent INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS citizen_reviews (
            id SERIAL PRIMARY KEY,
            report_id TEXT NOT NULL,
            satisfied INTEGER DEFAULT NULL,
            comment TEXT DEFAULT '',
            reviewed_at TEXT DEFAULT '',
            review_token TEXT DEFAULT '',
            dispute_number INTEGER DEFAULT 1,
            ae_review_required INTEGER DEFAULT 0,
            ae_reviewed_by TEXT DEFAULT '',
            ae_reviewed_at TEXT DEFAULT '',
            ae_decision TEXT DEFAULT ''
        )""",
    ]
    for sql in statements:
        try:
            c.execute(sql)
            c.connection.commit()
        except Exception as e:
            print(f"[db] pg table error: {e}")
            c.connection.rollback()


def _safe_add_columns(c, conn):
    """Add new columns to existing databases without breaking old data."""
    new_cols = {
        "staff": [
            ("supervised_by",       "INTEGER DEFAULT NULL"),
            ("zone",                "TEXT DEFAULT ''"),
            ("division",            "TEXT DEFAULT ''"),
            ("ward_list",           "TEXT DEFAULT ''"),
            ("phone",               "TEXT DEFAULT ''"),
            ("password_expires_at", "TEXT DEFAULT NULL"),
            ("temp_password_expired", "INTEGER DEFAULT 0"),
        ],
        "reports": [
            ("assigned_officer",    "TEXT DEFAULT ''"),
            ("verified_damage_type","TEXT DEFAULT ''"),
            ("site_condition",      "TEXT DEFAULT ''"),
            ("site_photo_data",     "TEXT DEFAULT ''"),
            ("verified_by",         "TEXT DEFAULT ''"),
            ("verified_at",         "TEXT DEFAULT ''"),
            ("intake_channel",      "TEXT DEFAULT 'citizen_web'"),
            ("intake_ref",          "TEXT DEFAULT ''"),
            ("rqi_flagged",         "INTEGER DEFAULT 0"),
            ("accident_risk",       "TEXT DEFAULT ''"),
            ("recommended_action",  "TEXT DEFAULT ''"),
            ("estimated_size",      "TEXT DEFAULT ''"),
            ("location_text",       "TEXT DEFAULT ''"),
            ("division_name",       "TEXT DEFAULT ''"),
            ("ward_flag_reason",    "TEXT DEFAULT ''"),
            ("ward_flag_status",    "TEXT DEFAULT ''"),
            ("ward_flag_by",        "TEXT DEFAULT ''"),
            ("ward_flag_at",        "TEXT DEFAULT ''"),
            ("escalation_level",    "INTEGER DEFAULT 0"),
            ("escalation_count",    "INTEGER DEFAULT 0"),
            ("sla_due_at",          "TEXT DEFAULT ''"),
            ("last_escalated_at",   "TEXT DEFAULT ''"),
            ("ae_site_visit_required","INTEGER DEFAULT 0"),
            ("ae_site_visit_done",  "INTEGER DEFAULT 0"),
            ("dispute_count",       "INTEGER DEFAULT 0"),
            ("citizen_satisfied",   "INTEGER DEFAULT NULL"),
            ("satisfaction_reviewed_at","TEXT DEFAULT ''"),
            ("work_done_photo",     "TEXT DEFAULT ''"),
            ("work_done_at",        "TEXT DEFAULT ''"),
            ("verify_lat",      "REAL DEFAULT NULL"),
            ("verify_lng",      "REAL DEFAULT NULL"),
            ("work_done_lat",   "REAL DEFAULT NULL"),
            ("work_done_lng",   "REAL DEFAULT NULL"),
        ],
    }
    for table, cols in new_cols.items():
        for col_name, col_def in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                conn.commit()
            except Exception:
                pass  # Column already exists


def _seed_admin(c, conn):
    """Seed default admin account if not present."""
    try:
        if USE_POSTGRES:
            try: conn.rollback()
            except: pass
        c.execute(_q("SELECT id FROM staff WHERE username = ?"), ("admin",))
        if not c.fetchone():
            c.execute(_q("""
                INSERT INTO staff
                    (name, username, password_hash, role, is_active,
                     must_change_password, created_at, created_by, zone)
                VALUES (?, ?, ?, ?, 1, 0, ?, ?, 'all')
            """), (
                "IT Administrator", "admin",
                hash_password("Admin@2024!"),
                "admin", now(), "system",
            ))
            conn.commit()
    except Exception as e:
        print(f"[db] admin seed error: {e}")
        try: conn.rollback()
        except: pass

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

def get_system_setting(key: str) -> str:
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute(_q("SELECT value FROM system_settings WHERE key = ?"), (key,))
        row = c.fetchone()
        conn.close()
        return dict(row)["value"] if row else ""
    except Exception:
        conn.close(); return ""

def set_system_setting(key: str, value: str):
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute(_q(
            "INSERT INTO system_settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ), (key, value))
    else:
        c.execute(_q("INSERT OR REPLACE INTO system_settings (key,value) VALUES (?,?)"), (key, value))
    conn.commit(); conn.close()

def is_setup_complete() -> bool:
    return get_system_setting("setup_complete") == "1"

def mark_setup_complete():
    set_system_setting("setup_complete", "1")

def setup_first_commissioner(name, username, password, org_name):
    if is_setup_complete():
        return False, "Setup already completed"
    ok, err = validate_password_strength(password)
    if not ok:
        return False, err
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute(_q("SELECT id FROM staff WHERE username = ?"), (username,))
        if c.fetchone():
            conn.close(); return False, "Username already exists"
        c.execute(_q("""
            INSERT INTO staff
                (name, username, password_hash, role, is_active,
                 must_change_password, created_at, created_by, zone)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?, 'all')
        """), (name, username, hash_password(password), "commissioner", now(), "setup"))
        if USE_POSTGRES:
            c.execute(_q(
                "INSERT INTO system_settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            ), ("setup_complete","1"))
            c.execute(_q(
                "INSERT INTO system_settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            ), ("org_name", org_name))
        else:
            c.execute(_q("INSERT OR REPLACE INTO system_settings (key,value) VALUES (?,?)"), ("setup_complete","1"))
            c.execute(_q("INSERT OR REPLACE INTO system_settings (key,value) VALUES (?,?)"), ("org_name", org_name))
        conn.commit(); conn.close()
        return True, "Setup complete"
    except Exception as e:
        conn.close(); return False, str(e)

# ─────────────────────────────────────────────────────────────────────────────
# REPORT ID GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def _generate_report_id() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    ts  = datetime.now(ist).strftime("%y%m")
    rand = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"GVMC-{ts}-{rand}"

# ─────────────────────────────────────────────────────────────────────────────
# CSV BACKUP
# ─────────────────────────────────────────────────────────────────────────────

CSV_HEADERS = [
    "report_id","city","ward","damage_type","description",
    "status","submitted_at","updated_at","updated_by",
    "citizen_name","citizen_phone","citizen_email",
    "assigned_to","latitude","longitude",
    "severity","severity_details","estimated_cost","urgency",
    "intake_channel","intake_ref",
    "verified_damage_type","site_condition","verified_by","verified_at",
]

def _get_csv_backup_path() -> str:
    ist  = timezone(timedelta(hours=5, minutes=30))
    year = datetime.now(ist).year
    os.makedirs("data", exist_ok=True)
    return f"data/resolved_{year}.csv"

def _append_to_csv(report: dict):
    try:
        path   = _get_csv_backup_path()
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(report)
    except Exception as e:
        print(f"[csv] {e}")

# ─────────────────────────────────────────────────────────────────────────────
# REPORT CRUD
# ─────────────────────────────────────────────────────────────────────────────

def add_report(
    city, ward, damage_type, description, photo_path,
    citizen_name, citizen_phone, citizen_email,
    latitude, longitude,
    severity="unknown", severity_details="",
    estimated_cost="", urgency="",
    photo_data="",
    intake_channel="citizen_web", intake_ref="",
    location_text="",
):
    """Add a new citizen complaint. Returns report_id."""
    conn = get_conn(); c = conn.cursor()
    rid  = _generate_report_id()
    ts   = now()

    # Calculate initial SLA due time (48h for WAS)
    ist      = timezone(timedelta(hours=5, minutes=30))
    sla_due  = (datetime.now(ist) + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    # Determine initial status: if no ward, needs triage
    initial_status = "pending_triage" if not ward else "open"

    # FIX 10 — persist division_name at submission time so it's available
    # for training labels, CSV export, and AE/ZC scoping without recomputing
    division_name = ""
    if ward:
        try:
            from wards import get_division_for_ward_name
            division_name = get_division_for_ward_name(ward) or ""
        except Exception:
            division_name = ""

    c.execute(_q("""
        INSERT INTO reports (
            report_id, city, ward, division_name, damage_type, description,
            location_text, photo_path,
            citizen_name, citizen_phone, citizen_email,
            latitude, longitude,
            severity, severity_details, estimated_cost, urgency,
            photo_data, status, submitted_at, updated_at,
            intake_channel, intake_ref, sla_due_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """), (
        rid, city or "GVMC", ward or "", division_name, damage_type, description,
        location_text or "", photo_path or "",
        citizen_name, citizen_phone, citizen_email or "",
        latitude, longitude,
        severity, severity_details, estimated_cost, urgency,
        photo_data, initial_status, ts, ts,
        intake_channel, intake_ref, sla_due,
    ))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (rid, "submitted", "", f"citizen submission via {intake_channel}", "citizen", ts))
    conn.commit(); conn.close()
    return rid


def add_manual_report(
    ward, damage_type, description, citizen_name,
    citizen_phone, citizen_email, source, logged_by,
    docket_ref="", photo_data="",
    severity="unknown", severity_details="", urgency="",
    location_text="",
):
    """Staff-logged complaint (phone, letter, WhatsApp, Spandana)."""
    conn = get_conn(); c = conn.cursor()
    rid  = _generate_report_id()
    ts   = now()
    ist  = timezone(timedelta(hours=5, minutes=30))
    sla_due = (datetime.now(ist) + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    c.execute(_q("""
        INSERT INTO reports (
            report_id, city, ward, damage_type, description,
            location_text, photo_path,
            citizen_name, citizen_phone, citizen_email,
            latitude, longitude,
            severity, severity_details, estimated_cost, urgency,
            photo_data, status, submitted_at, updated_at,
            intake_channel, intake_ref, assigned_officer, sla_due_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """), (
        rid, "", ward or "", damage_type, description,
        location_text or "", "",
        citizen_name, citizen_phone, citizen_email or "",
        None, None,
        severity, severity_details, "", urgency,
        photo_data or "",
        "pending_triage" if not ward else "open",
        ts, ts,
        source, docket_ref, logged_by, sla_due,
    ))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (rid, "manual_intake", "", f"logged by {logged_by} via {source}", logged_by, ts))
    conn.commit(); conn.close()
    return rid


def get_report_by_id(report_id: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM reports WHERE report_id = ?"), (report_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_reports_by_phone(phone: str) -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT * FROM reports WHERE citizen_phone = ? ORDER BY submitted_at DESC"
    ), (phone,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_reports(
    status=None, ward=None, severity=None, search=None,
    assigned_to=None, intake_channel=None, damage_type=None,
    assigned_officer=None, limit=200, offset=0,
):
    conn = get_conn(); c = conn.cursor()
    conds, params = [], []

    if status == "active_only":
        conds.append("status NOT IN ('resolved','closed','pending_triage')")
    elif status and status != "all":
        conds.append("status = ?"); params.append(status)

    if ward and ward != "all":
        conds.append("ward = ?"); params.append(ward)
    if damage_type and damage_type != "all":
        conds.append("damage_type = ?"); params.append(damage_type)
    if severity and severity != "all":
        conds.append("severity = ?"); params.append(severity)
    if assigned_to == "unassigned":
        conds.append("(assigned_to = '' OR assigned_to IS NULL)")
    elif assigned_to:
        conds.append("assigned_to = ?"); params.append(assigned_to)
    if assigned_officer:
        conds.append("assigned_officer = ?"); params.append(assigned_officer)
    if intake_channel:
        conds.append("intake_channel = ?"); params.append(intake_channel)
    if search:
        conds.append(
            "(report_id LIKE ? OR ward LIKE ? OR damage_type LIKE ? "
            "OR citizen_name LIKE ? OR description LIKE ?)"
        )
        s = f"%{search}%"; params.extend([s,s,s,s,s])

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.extend([limit, offset])

    c.execute(_q(f"""
        SELECT * FROM reports {where}
        ORDER BY
            CASE severity
                WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                WHEN 'medium'   THEN 3 ELSE 4
            END,
            submitted_at DESC
        LIMIT ? OFFSET ?
    """), params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reports_for_role(
    staff: dict, status=None, ward=None,
    severity=None, search=None, damage_type=None,
    limit=200, offset=0,
) -> list:
    """
    Role-scoped report access — every role sees exactly what they should.
    """
    role = staff.get("role", "viewer")

    if role in ("commissioner", "admin"):
        # Full city access
        return get_all_reports(
            status=status, ward=ward, severity=severity,
            search=search, damage_type=damage_type,
            limit=limit, offset=offset,
        )

    elif role == "zonal_commissioner":
        from wards import get_wards_for_zone
        zone = staff.get("zone", "")
        ward_names = get_wards_for_zone(zone) if zone else []
        if not ward_names:
            # Zone not configured — fail closed, not open.
            # Fill in wards.py's ZONE_DIVISION_MAP to fix this properly.
            return []
        conn = get_conn(); c = conn.cursor()
        conds  = [f"ward IN ({','.join(['?']*len(ward_names))})"]
        params = list(ward_names)
        if status == "active_only":
            conds.append("status NOT IN ('resolved','closed','pending_triage')")
        elif status and status != "all":
            conds.append("status = ?"); params.append(status)
        if severity and severity != "all":
            conds.append("severity = ?"); params.append(severity)
        if damage_type and damage_type != "all":
            conds.append("damage_type = ?"); params.append(damage_type)
        where = "WHERE " + " AND ".join(conds)
        params.extend([limit, offset])
        c.execute(_q(f"SELECT * FROM reports {where} ORDER BY submitted_at DESC LIMIT ? OFFSET ?"), params)
        results = [dict(r) for r in c.fetchall()]
        conn.close()
        return results

    elif role == "ae":
        from wards import get_wards_for_division
        division = staff.get("division", "")
        ward_names = get_wards_for_division(division) if division else []
        if not ward_names:
            # Division not set, or has no wards mapped — fail closed, not open.
            return []
        conn = get_conn(); c = conn.cursor()
        conds  = [f"ward IN ({','.join(['?']*len(ward_names))})"]
        params = list(ward_names)
        if status == "active_only":
            conds.append("status NOT IN ('resolved','closed','pending_triage')")
        elif status and status != "all":
            conds.append("status = ?"); params.append(status)
        if ward:
            conds.append("ward = ?"); params.append(ward)
        if severity and severity != "all":
            conds.append("severity = ?"); params.append(severity)
        if damage_type and damage_type != "all":
            conds.append("damage_type = ?"); params.append(damage_type)
        if search:
            conds.append("(report_id LIKE ? OR ward LIKE ? OR citizen_name LIKE ? OR description LIKE ?)")
            s = f"%{search}%"; params.extend([s,s,s,s])
        where = "WHERE " + " AND ".join(conds)
        params.extend([limit, offset])
        c.execute(_q(f"SELECT * FROM reports {where} ORDER BY submitted_at DESC LIMIT ? OFFSET ?"), params)
        results = [dict(r) for r in c.fetchall()]
        conn.close()
        return results

    elif role in ("viewer",):
        # Read-only, full access
        return get_all_reports(
            status=status, ward=ward, severity=severity,
            search=search, damage_type=damage_type,
            limit=limit, offset=offset,
        )

    elif role in ("was",):
        ward_str = staff.get("ward_list", "") or ""
        was_wards = [w.strip() for w in ward_str.split(",") if w.strip()]
        if ward:
            return get_all_reports(
                status=status, ward=ward, severity=severity,
                search=search, damage_type=damage_type,
                limit=limit, offset=offset,
            )
        if not was_wards:
            return []
        if len(was_wards) == 1:
            return get_all_reports(
                status=status, ward=was_wards[0], severity=severity,
                search=search, damage_type=damage_type,
                limit=limit, offset=offset,
            )
        conn = get_conn(); c = conn.cursor()
        conds  = [f"ward IN ({','.join(['?']*len(was_wards))})"]
        params = list(was_wards)
        if status == "active_only":
            conds.append("status NOT IN ('resolved','closed','pending_triage')")
        elif status and status != "all":
            conds.append("status = ?"); params.append(status)
        if severity and severity != "all":
            conds.append("severity = ?"); params.append(severity)
        if damage_type and damage_type != "all":
            conds.append("damage_type = ?"); params.append(damage_type)
        if search:
            conds.append(
                "(report_id LIKE ? OR ward LIKE ? OR citizen_name LIKE ? OR description LIKE ?)"
            )
            s = f"%{search}%"; params.extend([s,s,s,s])
        where = "WHERE " + " AND ".join(conds)
        params.extend([limit, offset])
        c.execute(_q(
            f"SELECT * FROM reports {where} ORDER BY submitted_at DESC LIMIT ? OFFSET ?"
        ), params)
        results = [dict(r) for r in c.fetchall()]
        conn.close()
        return results

    elif role == "field_engineer":
        # Legacy pilot role — only assigned to them
        return get_all_reports(
            status=status, ward=ward, severity=severity,
            search=search, damage_type=damage_type,
            assigned_to=staff["name"],
            limit=limit, offset=offset,
        )

    elif role in ("grievance_officer", "triage_officer"):
        # Can see all reports (read) + triage queue
        return get_all_reports(
            status=status, ward=ward, severity=severity,
            search=search, damage_type=damage_type,
            limit=limit, offset=offset,
        )

    return []


def get_reports_for_division(
    division: str, status=None, severity=None,
    search=None, damage_type=None, limit=200, offset=0,
) -> list:
    """Returns reports for a specific AE division."""
    conn = get_conn(); c = conn.cursor()
    # Get all wards in this division
    c.execute(_q(
        "SELECT DISTINCT ward_list FROM staff WHERE division=? AND role='was' AND is_active=1"
    ), (division,))
    rows    = [dict(r) for r in c.fetchall()]
    conn.close()

    wards_in_division = set()
    for r in rows:
        wl = r.get("ward_list","") or ""
        for w in wl.split(","):
            w = w.strip()
            if w:
                wards_in_division.add(w)

    if not wards_in_division:
        # Fall back to full access if division not configured
        return get_all_reports(
            status=status, severity=severity,
            search=search, damage_type=damage_type,
            limit=limit, offset=offset,
        )

    conn2 = get_conn(); c2 = conn2.cursor()
    conds, params = [], []
    placeholders = ",".join(["?"] * len(wards_in_division))
    conds.append(f"ward IN ({placeholders})")
    params.extend(list(wards_in_division))

    if status == "active_only":
        conds.append("status NOT IN ('resolved','closed','pending_triage')")
    elif status and status != "all":
        conds.append("status = ?"); params.append(status)
    if severity and severity != "all":
        conds.append("severity = ?"); params.append(severity)
    if damage_type and damage_type != "all":
        conds.append("damage_type = ?"); params.append(damage_type)
    if search:
        conds.append(
            "(report_id LIKE ? OR ward LIKE ? OR citizen_name LIKE ? OR description LIKE ?)"
        )
        s = f"%{search}%"; params.extend([s,s,s,s])

    where = "WHERE " + " AND ".join(conds)
    params.extend([limit, offset])
    c2.execute(_q(f"""
        SELECT * FROM reports {where}
        ORDER BY
            CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                          WHEN 'medium'   THEN 3 ELSE 4 END,
            submitted_at DESC
        LIMIT ? OFFSET ?
    """), params)
    results = [dict(r) for r in c2.fetchall()]
    conn2.close()
    return results


def update_report_status(report_id: str, new_status: str, done_by: str):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM reports WHERE report_id = ?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return
    r, old_status, ts = dict(row), dict(row)["status"], now()
    c.execute(_q(
        "UPDATE reports SET status=?, updated_at=?, updated_by=? WHERE report_id=?"
    ), (new_status, ts, done_by, report_id))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "status_change", old_status, new_status, done_by, ts))
    conn.commit()
    if new_status in ("resolved","closed"):
        r["status"] = new_status; r["updated_at"] = ts; r["updated_by"] = done_by
        _append_to_csv(r)
    conn.close()


def assign_report(
    report_id: str, assigned_to: str, done_by: str, assigned_officer: str = ""
) -> tuple:
    """Assign complaint to WAS or field engineer. Only allowed while status='open' —
    prevents silent re-assignment/overwrite of an already-assigned complaint."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT status FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Report not found"
    if dict(row)["status"] != "open":
        conn.close(); return False, "Already assigned — cannot reassign here"
    ts      = now()
    officer = assigned_officer or done_by
    c.execute(_q("""
        UPDATE reports
        SET assigned_to=?, assigned_officer=?,
            status='assigned', updated_at=?, updated_by=?
        WHERE report_id=?
    """), (assigned_to, officer, ts, done_by, report_id))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "assigned", "", f"{assigned_to} (officer: {officer})", done_by, ts))
    conn.commit(); conn.close()
    return True, f"Assigned to {assigned_to}"


def update_report_severity(
    report_id: str, severity: str,
    severity_details: str = "", estimated_cost: str = "", urgency: str = "",
):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "UPDATE reports SET severity=?, severity_details=?, "
        "estimated_cost=?, urgency=? WHERE report_id=?"
    ), (severity, severity_details, estimated_cost, urgency, report_id))
    conn.commit(); conn.close()


def mark_work_done(report_id: str, work_done_photo: str, done_by: str):
    """WAS marks work as done and uploads after-photo."""
    conn = get_conn(); c = conn.cursor()
    ts = now()
    c.execute(_q(
        "UPDATE reports SET work_done_photo=?, work_done_at=?, "
        "status='resolved', updated_at=?, updated_by=? "
        "WHERE report_id=?"
    ), (work_done_photo, ts, ts, done_by, report_id))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "work_done", "inspecting", "inspected", done_by, ts))
    conn.commit(); conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE — GPS-FAILED COMPLAINTS
# ─────────────────────────────────────────────────────────────────────────────

def get_pending_triage_reports(limit: int = 100) -> list:
    """
    Returns complaints where GPS failed and ward is empty.
    These are the Grievance Officer's work queue.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        SELECT * FROM reports
        WHERE status = 'pending_triage'
           OR (status = 'open' AND (ward = '' OR ward IS NULL))
        ORDER BY submitted_at ASC
        LIMIT ?
    """), (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def assign_ward_from_triage(
    report_id: str, ward: str, assigned_by: str
) -> tuple:
    """
    Grievance Officer assigns a ward to a GPS-failed complaint.
    Complaint moves from pending_triage → open.
    SLA clock starts from this moment.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Report not found"

    ts      = now()
    ist     = timezone(timedelta(hours=5, minutes=30))
    sla_due = (datetime.now(ist) + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    # FIX 10 — backfill division_name now that ward is finally known
    division_name = ""
    try:
        from wards import get_division_for_ward_name
        division_name = get_division_for_ward_name(ward) or ""
    except Exception:
        division_name = ""

    c.execute(_q("""
        UPDATE reports
        SET ward=?, division_name=?, status='open', updated_at=?, updated_by=?, sla_due_at=?
        WHERE report_id=?
    """), (ward, division_name, ts, assigned_by, sla_due, report_id))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "triage_ward_assigned", "", ward, assigned_by, ts))
    conn.commit(); conn.close()
    return True, f"Ward assigned: {ward}"

# ─────────────────────────────────────────────────────────────────────────────
# WARD FLAG — WAS FLAGS INCORRECT WARD
# ─────────────────────────────────────────────────────────────────────────────

def flag_incorrect_ward(
    report_id: str, requested_ward: str,
    reason: str, flagged_by: str
) -> tuple:
    """
    WAS flags that a complaint is in the wrong ward.
    Creates a ward_reassignment_log record.
    AE must approve/reject — SLA clock does NOT pause.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Report not found"
    r = dict(row)

    # Prevent duplicate pending flags
    if r.get("ward_flag_status") == "pending":
        conn.close(); return False, "A ward flag is already pending review"

    ts = now()
    c.execute(_q("""
        UPDATE reports
        SET ward_flag_reason=?, ward_flag_status='pending',
            ward_flag_by=?, ward_flag_at=?, updated_at=?
        WHERE report_id=?
    """), (reason, flagged_by, ts, ts, report_id))

    c.execute(_q("""
        INSERT INTO ward_reassignment_log
            (report_id, original_ward, requested_ward,
             flagged_by, flagged_at, decision)
        VALUES (?,?,?,?,?,?)
    """), (report_id, r.get("ward",""), requested_ward, flagged_by, ts, "pending"))

    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "ward_flag", r.get("ward",""), requested_ward, flagged_by, ts))
    conn.commit(); conn.close()
    return True, "Ward flag submitted. Your AE will review."


def get_pending_ward_flags(division: str = "") -> list:
    """
    Returns open ward flag requests.
    AE sees flags for their division. Admin/commissioner sees all.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
    SELECT r.*, wrl.requested_ward, wrl.id as flag_id
    FROM reports r
    JOIN ward_reassignment_log wrl ON wrl.report_id = r.report_id
    WHERE r.ward_flag_status = 'pending'
      AND wrl.decision = 'pending'
    ORDER BY r.ward_flag_at ASC
    """))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_ward_reassignment(
    report_id: str, approved: bool,
    reviewed_by: str, reason: str = ""
) -> tuple:
    """
    AE approves or rejects a WAS ward flag.
    If approved: ward changes, complaint moves to new ward's WAS.
    If rejected: complaint stays in current ward, WAS must handle.
    SLA clock continues in both cases — no reset.
    """
    conn = get_conn(); c = conn.cursor()

    # Get the pending flag
    c.execute(_q("""
    SELECT wrl.*, r.ward as current_ward
    FROM ward_reassignment_log wrl
    JOIN reports r ON wrl.report_id = r.report_id
    WHERE wrl.report_id = ? AND wrl.decision = 'pending'
    ORDER BY wrl.flagged_at DESC LIMIT 1
    """), (report_id,))
    flag = c.fetchone()
    if not flag:
        conn.close(); return False, "No pending flag found"
    f  = dict(flag)
    ts = now()

    if approved:
        # FIX 10 — recompute division_name since ward is changing
        new_division = ""
        try:
            from wards import get_division_for_ward_name
            new_division = get_division_for_ward_name(f["requested_ward"]) or ""
        except Exception:
            new_division = ""
        # Update ward on the complaint
        c.execute(_q(
            "UPDATE reports SET ward=?, division_name=?, ward_flag_status='approved', "
            "updated_at=?, updated_by=?, assigned_to='' WHERE report_id=?"
        ), (f["requested_ward"], new_division, ts, reviewed_by, report_id))
        decision_text = "approved"
        audit_action  = "ward_reassigned"
        audit_new     = f["requested_ward"]
    else:
        # Keep complaint in current ward
        c.execute(_q(
            "UPDATE reports SET ward_flag_status='rejected', "
            "updated_at=?, updated_by=? WHERE report_id=?"
        ), (ts, reviewed_by, report_id))
        decision_text = "rejected"
        audit_action  = "ward_flag_rejected"
        audit_new     = f["current_ward"]

    # Update the log record
    c.execute(_q("""
        UPDATE ward_reassignment_log
        SET reviewed_by=?, reviewed_at=?, decision=?, reason=?
        WHERE report_id=? AND decision='pending'
    """), (reviewed_by, ts, decision_text, reason, report_id))

    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, audit_action, f["current_ward"], audit_new, reviewed_by, ts))

    conn.commit(); conn.close()
    action_word = "approved — ward changed" if approved else "rejected — complaint stays in current ward"
    return True, f"Ward flag {action_word}"

# ─────────────────────────────────────────────────────────────────────────────
# SLA TRACKING
# ─────────────────────────────────────────────────────────────────────────────

# SLA level → hours until breach at this level
SLA_LEVELS = {
    0: 48,   # WAS must resolve within 48h
    1: 72,   # AE gets it at 48h, must resolve within 24 more = 72h total
    2: 96,   # ZC gets it at 72h, must resolve within 24 more = 96h total
    3: 120,  # Commissioner gets it at 96h, max = 120h
}

# Hours before breach to send warning SMS
SLA_WARNING_BUFFER = 12

# Role responsible at each escalation level
SLA_LEVEL_ROLE = {
    0: "was",
    1: "ae",
    2: "zonal_commissioner",
    3: "commissioner",
}


def get_sla_breached_reports(level: int) -> list:
    """
    Returns reports whose SLA is breached at a given escalation level.
    Used by the watchdog to trigger escalations.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        SELECT * FROM reports
        WHERE escalation_level = ?
          AND status NOT IN ('resolved','closed','pending_triage')
          AND sla_due_at < ?
          AND sla_due_at != ''
        ORDER BY sla_due_at ASC
    """), (level, now()))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sla_warning_reports(level: int, warning_hours: int = 12) -> list:
    """
    Returns reports approaching SLA breach (within warning_hours of deadline).
    """
    conn = get_conn(); c = conn.cursor()
    ist      = timezone(timedelta(hours=5, minutes=30))
    now_dt   = datetime.now(ist)
    warn_dt  = (now_dt + timedelta(hours=warning_hours)).strftime("%Y-%m-%d %H:%M:%S")
    now_str  = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    c.execute(_q("""
        SELECT * FROM reports
        WHERE escalation_level = ?
          AND status NOT IN ('resolved','closed','pending_triage')
          AND sla_due_at > ?
          AND sla_due_at <= ?
        ORDER BY sla_due_at ASC
    """), (level, now_str, warn_dt))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def escalate_report(report_id: str, from_level: int, escalated_by: str = "system") -> bool:
    """
    Escalate a complaint to the next SLA level.
    Updates escalation_level, escalation_count, and sla_due_at.
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False
    r = dict(row)

    new_level = from_level + 1
    if new_level > 3:
        # Already at Commissioner level — just mark and return
        conn.close(); return False

    ist     = timezone(timedelta(hours=5, minutes=30))
    # New SLA deadline: 24h from now at the new level
    sla_due = (datetime.now(ist) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    ts      = now()
    old_count = r.get("escalation_count", 0) or 0

    c.execute(_q("""
        UPDATE reports
        SET escalation_level=?, escalation_count=?, sla_due_at=?,
            last_escalated_at=?, updated_at=?
        WHERE report_id=?
    """), (new_level, old_count + 1, sla_due, ts, ts, report_id))

    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (
        report_id, "sla_escalated",
        f"level_{from_level}",
        f"level_{new_level} (escalation #{old_count+1})",
        escalated_by, ts,
    ))
    conn.commit(); conn.close()
    return True


def log_sla_event(
    report_id: str, event_type: str, escalation_level: int,
    target_role: str, target_name: str,
    message_sent: str = "", sms_sent: bool = False
):
    """Record every SLA warning and breach event for audit purposes."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        INSERT INTO sla_events
            (report_id, event_type, escalation_level, target_role,
             target_name, message_sent, triggered_at, sms_sent)
        VALUES (?,?,?,?,?,?,?,?)
    """), (
        report_id, event_type, escalation_level, target_role,
        target_name, message_sent, now(), 1 if sms_sent else 0,
    ))
    conn.commit(); conn.close()


def get_staff_for_escalation_level(level: int, ward: str = "", zone: str = "") -> list:
    """
    Returns the right staff members to notify at each escalation level.
    level 0 → WAS for this ward
    level 1 → AE for this division
    level 2 → Zonal Commissioner for this zone
    level 3 → Commissioner + admin
    """
    conn = get_conn(); c = conn.cursor()
    role_at_level = SLA_LEVEL_ROLE.get(level, "commissioner")

    if level == 0 and ward:
        # Find WAS assigned to this ward
        c.execute(_q("""
            SELECT * FROM staff
            WHERE role='was' AND is_active=1
              AND (ward_list LIKE ? OR ward_list = ?)
        """), (f"%{ward}%", ward))
    elif level == 1:
        # Find AE for this ward's division
        if zone:
            c.execute(_q("SELECT * FROM staff WHERE role='ae' AND is_active=1 AND division=?"), (zone,))
        else:
            c.execute(_q("SELECT * FROM staff WHERE role='ae' AND is_active=1"))
    elif level == 2:
        if zone:
            c.execute(_q("SELECT * FROM staff WHERE role='zonal_commissioner' AND is_active=1 AND zone=?"), (zone,))
        else:
            c.execute(_q("SELECT * FROM staff WHERE role='zonal_commissioner' AND is_active=1"))
    else:
        c.execute(_q(
            "SELECT * FROM staff WHERE role IN ('commissioner','admin') AND is_active=1"
        ))

    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# CITIZEN REVIEW / SECOND DISPUTE
# ─────────────────────────────────────────────────────────────────────────────

def create_citizen_review_token(report_id: str) -> str:
    """
    Generate a one-time review token for the citizen satisfaction link.
    Sent via SMS when complaint is resolved.
    """
    token = secrets.token_urlsafe(32)
    conn  = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT COUNT(*) as cnt FROM citizen_reviews WHERE report_id=?"), (report_id,))
    existing_count = dict(c.fetchone())["cnt"] or 0
    dispute_number = existing_count + 1

    c.execute(_q("""
        INSERT INTO citizen_reviews
            (report_id, review_token, dispute_number)
        VALUES (?,?,?)
    """), (report_id, token, dispute_number))
    conn.commit(); conn.close()
    return token


def get_review_by_token(token: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT cr.*, r.report_id as rid, r.damage_type, r.ward, "
        "r.work_done_photo, r.photo_data, r.assigned_to "
        "FROM citizen_reviews cr "
        "JOIN reports r ON cr.report_id = r.report_id "
        "WHERE cr.review_token = ? AND cr.satisfied IS NULL"
    ), (token,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def submit_citizen_review(
    token: str, satisfied: bool, comment: str = ""
) -> tuple:
    """
    Process citizen satisfaction response.

    First dispute (dispute_number=1):
      - Satisfied → complaint closed
      - Not satisfied → AE site visit required (NOT force close)

    Second dispute (dispute_number=2):
      - AE must personally verify → only AE can force close
    """
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT * FROM citizen_reviews WHERE review_token=? AND satisfied IS NULL"
    ), (token,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Review already submitted or token expired"

    rv = dict(row)
    ts = now()

    # Mark review as submitted
    c.execute(_q("""
        UPDATE citizen_reviews
        SET satisfied=?, comment=?, reviewed_at=?
        WHERE review_token=?
    """), (1 if satisfied else 0, comment, ts, token))

    if satisfied:
        # Close the complaint permanently
        c.execute(_q("""
            UPDATE reports
            SET status='closed', citizen_satisfied=1,
                satisfaction_reviewed_at=?, updated_at=?
            WHERE report_id=?
        """), (ts, ts, rv["report_id"]))
        c.execute(_q(
            "INSERT INTO audit_log "
            "(report_id,action,old_value,new_value,done_by,done_at) "
            "VALUES (?,?,?,?,?,?)"
        ), (rv["report_id"], "citizen_satisfied", "resolved", "closed", "citizen", ts))
        conn.commit(); conn.close()
        return True, "closed"

    else:
        # Citizen NOT satisfied
        dispute_number = rv.get("dispute_number", 1)

        # Increment dispute count on report
        c.execute(_q("""
            UPDATE reports
            SET dispute_count = COALESCE(dispute_count,0) + 1,
                citizen_satisfied=0, satisfaction_reviewed_at=?,
                updated_at=?, status='disputed'
            WHERE report_id=?
        """), (ts, ts, rv["report_id"]))

        # Always require AE site visit — no auto-close (production rule)
        c.execute(_q("""
            UPDATE reports
            SET ae_site_visit_required=1
            WHERE report_id=?
        """), (rv["report_id"],))

        c.execute(_q("""
            UPDATE citizen_reviews
            SET ae_review_required=1
            WHERE review_token=?
        """), (token,))

        c.execute(_q(
            "INSERT INTO audit_log "
            "(report_id,action,old_value,new_value,done_by,done_at) "
            "VALUES (?,?,?,?,?,?)"
        ), (
            rv["report_id"], "citizen_disputed",
            f"dispute_{dispute_number}", "ae_site_visit_required",
            "citizen", ts,
        ))
        conn.commit(); conn.close()
        return True, "disputed_ae_required"


def get_disputed_reports_for_ae(ae_name: str = "", division: str = "") -> list:
    """
    Returns complaints where citizen disputed and AE site visit is required.
    AE must physically verify → can force close OR reopen.
    If division is provided, scopes to that division's wards only.
    """
    conn = get_conn(); c = conn.cursor()
    if division:
        ward_list = get_wards_in_division(division)
        if ward_list:
            placeholders = ",".join(["?"] * len(ward_list))
            c.execute(_q(f"""
                SELECT r.*, cr.dispute_number, cr.comment as citizen_comment
                FROM reports r
                LEFT JOIN citizen_reviews cr ON cr.report_id = r.report_id
                WHERE r.ae_site_visit_required = 1
                  AND r.ae_site_visit_done = 0
                  AND r.status = 'disputed'
                  AND r.ward IN ({placeholders})
                ORDER BY r.updated_at ASC
            """), ward_list)
            rows = c.fetchall()
            conn.close()
            return [dict(r) for r in rows]
    c.execute(_q("""
        SELECT r.*, cr.dispute_number, cr.comment as citizen_comment
        FROM reports r
        LEFT JOIN citizen_reviews cr ON cr.report_id = r.report_id
        WHERE r.ae_site_visit_required = 1
          AND r.ae_site_visit_done = 0
          AND r.status = 'disputed'
        ORDER BY r.updated_at ASC
    """))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ae_resolve_disputed(
    report_id: str, decision: str,
    ae_name: str, ae_notes: str = ""
) -> tuple:
    """
    AE reviews disputed complaint after physical site visit.
    decision: 'force_close' | 'reopen'
    Only AE can close a disputed ticket — never auto-close.
    """
    conn = get_conn(); c = conn.cursor()
    ts = now()

    if decision == "force_close":
        new_status = "closed"
        audit_new  = "force_closed_by_ae"
    elif decision == "reopen":
        new_status = "open"
        audit_new  = "reopened_by_ae_after_dispute"
    else:
        conn.close(); return False, "Invalid decision"

    c.execute(_q("""
        UPDATE reports
        SET status=?, ae_site_visit_done=1,
            updated_at=?, updated_by=?
        WHERE report_id=?
    """), (new_status, ts, ae_name, report_id))

    c.execute(_q("""
        UPDATE citizen_reviews
        SET ae_reviewed_by=?, ae_reviewed_at=?, ae_decision=?
        WHERE report_id=? AND ae_review_required=1
    """), (ae_name, ts, decision, report_id))

    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "ae_dispute_resolved", "disputed", audit_new, ae_name, ts))

    if ae_notes:
        c.execute(_q(
            "INSERT INTO comments (report_id,comment,added_by,added_at) VALUES (?,?,?,?)"
        ), (report_id, f"AE site visit: {ae_notes}", ae_name, ts))

    if new_status == "closed":
        # Append to CSV archive
        c.execute(_q("SELECT * FROM reports WHERE report_id=?"), (report_id,))
        r_row = c.fetchone()
        if r_row:
            _append_to_csv(dict(r_row))

    conn.commit(); conn.close()
    return True, f"Complaint {decision.replace('_',' ')} by AE"

# ─────────────────────────────────────────────────────────────────────────────
# DIVISION / WARD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_wards_in_division(division: str) -> list:
    """Return ward names belonging to a division (from WAS ward_list assignments)."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT ward_list FROM staff WHERE division=? AND role='was' AND is_active=1"
    ), (division,))
    rows = c.fetchall()
    conn.close()
    wards = []
    for r in rows:
        wl = dict(r).get("ward_list","") or ""
        for w in wl.split(","):
            w = w.strip()
            if w:
                wards.append(w)
    return list(set(wards))


def get_map_reports() -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT report_id, ward, damage_type, status, severity,
               latitude, longitude, submitted_at, assigned_to
        FROM reports
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY submitted_at DESC LIMIT 500
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ward_list() -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT DISTINCT ward FROM reports WHERE ward != '' ORDER BY ward")
    rows = c.fetchall()
    conn.close()
    return [dict(r)["ward"] for r in rows]


def get_active_officers() -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT id, name, username, role
        FROM staff
        WHERE is_active=1 AND role IN ('field_engineer','was','ae')
        ORDER BY role, name
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_was_for_ward(ward: str) -> dict | None:
    """Find the WAS responsible for a given ward."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        SELECT * FROM staff
        WHERE role='was' AND is_active=1
          AND (ward_list LIKE ? OR ward_list = ?)
        LIMIT 1
    """), (f"%{ward}%", ward))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_engineers_under(officer_id: int) -> list:
    """Returns WAS/engineers supervised by this officer (by id)."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        SELECT id, name, username, role
        FROM staff
        WHERE is_active=1 AND role IN ('was','field_engineer')
          AND supervised_by=?
        ORDER BY name
    """), (officer_id,))
    rows = c.fetchall()
    if not rows:
        c.execute("""
            SELECT id, name, username, role FROM staff
            WHERE is_active=1 AND role IN ('was','field_engineer')
            ORDER BY name
        """)
        rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_engineers_under_by_name(officer_name: str) -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT id FROM staff WHERE name=? AND role='ae' AND is_active=1 LIMIT 1"
    ), (officer_name,))
    row = c.fetchone()
    conn.close()
    if not row:
        conn2 = get_conn(); c2 = conn2.cursor()
        c2.execute("""
            SELECT id, name, username, role FROM staff
            WHERE is_active=1 AND role IN ('was','field_engineer')
            ORDER BY name
        """)
        rows = c2.fetchall(); conn2.close()
        return [dict(r) for r in rows]
    return get_engineers_under(dict(row)["id"])

# ─────────────────────────────────────────────────────────────────────────────
# COMMENTS & AUDIT
# ─────────────────────────────────────────────────────────────────────────────

def add_comment(report_id: str, comment: str, added_by: str):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "INSERT INTO comments (report_id,comment,added_by,added_at) VALUES (?,?,?,?)"
    ), (report_id, comment, added_by, now()))
    conn.commit(); conn.close()


def get_comments(report_id: str) -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT * FROM comments WHERE report_id=? ORDER BY added_at ASC"
    ), (report_id,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]


def get_all_comments() -> dict:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM comments ORDER BY added_at ASC")
    result = {}
    for r in c.fetchall():
        d = dict(r); result.setdefault(d["report_id"], []).append(d)
    conn.close(); return result

def get_all_comments_grouped(): return get_all_comments()


def get_audit_log(report_id: str) -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT * FROM audit_log WHERE report_id=? ORDER BY done_at ASC"
    ), (report_id,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]


def get_all_audit_logs() -> dict:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM audit_log ORDER BY done_at ASC")
    result = {}
    for r in c.fetchall():
        d = dict(r); result.setdefault(d["report_id"], []).append(d)
    conn.close(); return result

def get_all_audits_grouped(): return get_all_audit_logs()

# ─────────────────────────────────────────────────────────────────────────────
# AI CORRECTIONS & TRAINING
# ─────────────────────────────────────────────────────────────────────────────

def log_ai_correction(
    report_id, original_ai_severity, corrected_severity,
    original_damage_type, corrected_damage_type,
    corrected_by, photo_path="", ward="", notes="",
):
    ai_correct = (
        original_ai_severity == corrected_severity and
        original_damage_type == corrected_damage_type
    )
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        INSERT INTO ai_corrections
            (report_id, original_ai_severity, corrected_severity,
             original_damage_type, corrected_damage_type,
             original_ai_correct, corrected_by, corrected_at,
             photo_path, ward, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """), (
        report_id, original_ai_severity, corrected_severity,
        original_damage_type, corrected_damage_type,
        1 if ai_correct else 0, corrected_by, now(),
        photo_path, ward, notes,
    ))
    conn.commit(); conn.close()


def get_ai_accuracy_stats() -> dict:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) as total FROM ai_corrections")
    total = dict(c.fetchone())["total"] or 0
    c.execute("SELECT COUNT(*) as correct FROM ai_corrections WHERE original_ai_correct=1")
    correct = dict(c.fetchone())["correct"] or 0
    conn.close()
    return {
        "total_corrections": total,
        "ai_correct_count":  correct,
        "accuracy_pct":      round(correct/total*100,1) if total > 0 else 0,
        "needs_correction":  total - correct,
    }


def save_inspection_verification(
    report_id, verified_damage_type, site_condition,
    site_photo_data, verified_by, notes="",
    is_override=False, override_reason="",
):
    conn = get_conn(); c = conn.cursor(); ts = now()
    c.execute(_q("SELECT * FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False
    r = dict(row)
    c.execute(_q("""
        UPDATE reports
        SET verified_damage_type=?, site_condition=?,
            site_photo_data=?, verified_by=?, verified_at=?
        WHERE report_id=?
    """), (verified_damage_type, site_condition, site_photo_data, verified_by, ts, report_id))

    action     = "override_verification" if is_override else "inspection_verified"
    audit_note = f"OVERRIDE — {override_reason}" if is_override else verified_damage_type
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, action, r.get("damage_type",""), audit_note, verified_by, ts))

    if notes:
        c.execute(_q(
            "INSERT INTO comments (report_id,comment,added_by,added_at) VALUES (?,?,?,?)"
        ), (report_id, f"Site inspection: {notes}", verified_by, ts))
    if is_override:
        c.execute(_q(
            "INSERT INTO comments (report_id,comment,added_by,added_at) VALUES (?,?,?,?)"
        ), (report_id, f"⚠️ SUPERVISOR OVERRIDE — assigned engineer did not close within SLA. Reason: {override_reason}", verified_by, ts))

    conn.commit(); conn.close()
    if verified_damage_type and verified_damage_type != r.get("damage_type",""):
        log_ai_correction(
            report_id, r.get("severity",""), r.get("severity",""),
            r.get("damage_type",""), verified_damage_type, verified_by,
            r.get("photo_path",""), r.get("ward",""),
            f"Site correction. Condition: {site_condition}",
        )
    save_training_sample(
        report_id, r.get("ward",""), r.get("damage_type",""),
        verified_damage_type, r.get("severity","unknown"),
        site_condition, verified_by, ts, r.get("photo_data",""),
        is_override=is_override,
    )
    return True

# ─────────────────────────────────────────────────────────────────────────────
# STAFF MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def add_staff(
    name, username, password, role,
    created_by="system", must_change=True,
    supervised_by=None, zone="", division="", ward_list="", phone="",
):
    if role not in VALID_ROLES:
        return False, f"Invalid role: {role}"
    if role == "ae" and not division:
        return False, "Assistant Engineer accounts require a division"
    if role == "was" and not ward_list:
        return False, "Ward Amenities Secretary accounts require at least one ward"
    if role == "zonal_commissioner" and not zone:
        return False, "Zonal Commissioner accounts require a zone"
    ok, err = validate_password_strength(password)
    if not ok:
        return False, err
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute(_q("SELECT id FROM staff WHERE username=?"), (username,))
        if c.fetchone():
            conn.close(); return False, "Username already exists"
       # Set 24hr expiry for temp passwords
        expires = None
        if must_change:
            from datetime import datetime, timezone, timedelta
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

        c.execute(_q("""
            INSERT INTO staff
                (name, username, password_hash, role, is_active,
                 must_change_password, created_at, created_by,
                 supervised_by, zone, division, ward_list, phone,
                 password_expires_at)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?)
        """), (
            name, username, hash_password(password), role,
            1 if must_change else 0, now(), created_by,
            supervised_by, zone or "", division or "", ward_list or "", phone or "",
            expires,
        ))
        conn.commit(); conn.close()
        return True, password
    except Exception as e:
        conn.close(); return False, str(e)


def add_staff_with_log(
    name, username, password, role,
    created_by="system", supervised_by=None,
    zone="", division="", ward_list="", phone="",
):
    ok, result = add_staff(
        name, username, password, role, created_by,
        must_change=True, supervised_by=supervised_by,
        zone=zone, division=division, ward_list=ward_list, phone=phone,
    )
    if ok:
        conn = get_conn(); c = conn.cursor()
        c.execute(_q(
            "INSERT INTO staff_audit_log "
            "(action,target_username,done_by,done_at,details) VALUES (?,?,?,?,?)"
        ), ("create_account", username, created_by, now(),
            f"role={role},division={division},zone={zone}"))
        conn.commit(); conn.close()
    return ok, result


def get_staff_by_username(username: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM staff WHERE username=?"), (username,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None


def get_staff_by_id(staff_id: int) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM staff WHERE id=?"), (staff_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None


def get_all_staff() -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM staff ORDER BY role, name")
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]


def get_manageable_staff(creator_role: str) -> list:
    manageable = ROLE_CAN_CREATE.get(creator_role, set())
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM staff ORDER BY role, name")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return [r for r in rows if r["role"] in manageable]


def get_team_members(
    creator_role: str, creator_username: str, creator_id: int = None
) -> list:
    manageable = ROLE_CAN_CREATE.get(creator_role, set())
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT * FROM staff ORDER BY role, name")
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    result = [
        r for r in rows
        if r["role"] in manageable and r["username"] != creator_username
    ]
    if creator_role == "ae" and creator_id:
        supervised = [r for r in result if r.get("supervised_by") == creator_id]
        if supervised:
            return supervised
        return [r for r in result if r["role"] in ("was","field_engineer")]
    return result


def toggle_staff_active(staff_id: int, done_by: str) -> tuple:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT is_active, username FROM staff WHERE id=?"), (staff_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Staff not found"
    r       = dict(row)
    new_val = 0 if r["is_active"] else 1
    c.execute(_q("UPDATE staff SET is_active=? WHERE id=?"), (new_val, staff_id))
    if new_val == 0:
        c.execute(_q("DELETE FROM sessions WHERE staff_id=?"), (staff_id,))
    conn.commit(); conn.close()
    return True, "activated" if new_val else "deactivated"


def reset_staff_password(staff_id: int, done_by: str) -> tuple:
    new_pass = generate_temp_password()
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT username FROM staff WHERE id=?"), (staff_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Staff not found"
    c.execute(_q(
        "UPDATE staff SET password_hash=?, must_change_password=1 WHERE id=?"
    ), (hash_password(new_pass), staff_id))
    conn.commit(); conn.close()
    return True, new_pass


def reset_password_with_log(staff_id: int, creator_role: str, done_by: str) -> tuple:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT username, role FROM staff WHERE id=?"), (staff_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Staff not found"
    r = dict(row)
    if not can_manage_user(creator_role, r["role"]) and creator_role != "admin":
        conn.close(); return False, "You do not have permission"
    new_pass = generate_temp_password()
    c.execute(_q(
        "UPDATE staff SET password_hash=?, must_change_password=1 WHERE id=?"
    ), (hash_password(new_pass), staff_id))
    c.execute(_q(
        "INSERT INTO staff_audit_log "
        "(action,target_username,done_by,done_at) VALUES (?,?,?,?)"
    ), ("password_reset", r["username"], done_by, now()))
    conn.commit(); conn.close()
    return True, new_pass


def change_password(staff_id: int, current_password: str, new_password: str) -> tuple:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT password_hash FROM staff WHERE id=?"), (staff_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return False, "Account not found"
    if not verify_password(current_password, dict(row)["password_hash"]):
        conn.close(); return False, "Current password is incorrect"
    ok, err = validate_password_strength(new_password)
    if not ok:
        conn.close(); return False, err
    c.execute(_q(
        "UPDATE staff SET password_hash=?, must_change_password=0 WHERE id=?"
    ), (hash_password(new_password), staff_id))
    conn.commit(); conn.close()
    return True, ""


def update_staff_supervisor(engineer_id: int, officer_id: int, done_by: str) -> tuple:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "UPDATE staff SET supervised_by=? WHERE id=? AND role IN ('was','field_engineer')"
    ), (officer_id, engineer_id))
    conn.commit(); conn.close()
    return True, "Engineer assigned to officer"

# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

SESSION_IDLE_HOURS = 8


def create_session(staff_id: int) -> str:
    token = secrets.token_urlsafe(48)
    ts    = now()
    conn  = get_conn(); c = conn.cursor()
    c.execute(_q(
        "INSERT INTO sessions (token,staff_id,created_at,last_seen) VALUES (?,?,?,?)"
    ), (token, staff_id, ts, ts))
    c.execute(_q("UPDATE staff SET last_login=? WHERE id=?"), (ts, staff_id))
    conn.commit(); conn.close()
    return token


def get_staff_by_token(token: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        SELECT s.*, st.*
        FROM sessions s
        JOIN staff st ON s.staff_id = st.id
        WHERE s.token=? AND st.is_active=1
    """), (token,))
    row = c.fetchone()
    if not row:
        conn.close(); return None
    r = dict(row)
    try:
        last_seen = datetime.strptime(r.get("last_seen",""), "%Y-%m-%d %H:%M:%S")
        ist     = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist).replace(tzinfo=None)
        if (now_ist - last_seen).total_seconds() > SESSION_IDLE_HOURS * 3600:
            c.execute(_q("DELETE FROM sessions WHERE token=?"), (token,))
            conn.commit(); conn.close(); return None
    except Exception:
        pass
    c.execute(_q("UPDATE sessions SET last_seen=? WHERE token=?"), (now(), token))
    conn.commit(); conn.close()
    return r


def delete_session(token: str):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("DELETE FROM sessions WHERE token=?"), (token,))
    conn.commit(); conn.close()


def cleanup_expired_sessions():
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute("DELETE FROM sessions WHERE last_seen::timestamptz < NOW() - INTERVAL '8 hours'")
    else:
        c.execute("DELETE FROM sessions WHERE last_seen < datetime('now','-8 hours')")
    conn.commit(); conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN SECURITY
# ─────────────────────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 5
IP_MAX_ATTEMPTS = 20  # higher threshold — demo machine logs in as many staff accounts


def record_login_attempt(username: str, ip: str, success: bool):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "INSERT INTO login_attempts (username,ip,attempted_at,success) VALUES (?,?,?,?)"
    ), (username, ip, now(), 1 if success else 0))
    conn.commit(); conn.close()


def is_locked_out(username: str, ip: str) -> bool:
    """
    Username lockout and IP lockout are checked independently.
    IP lockout threshold is higher (20) to avoid locking out demo
    machines testing multiple accounts — only username lockout is 5.
    """
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute(_q("""
            SELECT COUNT(*) as cnt FROM login_attempts
            WHERE username=? AND success=0
              AND attempted_at::timestamptz > NOW() - INTERVAL '15 minutes'
        """), (username,))
        username_failures = dict(c.fetchone())["cnt"] or 0
        c.execute(_q("""
            SELECT COUNT(*) as cnt FROM login_attempts
            WHERE ip=? AND success=0
              AND attempted_at::timestamptz > NOW() - INTERVAL '15 minutes'
        """), (ip,))
        ip_failures = dict(c.fetchone())["cnt"] or 0
    else:
        c.execute(_q("""
            SELECT COUNT(*) as cnt FROM login_attempts
            WHERE username=? AND success=0
              AND attempted_at > datetime('now','-15 minutes')
        """), (username,))
        username_failures = dict(c.fetchone())["cnt"] or 0
        c.execute(_q("""
            SELECT COUNT(*) as cnt FROM login_attempts
            WHERE ip=? AND success=0
              AND attempted_at > datetime('now','-15 minutes')
        """), (ip,))
        ip_failures = dict(c.fetchone())["cnt"] or 0
    conn.close()
    # Username: locked after 5 failures (brute force protection)
    # IP: locked after 20 failures (prevents spray attacks without
    #     locking out demo machines testing multiple accounts)
    return username_failures >= MAX_ATTEMPTS or ip_failures >= 20


def is_login_locked(username: str, ip: str) -> tuple:
    locked = is_locked_out(username, ip)
    return locked, "Too many failed attempts. Please wait 15 minutes." if locked else ""


def cleanup_login_attempts():
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute("DELETE FROM login_attempts WHERE attempted_at::timestamptz < NOW() - INTERVAL '24 hours'")
    else:
        c.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now','-24 hours')")
    conn.commit(); conn.close()


def authenticate_staff(username: str, password: str, ip: str = "") -> tuple:
    if is_locked_out(username, ip):
        return None, "Too many failed attempts. Please wait 15 minutes."
    staff = get_staff_by_username(username)
    if not staff:
        record_login_attempt(username, ip, False)
        return None, "Invalid username or password"
    # Check if temp password has expired (24 hour window)
    if staff.get("must_change_password") and staff.get("password_expires_at"):
        try:
            from datetime import datetime, timezone
            expires = datetime.strptime(staff["password_expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires:
                # Auto-disable account
                conn = get_conn()
                conn.execute(_q("UPDATE staff SET is_active=0, temp_password_expired=1 WHERE username=?"), (username,))
                conn.commit()
                conn.close()
                return None, "Your temporary password has expired (24 hour limit). Contact your administrator to reset access."
        except Exception:
            pass
    if not verify_password(password, staff["password_hash"]):
        record_login_attempt(username, ip, False)
        return None, "Invalid username or password"
    record_login_attempt(username, ip, True)
    return staff, ""


def login_staff(username: str, password: str):
    staff, _ = authenticate_staff(username, password, "")
    return staff

# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

def get_analytics_data() -> dict:
    conn = get_conn(); c = conn.cursor()
    ist  = timezone(timedelta(hours=5, minutes=30))

    def cnt(sql, params=()):
        c.execute(sql, params); return dict(c.fetchone())["cnt"] or 0

    total    = cnt("SELECT COUNT(*) as cnt FROM reports")
    resolved = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed')")
    open_c   = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed','pending_triage')")
    critical = cnt("SELECT COUNT(*) as cnt FROM reports WHERE severity='critical'")
    today_c  = cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE submitted_at LIKE ?"),
                   (datetime.now(ist).strftime("%Y-%m-%d")+"%",))

    c.execute("SELECT MIN(submitted_at) as first FROM reports")
    first_str = dict(c.fetchone())["first"]
    daily_avg = 0
    if first_str and total > 0:
        try:
            days = max((datetime.now() - datetime.strptime(first_str[:10],"%Y-%m-%d")).days, 1)
            daily_avg = round(total/days, 1)
        except Exception:
            pass

    if USE_POSTGRES:
        c.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM
                (updated_at::timestamp - submitted_at::timestamp))/86400) as avg_d
            FROM reports WHERE status IN ('resolved','closed')
        """)
    else:
        c.execute("""
            SELECT AVG((julianday(updated_at) - julianday(submitted_at))) as avg_d
            FROM reports WHERE status IN ('resolved','closed')
        """)
    avg_resolution_days = round(dict(c.fetchone())["avg_d"] or 0, 1)

    this_month   = datetime.now(ist).strftime("%Y-%m")
    this_month_c = cnt(_q(
        "SELECT COUNT(*) as cnt FROM reports WHERE submitted_at LIKE ?"
    ), (f"{this_month}%",))
    last_month   = (datetime.now(ist).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    last_month_c = cnt(_q(
        "SELECT COUNT(*) as cnt FROM reports WHERE submitted_at LIKE ?"
    ), (f"{last_month}%",))
    trend_pct = abs(round((this_month_c - last_month_c) / last_month_c * 100)) if last_month_c > 0 else 0
    trend_dir = (
        "up" if this_month_c > last_month_c else
        ("down" if this_month_c < last_month_c else "flat")
    )

    unassigned = cnt(
        "SELECT COUNT(*) as cnt FROM reports WHERE status='open' "
        "AND (assigned_to='' OR assigned_to IS NULL)"
    )
    if USE_POSTGRES:
        sla_breach = cnt(
            "SELECT COUNT(*) as cnt FROM reports "
            "WHERE status NOT IN ('resolved','closed') "
            "AND submitted_at::timestamp < NOW() - INTERVAL '7 days'"
        )
    else:
        sla_breach = cnt(
            "SELECT COUNT(*) as cnt FROM reports "
            "WHERE status NOT IN ('resolved','closed') "
            "AND submitted_at < datetime('now','-7 days')"
        )

    resolution_rate = round(resolved/total*100) if total > 0 else 0

    c.execute("""
        SELECT ward, COUNT(*) as count,
               SUM(CASE WHEN status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN severity='critical' AND status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as critical_count
        FROM reports WHERE ward != '' AND ward IS NOT NULL GROUP BY ward ORDER BY count DESC LIMIT 10
    """)
    ward_rows = [dict(r) for r in c.fetchall()]

    c.execute("SELECT damage_type, COUNT(*) as count FROM reports GROUP BY damage_type ORDER BY count DESC LIMIT 10")
    damage_rows = [dict(r) for r in c.fetchall()]

    c.execute("SELECT severity, COUNT(*) as count FROM reports GROUP BY severity")
    severity_rows = [dict(r) for r in c.fetchall()]

    c.execute("SELECT status, COUNT(*) as count FROM reports GROUP BY status")
    status_data = {dict(r)["status"]: dict(r)["count"] for r in c.fetchall()}

    if USE_POSTGRES:
        c.execute("""
            SELECT TO_CHAR(submitted_at::timestamp,'YYYY-MM') as month, COUNT(*) as count
            FROM reports GROUP BY month ORDER BY month DESC LIMIT 12
        """)
    else:
        c.execute("""
            SELECT strftime('%Y-%m', submitted_at) as month, COUNT(*) as count
            FROM reports GROUP BY month ORDER BY month DESC LIMIT 12
        """)
    month_rows = list(reversed([dict(r) for r in c.fetchall()]))

    if USE_POSTGRES:
        c.execute("""
            SELECT TO_CHAR(submitted_at::timestamp,'DD Mon') as day, COUNT(*) as count
            FROM reports
            WHERE submitted_at::timestamp >= NOW() - INTERVAL '30 days'
            GROUP BY TO_CHAR(submitted_at::timestamp,'DD Mon'), DATE(submitted_at::timestamp)
            ORDER BY DATE(submitted_at::timestamp)
        """)
    else:
        c.execute("""
            SELECT strftime('%d %b', submitted_at) as day, COUNT(*) as count
            FROM reports WHERE submitted_at >= datetime('now','-30 days')
            GROUP BY strftime('%Y-%m-%d', submitted_at)
            ORDER BY strftime('%Y-%m-%d', submitted_at)
        """)
    day_rows = [dict(r) for r in c.fetchall()]

    if USE_POSTGRES:
        c.execute("""
            SELECT EXTRACT(HOUR FROM submitted_at::timestamp)::int as hour,
                   COUNT(*) as count
            FROM reports GROUP BY hour ORDER BY hour
        """)
    else:
        c.execute("""
            SELECT CAST(strftime('%H', submitted_at) AS INTEGER) as hour,
                   COUNT(*) as count
            FROM reports GROUP BY hour ORDER BY hour
        """)
    hourly_data = [dict(r) for r in c.fetchall()]

    WEEKDAYS = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
    if USE_POSTGRES:
        c.execute("""
            SELECT EXTRACT(DOW FROM submitted_at::timestamp)::int as dow,
                   COUNT(*) as count
            FROM reports GROUP BY dow ORDER BY dow
        """)
    else:
        c.execute("""
            SELECT CAST(strftime('%w', submitted_at) AS INTEGER) as dow,
                   COUNT(*) as count
            FROM reports GROUP BY dow ORDER BY dow
        """)
    wd_raw       = {dict(r)["dow"]: dict(r)["count"] for r in c.fetchall()}
    weekday_data = [{"day": WEEKDAYS[i], "count": wd_raw.get(i,0)} for i in range(7)]

    c.execute("""
        SELECT intake_channel as source, COUNT(*) as count
        FROM reports GROUP BY intake_channel ORDER BY count DESC
    """)
    source_rows = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT assigned_to as name, COUNT(*) as resolved_count
        FROM reports
        WHERE status IN ('resolved','closed')
          AND assigned_to != '' AND assigned_to IS NOT NULL
        GROUP BY assigned_to ORDER BY resolved_count DESC LIMIT 10
    """)
    officer_rows = [dict(r) for r in c.fetchall()]

    ai_stats = get_ai_accuracy_stats()
    conn.close()

    return {
        "total": total, "resolved": resolved, "open_c": open_c,
        "critical": critical, "today_c": today_c, "daily_avg": daily_avg,
        "avg_resolution_days": avg_resolution_days,
        "this_month_c": this_month_c, "trend_dir": trend_dir,
        "trend_pct": trend_pct, "unassigned": unassigned,
        "sla_breach": sla_breach, "resolution_rate": resolution_rate,
        "ward_rows": ward_rows, "damage_rows": damage_rows,
        "severity_rows": severity_rows, "status_data": status_data,
        "month_rows": month_rows, "day_rows": day_rows,
        "hourly_data": hourly_data, "weekday_data": weekday_data,
        "source_rows": source_rows, "officer_rows": officer_rows,
        "ai_accuracy": ai_stats,
    }

def get_analytics(): return get_analytics_data()


def get_commissioner_data(ward_filter: list = None) -> dict:
    """ward_filter: list of ward name strings to scope to (zonal commissioner).
    None = unscoped, full city (commissioner/admin)."""
    conn = get_conn(); c = conn.cursor()

    ward_sql, ward_params = "", ()
    if ward_filter is not None:
        if ward_filter:
            placeholders = ",".join(["?"] * len(ward_filter))
            ward_sql = f" AND ward IN ({placeholders})"
            ward_params = tuple(ward_filter)
        else:
            # Zone configured but resolves to zero wards — fail closed
            # with a condition that's always false, not invalid SQL.
            ward_sql = " AND 1=0"

    def cnt(sql, params=()):
        c.execute(_q(sql), params); return dict(c.fetchone())["cnt"] or 0

    total          = cnt("SELECT COUNT(*) as cnt FROM reports WHERE 1=1" + ward_sql, ward_params)
    open_total     = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed','pending_triage')" + ward_sql, ward_params)
    critical_open  = cnt("SELECT COUNT(*) as cnt FROM reports WHERE severity='critical' AND status NOT IN ('resolved','closed','pending_triage')" + ward_sql, ward_params)
    inspecting_now = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='inspecting'" + ward_sql, ward_params)
    unassigned     = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='open' AND (assigned_to='' OR assigned_to IS NULL)" + ward_sql, ward_params)
    pending_triage = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='pending_triage'" + ward_sql, ward_params)
    disputed_count = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='disputed'" + ward_sql, ward_params)
    ae_visit_req   = cnt("SELECT COUNT(*) as cnt FROM reports WHERE ae_site_visit_required=1 AND ae_site_visit_done=0" + ward_sql, ward_params)

    if USE_POSTGRES:
        resolved_today      = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed') AND updated_at::date = CURRENT_DATE" + ward_sql, ward_params)
        resolved_this_month = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed') AND TO_CHAR(updated_at::timestamp,'YYYY-MM') = TO_CHAR(NOW(),'YYYY-MM')" + ward_sql, ward_params)
        sla_breached        = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed') AND submitted_at::timestamp < NOW() - INTERVAL '7 days'" + ward_sql, ward_params)
        c.execute(_q("SELECT AVG(EXTRACT(EPOCH FROM (updated_at::timestamp - submitted_at::timestamp))/86400) as avg_days FROM reports WHERE status IN ('resolved','closed')" + ward_sql), ward_params)
    else:
        resolved_today      = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed') AND date(updated_at) = date('now')" + ward_sql, ward_params)
        resolved_this_month = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed') AND strftime('%Y-%m',updated_at) = strftime('%Y-%m','now')" + ward_sql, ward_params)
        sla_breached        = cnt("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed') AND submitted_at < datetime('now','-7 days')" + ward_sql, ward_params)
        c.execute(_q("SELECT AVG((julianday(updated_at) - julianday(submitted_at))) as avg_days FROM reports WHERE status IN ('resolved','closed')" + ward_sql), ward_params)

    avg_days        = round(dict(c.fetchone())["avg_days"] or 0, 1)
    resolution_rate = round((total-open_total)/total*100) if total > 0 else 0

    c.execute(_q(f"""
        SELECT report_id, ward, damage_type, submitted_at,
               severity_details, assigned_to, assigned_officer
        FROM reports
        WHERE severity='critical' AND status NOT IN ('resolved','closed'){ward_sql}
        ORDER BY submitted_at ASC LIMIT 10
    """), ward_params)
    critical_reports = [dict(r) for r in c.fetchall()]

    c.execute(_q(f"""
        SELECT assigned_to as name, COUNT(*) as resolved_count
        FROM reports
        WHERE status IN ('resolved','closed') AND assigned_to != ''{ward_sql}
        GROUP BY assigned_to ORDER BY resolved_count DESC LIMIT 8
    """), ward_params)
    eng_rows = [dict(r) for r in c.fetchall()]

    c.execute(_q(f"""
        SELECT ward,
               COUNT(*) as total,
               SUM(CASE WHEN status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN severity='critical' AND status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as critical_count
        FROM reports
        WHERE ward != '' AND ward IS NOT NULL{ward_sql}
        GROUP BY ward ORDER BY open_count DESC LIMIT 10
    """), ward_params)
    ward_rows = [dict(r) for r in c.fetchall()]

    if USE_POSTGRES:
        c.execute("""
            SELECT TO_CHAR(submitted_at::timestamp,'Mon YY') as month,
                   COUNT(*) as count
            FROM reports GROUP BY month ORDER BY MIN(submitted_at) DESC LIMIT 6
        """)
    else:
        c.execute("""
            SELECT strftime('%b %y', submitted_at) as month, COUNT(*) as count
            FROM reports GROUP BY month ORDER BY submitted_at DESC LIMIT 6
        """)
    month_rows = list(reversed([dict(r) for r in c.fetchall()]))

    c.execute(_q(f"SELECT damage_type, COUNT(*) as count FROM reports WHERE 1=1{ward_sql} GROUP BY damage_type ORDER BY count DESC LIMIT 6"), ward_params)
    damage_rows = [dict(r) for r in c.fetchall()]

    c.execute(_q(f"""
        SELECT assigned_officer as name,
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('resolved','closed') THEN 1 ELSE 0 END) as resolved_count
        FROM reports
        WHERE assigned_officer != '' AND assigned_officer IS NOT NULL{ward_sql}
        GROUP BY assigned_officer ORDER BY resolved_count DESC LIMIT 8
    """), ward_params)
    officer_rows = [dict(r) for r in c.fetchall()]

    conn.close()
    return {
        "total": total, "open_total": open_total,
        "critical_open": critical_open, "inspecting_now": inspecting_now,
        "unassigned": unassigned, "resolved_this_month": resolved_this_month,
        "sla_breached": sla_breached, "resolved_today": resolved_today,
        "resolution_rate": resolution_rate, "avg_days": avg_days,
        "critical_reports": critical_reports, "eng_rows": eng_rows,
        "ward_rows": ward_rows, "month_rows": month_rows,
        "damage_rows": damage_rows, "officer_rows": officer_rows,
        "pending_triage": pending_triage, "disputed_count": disputed_count,
        "ae_visit_required": ae_visit_req,
        "zone_performance": [],
        "sla_warning_now": 0,
        "sla_expiry_breached": 0,
        "override_stats": get_override_verification_stats(),
    }

def get_live_stats() -> dict:
    conn = get_conn(); c = conn.cursor()
    def cnt(sql):
        c.execute(sql); return dict(c.fetchone())["cnt"] or 0
    stats = {
        "open":           cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='open'"),
        "assigned":       cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='assigned'"),
        "open_total":     cnt("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed','pending_triage')"),
        "unassigned":     cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='open' AND (assigned_to='' OR assigned_to IS NULL)"),
        "critical_open":  cnt("SELECT COUNT(*) as cnt FROM reports WHERE severity='critical' AND status NOT IN ('resolved','closed','pending_triage')"),
        "resolved":       cnt("SELECT COUNT(*) as cnt FROM reports WHERE status IN ('resolved','closed')"),
        "total":          cnt("SELECT COUNT(*) as cnt FROM reports"),
        "inspecting":     cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='inspecting'"),
        "inspected":      cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='inspected'"),
        "pending_triage": cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='pending_triage'"),
        "disputed":       cnt("SELECT COUNT(*) as cnt FROM reports WHERE status='disputed'"),
        "new_count":      0,
    }
    if USE_POSTGRES:
        c.execute("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed') AND submitted_at::timestamp < NOW() - INTERVAL '7 days'")
    else:
        c.execute("SELECT COUNT(*) as cnt FROM reports WHERE status NOT IN ('resolved','closed') AND submitted_at < datetime('now','-7 days')")
    stats["sla_breached"] = dict(c.fetchone())["cnt"] or 0
    conn.close(); return stats

# ─────────────────────────────────────────────────────────────────────────────
# WARD STATS (PUBLIC)
# ─────────────────────────────────────────────────────────────────────────────

def get_ward_stats(ward: str) -> dict:
    conn = get_conn(); c = conn.cursor()
    def cnt(sql, p):
        c.execute(sql, p); return dict(c.fetchone())["cnt"] or 0
    total      = cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE ward=?"), (ward,))
    open_count = cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE ward=? AND status NOT IN ('resolved','closed')"), (ward,))
    resolved   = cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE ward=? AND status IN ('resolved','closed')"), (ward,))
    critical   = cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE ward=? AND severity='critical'"), (ward,))
    rate       = round(resolved/total*100) if total > 0 else 0
    c.execute(_q(
        "SELECT report_id, damage_type, status, severity, submitted_at, assigned_to "
        "FROM reports WHERE ward=? ORDER BY submitted_at DESC LIMIT 10"
    ), (ward,))
    recent = [dict(r) for r in c.fetchall()]
    conn.close()
    return {
        "ward": ward, "ward_name": ward, "total": total, "open": open_count,
        "resolved": resolved, "critical": critical, "pending": open_count,
        "resolution_rate": rate, "rate": rate,
        "recent_reports": recent, "recent": recent, "rank": 1, "total_wards": 98,
    }


def get_ward_public_data(ward_id: str) -> dict:
    stats      = get_ward_stats(ward_id)
    all_stats  = get_all_ward_stats()
    sorted_wards = sorted(all_stats, key=lambda w: w["resolution_rate"], reverse=True)
    rank = next((i+1 for i,w in enumerate(sorted_wards) if w["ward"]==ward_id), 1)
    stats["rank"] = rank; stats["total_wards"] = len(sorted_wards) or 98
    return stats


def get_all_ward_stats() -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT ward,
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('resolved','closed') THEN 1 ELSE 0 END) as resolved,
               SUM(CASE WHEN status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as open_count
        FROM reports WHERE ward != '' AND ward IS NOT NULL GROUP BY ward ORDER BY total DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["resolution_rate"] = round(r["resolved"]/r["total"]*100) if r["total"] > 0 else 0
    return rows


def get_all_wards_data() -> dict:
    rows        = get_all_ward_stats()
    sorted_rows = sorted(rows, key=lambda w: w["resolution_rate"], reverse=True)
    return {
        "wards": [{
            "ward": w["ward"], "total": w["total"],
            "resolved": w["resolved"], "open": w["open_count"],
            "rate": w["resolution_rate"], "rank": i+1, "avg_days": 0,
        } for i,w in enumerate(sorted_rows)],
        "total_wards": len(sorted_rows),
    }

# ─────────────────────────────────────────────────────────────────────────────
# RQI — ROAD QUALITY INDEX
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    R   = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a   = (
        math.sin(math.radians(lat2-lat1)/2)**2 +
        math.cos(phi1)*math.cos(phi2)*math.sin(math.radians(lon2-lon1)/2)**2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def check_rqi_breach(report_id: str, ward: str, damage_type: str, lat, lng) -> bool:
    if not lat or not lng: return False
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute("""
            SELECT report_id, repair_lat, repair_lng, contractor_name, repaired_at
            FROM repair_records
            WHERE repaired_at::timestamptz > NOW() - INTERVAL '6 months'
              AND repair_lat IS NOT NULL
        """)
    else:
        c.execute("""
            SELECT report_id, repair_lat, repair_lng, contractor_name, repaired_at
            FROM repair_records
            WHERE repaired_at > datetime('now','-6 months')
              AND repair_lat IS NOT NULL
        """)
    repairs = [dict(r) for r in c.fetchall()]
    breach_found = False
    for rep in repairs:
        dist = _haversine_m(lat, lng, rep["repair_lat"], rep["repair_lng"])
        if dist <= 50:
            try:
                days = (datetime.now() - datetime.strptime(rep["repaired_at"][:19],"%Y-%m-%d %H:%M:%S")).days
            except Exception:
                days = 0
            c.execute(_q("""
                INSERT INTO rqi_events
                    (original_report_id, new_report_id, contractor_name,
                     distance_m, days_since_repair, flagged_at)
                VALUES (?,?,?,?,?,?)
            """), (rep["report_id"], report_id, rep.get("contractor_name",""), round(dist,1), days, now()))
            c.execute(_q("UPDATE reports SET rqi_flagged=1 WHERE report_id=?"), (report_id,))
            breach_found = True
    if breach_found: conn.commit()
    conn.close(); return breach_found


def check_duplicate_complaint(
    report_id: str, damage_type: str, lat, lng,
    ward: str = "", radius_m: int = 100,
) -> dict:
    conn = get_conn(); c = conn.cursor()
    if lat and lng:
        c.execute(_q("""
            SELECT report_id, damage_type, ward, status, submitted_at, latitude, longitude
            FROM reports
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
              AND status NOT IN ('resolved','closed')
              AND report_id != ? AND damage_type = ?
        """), (report_id, damage_type))
        candidates  = [dict(r) for r in c.fetchall()]
        conn.close()
        nearest, nearest_dist = None, float("inf")
        for r in candidates:
            try:
                dist = _haversine_m(lat, lng, float(r["latitude"]), float(r["longitude"]))
                if dist <= radius_m and dist < nearest_dist:
                    nearest_dist = dist; nearest = r
            except Exception:
                continue
        if nearest:
            return {
                "found": True, "report_id": nearest["report_id"],
                "damage_type": nearest["damage_type"], "ward": nearest["ward"],
                "status": nearest["status"], "distance_m": round(nearest_dist),
            }
        return {"found": False}
    else:
        if not ward:
            conn.close(); return {"found": False}
        if USE_POSTGRES:
            c.execute(_q("""
                SELECT report_id, damage_type, ward, status FROM reports
                WHERE ward=? AND damage_type=? AND status NOT IN ('resolved','closed')
                  AND report_id!=?
                  AND submitted_at::timestamptz > NOW() - INTERVAL '24 hours'
                ORDER BY submitted_at DESC LIMIT 1
            """), (ward, damage_type, report_id))
        else:
            c.execute(_q("""
                SELECT report_id, damage_type, ward, status FROM reports
                WHERE ward=? AND damage_type=? AND status NOT IN ('resolved','closed')
                  AND report_id!=?
                  AND submitted_at > datetime('now','-24 hours')
                ORDER BY submitted_at DESC LIMIT 1
            """), (ward, damage_type, report_id))
        row = c.fetchone(); conn.close()
        if row:
            r = dict(row)
            return {
                "found": True, "report_id": r["report_id"],
                "damage_type": r["damage_type"], "ward": r["ward"],
                "status": r["status"], "distance_m": None,
            }
        return {"found": False}


def get_rqi_data() -> dict:
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT e.*, r.ward, r.damage_type
        FROM rqi_events e
        LEFT JOIN reports r ON e.new_report_id = r.report_id
        ORDER BY e.flagged_at DESC LIMIT 50
    """)
    events = [dict(r) for r in c.fetchall()]
    c.execute("SELECT COUNT(*) as cnt FROM rqi_events WHERE seen_by_commissioner=0")
    unseen = dict(c.fetchone())["cnt"] or 0
    conn.close()
    return {"events": events, "unseen_count": unseen}


def mark_rqi_seen():
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE rqi_events SET seen_by_commissioner=1")
    conn.commit(); conn.close()


def add_repair_record(
    report_id: str, contractor_name: str,
    repair_lat=None, repair_lng=None,
    recorded_by: str = "", warranty_months: int = 6,
):
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("""
        INSERT INTO repair_records
            (report_id, contractor_name, repair_lat, repair_lng,
             warranty_months, repaired_at, recorded_by)
        VALUES (?,?,?,?,?,?,?)
    """), (report_id, contractor_name, repair_lat, repair_lng,
           warranty_months, now(), recorded_by))
    conn.commit(); conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# STAFF AUDIT LOG
# ─────────────────────────────────────────────────────────────────────────────

def get_staff_audit_log(limit: int = 100) -> list:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM staff_audit_log ORDER BY done_at DESC LIMIT ?"), (limit,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]
def get_override_verification_stats(days: int = 30) -> dict:
    """Override-verification counts per AE/admin — commissioner visibility
    into how often supervisors are bypassing the assigned engineer."""
    conn = get_conn(); c = conn.cursor()
    if USE_POSTGRES:
        c.execute(f"""
            SELECT done_by, COUNT(*) as override_count
            FROM audit_log
            WHERE action = 'override_verification'
              AND done_at::timestamp > NOW() - INTERVAL '{days} days'
            GROUP BY done_by ORDER BY override_count DESC
        """)
    else:
        c.execute(f"""
            SELECT done_by, COUNT(*) as override_count
            FROM audit_log
            WHERE action = 'override_verification'
              AND done_at > datetime('now','-{days} days')
            GROUP BY done_by ORDER BY override_count DESC
        """)
    rows = [dict(r) for r in c.fetchall()]
    c.execute("SELECT COUNT(*) as cnt FROM audit_log WHERE action='override_verification'")
    total_all_time = dict(c.fetchone())["cnt"] or 0
    conn.close()
    return {
        "by_officer": rows,
        "total_30d": sum(r["override_count"] for r in rows),
        "total_all_time": total_all_time,
    }
# ─────────────────────────────────────────────────────────────────────────────
# TRAINING DATA
# ─────────────────────────────────────────────────────────────────────────────

TRAINING_DIR = "training_data"
TRAINING_CSV = "training_data/labels.csv"
TRAINING_CSV_HEADERS = [
    "report_id","ward","citizen_damage_type","verified_damage_type",
    "severity","site_condition","verified_by","verified_at","photo_filename",
    "is_override",
]


def save_training_sample(
    report_id, ward, citizen_damage_type, verified_damage_type,
    severity, site_condition, verified_by, verified_at, photo_data,
    is_override=False,
):
    try:
        os.makedirs(TRAINING_DIR, exist_ok=True)
        photo_filename = ""
        if photo_data and photo_data.startswith("data:"):
            try:
                header, b64 = photo_data.split(",", 1)
                ext         = header.split("/")[1].split(";")[0]
                safe_label  = verified_damage_type.replace(" ","_").replace("/","_")
                photo_filename = f"{safe_label}_{secrets.token_hex(6)}.{ext}"
                with open(os.path.join(TRAINING_DIR, photo_filename), "wb") as f:
                    f.write(base64.b64decode(b64))
            except Exception as e:
                print(f"[training] photo: {e}")
        exists = os.path.exists(TRAINING_CSV)
        with open(TRAINING_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRAINING_CSV_HEADERS, extrasaction="ignore")
            if not exists: w.writeheader()
            w.writerow({
                "report_id": report_id, "ward": ward,
                "citizen_damage_type": citizen_damage_type,
                "verified_damage_type": verified_damage_type,
                "severity": severity, "site_condition": site_condition,
                "verified_by": verified_by, "verified_at": verified_at,
                "photo_filename": photo_filename,
                "is_override": "yes" if is_override else "no",
            })
    except Exception as e:
        print(f"[training] error: {e}")


def get_training_stats() -> dict:
    try:
        if not os.path.exists(TRAINING_CSV):
            return {"total": 0, "by_type": {}, "corrections": 0}
        total, by_type, corrections = 0, {}, 0
        with open(TRAINING_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total += 1
                dt = row.get("verified_damage_type","Unknown")
                by_type[dt] = by_type.get(dt,0) + 1
                if row.get("citizen_damage_type") != row.get("verified_damage_type"):
                    corrections += 1
        return {"total": total, "by_type": by_type, "corrections": corrections}
    except Exception:
        return {"total": 0, "by_type": {}, "corrections": 0}

# ─────────────────────────────────────────────────────────────────────────────
# COMPATIBILITY ALIASES — bridge old main.py calls to new function names
# ─────────────────────────────────────────────────────────────────────────────

def get_division_by_ward(ward_name: str) -> str:
    """Old name — routes to wards.py lookup."""
    try:
        from wards import get_division_for_ward_name
        return get_division_for_ward_name(ward_name)
    except Exception:
        return "Unknown"


def save_work_done_photo(report_id: str, photo_data: str, done_by: str):
    """Old name → mark_work_done()"""
    return mark_work_done(report_id, photo_data, done_by)


def save_citizen_review(report_id: str, satisfied: bool, note: str = ""):
    """
    Old name used by main.py citizen_review route.
    New system uses token-based reviews — this bridges the gap.
    """
    conn = get_conn(); c = conn.cursor(); ts = now()
    # FIX DB-1: "satisfied" is not a valid status — use "resolved" to keep
    # the complaint in a valid state after citizen confirms work is done.
    status_val = "resolved" if satisfied else "disputed"
    c.execute(_q("""
        UPDATE reports
        SET citizen_satisfied=?, satisfaction_reviewed_at=?,
            updated_at=?, status=?
        WHERE report_id=?
    """), (1 if satisfied else 0, ts, ts, status_val, report_id))
    if not satisfied:
        c.execute(_q("""
            UPDATE reports
            SET dispute_count = COALESCE(dispute_count,0) + 1,
                ae_site_visit_required=1
            WHERE report_id=?
        """), (report_id,))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "citizen_review", "", status_val, "citizen", ts))
    conn.commit(); conn.close()


def force_close_second_dispute(report_id: str):
    """
    Old name used by main.py.
    New system requires AE to close — this routes to ae_resolve_disputed.
    For backward compat, marks ae_site_visit_required instead of force closing.
    """
    conn = get_conn(); c = conn.cursor(); ts = now()
    c.execute(_q("""
        UPDATE reports
        SET ae_site_visit_required=1, status='disputed',
            updated_at=?, updated_by=?
        WHERE report_id=?
    """), (ts, "System — AE Review Required", report_id))
    c.execute(_q(
        "INSERT INTO audit_log "
        "(report_id,action,old_value,new_value,done_by,done_at) "
        "VALUES (?,?,?,?,?,?)"
    ), (report_id, "second_dispute_ae_required", "resolved",
        "ae_site_visit_required", "System", ts))
    conn.commit(); conn.close()


def get_disputed_reports_for_review(division_name: str = None) -> list:
    """Old name → get_disputed_reports_for_ae()"""
    return get_disputed_reports_for_ae(division=division_name or "")


def log_training_label(
    report_id: str, ward: str,
    reported_type: str, verified_type: str,
    verified_by: str, division: str = "",
    auto_assigned: bool = False,
    escalation_count: int = 0,
):
    """Old name — saves to training CSV."""
    save_training_sample(
        report_id=report_id,
        ward=ward,
        citizen_damage_type=reported_type,
        verified_damage_type=verified_type,
        severity="",
        site_condition="",
        verified_by=verified_by,
        verified_at=now(),
        photo_data="",
    )
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("UPDATE reports SET verified_damage_type=? WHERE report_id=?"),
              (verified_type, report_id))
    conn.commit(); conn.close()


def get_archived_report(report_id: str) -> dict | None:
    """
    Old name — archive table removed in new schema.
    Falls back to live reports table.
    """
    return get_report_by_id(report_id)


def get_archived_reports_by_phone(phone: str) -> list:
    """Old name — falls back to live reports only."""
    return get_reports_by_phone(phone)


def get_live_stats_for_role(
    staff_name: str, role: str,
    division: str = "", ward_list: str = ""
) -> dict:
    """Old name — returns scoped live stats."""
    stats = get_live_stats()
    if role in ("was", "field_engineer"):
        conn = get_conn(); c = conn.cursor()
        def cnt(sql, p):
            c.execute(sql, p); return dict(c.fetchone())["cnt"] or 0
        scoped = {
            "open":         cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status='open'"), (staff_name,)),
            "assigned":     cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status='assigned'"), (staff_name,)),
            "open_total":   cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status NOT IN ('resolved','closed')"), (staff_name,)),
            "critical_open":cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND severity='critical' AND status NOT IN ('resolved','closed')"), (staff_name,)),
            "resolved":     cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status IN ('resolved','closed')"), (staff_name,)),
            "total":        cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=?"), (staff_name,)),
            "inspecting":   cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status='inspecting'"), (staff_name,)),
            "inspected":    cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status='inspected'"), (staff_name,)),
            "disputed":     cnt(_q("SELECT COUNT(*) as cnt FROM reports WHERE assigned_to=? AND status='disputed'"), (staff_name,)),
            "unassigned": 0, "new_count": 0, "sla_breached": 0, "pending_triage": 0,
        }
        conn.close(); return scoped
    elif role in ("zonal_commissioner", "ae") and division:
        conn = get_conn(); c = conn.cursor()
        def cntd(sql, p):
            c.execute(sql, p); return dict(c.fetchone())["cnt"] or 0
        scoped = {
            "open":         cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status='open'"), (division,)),
            "assigned":     cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status='assigned'"), (division,)),
            "open_total":   cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status NOT IN ('resolved','closed')"), (division,)),
            "resolved":     cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status IN ('resolved','closed')"), (division,)),
            "total":        cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=?"), (division,)),
            "critical_open":cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND severity='critical' AND status NOT IN ('resolved','closed')"), (division,)),
            "unassigned":   cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status='open' AND (assigned_to='' OR assigned_to IS NULL)"), (division,)),
            "disputed":     cntd(_q("SELECT COUNT(*) as cnt FROM reports WHERE division_name=? AND status='disputed'"), (division,)),
            "inspecting": 0, "inspected": 0, "new_count": 0,
            "sla_breached": 0, "pending_triage": 0,
        }
        conn.close(); return scoped
    return stats

def create_repair_record(
    report_id: str, recorded_by: str,
    contractor_name: str = "", warranty_months: int = 6,
):
    """Alias used by demo_complaints.py"""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT latitude, longitude FROM reports WHERE report_id=?"), (report_id,))
    row = c.fetchone(); conn.close()
    r = dict(row) if row else {}
    add_repair_record(report_id, contractor_name,
                      r.get("latitude"), r.get("longitude"),
                      recorded_by, warranty_months)

def get_ae_for_division(division: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM staff WHERE role='ae' AND division=? AND is_active=1 LIMIT 1"), (division,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def get_zonal_commissioner_for_division(division: str) -> dict | None:
    conn = get_conn(); c = conn.cursor()
    c.execute(_q("SELECT * FROM staff WHERE role='zonal_commissioner' AND is_active=1 LIMIT 1"))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def get_parent_name_for_engineer(engineer_name: str) -> str:
    """Given a WAS/field_engineer's name, returns their supervising officer's
    name, or '' if none set. Used to gate override-verification authority."""
    conn = get_conn(); c = conn.cursor()
    c.execute(_q(
        "SELECT supervised_by FROM staff WHERE name=? AND role IN ('was','field_engineer') LIMIT 1"
    ), (engineer_name,))
    row = c.fetchone()
    if not row or not dict(row)["supervised_by"]:
        conn.close(); return ""
    c.execute(_q("SELECT name FROM staff WHERE id=?"), (dict(row)["supervised_by"],))
    row2 = c.fetchone(); conn.close()
    return dict(row2)["name"] if row2 else ""

def get_zone_performance() -> list:
    """Alias — returns commissioner zone performance data."""
    conn = get_conn(); c = conn.cursor()
    c.execute("""
        SELECT division_name as division,
               COUNT(*) as total,
               SUM(CASE WHEN status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN status IN ('resolved','closed') THEN 1 ELSE 0 END) as resolved,
               SUM(CASE WHEN severity='critical' AND status NOT IN ('resolved','closed') THEN 1 ELSE 0 END) as critical_count
        FROM reports
        WHERE division_name != '' AND division_name IS NOT NULL
        GROUP BY division_name ORDER BY open_count DESC
    """)
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    for r in rows:
        r["resolution_rate"] = round(r["resolved"]/r["total"]*100) if r["total"] > 0 else 0
    return rows