"""SQLite persistence with explicit provenance metadata.

The bundled catalogue is rebuilt from validated CSV files. Runtime tables hold
only feedback and import audit entries; no patient, prescription or OCR data is
persisted.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .catalogue import (
    CatalogueError,
    catalogue_mode,
    load_kendras,
    load_medicines,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS medicines (
    id TEXT PRIMARY KEY,
    generic_name TEXT NOT NULL,
    synonyms TEXT NOT NULL DEFAULT '',
    common_brands TEXT NOT NULL DEFAULT '',
    composition TEXT NOT NULL,
    strength TEXT NOT NULL,
    dosage_form TEXT NOT NULL,
    route TEXT NOT NULL,
    release_type TEXT NOT NULL,
    pack_size REAL NOT NULL,
    pack_unit TEXT NOT NULL,
    category TEXT NOT NULL,
    product_code TEXT NOT NULL DEFAULT '',
    jan_price_paise INTEGER,
    jan_price_source TEXT NOT NULL DEFAULT '',
    jan_price_verified INTEGER NOT NULL DEFAULT 0,
    is_sample INTEGER NOT NULL DEFAULT 1,
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    last_verified TEXT NOT NULL DEFAULT '',
    search_blob TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kendras (
    id TEXT PRIMARY KEY,
    kendra_code TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    locality TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL,
    district TEXT NOT NULL,
    state TEXT NOT NULL,
    pincode TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    coordinate_precision TEXT NOT NULL DEFAULT 'unknown',
    is_sample INTEGER NOT NULL DEFAULT 1,
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    last_verified TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_kendras_state ON kendras(state);
CREATE INDEX IF NOT EXISTS idx_kendras_district ON kendras(district);
CREATE INDEX IF NOT EXISTS idx_kendras_pincode ON kendras(pincode);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MEDICINE_INSERT_FIELDS = [
    "id",
    "generic_name",
    "synonyms",
    "common_brands",
    "composition",
    "strength",
    "dosage_form",
    "route",
    "release_type",
    "pack_size",
    "pack_unit",
    "category",
    "product_code",
    "jan_price_paise",
    "jan_price_source",
    "jan_price_verified",
    "is_sample",
    "source_name",
    "source_url",
    "last_verified",
    "search_blob",
]

KENDRA_INSERT_FIELDS = [
    "id",
    "kendra_code",
    "name",
    "owner_name",
    "address",
    "locality",
    "city",
    "district",
    "state",
    "pincode",
    "phone",
    "latitude",
    "longitude",
    "coordinate_precision",
    "is_sample",
    "source_name",
    "source_url",
    "last_verified",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return connection


def _file_hash(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _populate(
    connection: sqlite3.Connection,
    medicines: list[dict[str, Any]],
    kendras: list[dict[str, Any]],
    version: str,
) -> None:
    connection.execute("DELETE FROM medicines")
    connection.execute("DELETE FROM kendras")
    connection.executemany(
        f"INSERT INTO medicines ({', '.join(MEDICINE_INSERT_FIELDS)}) "
        f"VALUES ({', '.join(':' + f for f in MEDICINE_INSERT_FIELDS)})",
        medicines,
    )
    connection.executemany(
        f"INSERT INTO kendras ({', '.join(KENDRA_INSERT_FIELDS)}) "
        f"VALUES ({', '.join(':' + f for f in KENDRA_INSERT_FIELDS)})",
        kendras,
    )
    _set_meta(connection, "catalogue_version", version)
    _set_meta(connection, "catalogue_loaded_at", utc_now())
    _set_meta(connection, "catalogue_mode", catalogue_mode(medicines, kendras))
    _set_meta(connection, "medicine_count", str(len(medicines)))
    _set_meta(connection, "kendra_count", str(len(kendras)))
    connection.execute(
        "INSERT INTO audit_log(created_at, action, detail) VALUES(?, ?, ?)",
        (
            utc_now(),
            "catalogue_loaded",
            f"version={version} medicines={len(medicines)} kendras={len(kendras)}",
        ),
    )


def init_database(
    database_path: str | Path,
    medicines_path: str | Path,
    kendras_path: str | Path,
    force_reload: bool = False,
) -> None:
    database_path = Path(database_path)
    medicines_path = Path(medicines_path)
    kendras_path = Path(kendras_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = connect(database_path)
    try:
        connection.executescript(SCHEMA)
        version = _file_hash([medicines_path, kendras_path])
        medicines = load_medicines(medicines_path)
        kendras = load_kendras(kendras_path)

        connection.execute("BEGIN IMMEDIATE")
        try:
            current_version = _get_meta(connection, "catalogue_version")
            medicine_count = connection.execute("SELECT COUNT(*) AS n FROM medicines").fetchone()["n"]
            kendra_count = connection.execute("SELECT COUNT(*) AS n FROM kendras").fetchone()["n"]
            if (
                not force_reload
                and current_version == version
                and medicine_count == len(medicines)
                and kendra_count == len(kendras)
            ):
                connection.execute("COMMIT")
                return
            _populate(connection, medicines, kendras, version)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def load_connection(path: str | Path) -> sqlite3.Connection:
    return connect(path)


def get_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}


def all_medicines(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM medicines ORDER BY generic_name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


def medicine_by_id(connection: sqlite3.Connection, medicine_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM medicines WHERE id=?", (medicine_id,)).fetchone()
    return dict(row) if row else None


def catalogue_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    meta = get_meta(connection)
    return {
        "medicines": int(meta.get("medicine_count") or 0),
        "kendras": int(meta.get("kendra_count") or 0),
        "mode": meta.get("catalogue_mode") or "empty",
        "version": meta.get("catalogue_version") or "",
        "loaded_at": meta.get("catalogue_loaded_at") or "",
    }


def medicine_categories(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT category FROM medicines WHERE category <> '' ORDER BY category COLLATE NOCASE"
    ).fetchall()
    return [row["category"] for row in rows]


def medicine_forms(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT dosage_form FROM medicines WHERE dosage_form <> '' ORDER BY dosage_form COLLATE NOCASE"
    ).fetchall()
    return [row["dosage_form"] for row in rows]


def kendra_facets(connection: sqlite3.Connection) -> dict[str, Any]:
    states = [
        row["state"]
        for row in connection.execute(
            "SELECT DISTINCT state FROM kendras ORDER BY state COLLATE NOCASE"
        )
    ]
    districts = [
        {"state": row["state"], "district": row["district"]}
        for row in connection.execute(
            "SELECT DISTINCT state, district FROM kendras ORDER BY state, district COLLATE NOCASE"
        )
    ]
    return {"states": states, "districts": districts}


def search_kendras(
    connection: sqlite3.Connection,
    state: str = "",
    district: str = "",
    city: str = "",
    pincode: str = "",
    kendra_code: str = "",
    query: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []

    for column, value in (
        ("state", state),
        ("district", district),
        ("city", city),
        ("pincode", pincode),
        ("kendra_code", kendra_code),
    ):
        value = (value or "").strip()
        if value:
            clauses.append(f"{column} = ? COLLATE NOCASE")
            params.append(value)
    if query:
        like = f"%{query.strip()}%"
        clauses.append(
            "(name LIKE ? OR address LIKE ? OR locality LIKE ? OR city LIKE ? "
            "OR district LIKE ? OR state LIKE ? OR pincode LIKE ? OR kendra_code LIKE ?)"
        )
        params.extend([like] * 8)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = connection.execute(f"SELECT COUNT(*) AS n FROM kendras{where}", params).fetchone()["n"]
    rows = connection.execute(
        f"SELECT * FROM kendras{where} ORDER BY state, district, city, name LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 100)), max(0, offset)],
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.asin(min(1.0, math.sqrt(a)))


def nearby_kendras(
    connection: sqlite3.Connection,
    latitude: float,
    longitude: float,
    radius_km: float = 25.0,
    limit: int = 30,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM kendras WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        distance = _haversine_km(latitude, longitude, record["latitude"], record["longitude"])
        if distance <= radius_km:
            record["distance_km"] = round(distance, 1)
            results.append(record)
    results.sort(key=lambda item: item["distance_km"])
    return results[: max(1, min(limit, 100))]


def add_feedback(
    connection: sqlite3.Connection,
    category: str,
    message: str,
    contact: str = "",
    page: str = "",
) -> int:
    cursor = connection.execute(
        "INSERT INTO feedback(created_at, category, message, contact, page) VALUES(?, ?, ?, ?, ?)",
        (utc_now(), category, message, contact, page),
    )
    return int(cursor.lastrowid)


def purge_feedback(connection: sqlite3.Connection, retention_days: int = 365) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
    cursor = connection.execute("DELETE FROM feedback WHERE created_at < ?", (cutoff,))
    return cursor.rowcount


def list_feedback(connection: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, created_at, category, message, contact, page, status FROM feedback "
        "ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]


def log_audit(connection: sqlite3.Connection, action: str, detail: str = "") -> None:
    connection.execute(
        "INSERT INTO audit_log(created_at, action, detail) VALUES(?, ?, ?)",
        (utc_now(), action, detail[:1000]),
    )


__all__ = [
    "CatalogueError",
    "add_feedback",
    "all_medicines",
    "catalogue_summary",
    "connect",
    "get_meta",
    "init_database",
    "kendra_facets",
    "list_feedback",
    "load_connection",
    "log_audit",
    "medicine_by_id",
    "medicine_categories",
    "medicine_forms",
    "nearby_kendras",
    "purge_feedback",
    "search_kendras",
]
