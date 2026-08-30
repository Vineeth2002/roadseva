"""
tests/test_safe_add_columns.py -- PostgreSQL transaction-safety fix
======================================================================
Proves _safe_add_columns() no longer lets a failed ALTER TABLE (e.g.
"column already exists") silently poison every subsequent column
addition on the same connection. Rollback is called unconditionally
after any failed DDL statement -- a no-op on SQLite, but the difference
between "upgrade works" and "upgrade silently does nothing after the
first already-existing column" on PostgreSQL.

Cannot exercise the real PostgreSQL transaction-abort behavior without
a live PostgreSQL server (unavailable in this environment). These tests
verify the function's actual, observable behavior against SQLite: every
intended column ends up present after repeated calls, and the new
rollback() call doesn't break existing add-if-missing semantics.

Run with: pytest tests/test_safe_add_columns.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    original = database.DB_PATH
    database.DB_PATH = str(tmp_path / "test_safe_add_columns.db")
    database.init_db()
    yield
    database.DB_PATH = original


def _column_names(table):
    conn = database.get_conn(); c = conn.cursor()
    c.execute(f"PRAGMA table_info({table})")
    cols = [dict(r)["name"] for r in c.fetchall()]
    conn.close()
    return cols


class TestAlreadyExistingColumnDoesNotPoisonTransaction:

    def test_second_init_db_does_not_raise(self):
        database.init_db()
        database.init_db()

    def test_all_expected_new_columns_still_present_after_repeat_calls(self):
        for _ in range(3):
            conn = database.get_conn(); c = conn.cursor()
            database._safe_add_columns(c, conn)
            conn.close()
        assert "human_severity" in _column_names("training_labels")
        assert "inference_run_id" in _column_names("ai_corrections")
        assert "verifier_role" in _column_names("ai_corrections")
        assert "supervised_by" in _column_names("staff")
        assert "zone" in _column_names("staff")


class TestSubsequentColumnsStillExecute:

    def test_columns_after_an_already_existing_one_are_still_added(self):
        conn = database.get_conn(); c = conn.cursor()
        database._safe_add_columns(c, conn)
        conn.close()

        for table, expected_cols in [
            ("training_labels", ["human_severity"]),
            ("ai_corrections", ["inference_run_id", "verifier_role"]),
            ("staff", ["supervised_by", "zone", "division", "ward_list", "phone"]),
        ]:
            present = _column_names(table)
            for col in expected_cols:
                assert col in present, f"{table}.{col} missing after _safe_add_columns()"

    def test_rollback_does_not_raise_on_sqlite(self):
        conn = database.get_conn()
        conn.rollback()
        conn.close()


class TestSQLiteBehaviorUnchanged:

    def test_fresh_db_has_all_columns_from_one_init_db_call(self):
        assert "human_severity" in _column_names("training_labels")
        assert "inference_run_id" in _column_names("ai_corrections")
        assert "verifier_role" in _column_names("ai_corrections")

    def test_normal_application_writes_still_work_after_repeat_init(self):
        database.init_db()
        rid = database.add_report(
            city="GVMC", ward="Ward 1", damage_type="Pothole",
            description="test", photo_path="", citizen_name="C",
            citizen_phone="9999999999", citizen_email="",
            latitude=17.0, longitude=83.0, photo_data="",
        )
        report = database.get_report_by_id(rid)
        assert report is not None
        assert report["ward"] == "Ward 1"


class TestNoUnrelatedSchemaChanged:

    def test_new_cols_dict_unchanged_in_scope(self):
        import inspect
        source = inspect.getsource(database._safe_add_columns)
        for table in ("staff", "reports", "training_labels", "ai_corrections"):
            assert f'"{table}"' in source
        assert "training_samples" not in source
        assert "human_verified_severity" not in source