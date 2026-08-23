import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import swot_maintenance as sm

AS_OF = "2026-08-23"


def _d(days_ago):
    return (dt.date.fromisoformat(AS_OF) - dt.timedelta(days=days_ago)).isoformat()


def _bullet(bid="seed:nubank:S:abc", **kw):
    b = {"id": bid, "entity": "nubank", "label": "Nubank", "dimension": "S",
         "text": "Força competitiva sustentada.", "approved_at": _d(200)}
    b.update(kw)
    return b


def _curated(bullets, retirements=None):
    return {"bullets": bullets, "retirements": retirements or []}


def test_stale_bullet_is_flagged():
    props = sm.find_stale(_curated([_bullet(approved_at=_d(120))]), [], as_of=AS_OF, stale_days=90)
    assert len(props) == 1
    p = props[0]
    assert p["kind"] == "stale" and p["status"] == "pending"
    assert p["target_bullet_id"] == "seed:nubank:S:abc"
    assert p["days_stale"] == 120 and p["dimension"] == "S"


def test_fresh_bullet_is_not_flagged():
    props = sm.find_stale(_curated([_bullet(approved_at=_d(30))]), [], as_of=AS_OF, stale_days=90)
    assert props == []


def test_news_corroboration_keeps_a_bullet_fresh():
    # an old approval, but a recent reinforcement reaching the bullet -> not stale
    reinf = [{"bullet_id": "seed:nubank:S:abc", "date": _d(10)}]
    props = sm.find_stale(_curated([_bullet(approved_at=_d(200))]), reinf, as_of=AS_OF, stale_days=90)
    assert props == []


def test_reaffirmation_resets_the_clock():
    b = _bullet(approved_at=_d(200), reaffirmed_at=_d(15))
    assert sm.find_stale(_curated([b]), [], as_of=AS_OF, stale_days=90) == []


def test_retired_bullet_is_skipped():
    cur = _curated([_bullet(approved_at=_d(200))],
                   retirements=[{"target_bullet_id": "seed:nubank:S:abc"}])
    assert sm.find_stale(cur, [], as_of=AS_OF, stale_days=90) == []


def test_id_is_stable_within_a_staleness_cycle():
    # same last-affirmed date -> same id every run (idempotent via merge)
    cur = _curated([_bullet(approved_at=_d(120))])
    a = sm.find_stale(cur, [], as_of=AS_OF, stale_days=90)[0]
    b = sm.find_stale(cur, [], as_of="2026-08-24", stale_days=90)[0]
    assert a["id"] == b["id"] == "stale:seed:nubank:S:abc:" + _d(120)


def test_new_id_after_reaffirmation():
    old = sm.find_stale(_curated([_bullet(approved_at=_d(200))]), [], as_of=AS_OF, stale_days=90)[0]
    # analyst kept it (reaffirmed) but it later goes stale again -> a fresh cycle id
    reaffirmed = _bullet(approved_at=_d(200), reaffirmed_at=_d(100))
    new = sm.find_stale(_curated([reaffirmed]), [], as_of=AS_OF, stale_days=90)[0]
    assert old["id"] != new["id"]
    assert new["id"].endswith(_d(100))


def test_last_affirmed_prefers_most_recent_signal():
    b = _bullet(approved_at=_d(200), reaffirmed_at=_d(150))
    assert sm.last_affirmed(b, [_d(40), _d(300)]) == _d(40)
