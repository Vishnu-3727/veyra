"""API-boundary tests: historical run isolation, operation locking, parameter bounds,
optional auth, and human-review provenance.

These exercise the real FastAPI app through TestClient against a temporary database and
temporary raw-data directory, so they assert what a caller actually observes.
"""
import pytest
from fastapi.testclient import TestClient

from app import api
from app import db
from app import settings as llm_settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "raw").mkdir()
    monkeypatch.setattr(config, "API_AUTH_TOKEN", "")
    monkeypatch.setattr(llm_settings, "_settings", None)
    db.init_db()
    with TestClient(api.app) as c:
        yield c


def _generate_and_run(client, seed, size):
    assert client.post(f"/dataset/generate?seed={seed}&size={size}").status_code == 200
    resp = client.post("/reconcile/run")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_is_public_but_other_endpoints_require_a_configured_token(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "API_AUTH_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200  # run.sh's health probe must keep working
    assert client.get("/meta").status_code == 401
    assert client.get("/meta", headers={"X-API-Token": "s3cret"}).status_code == 200
    assert client.get("/meta", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/meta", headers={"X-API-Token": "wrong"}).status_code == 401


def test_historical_run_keeps_its_own_payment_metadata_after_a_new_dataset(client):
    """The whole point of the per-run payment snapshot: run A's decisions must still carry run
    A's payment metadata after an unrelated dataset B has replaced the current source tables."""
    run_a = _generate_and_run(client, seed=1, size=30)["run_id"]
    listing_a = client.get(f"/decisions?run_id={run_a}&limit=1000").json()
    assert listing_a["total"] == 30
    sample = listing_a["results"][0]
    assert sample["customer_name"] and sample["amount"] is not None

    run_b = _generate_and_run(client, seed=7, size=5)["run_id"]
    assert run_b != run_a

    after = client.get(f"/decisions?run_id={run_a}&limit=1000").json()
    assert after["total"] == 30
    assert len(after["results"]) == 30
    regained = {r["payment_id"]: r for r in after["results"]}[sample["payment_id"]]
    assert regained["customer_name"] == sample["customer_name"]
    assert regained["amount"] == sample["amount"]

    detail = client.get(f"/decisions/{sample['payment_id']}?run_id={run_a}").json()
    assert detail["payment"] is not None
    assert detail["payment"]["customer_name"] == sample["customer_name"]

    # run B's own smaller batch is unaffected
    assert client.get(f"/decisions?run_id={run_b}&limit=1000").json()["total"] == 5


def test_historical_exceptions_and_audit_survive_a_new_dataset(client):
    run_a = _generate_and_run(client, seed=3, size=30)["run_id"]
    exceptions_before = client.get(f"/exceptions?run_id={run_a}").json()["results"]
    assert exceptions_before, "seeded dataset should produce at least one exception"
    _generate_and_run(client, seed=9, size=5)

    exceptions_after = client.get(f"/exceptions?run_id={run_a}").json()["results"]
    assert len(exceptions_after) == len(exceptions_before)
    assert exceptions_after[0]["customer_name"] == exceptions_before[0]["customer_name"]
    audit = client.get(f"/audit?run_id={run_a}&limit=2000").json()["results"]
    assert len(audit) == 30


@pytest.mark.parametrize("path", [
    "/decisions?limit=1000000000",
    "/decisions?limit=0",
    "/decisions?offset=-1",
    "/runs?limit=-5",
    "/runs?limit=100000",
    "/exceptions?limit=0",
    "/audit?limit=99999",
])
def test_absurd_pagination_parameters_are_rejected(client, path):
    assert client.get(path).status_code == 422


def test_dataset_size_is_bounded_by_the_measured_maximum(client):
    import config

    assert client.post(f"/dataset/generate?size={config.MAX_DATASET_SIZE + 1}").status_code == 422
    assert client.post("/dataset/generate?size=0").status_code == 422


def test_dataset_generation_and_reconciliation_share_one_operation_lock(client):
    """A second batch-mutating operation must be refused, not allowed to interleave with the
    first and produce a run built from two different datasets."""
    _generate_and_run(client, seed=2, size=5)
    assert api._dataset_operation_lock.acquire(blocking=False)
    try:
        assert client.post("/reconcile/run").status_code == 409
        assert client.post("/dataset/generate?size=5").status_code == 409
    finally:
        api._dataset_operation_lock.release()
    # lock released -> operations work again
    assert client.post("/reconcile/run").status_code == 200


def test_marking_reviewed_writes_a_human_audit_event_and_leaves_the_decision_intact(client):
    run_id = _generate_and_run(client, seed=4, size=30)["run_id"]
    exc = client.get(f"/exceptions?run_id={run_id}").json()["results"][0]
    payment_id = exc["payment_id"]
    before = client.get(f"/decisions/{payment_id}?run_id={run_id}").json()["decision"]

    resolved = client.post(f"/exceptions/{exc['id']}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] == 1

    after = client.get(f"/decisions/{payment_id}?run_id={run_id}").json()["decision"]
    assert after["status"] == before["status"] == "EXCEPTION"
    assert after["matched_bank_ref"] == before["matched_bank_ref"] is None
    assert after["category"] == before["category"]

    trail = client.get(f"/audit?run_id={run_id}&payment_id={payment_id}").json()["results"]
    reviews = [a for a in trail if a["actor"] == "human_reviewer"]
    assert len(reviews) == 1
    assert reviews[0]["status"] == "EXCEPTION_REVIEWED"
    assert reviews[0]["exception_id"] == exc["id"]
    assert reviews[0]["decision_id"] == before["id"]
    # the engine's own event is still there, so provenance reads decision -> exception -> review
    assert {a["actor"] for a in trail} >= {"human_reviewer"}
    assert len(trail) == 2


def test_resolving_an_unknown_exception_is_a_404(client):
    assert client.post("/exceptions/999999/resolve").status_code == 404


def test_reviewing_an_exception_twice_produces_exactly_one_review_event(client):
    """CASE 15: a double-click should not inflate the audit trail with duplicate review events."""
    run_id = _generate_and_run(client, seed=5, size=30)["run_id"]
    exc = client.get(f"/exceptions?run_id={run_id}").json()["results"][0]

    first = client.post(f"/exceptions/{exc['id']}/resolve").json()
    assert first["resolved"] == 1
    assert first["already_reviewed"] is False

    second = client.post(f"/exceptions/{exc['id']}/resolve").json()
    assert second["resolved"] == 1
    assert second["already_reviewed"] is True

    third = client.post(f"/exceptions/{exc['id']}/resolve").json()
    assert third["already_reviewed"] is True

    trail = client.get(f"/audit?run_id={run_id}&payment_id={exc['payment_id']}").json()["results"]
    reviews = [a for a in trail if a["actor"] == "human_reviewer"]
    assert len(reviews) == 1  # one review action = one provenance event

    decision = client.get(f"/decisions/{exc['payment_id']}?run_id={run_id}").json()["decision"]
    assert decision["status"] == "EXCEPTION"
    assert decision["matched_bank_ref"] is None


def test_baseline_is_scoped_to_the_requested_run_not_the_current_dataset(client):
    """CASE 13/23: after dataset B, run A's baseline must still be computed over run A's data.
    """
    run_a = _generate_and_run(client, seed=11, size=40)["run_id"]
    run_b = _generate_and_run(client, seed=12, size=30)["run_id"]

    baseline_a = client.get(f"/baseline?run_id={run_a}").json()
    assert baseline_a["run_id"] == run_a
    assert baseline_a["source"] == "run_snapshot"
    assert baseline_a["source_records"] == 40

    baseline_b = client.get(f"/baseline?run_id={run_b}").json()
    assert baseline_b["run_id"] == run_b
    assert baseline_b["source_records"] == 30

    # omitting run_id uses the latest COMPLETED run (run_b here), never the current raw files
    latest = client.get("/baseline").json()
    assert latest["run_id"] == run_b
    assert latest["source"] == "run_snapshot"


def test_baseline_for_an_unknown_run_is_a_404_not_the_current_dataset(client):
    """A bogus run_id previously returned 200 with the CURRENT dataset's numbers stamped with
    the caller's run_id -- exactly the cross-dataset comparison this endpoint prevents."""
    _generate_and_run(client, seed=13, size=30)

    missing = client.get("/baseline?run_id=NOPE_NOT_A_RUN")
    assert missing.status_code == 404
    assert "NOPE_NOT_A_RUN" in missing.json()["detail"]


def test_exceptions_and_audit_endpoints_report_totals_for_pagination(client):
    run_id = _generate_and_run(client, seed=21, size=75)["run_id"]
    exc = client.get(f"/exceptions?run_id={run_id}&limit=3").json()
    assert exc["total"] >= len(exc["results"])
    assert len(exc["results"]) == 3

    audit = client.get(f"/audit?run_id={run_id}&limit=5").json()
    assert audit["total"] == 75
    assert len(audit["results"]) == 5


def test_runs_expose_lifecycle_status(client):
    run_id = _generate_and_run(client, seed=22, size=20)["run_id"]
    runs = client.get("/runs?limit=5").json()
    assert all("status" in r for r in runs)
    mine = next(r for r in runs if r["run_id"] == run_id)
    assert mine["status"] == "COMPLETED"
    assert mine["error"] is None
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["metrics"]["status"] == "COMPLETED"
    assert detail["dataset_seed"] == 22
    assert detail["ai_provider"] is not None
