import os
from dotenv import load_dotenv
load_dotenv()

import database
from wards import WARD_NAMES

print("--- RoadSeva Pre-Submit Check ---")

# DB init
try:
    database.init_db()
    print("DB init:       OK")
except Exception as e:
    print("DB init:       FAIL -", e)

# Admin login
try:
    admin_pass = os.getenv("ADMIN_PASSWORD", "Admin@2024!")
    s, err = database.authenticate_staff("admin", admin_pass, "127.0.0.1")
    print("Admin login:   " + ("OK" if s else "FAIL - " + err))
except Exception as e:
    print("Admin login:   FAIL -", e)

# Ward count
try:
    count = len(WARD_NAMES)
    print("Ward count:    " + str(count) + (" OK" if count == 98 else " FAIL — expected 98"))
except Exception as e:
    print("Ward count:    FAIL -", e)

# Templates
templates = [
    "about","account_log","admin","analytics","change_password",
    "citizen","commissioner","credential_card","field","landing",
    "login","map","privacy","route","rqi","setup","staff",
    "staff_log","submitted","team","track","ward_public","wards"
]
missing = [t for t in templates if not os.path.exists(f"templates/{t}.html")]
print("Templates:     " + ("ALL OK" if not missing else "MISSING: " + str(missing)))

# Groq API key
groq_key = os.getenv("GROQ_API_KEY", "")
print("Groq API key:  " + ("SET" if groq_key else "MISSING — add to .env"))

# Failed logins
try:
    conn = database.get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM login_attempts WHERE success=0")
        failed = c.fetchone()[0]
    finally:
        conn.close()
    print("Failed logins: " + str(failed) + (" — clear before demo" if failed >= 5 else " OK"))
except Exception as e:
    print("Failed logins: FAIL -", e)

# Reports count
try:
    conn = database.get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reports")
        rcount = c.fetchone()[0]
    finally:
        conn.close()
    print("Reports in DB: " + str(rcount))
except Exception as e:
    print("Reports in DB: FAIL -", e)

print("---------------------------------")
print("If all lines show OK — you are ready.")