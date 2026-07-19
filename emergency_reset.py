import database

leaked_active_usernames = ('ae_east', 'engineer1', 'viewer1', 'was_ward22', 'zc_east')

for username in leaked_active_usernames:
    staff = database.get_staff_by_username(username)
    if not staff:
        print(f"{username}: not found — skip")
        continue
    ok, new_pass = database.reset_staff_password(staff["id"], "emergency_leak_response")
    if ok:
        print(f"{username}: RESET OK — new temp password: {new_pass}")
    else:
        print(f"{username}: RESET FAILED — {new_pass}")