"""Import CLI validation, provenance and atomic-write tests."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

import pytest

import scripts.import_catalogue as importer
from core.catalogue import KENDRA_FIELDS, MEDICINE_FIELDS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@pytest.fixture()
def staging(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copyfile(DATA / "medicines.csv", data_dir / "medicines.csv")
    shutil.copyfile(DATA / "kendras.csv", data_dir / "kendras.csv")
    return data_dir


def args_for(data_dir: Path, **overrides):
    values = {
        "medicines": data_dir / "medicines.csv",
        "kendras": data_dir / "kendras.csv",
        "data_dir": data_dir,
        "source_name": "Test import",
        "source_url": "https://janaushadhi.gov.in/download-link",
        "retrieved": "2026-10-30",
        "database": data_dir.parent / "import.db",
        "log": data_dir / "import_log.jsonl",
        "apply": False,
        "no_rebuild": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dry_run_validates_and_writes_nothing(staging):
    result = importer.run(args_for(staging))
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert result["medicines"]["count"] == 54
    assert result["kendras"]["count"] == 15
    assert result["mode"] == "sample"
    assert not (staging / "import_log.jsonl").exists()
    assert list(staging.glob("*.backup-*")) == []


def test_apply_writes_canonical_files_backs_up_and_rebuilds(staging):
    result = importer.run(args_for(staging, apply=True))
    assert result["applied"] is True
    assert result["dry_run"] is False
    assert result["source_name"] == "Test import"
    assert result["retrieved"] == "2026-10-30"
    assert result["mode"] == "sample"
    assert Path(result["database"]).exists()
    assert list(staging.glob("*.backup-*")), "existing files must be backed up"

    log_lines = (staging / "import_log.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(log_lines[-1])
    assert entry["applied"] is True
    assert entry["user"]
    assert entry["hashes"]["medicines"]
    assert "message" not in entry


def test_apply_requires_global_provenance(staging):
    with pytest.raises(Exception) as error:
        importer.run(args_for(staging, apply=True, source_name=""))
    assert "source-name" in str(error.value)

    with pytest.raises(Exception) as error:
        importer.run(args_for(staging, apply=True, retrieved="30-10-2026"))
    assert "YYYY-MM-DD" in str(error.value)


def test_non_sample_rows_require_row_level_provenance(staging):
    path = staging / "medicines.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["is_sample"] = "0"
    rows[0]["source_name"] = ""
    rows[0]["source_url"] = ""
    rows[0]["last_verified"] = ""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MEDICINE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(Exception) as error:
        importer.run(args_for(staging, apply=True))
    assert any("non-sample row requires" in item for item in error.value.errors)


def test_import_rejects_missing_and_unvalidated_files(tmp_path):
    with pytest.raises(Exception):
        importer.run(
            args_for(
                tmp_path,
                medicines=tmp_path / "absent.csv",
                kendras=tmp_path / "absent-kendra.csv",
            )
        )


def test_canonical_csv_keeps_only_known_columns(staging):
    # Add a stray column and confirm the importer drops it rather than passing
    # unvalidated fields into the database.
    path = staging / "kendras.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["unexpected"] = "must-not-survive"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*KENDRA_FIELDS, "unexpected"])
        writer.writeheader()
        writer.writerows(rows)

    importer.run(args_for(staging, apply=True, no_rebuild=True))
    with (staging / "kendras.csv").open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == KENDRA_FIELDS
        assert "unexpected" not in next(reader)
