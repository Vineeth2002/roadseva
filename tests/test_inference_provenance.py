"""
tests/test_inference_provenance.py -- ai_inference_runs
============================================================
Proves every real return path in severity.analyse_severity() produces
exactly one durable ai_inference_runs row: success, missing API key,
missing photo, JSON parse failure, and provider/generic exception.
Confirms attempt_number increments durably across calls, retries create
a separate row rather than overwriting, and no secret/PII leaks into the
provenance record.

Run with: pytest tests/test_inference_provenance.py -v
"""

import os
import sys
import json
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_inference.db")
    database.init_db()
    yield
    database.DB_PATH = original


def make_report(ward="Ward 1 - Kondapeta / Wilsonpeta"):
    return database.add_report(
        city="GVMC", ward=ward, damage_type="Pothole", description="test",
        photo_path="", citizen_name="Citizen", citizen_phone="9999999999",
        citizen_email="", latitude=17.0, longitude=83.0, photo_data="",
    )


def _mock_groq_response(content_text):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content_text))]
    return resp


class TestSuccessPath:

    def test_successful_inference_creates_a_row(self, tmp_path):
        rid = make_report()
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0fakejpegdata")

        good_json = json.dumps({
            "severity": "high", "damage_confirmed": True,
            "damage_description": "Large pothole", "estimated_size": "2ft",
            "accident_risk": "high", "urgency": "Repair within 48 hours",
            "recommended_action": "Patch immediately",
        })

        with patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"}):
            with patch("groq.Groq") as MockGroq:
                MockGroq.return_value.chat.completions.create.return_value = _mock_groq_response(good_json)
                from severity import analyse_severity
                result = analyse_severity(str(photo), "Pothole", report_id=rid, source_photo_reference="local_upload")

        assert result["severity"] == "high"
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1
        assert runs[0]["validation_status"] == "ok"
        assert runs[0]["raw_severity"] == "high"
        assert runs[0]["raw_damage_confirmed"] == 1
        assert runs[0]["provider"] == "groq"
        assert runs[0]["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        assert runs[0]["source_photo_reference"] == "local_upload"
        assert runs[0]["attempt_number"] == 1


class TestFailurePaths:

    def test_missing_api_key_creates_a_row(self, tmp_path):
        rid = make_report()
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"data")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            result = analyse_severity(str(photo), "Pothole", report_id=rid, source_photo_reference="local_upload")
        assert result["severity"] == "unknown"
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1
        assert runs[0]["validation_status"] == "missing_api_key"

    def test_missing_photo_creates_a_row(self):
        rid = make_report()
        with patch.dict(os.environ, {"GROQ_API_KEY": "fake-key"}):
            from severity import analyse_severity
            result = analyse_severity("/tmp/definitely_does_not_exist_xyz.jpg", "Pothole",
                                       report_id=rid, source_photo_reference="local_upload")
        assert result["severity"] == "unknown"
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1
        assert runs[0]["validation_status"] == "missing_photo"

    def test_parse_failure_creates_a_row(self, tmp_path):
        rid = make_report()
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"data")
        with patch.dict(os.environ, {"GROQ_API_KEY": "fake-key"}):
            with patch("groq.Groq") as MockGroq:
                MockGroq.return_value.chat.completions.create.return_value = _mock_groq_response("not valid json at all {{{")
                from severity import analyse_severity
                result = analyse_severity(str(photo), "Pothole", report_id=rid, source_photo_reference="local_upload")
        assert result["severity"] == "unknown"
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1
        assert runs[0]["validation_status"] == "parse_error"
        assert runs[0]["error_detail"]

    def test_provider_exception_creates_a_row(self, tmp_path):
        rid = make_report()
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"data")
        with patch.dict(os.environ, {"GROQ_API_KEY": "fake-key"}):
            with patch("groq.Groq") as MockGroq:
                MockGroq.return_value.chat.completions.create.side_effect = RuntimeError("rate limited")
                from severity import analyse_severity
                result = analyse_severity(str(photo), "Pothole", report_id=rid, source_photo_reference="local_upload")
        assert result["severity"] == "unknown"
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1
        assert runs[0]["validation_status"] == "provider_error"
        assert "rate limited" in runs[0]["error_detail"]


class TestAttemptNumbering:

    def test_attempt_number_starts_at_one(self):
        rid = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
        runs = database.get_inference_runs_for_report(rid)
        assert runs[0]["attempt_number"] == 1

    def test_attempt_number_increments_on_second_call(self):
        rid = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="reports.photo_data")
        runs = database.get_inference_runs_for_report(rid)
        assert [r["attempt_number"] for r in runs] == [1, 2]

    def test_retry_creates_separate_row_not_overwrite(self):
        rid = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="reports.photo_data")
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 2
        assert runs[0]["source_photo_reference"] == "local_upload"
        assert runs[1]["source_photo_reference"] == "reports.photo_data"

    def test_different_reports_have_independent_attempt_counters(self):
        rid1 = make_report()
        rid2 = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid1, source_photo_reference="local_upload")
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid2, source_photo_reference="local_upload")
        assert database.get_inference_runs_for_report(rid1)[0]["attempt_number"] == 1
        assert database.get_inference_runs_for_report(rid2)[0]["attempt_number"] == 1


class TestBackwardCompatibility:

    def test_report_result_shape_unchanged(self, tmp_path):
        rid = make_report()
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"data")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            result = analyse_severity(str(photo), "Pothole", report_id=rid, source_photo_reference="local_upload")
        expected_keys = {"severity", "severity_details", "estimated_cost", "urgency",
                          "accident_risk", "recommended_action", "estimated_size", "damage_confirmed"}
        assert set(result.keys()) == expected_keys

    def test_call_without_report_id_still_works_no_logging(self, tmp_path):
        photo = tmp_path / "test.jpg"
        photo.write_bytes(b"data")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            result = analyse_severity(str(photo), "Pothole")
        assert result["severity"] == "unknown"
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM ai_inference_runs")
        assert dict(c.fetchone())["n"] == 0
        conn.close()


class TestNoSecretLeakage:

    def test_api_key_never_appears_in_provenance_row(self):
        rid = make_report()
        fake_key = "gsk_supersecretapikeyvalue12345"
        with patch.dict(os.environ, {"GROQ_API_KEY": fake_key}):
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
        runs = database.get_inference_runs_for_report(rid)
        for row in runs:
            for v in row.values():
                assert fake_key not in str(v)

    def test_citizen_pii_not_present_in_provenance_row(self):
        rid = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
        runs = database.get_inference_runs_for_report(rid)
        for row in runs:
            for v in row.values():
                assert "9999999999" not in str(v)


class TestDurabilityAndNoRegression:

    def test_persists_across_reinitialization(self):
        rid = make_report()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            from severity import analyse_severity
            analyse_severity("/tmp/nope.jpg", "Pothole", report_id=rid, source_photo_reference="local_upload")
        database.init_db()
        runs = database.get_inference_runs_for_report(rid)
        assert len(runs) == 1

    def test_training_labels_storage_unaffected(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), "")
        stats = database.get_training_stats()
        assert stats["total"] == 1