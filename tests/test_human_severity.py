"""
tests/test_human_severity.py -- independent human severity verification
==========================================================================
Proves human severity is captured via the ACTUAL live route the field.html
UI submits to (/verify-inspection -> save_inspection_verification()), is
never copied from the AI value, is stored independently in both
ai_corrections and training_labels, and is correctly linked to the exact
ai_inference_runs row it verifies (server-determined, never client-
supplied).

Run with: pytest tests/test_human_severity.py -v
"""

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import authz


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_human_severity.db")
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


def make_staff(role, username, **kw):
    ok, result = database.add_staff(
        name=kw.get("name", f"Test {role.title()} {username}"), username=username, password="TestPass@2024!",
        role=role, created_by="test_suite", must_change=False,
        zone=kw.get("zone", ""), division=kw.get("division", ""),
        ward_list=kw.get("ward_list", ""),
    )
    assert ok, f"setup failed: {result}"
    return database.get_staff_by_username(username)


def login(role, username, **kw):
    from main import app
    staff = make_staff(role, username, **kw)
    token = database.create_session(staff["id"])
    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set("session_token", token)
    return c, staff


def get_csrf(client):
    from security import generate_csrf_token
    session_token = list(client.cookies.jar)[0].value
    return generate_csrf_token(session_token)


def make_report_with_ai_severity(ai_severity="high", assigned_to=""):
    rid = database.add_report(
        city="GVMC", ward="Ward 1 - Kondapeta / Wilsonpeta", damage_type="Pothole",
        description="test", photo_path="", citizen_name="Citizen",
        citizen_phone="9999999999", citizen_email="",
        latitude=17.0, longitude=83.0, photo_data="data:image/jpeg;base64,AAAA",
    )
    database.update_report_severity(rid, ai_severity, "AI assessment", "", "")
    database.log_inference_attempt(rid, "groq", "meta-llama/llama-4-scout-17b-16e-instruct",
        database.now(), database.now(), ai_severity, True, "ok", "", "local_upload")
    if assigned_to:
        database.assign_report(rid, assigned_to, "test_suite")
    return rid


def small_jpeg_file():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), color=(120, 120, 120)).save(buf, format="JPEG")
    buf.seek(0)
    return ("site.jpg", buf, "image/jpeg")


class TestHumanSeverityRequired:

    def test_blank_value_rejected_by_route(self):
        client, staff = login("field_engineer", "fe_route")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert resp.status_code == 302
        assert "error" in resp.headers.get("location", "")

    def test_valid_critical_accepted(self):
        client, staff = login("field_engineer", "fe_c")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "critical", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert resp.status_code == 302
        assert "error" not in resp.headers.get("location", "")

    def test_valid_high_accepted(self):
        client, staff = login("field_engineer", "fe_h")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "high", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert "error" not in resp.headers.get("location", "")

    def test_valid_medium_accepted(self):
        client, staff = login("field_engineer", "fe_m")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "medium", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert "error" not in resp.headers.get("location", "")

    def test_valid_low_accepted(self):
        client, staff = login("field_engineer", "fe_l")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "low", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert "error" not in resp.headers.get("location", "")

    def test_invalid_value_rejected(self):
        client, staff = login("field_engineer", "fe_bad")
        rid = make_report_with_ai_severity(assigned_to=staff["name"])
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "super-bad", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert "error" in resp.headers.get("location", "")


class TestNoCopyingFromAI:

    def test_human_severity_not_prefilled_from_ai_in_template(self):
        html = open("templates/field.html", encoding="utf-8").read()
        block_start = html.index('name="human_severity"')
        block = html[block_start:block_start + 400]
        assert 'value="" selected disabled' in block
        assert 'value="critical" selected' not in block
        assert 'value="high" selected' not in block
        assert 'value="medium" selected' not in block
        assert 'value="low" selected' not in block

    def test_ai_severity_unchanged_by_human_verification(self):
        rid = make_report_with_ai_severity(ai_severity="critical")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="low", verifier_role="was",
        )
        report = database.get_report_by_id(rid)
        assert report["severity"] == "critical"


