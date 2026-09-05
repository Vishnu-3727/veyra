"""Historical baseline correctness: the naive-baseline comparison for a run must use THAT run's
source snapshot, so generating dataset B after run A can never change run A's baseline -- and
Veyra's decisions and the baseline are always compared over the same dataset."""
import pytest

from app import db
from app import settings as llm_settings
from app.baseline import compute_run_baseline
from app.pipeline import run_reconciliation


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(llm_settings, "_settings", None)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    db.init_db()
    yield tmp_path
    monkeypatch.setattr(llm_settings, "_settings", None)


def _make_and_run(tmp_path, size):
    from app.generate_dataset import generate

    raw = tmp_path / f"raw_{size}"
    raw.mkdir(exist_ok=True)
    generate(42, size, raw)
    return run_reconciliation(raw)["run_id"]


def test_baseline_is_attached_to_the_same_run_id_as_its_dataset(tmp_env):
    run_a = _make_and_run(tmp_env, 40)
    r = compute_run_baseline(run_a)
    assert r["run_id"] == run_a
    assert r["source"] == "run_snapshot"


def test_generating_dataset_b_does_not_alter_run_as_baseline(tmp_env):
    """CASE 13: a historical baseline must survive dataset B, using dataset A's records."""
    run_a = _make_and_run(tmp_env, 40)
    baseline_a_before = compute_run_baseline(run_a)

    _make_and_run(tmp_env, 30)  # dataset B

    baseline_a_after = compute_run_baseline(run_a)
    # identical, and still keyed to run A: dataset B did not leak in
    assert baseline_a_after["outcomes"] == baseline_a_before["outcomes"]
    assert baseline_a_after["run_id"] == run_a
    assert baseline_a_after["source_records"] == baseline_a_before["source_records"]


def test_missing_run_metadata_returns_none_from_run_baseline(tmp_env):
    """A run with no snapshot must return None so the caller can fall back explicitly, never
    silently substituting the current dataset."""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status) VALUES ('legacy', '2026-01-01', 'COMPLETED')"
        )
    assert compute_run_baseline("legacy") is None