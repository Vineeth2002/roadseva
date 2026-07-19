import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "roadseva.db")

confirm = input(f"WARNING: This will wipe all data in '{DB_PATH}'. Type YES to continue: ")
if confirm != "YES":
    print("Aborted.")
    exit()

c = sqlite3.connect(DB_PATH)
c.execute("DELETE FROM staff WHERE username != 'admin'")
c.execute("DELETE FROM reports")
c.execute("DELETE FROM sessions")
c.execute("DELETE FROM audit_log")
c.execute("DELETE FROM comments")
c.commit()
c.close()
print("Clean. Only admin remains.")
for r in sqlite3.connect(DB_PATH).execute("SELECT username, role FROM staff"):
    print(" ", r[0], "-", r[1])