class TestSeparateStorage:

    def test_training_labels_stores_both_independently(self):
        rid = make_report_with_ai_severity(ai_severity="high")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="medium", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT severity, human_severity FROM training_labels WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["severity"] == "high"
        assert row["human_severity"] == "medium"

    def test_ai_corrections_stores_original_and_human_severity(self):
        rid = make_report_with_ai_severity(ai_severity="critical")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="low", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT original_ai_severity, corrected_severity FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["original_ai_severity"] == "critical"
        assert row["corrected_severity"] == "low"


class TestInferenceLinkage:

    def test_inference_run_id_points_to_correct_run(self):
        rid = make_report_with_ai_severity(ai_severity="high")
        database.log_inference_attempt(rid, "groq", "meta-llama/llama-4-scout-17b-16e-instruct",
            database.now(), database.now(), "medium", True, "ok", "", "reports.photo_data")
        latest = database.get_latest_inference_run(rid)
        assert latest["attempt_number"] == 2
        assert latest["raw_severity"] == "medium"

        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="medium", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT inference_run_id, original_ai_severity FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["inference_run_id"] == latest["id"]
        assert row["original_ai_severity"] == "medium"

    def test_wrong_report_inference_run_not_used(self):
        rid_a = make_report_with_ai_severity(ai_severity="high")
        rid_b = make_report_with_ai_severity(ai_severity="critical")
        run_a = database.get_latest_inference_run(rid_a)
        run_b = database.get_latest_inference_run(rid_b)
        assert run_a["id"] != run_b["id"]
        assert run_a["report_id"] == rid_a
        assert run_b["report_id"] == rid_b

    def test_missing_inference_run_handled_safely(self):
        rid = database.add_report(
            city="GVMC", ward="Ward 1 - Kondapeta / Wilsonpeta", damage_type="Pothole",
            description="test", photo_path="", citizen_name="Citizen",
            citizen_phone="9999999999", citizen_email="",
            latitude=17.0, longitude=83.0, photo_data="",
        )
        ok = database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="medium", verifier_role="was",
        )
        assert ok is True
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT inference_run_id FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["inference_run_id"] is None


class TestVerifierRole:

    def test_verifier_role_stored(self):
        rid = make_report_with_ai_severity()
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="high", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT verifier_role FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["verifier_role"] == "was"


class TestAgreementDisagreement:

    def test_agreement_recorded_correctly(self):
        rid = make_report_with_ai_severity(ai_severity="high")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="high", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT original_ai_correct FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["original_ai_correct"] == 1

    def test_disagreement_recorded_correctly(self):
        rid = make_report_with_ai_severity(ai_severity="high")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="low", verifier_role="was",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT original_ai_correct FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["original_ai_correct"] == 0


