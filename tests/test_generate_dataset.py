"""Tests for dataset publication: a generation run either replaces the whole dataset or none of it.

The benchmark numbers quoted in the README are only meaningful if a given
seed/size keeps producing byte-identical files, and reconciliation is only
trustworthy if the raw directory never holds payments from one dataset next to
ground truth from another.
"""
import csv
import json

import pytest

from app import generate_dataset
from app.generate_dataset import CSV_SCHEMAS, SUMMARY_FILE, generate

DATASET_FILES = list(CSV_SCHEMAS) + [SUMMARY_FILE]


def snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def data_row_count(path):
    with open(path, newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


def temp_leftovers(out_dir):
    return list(out_dir.parent.glob(".generation_tmp_*")) + list(out_dir.glob(".generation_tmp_*"))


def test_same_seed_and_size_produce_byte_identical_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    summary_a = generate(42, 40, a)
    summary_b = generate(42, 40, b)

    assert summary_a == summary_b
    assert snapshot(a) == snapshot(b)
    assert sorted(snapshot(a)) == sorted(DATASET_FILES)


def test_published_files_have_expected_headers_and_row_counts(tmp_path):
    out = tmp_path / "raw"
    summary = generate(7, 60, out)

    for name, fieldnames in CSV_SCHEMAS.items():
        with open(out / name, newline="") as f:
            assert next(csv.reader(f)) == fieldnames

    assert data_row_count(out / "payments.csv") == summary["payments"]
    assert data_row_count(out / "bank_settlements.csv") == summary["bank_settlements"]
    assert data_row_count(out / "invoices.csv") == summary["invoices"]
    # ground truth carries exactly one verdict per payment
    assert data_row_count(out / "ground_truth.csv") == summary["payments"]
    assert summary["resolvable"] + summary["unresolvable"] == summary["payments"]
    assert json.loads((out / SUMMARY_FILE).read_text()) == summary
    assert temp_leftovers(out) == []


def test_generates_into_a_nonexistent_nested_directory(tmp_path):
    out = tmp_path / "fresh" / "nested" / "raw"
    summary = generate(3, 30, out)

    assert sorted(snapshot(out)) == sorted(DATASET_FILES)
    assert data_row_count(out / "payments.csv") == summary["payments"]


def test_write_failure_leaves_the_previous_dataset_byte_identical(tmp_path, monkeypatch):
    out = tmp_path / "raw"
    generate(42, 40, out)
    before = snapshot(out)

    real_write_csv = generate_dataset._write_csv
    calls = []

    def failing_write_csv(path, rows, fieldnames):
        calls.append(path.name)
        if len(calls) == 3:
            raise OSError("simulated disk failure mid-write")
        return real_write_csv(path, rows, fieldnames)

    monkeypatch.setattr(generate_dataset, "_write_csv", failing_write_csv)

    with pytest.raises(OSError):
        generate(99, 55, out)  # different seed AND size: any leakage is visible

    assert len(calls) == 3  # generation really did reach the write phase
    assert snapshot(out) == before
    assert temp_leftovers(out) == []


def test_validation_failure_leaves_the_previous_dataset_byte_identical(tmp_path, monkeypatch):
    out = tmp_path / "raw"
    generate(42, 40, out)
    before = snapshot(out)

    def failing_validate(stage, expected_counts):
        raise RuntimeError("simulated truncated staged file")

    monkeypatch.setattr(generate_dataset, "_validate_staged", failing_validate)

    with pytest.raises(RuntimeError):
        generate(99, 55, out)

    assert snapshot(out) == before
    assert temp_leftovers(out) == []


def test_row_count_mismatch_in_a_staged_file_is_rejected(tmp_path, monkeypatch):
    """The validator must re-read the staged bytes, not trust the writer."""
    out = tmp_path / "raw"
    generate(42, 40, out)
    before = snapshot(out)

    real_write_csv = generate_dataset._write_csv

    def truncating_write_csv(path, rows, fieldnames):
        if path.name == "invoices.csv":
            rows = rows[:-1]  # silently short write
        return real_write_csv(path, rows, fieldnames)

    monkeypatch.setattr(generate_dataset, "_write_csv", truncating_write_csv)

    with pytest.raises(RuntimeError):
        generate(99, 55, out)

    assert snapshot(out) == before
    assert temp_leftovers(out) == []
