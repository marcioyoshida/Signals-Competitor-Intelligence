"""#165: phone usage by device class — the Mobile M2 gate evidence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_builder
from src.synth import mobile_usage as mu


def _s(day, device, officer="cso", standalone=False, kind="session"):
    return {"kind": kind, "device": device, "officer": officer, "standalone": standalone,
            "created_at": f"2026-{day}T12:00:00+00:00"}


def test_weekly_sessions_by_device_and_gate_streak():
    ev = []
    for wk_day in ("09-07", "09-14", "09-21", "09-28"):    # 4 consecutive ISO weeks
        ev += [_s(wk_day, "phone", standalone=True), _s(wk_day, "desktop"), _s(wk_day, "desktop")]
    ev.append(_s("09-28", "phone", kind="install"))
    ev.append(_s("09-28", "phone", kind="ask"))
    out = mu.build(ev, [{"device": "phone"}, {"device": "desktop"}])
    top = out["weeks"][0]
    assert top["week"] == "2026-W40" and top["by_device"] == {"phone": 1, "desktop": 2}
    assert top["phone_share"] == round(1 / 3, 3) and top["standalone"] == 1 and top["installs"] == 1
    assert out["gate"]["weeks_met"] == 4 and out["gate"]["usage_condition_met"] is True
    jobs = {j["job"]: j["count"] for j in out["phone_jobs"]}
    assert jobs["perguntar"] == 1 and jobs["aprovar/rejeitar"] == 1 and jobs["abrir painel"] == 4


def test_qa_and_operator_sessions_never_count_toward_the_gate():
    # the QA phone gate signs in as qa-internal-* on every pipeline run: automation is not demand
    ev = []
    for wk_day in ("09-07", "09-14", "09-21", "09-28"):
        ev += [dict(_s(wk_day, "phone"), tenant="qa-internal-admin"),
               dict(_s(wk_day, "phone"), tenant="operator"),
               dict(_s(wk_day, "desktop"), tenant="acme-bank")]
    ev.append(dict(_s("09-28", "phone", kind="ask"), tenant="qa-internal-test"))
    out = mu.build(ev, [{"device": "phone", "tenant": "operator"}])
    assert out["gate"]["weeks_met"] == 0 and out["gate"]["usage_condition_met"] is False
    assert out["weeks"][0]["by_device"] == {"desktop": 1} and out["internal_sessions_excluded"] == 8
    assert out["phone_jobs"] == []


def test_a_gap_week_breaks_the_streak():
    ev = [_s("09-28", "phone"), _s("09-14", "phone")]      # W40 and W38: not consecutive
    assert mu.build(ev)["gate"]["weeks_met"] == 1


def test_mobile_usage_is_operator_only_in_every_derived_feed():
    feed = {"feed": [], "entities": [], "industries": [], "mobile_usage": {"weeks": [{"week": "x"}]}}
    assert feed_builder.scope_feed_to_modules(feed, ["banking"]).get("mobile_usage") == {}
    assert feed_builder.derive_entry_feed(feed, industries=("betting",)).get("mobile_usage") == {}
    assert not feed_builder.derive_sample_feed(feed).get("mobile_usage")
