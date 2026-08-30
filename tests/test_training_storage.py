"""
tests/test_training_storage.py -- durable training_labels storage
====================================================================
Proves save_training_sample() no longer writes to the local filesystem
(training_data/labels.csv + individual photo files, both non-durable on
Render's ephemeral disk) and instead writes to the training_labels DB
table -- the same durability guarantee already used throughout this
codebase for report/photo data.

Run with: pytest tests/test_training_storage.py -v
"""

import os
import sys

import pytest

os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_training.db")
    database.init_db()
    yield
    database.DB_PATH = original


REAL_PHOTO = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/notarealimage"


def make_report(ward="Ward 1 - Kondapeta / Wilsonpeta"):
    return database.add_report(
        city="GVMC", ward=ward, damage_type="Pothole", description="test",
        photo_path="", citizen_name="Citizen", citizen_phone="9999999999",
        citizen_email="", latitude=17.0, longitude=83.0, photo_data=REAL_PHOTO,
    )


class TestTrainingSampleCreation:

    def test_save_training_sample_creates_a_row(self):
        rid = make_report()
        database.save_training_sample(
            rid, "Ward 1 - Kondapeta / Wilsonpeta", "Pothole", "Pothole",
            "medium", "dry", "Engineer1", database.now(), REAL_PHOTO,
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM training_labels")
        assert dict(c.fetchone())["n"] == 1
        conn.close()

    def test_metadata_fields_persisted_correctly(self):
        rid = make_report()
        database.save_training_sample(
            rid, "Ward 1 - Kondapeta / Wilsonpeta", "Pothole", "Crack",
            "high", "wet", "Engineer2", "2026-01-01 10:00:00", REAL_PHOTO,
            is_override=True,
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT * FROM training_labels WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["ward"] == "Ward 1 - Kondapeta / Wilsonpeta"
        assert row["citizen_damage_type"] == "Pothole"
        assert row["verified_damage_type"] == "Crack"
        assert row["severity"] == "high"
        assert row["site_condition"] == "wet"
        assert row["verified_by"] == "Engineer2"
        assert row["verified_at"] == "2026-01-01 10:00:00"
        assert row["is_override"] == 1

    def test_photo_source_recorded_when_real_photo_supplied(self):
        rid = make_report()
        database.save_training_sample(
            rid, "Ward 1", "Pothole", "Pothole", "medium", "dry",
            "Engineer1", database.now(), REAL_PHOTO,
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT photo_source_column FROM training_labels WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["photo_source_column"] == "photo_data"

    def test_empty_photo_recorded_as_no_source(self):
        rid = make_report()
        database.save_training_sample(
            rid, "Ward 1", "Pothole", "Pothole", "", "",
            "System", database.now(), "",
        )
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT photo_source_column FROM training_labels WHERE report_id=?", (rid,))
        row = dict(c.fetchone())
        conn.close()
        assert row["photo_source_column"] == ""

    def test_multiple_samples_for_different_reports(self):
        rid1 = make_report("Ward 1 - Kondapeta / Wilsonpeta")
        rid2 = make_report("Ward 4 - Pedda Uppada / Chepaluppada")
        database.save_training_sample(rid1, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        database.save_training_sample(rid2, "Ward 4", "Crack", "Crack", "low", "dry", "E2", database.now(), REAL_PHOTO)
        conn = database.get_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) as n FROM training_labels")
        assert dict(c.fetchone())["n"] == 2
        conn.close()


class TestTrainingStats:

    def test_empty_stats_before_any_samples(self):
        stats = database.get_training_stats()
        # UPDATED by the human-severity feature: get_training_stats() now
        # also reports severity_eligible (count of samples with a real,
        # independently-entered human_severity). See
        # tests/test_human_severity.py for dedicated coverage of that field.
        assert stats == {"total": 0, "by_type": {}, "corrections": 0, "severity_eligible": 0}

    def test_total_count_correct(self):
        rid = make_report()
        for i in range(3):
            database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        stats = database.get_training_stats()
        assert stats["total"] == 3

    def test_by_type_breakdown(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "low", "dry", "E1", database.now(), REAL_PHOTO)
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "low", "dry", "E1", database.now(), REAL_PHOTO)
        stats = database.get_training_stats()
        assert stats["by_type"]["Crack"] == 2
        assert stats["by_type"]["Pothole"] == 1

    def test_corrections_count_matches_citizen_vs_verified_mismatch(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "low", "dry", "E1", database.now(), REAL_PHOTO)
        stats = database.get_training_stats()
        assert stats["total"] == 2
        assert stats["corrections"] == 1


class TestExportCsv:

    def test_export_produces_valid_csv_with_expected_headers(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "high", "wet", "E1", "2026-01-01 10:00:00", REAL_PHOTO, is_override=True)
        csv_text = database.export_training_data_csv()
        lines = csv_text.strip().split("\r\n")
        header = lines[0].split(",")
        assert header == database.TRAINING_CSV_HEADERS

    def test_export_row_content_matches_saved_sample(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "high", "wet", "E1", "2026-01-01 10:00:00", REAL_PHOTO, is_override=True)
        csv_text = database.export_training_data_csv()
        assert rid in csv_text
        assert "Crack" in csv_text
        assert "yes" in csv_text

    def test_export_empty_when_no_samples(self):
        csv_text = database.export_training_data_csv()
        lines = csv_text.strip().split("\r\n")
        assert len(lines) == 1


class TestDurabilityAcrossRestart:

    def test_samples_survive_init_db_rerun(self):
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        database.init_db()
        stats = database.get_training_stats()
        assert stats["total"] == 1

    def test_no_local_filesystem_artifacts_created(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rid = make_report()
        database.save_training_sample(rid, "Ward 1", "Pothole", "Pothole", "low", "dry", "E1", database.now(), REAL_PHOTO)
        assert not os.path.exists("training_data")


class TestNoRegressionToReportPhotoStorage:

    def test_report_photo_data_unchanged_by_training_save(self):
        rid = make_report()
        before = database.get_report_by_id(rid)
        database.save_training_sample(rid, "Ward 1", "Pothole", "Crack", "high", "wet", "E1", database.now(), REAL_PHOTO)
        after = database.get_report_by_id(rid)
        assert after["photo_data"] == before["photo_data"] == REAL_PHOTO


class TestFailureBehavior:

    def test_save_training_sample_does_not_raise_on_bad_report_id(self):
        database.save_training_sample(
            "GVMC-9999-NOTREAL", "Ward 1", "Pothole", "Pothole",
            "low", "dry", "E1", database.now(), REAL_PHOTO,
        )
        stats = database.get_training_stats()
        assert stats["total"] == 1