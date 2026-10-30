#!/usr/bin/env python3
"""Validate and import a medicine or Kendra catalogue.

The import is deliberately conservative:

* Validation is the same code the web application uses at startup.
* The command is a dry run unless ``--apply`` is supplied.
* Every applied import needs a source name and retrieval date. A source URL is
  also required when a file contains non-sample rows.
* Existing files are backed up beside the destination and replaced atomically.
* An append-only JSONL audit entry records what changed. It never contains
  patient, prescription or OCR data.

Example (validate first, then apply):

    python scripts/import_catalogue.py --medicines /path/to/medicines.csv \
        --source-name "PMBJP product list" \
        --source-url "https://janaushadhi.gov.in/download-link" \
        --retrieved 2026-10-30

    python scripts/import_catalogue.py ... --apply
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from core.catalogue import (  # noqa: E402
    CatalogueError,
    KENDRA_FIELDS,
    MEDICINE_FIELDS,
    catalogue_mode,
    load_kendras,
    load_medicines,
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_rows(path: Path, fields: list[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [field for field in fields if field not in headers]
        if missing:
            raise CatalogueError(
                f"{path.name} is missing columns: {', '.join(missing)}"
            )
        return [{field: (row.get(field) or "") for field in fields} for row in reader]


def _is_sample(row: dict[str, Any]) -> bool:
    return str(row.get("is_sample") or "1").strip() != "0"


def _provenance_errors(rows: list[dict[str, Any]], kind: str) -> list[str]:
    """Require source metadata on every non-sample row."""
    errors: list[str] = []
    for line, row in enumerate(rows, start=2):
        if _is_sample(row):
            continue
        for field in ("source_name", "source_url", "last_verified"):
            if not str(row.get(field) or "").strip():
                errors.append(f"line {line}: non-sample row requires {field}")
        if kind == "medicine" and str(row.get("jan_price") or "").strip():
            if not str(row.get("jan_price_source") or "").strip():
                errors.append(f"line {line}: a price requires jan_price_source")
    return errors


def _validate_date(value: str) -> str:
    if not DATE_RE.match(value or ""):
        raise CatalogueError("retrieved must be a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogueError("retrieved is not a valid calendar date") from exc
    return value


def _validate_source_url(value: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CatalogueError("source-url must be an absolute http(s) URL")
    return value


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            writer = csv.DictWriter(
                handle, fieldnames=fields, extrasaction="raise", lineterminator="\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fields})
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore(backups: dict[Path, Path | None], written: list[Path]) -> None:
    for destination in written:
        backup = backups.get(destination)
        try:
            if backup is not None and backup.exists():
                os.replace(backup, destination)
            elif destination.exists():
                destination.unlink()
        except OSError:
            # The original error is more useful; leave the backup in place.
            pass


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.medicines and not args.kendras:
        raise CatalogueError("provide --medicines and/or --kendras")

    data_dir = Path(args.data_dir) if args.data_dir else ROOT / "data"
    medicines_path = args.medicines or data_dir / "medicines.csv"
    kendras_path = args.kendras or data_dir / "kendras.csv"
    medicines_source = Path(medicines_path)
    kendras_source = Path(kendras_path)

    medicine_rows: list[dict[str, str]] | None = None
    kendra_rows: list[dict[str, str]] | None = None
    medicines: list[dict[str, Any]] | None = None
    kendras: list[dict[str, Any]] | None = None

    if args.medicines:
        if not medicines_source.exists():
            raise CatalogueError(f"file not found: {medicines_source}")
        medicine_rows = _read_rows(medicines_source, MEDICINE_FIELDS)
        medicines = load_medicines(medicines_source)
        errors = _provenance_errors(medicine_rows, "medicine")
        if errors:
            raise CatalogueError("medicine provenance failed validation", errors)

    if args.kendras:
        if not kendras_source.exists():
            raise CatalogueError(f"file not found: {kendras_source}")
        kendra_rows = _read_rows(kendras_source, KENDRA_FIELDS)
        kendras = load_kendras(kendras_source)
        errors = _provenance_errors(kendra_rows, "kendra")
        if errors:
            raise CatalogueError("Kendra provenance failed validation", errors)

    # When only one side is supplied, describe the other side as unchanged.
    counts = {
        "medicines": len(medicines) if medicines is not None else None,
        "kendras": len(kendras) if kendras is not None else None,
    }
    summary = {
        "ok": True,
        "applied": False,
        "dry_run": not args.apply,
        "medicines": (
            {"path": str(medicines_source), "count": counts["medicines"]}
            if medicines is not None
            else None
        ),
        "kendras": (
            {"path": str(kendras_source), "count": counts["kendras"]}
            if kendras is not None
            else None
        ),
    }

    if not args.apply:
        if medicines is not None and kendras is not None:
            summary["mode"] = catalogue_mode(medicines, kendras)
        return summary

    if not args.source_name.strip():
        raise CatalogueError("--source-name is required with --apply")
    retrieved = _validate_date(args.retrieved)
    combined_rows = [*(medicine_rows or []), *(kendra_rows or [])]
    has_non_sample = any(not _is_sample(row) for row in combined_rows)
    if has_non_sample:
        if not args.source_url.strip():
            raise CatalogueError("--source-url is required for non-sample imports")
        _validate_source_url(args.source_url)

    destinations: dict[Path, tuple[list[str], list[dict[str, str]]]] = {}
    if medicine_rows is not None:
        destinations[data_dir / "medicines.csv"] = (MEDICINE_FIELDS, medicine_rows)
    if kendra_rows is not None:
        destinations[data_dir / "kendras.csv"] = (KENDRA_FIELDS, kendra_rows)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: dict[Path, Path | None] = {}
    written: list[Path] = []
    try:
        for destination in destinations:
            if destination.exists():
                backup = destination.with_name(f"{destination.name}.backup-{stamp}")
                backup.write_bytes(destination.read_bytes())
                backups[destination] = backup
            else:
                backups[destination] = None

        for destination, (fields, rows) in destinations.items():
            _write_csv(destination, fields, rows)
            written.append(destination)

        result: dict[str, Any] = {
            **summary,
            "applied": True,
            "dry_run": False,
            "retrieved": retrieved,
            "source_name": args.source_name.strip(),
            "source_url": args.source_url.strip(),
            "backups": {str(k): (str(v) if v else None) for k, v in backups.items()},
            "hashes": {
                "medicines": _sha256(data_dir / "medicines.csv"),
                "kendras": _sha256(data_dir / "kendras.csv"),
            },
        }

        if not args.no_rebuild:
            database = Path(
                args.database
                or os.environ.get("DATABASE_PATH")
                or ROOT / "instance" / "janaushadhi.db"
            )
            db.init_database(
                database,
                data_dir / "medicines.csv",
                data_dir / "kendras.csv",
                force_reload=True,
            )
            result["database"] = str(database)
            connection = db.connect(database)
            try:
                result["mode"] = db.catalogue_summary(connection)["mode"]
            finally:
                connection.close()

        log_path = Path(args.log or data_dir / "import_log.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "user": getpass.getuser(),
            **result,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        result["log"] = str(log_path)
        return result
    except Exception:
        _restore(backups, written)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and import a catalogue with provenance metadata."
    )
    parser.add_argument("--medicines", type=Path, help="replacement medicines CSV")
    parser.add_argument("--kendras", type=Path, help="replacement Kendra CSV")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="destination data directory (default: project data/)",
    )
    parser.add_argument("--source-name", default="", help="human-readable dataset source")
    parser.add_argument("--source-url", default="", help="source page or document URL")
    parser.add_argument(
        "--retrieved", default="", help="date the source was retrieved (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite path to rebuild (default: DATABASE_PATH or instance/janaushadhi.db)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="audit log path (default: data/import_log.jsonl)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the files and rebuild the database (default: dry run)",
    )
    parser.add_argument(
        "--no-rebuild", action="store_true", help="skip the SQLite rebuild"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (CatalogueError, OSError) as exc:
        errors = getattr(exc, "errors", None) or [str(exc)]
        print(
            json.dumps(
                {"ok": False, "applied": False, "errors": errors},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
