import database

conn = database.get_conn()
c = conn.cursor()

usernames = (
    'commissioner1','zc_east','ae_east','ae_test2','was_ward22',
    'engineer1','grievance1','viewer1','itadmin1','triage_1'
)

placeholders = ",".join(["?"] * len(usernames))
c.execute(
    database._q(f"SELECT username, must_change_password, is_active FROM staff WHERE username IN ({placeholders})"),
    usernames
)
rows = c.fetchall()
conn.close()

if not rows:
    print("None of these usernames exist in the database yet.")
else:
    print(f"{'username':<16} {'must_change_password':<22} {'is_active'}")
    for r in rows:
        d = dict(r)
        flag = "⚠️  STILL LEAKED-PASSWORD VALID" if d['must_change_password'] == 1 else "OK — password changed"
        print(f"{d['username']:<16} {d['must_change_password']:<22} {d['is_active']}   {flag}")