import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import threads

AS_OF = "2026-08-23"


def _d(days_ago):
    return (dt.date.fromisoformat(AS_OF) - dt.timedelta(days=days_ago)).isoformat()


def _card(entity, text, days_ago, *, threat=0.6, nid=None):
    return {
        "id": nid or f"competitor:{entity}",   # stable id reused across dates
        "entity": entity, "narrative": text, "threat_score": threat,
        "run_date": _d(days_ago), "lenses": ["news"],
        "citations": [{"url": f"https://ex/{entity}/{days_ago}"}],
    }


def test_event_types_of_tags_by_keyword():
    assert "acquisition" in threads.event_types_of(_card("x", "A empresa anuncia aquisição da rival.", 1))
    assert "litigation" in threads.event_types_of(_card("x", "Novo processo judicial contra a empresa.", 1))
    assert "authorization" in threads.event_types_of(_card("x", "Recebeu autorização para banco múltiplo.", 1))


def test_primary_event_type_picks_dominant_and_avoids_duplicate_threads():
    # A bundled card touching authorization (3 hits) AND expansion (1 hit): breadth
    # still sees both, but it threads into ONLY the dominant primary (authorization).
    bundle = _card("itau",
                   "Itaú recebeu autorização preliminar para operar como banco múltiplo "
                   "nos EUA, conforme aprovação preliminar.", 1)
    assert set(threads.event_types_of(bundle)) == {"authorization", "expansion"}
    assert threads.primary_event_type(bundle) == "authorization"


def test_bundle_card_seeds_only_primary_thread():
    # 3 days of the same bundle (authorization-dominant: autoriza + banco multiplo +
    # aprovacao preliminar = 3 hits vs expansion's 1 'nos eua') -> ONE thread, not two.
    narrs = [_card("itau", "Itaú recebeu autorização preliminar para operar como banco "
                           "múltiplo nos EUA, conforme aprovação preliminar.", d)
             for d in (6, 3, 1)]
    th = threads.build_threads(narrs, as_of=AS_OF)
    assert list(th.keys()) == ["itau--authorization"]      # no itau--expansion shadow
    assert th["itau--authorization"]["n_developments"] == 3


def test_thread_needs_multiple_dates():
    # two cards same day -> one date -> not a thread
    narrs = [_card("itau", "aquisição em curso", 1, nid="a"),
             _card("itau", "aquisição avança", 1, nid="b")]
    assert threads.build_threads(narrs, as_of=AS_OF) == {}


def test_date_keyed_dedup_threads_multi_day_arc():
    # SAME reused id across 3 dates must still count as 3 developments
    narrs = [_card("itau", "autorização em análise", 6),
             _card("itau", "autorização preliminar concedida", 3),
             _card("itau", "autorização para banco múltiplo confirmada", 1)]
    th = threads.build_threads(narrs, as_of=AS_OF)
    t = th["itau--authorization"]
    assert t["n_developments"] == 3 and t["n_dates"] == 3
    assert t["status"] == "developing"          # >= DEVELOPING_MIN developments, active
    assert t["first_seen"] == _d(6) and t["last_updated"] == _d(1)


def test_open_vs_resolved_lifecycle():
    # exactly 2 developments, active -> open
    active = threads.build_threads(
        [_card("x", "parceria anunciada", 5), _card("x", "parceria ampliada", 2)], as_of=AS_OF)
    assert active["x--partnership"]["status"] == "open"
    # last update long ago -> resolved (kept as history)
    stale = threads.build_threads(
        [_card("x", "parceria anunciada", 60), _card("x", "parceria ampliada", 40)], as_of=AS_OF)
    assert stale["x--partnership"]["status"] == "resolved"


def test_build_card_is_labeled_grounded_and_alerts_hot():
    narrs = [_card("itau", "aquisição em curso", 5, threat=0.9),
             _card("itau", "aquisição fechada", 1, threat=0.8)]
    t = threads.build_threads(narrs, as_of=AS_OF)["itau--acquisition"]
    c = threads.build_card(t)
    assert c["axis"] == "threaded" and c["subject_type"] == "incident"
    assert c["is_inference"] and c["mode"] == "derived" and c["entity"] == "itau"
    assert c["is_alert"] is True                # active + peak >= ALERT_PEAK
    assert "Fio de incidente" in c["narrative"]
    assert len(c["citations"]) == 2            # aggregated across developments
    # resolved thread scores down and never alerts
    stale = threads.build_threads(
        [_card("y", "aquisição antiga", 60, threat=0.9),
         _card("y", "aquisição concluída", 40, threat=0.9)], as_of=AS_OF)["y--acquisition"]
    cs = threads.build_card(stale)
    assert cs["is_alert"] is False and cs["threat_score"] < 0.4
