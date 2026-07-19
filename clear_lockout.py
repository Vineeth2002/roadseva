"""
clear_lockout.py — Clears failed login attempts before a demo
Run if login is failing or locked out.
Command: D:\\Anaconda\\python.exe clear_lockout.py
"""

import sqlite3
import os

DB = "roadseva.db"

if not os.path.exists(DB):
    print("roadseva.db not found — nothing to clear")
else:
    conn = sqlite3.connect(DB)
    before = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE success=0"
    ).fetchone()[0]
    conn.execute("DELETE FROM login_attempts")
    conn.commit()
    conn.close()
    print(f"Cleared {before} failed login attempts")
    print("Login lockout lifted — you can now log in normally")