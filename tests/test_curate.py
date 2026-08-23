import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import curate, operatives, relational, swot_reconcile, swot_seed, swot_store

BUCKET = "onca-digests"


class FakeS3:
    def __init__(self):
        self.store: dict[tuple, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.store:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.store[(Bucket, Key)])}


def _seed_store(s3, key, proposals):
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=json.dumps({"proposals": proposals}).encode())


def _curated(s3):
    return json.loads(s3.store[(BUCKET, swot_store.CURATED_KEY)].decode())


def _prop(pid, **kw):
    p = {"id": pid, "entity": "nubank", "label": "Nubank", "dimension": "S",
         "text": "Força competitiva.", "kind": "seed", "status": "pending",
         "evidence": ["nubank-1"], "source_key": pid, "date": "2026-08-23"}
    p.update(kw)
    return p


# --- approve a seed -> curated active bullet -------------------------------
def test_approve_seed_promotes_to_curated_bullet():
    s3 = FakeS3()
    _seed_store(s3, swot_seed.SEED_PROPOSALS_KEY, [_prop("seed:nubank:S:abc")])
    r = curate.vet_swot(BUCKET, "seed:nubank:S:abc", "approved", s3=s3)
    assert r["status"] == "approved" and r["effect"] == "bullet"
    cur = _curated(s3)
    assert len(cur["bullets"]) == 1
    b = cur["bullets"][0]
    assert b["entity"] == "nubank" and b["dimension"] == "S" and b["origin"] == "seed"
    assert b["evidence"] == ["nubank-1"] and b["approved_at"]
    # status flipped in the source store
    store = json.loads(s3.store[(BUCKET, swot_seed.SEED_PROPOSALS_KEY)].decode())
    assert store["proposals"][0]["status"] == "approved"


def test_approve_new_reconcile_proposal_promotes():
    s3 = FakeS3()
    _seed_store(s3, swot_reconcile.PROPOSALS_KEY,
                [_prop("new:itau:x1", kind="new", entity="itau", narrative_id="itau-x1",
                       evidence=None, source_key=None)])
    r = curate.vet_swot(BUCKET, "new:itau:x1", "approved", s3=s3)
    assert r["effect"] == "bullet"
    b = _curated(s3)["bullets"][0]
    assert b["evidence"] == ["itau-x1"]        # fell back to narrative_id
    assert b["source_key"] == "new:itau:x1"    # fell back to id


def test_approve_challenge_records_retirement():
    s3 = FakeS3()
    _seed_store(s3, swot_reconcile.PROPOSALS_KEY,
                [_prop("challenge:nubank:nubank:peer:banking:S:n1", kind="challenge",
                       target_bullet_id="nubank:peer:banking:S")])
    r = curate.vet_swot(BUCKET, "challenge:nubank:nubank:peer:banking:S:n1", "approved", s3=s3)
    assert r["effect"] == "retirement"
    cur = _curated(s3)
    assert cur["bullets"] == []
    assert cur["retirements"][0]["target_bullet_id"] == "nubank:peer:banking:S"


# --- reject ----------------------------------------------------------------
def test_reject_sets_status_no_curated_write():
    s3 = FakeS3()
    _seed_store(s3, swot_seed.SEED_PROPOSALS_KEY, [_prop("seed:nubank:S:abc")])
    r = curate.vet_swot(BUCKET, "seed:nubank:S:abc", "rejected", s3=s3)
    assert r["status"] == "rejected" and r["effect"] == "none"
    assert (BUCKET, swot_store.CURATED_KEY) not in s3.store
    store = json.loads(s3.store[(BUCKET, swot_seed.SEED_PROPOSALS_KEY)].decode())
    assert store["proposals"][0]["status"] == "rejected"


# --- idempotency / edge cases ----------------------------------------------
def test_decide_is_idempotent():
    s3 = FakeS3()
    _seed_store(s3, swot_seed.SEED_PROPOSALS_KEY, [_prop("seed:nubank:S:abc")])
    curate.vet_swot(BUCKET, "seed:nubank:S:abc", "approved", s3=s3)
    again = curate.vet_swot(BUCKET, "seed:nubank:S:abc", "approved", s3=s3)
    assert again["status"] == "noop" and again["decision"] == "approved"
    assert len(_curated(s3)["bullets"]) == 1   # not double-promoted


