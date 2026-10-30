"""SQLite catalogue and Kendra query tests."""

from __future__ import annotations

from pathlib import Path

from core import db

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def make_db(tmp_path):
    path = tmp_path / "catalogue.db"
    db.init_database(path, DATA / "medicines.csv", DATA / "kendras.csv", force_reload=True)
    return path, db.connect(path)


def test_database_loads_with_provenance_and_no_patient_tables(tmp_path):
    path, connection = make_db(tmp_path)
    try:
        summary = db.catalogue_summary(connection)
        assert summary["medicines"] == 54
        assert summary["kendras"] == 15
        assert summary["mode"] == "sample"
        assert summary["version"]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"medicines", "kendras", "feedback", "audit_log", "meta"} <= tables
        assert not tables & {"users", "patients", "prescriptions", "accounts", "orders"}
        assert db.get_meta(connection)["catalogue_mode"] == "sample"
    finally:
        connection.close()


def test_reload_is_idempotent(tmp_path):
    path, connection = make_db(tmp_path)
    try:
        before = connection.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
    finally:
        connection.close()
    db.init_database(path, DATA / "medicines.csv", DATA / "kendras.csv")
    connection = db.connect(path)
    try:
        assert db.catalogue_summary(connection)["medicines"] == 54
        assert connection.execute("SELECT COUNT(*) AS n FROM medicines").fetchone()["n"] == 54
        assert connection.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == before
    finally:
        connection.close()


def test_medicine_lookup_and_search_helpers(tmp_path):
    _, connection = make_db(tmp_path)
    try:
        row = db.medicine_by_id(connection, "paracetamol-650-tablet")
        assert row is not None
        assert row["generic_name"] == "Paracetamol"
        assert row["strength"] == "650 mg"
        assert "paracetamol" in row["search_blob"]
        assert db.medicine_by_id(connection, "not-a-real-id") is None
        assert "tablet" in db.medicine_forms(connection)
        assert "Pain and fever" in db.medicine_categories(connection)
        assert len(db.all_medicines(connection)) == 54
    finally:
        connection.close()


def test_kendra_filters_search_and_facets(tmp_path):
    _, connection = make_db(tmp_path)
    try:
        facets = db.kendra_facets(connection)
        assert "Delhi" in facets["states"]
        assert {"state": "Delhi", "district": "Central Delhi"} in facets["districts"]

        result = db.search_kendras(connection, state="Delhi")
        assert result["total"] == 1
        assert result["items"][0]["city"] == "New Delhi"

        result = db.search_kendras(connection, pincode="110001")
        assert result["total"] == 1

        result = db.search_kendras(connection, query="Sample Kendra record")
        assert result["total"] == 15

        result = db.search_kendras(connection, query="no-such-locality")
        assert result["total"] == 0

        paged = db.search_kendras(connection, limit=5, offset=0)
        paged_two = db.search_kendras(connection, limit=5, offset=5)
        assert len(paged["items"]) == 5
        assert paged["items"] != paged_two["items"]
    finally:
        connection.close()


def test_nearby_kendras_uses_approximate_distance_and_limit(tmp_path):
    _, connection = make_db(tmp_path)
    try:
        rows = db.nearby_kendras(connection, 28.6139, 77.2090, radius_km=5)
        assert rows
        assert rows[0]["city"] == "New Delhi"
        assert rows[0]["distance_km"] == 0.0
        assert all("distance_km" in row for row in rows)

        far = db.nearby_kendras(connection, 0.0, 0.0, radius_km=100)
        assert far == []
    finally:
        connection.close()


def test_feedback_and_audit_are_operator_only_and_do_not_store_ip(tmp_path):
    _, connection = make_db(tmp_path)
    try:
        reference = db.add_feedback(
            connection, "website_problem", "A sufficiently long report message.", "user@example.invalid", "/help"
        )
        assert reference > 0
        rows = db.list_feedback(connection, limit=10)
        assert rows[0]["message"].startswith("A sufficiently")
        assert "ip" not in rows[0]
        db.log_audit(connection, "test_action", "detail")
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action='test_action'"
        ).fetchone()["n"] == 1
    finally:
        connection.close()