class TestExistingDamageTypeCorrectionUnaffected:

    def test_damage_type_correction_still_works(self):
        rid = make_report_with_ai_severity()
        database.save_inspection_verification(
            rid, "Road Cave-in", "worse", "data:image/jpeg;base64,BB", "Engineer",
            human_severity=None, verifier_role="",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT corrected_damage_type FROM ai_corrections WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["corrected_damage_type"] == "Road Cave-in"


class TestAuthorization:

    def test_unauthorized_user_cannot_submit(self):
        client, staff = login("field_engineer", "fe_unrelated")
        rid = make_report_with_ai_severity(assigned_to="Someone Else Entirely")
        csrf = get_csrf(client)
        resp = client.post("/verify-inspection", data={
            "report_id": rid, "verified_damage_type": "Pothole",
            "site_condition": "same", "human_severity": "high", "csrf": csrf,
        }, files={"site_photo": small_jpeg_file()}, follow_redirects=False)
        assert "error" in resp.headers.get("location", "")
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM ai_corrections WHERE report_id=?", (rid,))
        assert dict(c.fetchone())["n"] == 0
        conn.close()


class TestExistingHistoricalRowsUnaffected:

    def test_existing_row_without_human_severity_stays_null(self):
        rid = make_report_with_ai_severity()
        database.save_training_sample(
            rid, "Ward 1", "Pothole", "Pothole", "high", "same", "Engineer",
            database.now(), "",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT human_severity FROM training_labels WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["human_severity"] is None


class TestTrainingEligibility:

    def test_null_human_severity_not_counted_eligible(self):
        rid = make_report_with_ai_severity()
        database.save_training_sample(
            rid, "Ward 1", "Pothole", "Pothole", "high", "same", "Engineer",
            database.now(), "",
        )
        stats = database.get_training_stats()
        assert stats["severity_eligible"] == 0

    def test_human_severity_samples_counted_eligible(self):
        rid = make_report_with_ai_severity()
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="high", verifier_role="was",
        )
        stats = database.get_training_stats()
        assert stats["severity_eligible"] == 1


class TestMultipleVerificationsPreserveHistory:

    def test_reinspection_and_override_create_separate_rows(self):
        fe = make_staff("field_engineer", "fe_original")
        rid = make_report_with_ai_severity(ai_severity="high", assigned_to=fe["name"])

        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", fe["name"],
            human_severity="medium", verifier_role="field_engineer",
        )

        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("UPDATE reports SET escalation_level=1 WHERE report_id=?"), (rid,))
        conn.commit(); conn.close()

        supervisor = make_staff("field_engineer", fe["username"] + "_sup", name="Supervisor Engineer")
        conn = database.get_conn(); c = conn.cursor()
        c.execute(database._q("SELECT id FROM staff WHERE name=?"), (supervisor["name"],))
        sup_id = dict(c.fetchone())["id"]
        c.execute(database._q("UPDATE staff SET supervised_by=? WHERE name=?"), (sup_id, fe["name"]))
        conn.commit(); conn.close()

        allowed, is_override, err = authz.can_modify_inspection(
            supervisor, database.get_report_by_id(rid), "SLA breached, taking over"
        )
        assert allowed is True and is_override is True

        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,CC", supervisor["name"],
            is_override=True, override_reason="SLA breached, taking over",
            human_severity="high", verifier_role="field_engineer",
        )

        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT corrected_severity, corrected_by FROM ai_corrections WHERE report_id=? ORDER BY id", (rid,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        assert len(rows) == 2
        assert rows[0]["corrected_severity"] == "medium"
        assert rows[1]["corrected_severity"] == "high"
        assert rows[0]["corrected_by"] != rows[1]["corrected_by"]


class TestNoRegression:

    def test_export_training_data_csv_still_works(self):
        rid = make_report_with_ai_severity()
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="high", verifier_role="was",
        )
        csv_text = database.export_training_data_csv()
        assert "human_severity" in csv_text.split("\r\n")[0]
        assert "high" in csv_text

    def test_csv_exposes_both_severity_columns_distinctly_when_they_differ(self):
        """Contract requirement (G): the CSV must expose AI severity and
        human severity as two separate, unambiguous columns -- never
        collapsed into one field. Proven with a row where the two values
        genuinely differ, and by parsing the CSV into columns (not
        substring-searching the raw text) so this actually verifies
        column position, not just that both strings appear somewhere."""
        import csv as csv_module
        import io as io_module

        rid = make_report_with_ai_severity(ai_severity="critical")
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="low", verifier_role="was",
        )
        csv_text = database.export_training_data_csv()
        reader = csv_module.DictReader(io_module.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 1
        row = rows[0]
        # Both columns present, both populated, and -- the actual point
        # of this test -- holding DIFFERENT values, proving they are not
        # the same field read twice or one field silently overwriting
        # the other.
        assert row["severity"] == "critical"
        assert row["human_severity"] == "low"
        assert row["severity"] != row["human_severity"]

    def test_inference_provenance_unaffected(self):
        rid = make_report_with_ai_severity(ai_severity="high")
        runs_before = database.get_inference_runs_for_report(rid)
        database.save_inspection_verification(
            rid, "Pothole", "same", "data:image/jpeg;base64,BB", "Engineer",
            human_severity="medium", verifier_role="was",
        )
        runs_after = database.get_inference_runs_for_report(rid)
        assert runs_before == runs_after