def test_missing_proposal_is_noop():
    s3 = FakeS3()
    _seed_store(s3, swot_seed.SEED_PROPOSALS_KEY, [])
    r = curate.vet_swot(BUCKET, "seed:ghost:S:zzz", "approved", s3=s3)
    assert r["status"] == "noop" and r["detail"] == "missing"


def test_invalid_decision_errors():
    s3 = FakeS3()
    _seed_store(s3, swot_seed.SEED_PROPOSALS_KEY, [_prop("seed:nubank:S:abc")])
    r = curate.vet_swot(BUCKET, "seed:nubank:S:abc", "maybe", s3=s3)
    assert r["status"] == "error"


def test_vet_dispatch_unknown_queue():
    s3 = FakeS3()
    assert curate.vet(BUCKET, "x", "approved", queue="bogus", s3=s3)["status"] == "error"


# --- graph queue -----------------------------------------------------------
def test_approve_graph_proposal_sets_status():
    s3 = FakeS3()
    _seed_store(s3, relational.RELATIONAL_PROPOSALS_KEY,
                [{"id": "conv:a:b", "kind": "convergence", "status": "pending"}])
    r = curate.vet_graph(BUCKET, "conv:a:b", "approved", s3=s3)
    assert r["status"] == "approved"
    store = json.loads(s3.store[(BUCKET, relational.RELATIONAL_PROPOSALS_KEY)].decode())
    assert store["proposals"][0]["status"] == "approved"
    # no curated belief write for graph
    assert (BUCKET, swot_store.CURATED_KEY) not in s3.store


def test_reject_person_proposal():
    s3 = FakeS3()
    _seed_store(s3, operatives.PERSON_PROPOSALS_KEY,
                [{"id": "person:jsilva", "kind": "person", "status": "pending"}])
    r = curate.vet_graph(BUCKET, "person:jsilva", "rejected", s3=s3)
    assert r["status"] == "rejected"


# --- the fold: curated survives a belief rebuild ---------------------------
def test_curated_bullet_folds_into_beliefs_as_active():
    curated = {"bullets": [{
        "id": "seed:nubank:O:xyz", "entity": "nubank", "label": "Nubank",
        "dimension": "O", "text": "Expansão internacional em curso.",
        "source_key": "seed:nubank:O:xyz", "evidence": ["nubank-9"], "origin": "seed",
        "date": "2026-08-23", "approved_at": "2026-08-23T12:00:00+00:00",
    }], "retirements": []}
    beliefs = swot_store.build_beliefs([], as_of="2026-08-24", curated=curated)
    assert "nubank" in beliefs
    b = beliefs["nubank"]["bullets"][0]
    assert b["dimension"] == "O" and b["status"] == "active" and b["curated"] is True
    assert b["confidence"] == swot_store.CURATED_CONFIDENCE
    assert beliefs["nubank"]["counts"]["O"] == 1


def test_curated_retirement_marks_target_retired():
    # an axis-derived bullet whose id the analyst retired -> status retired, not active
    narr = {"id": "c1", "entity": "itau", "axis": "comparative",
            "swot_hint": {"dimension": "S", "cohort": "banking"}, "run_date": "2026-08-23"}
    base = swot_store.build_beliefs([narr], as_of="2026-08-23")
    bid = base["itau"]["bullets"][0]["id"]
    assert base["itau"]["counts"]["S"] == 1
    curated = {"bullets": [], "retirements": [{"target_bullet_id": bid, "entity": "itau"}]}
    after = swot_store.build_beliefs([narr], as_of="2026-08-23", curated=curated)
    retired = next(b for b in after["itau"]["bullets"] if b["id"] == bid)
    assert retired["status"] == "retired"
    assert after["itau"]["counts"]["S"] == 0    # no longer counted active
