import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_ifdata


def test_market_share_groups_by_codinst_and_resolves_names():
    # IfDataValores rows carry no institution name field (confirmed against
    # the live API) — only CodInst. Grouping by name instead of CodInst was
    # the bug: every row silently collapsed into one "?" bucket.
    rows = [
        {"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 300.0},
        {"CodInst": "00068987", "NomeColuna": "Carteira de Crédito", "Saldo": 999.0},
        {"CodInst": "00012345", "NomeColuna": "Ativo Total", "Saldo": 700.0},
    ]
    names = {"00068987": "Banco A", "00012345": "Banco B"}

    result = bcb_ifdata.market_share(rows, institution_names=names)

    assert result == [
        {"institution": "Banco B", "value": 700.0, "share_pct": 70.0},
        {"institution": "Banco A", "value": 300.0, "share_pct": 30.0},
    ]


def test_market_share_falls_back_to_code_when_names_missing():
    rows = [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}]

    result = bcb_ifdata.market_share(rows)

    assert result == [{"institution": "00068987", "value": 100.0, "share_pct": 100.0}]


# --- name -> entity_id resolution + durable store (ADR 015 §3) -----------

_SHARES = [
    {"institution": "ITAU UNIBANCO", "value": 700.0, "share_pct": 70.0},
    {"institution": "BANCO DO BRASIL", "value": 300.0, "share_pct": 30.0},
    {"institution": "SOME UNTRACKED BANK", "value": 10.0, "share_pct": 5.0},
]


def _resolver(item):
    return {
        "ITAU UNIBANCO": ["itau"],
        "BANCO DO BRASIL": ["bb"],
    }.get(item.get("institution"), [])


def test_map_to_entities_resolves_and_keeps_share():
    recs = bcb_ifdata.map_to_entities(
        _SHARES, resolver=_resolver, base_date=202603, today=dt.date(2026, 8, 31)
    )
    by = {r["entity"]: r for r in recs}
    # unknown institution is dropped (never invented)
    assert set(by) == {"itau", "bb"}
    assert by["itau"]["market_share_pct"] == 70.0
    assert by["itau"]["value"] == 700.0
    assert by["itau"]["base_date"] == 202603
    assert by["itau"]["source"] == "BCB" and by["itau"]["id"] == "bcb-ifdata:itau"
    assert by["bb"]["market_share_pct"] == 30.0


def test_map_to_entities_keeps_largest_share_on_duplicate():
    shares = [
        {"institution": "ITAU A", "value": 1.0, "share_pct": 20.0},
        {"institution": "ITAU B", "value": 2.0, "share_pct": 45.0},
    ]
    recs = bcb_ifdata.map_to_entities(shares, resolver=lambda i: ["itau"])
    assert len(recs) == 1 and recs[0]["market_share_pct"] == 45.0


def test_map_to_entities_skips_null_share():
    shares = [{"institution": "ITAU", "value": None, "share_pct": None}]
    assert bcb_ifdata.map_to_entities(shares, resolver=lambda i: ["itau"]) == []


class FakeS3:
    def __init__(self): self.store = {}
    def get_object(self, Bucket, Key):
        if Key not in self.store: raise KeyError(Key)
        return {"Body": _B(self.store[Key])}
    def put_object(self, Bucket, Key, Body, **kw): self.store[Key] = Body
class _B:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_store_roundtrip_and_share_by_entity():
    s3 = FakeS3()
    recs = bcb_ifdata.map_to_entities(
        _SHARES, resolver=_resolver, base_date=202603, today=dt.date(2026, 8, 31)
    )
    out = bcb_ifdata.update_store(recs, "b", s3=s3, today=dt.date(2026, 8, 31))
    assert out == {"updated": 2, "records": 2}
    index = bcb_ifdata.load_index("b", s3=s3)
    # list_records is share-desc
    assert [r["entity"] for r in bcb_ifdata.list_records(index)] == ["itau", "bb"]
    # the projection feed_builder joins on
    assert bcb_ifdata.share_by_entity(index) == {"itau": 70.0, "bb": 30.0}


# --- 900s-timeout regression guards (structured ingest stalls 2026-09-08..12) ---


def test_latest_base_date_probes_cheaply_and_does_not_download_full_report(monkeypatch):
    """The probe must use $top=1, not a full 58MB / 153k-row fetch per candidate.

    The old implementation called fetch_institutions() for EVERY candidate date and
    then the caller fetched the winner AGAIN — two+ full 58MB downloads per run.
    """
    seen: list[str] = []

    class _Resp:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            return {"value": [{"CodInst": "1"}] if "202603" in self.url else []}

    def _fake_get(url, **kwargs):
        seen.append(url)
        return _Resp(url)

    monkeypatch.setattr(bcb_ifdata.requests, "get", _fake_get)
    monkeypatch.setattr(
        bcb_ifdata, "_candidate_base_dates", lambda *a, **k: [202606, 202603]
    )
    monkeypatch.setattr(
        bcb_ifdata,
        "fetch_institutions",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("full fetch during probe")),
    )

    assert bcb_ifdata.latest_base_date() == 202603
    assert len(seen) == 2
    assert all("$top=1" in u for u in seen)


def test_candidate_base_dates_are_derived_from_today_not_hardcoded():
    got = bcb_ifdata._candidate_base_dates(dt.date(2026, 9, 13), back=3)
    assert got == [202606, 202603, 202512]
    # rolls the year correctly
    assert bcb_ifdata._candidate_base_dates(dt.date(2026, 2, 1), back=2) == [202512, 202509]


def test_map_to_entities_limit_caps_resolver_calls():
    """IF.data returns ~1,422 institutions; resolve_entities is a CPU-bound pass over
    the whole alias map, so an uncapped loop burned hundreds of seconds of the ingest
    budget on a sub-0.01%-share tail."""
    calls: list[str] = []

    def _counting(item):
        calls.append(item["institution"])
        return []

    bcb_ifdata.map_to_entities(_SHARES, resolver=_counting, limit=2)

    assert calls == ["ITAU UNIBANCO", "BANCO DO BRASIL"]


def test_map_to_entities_resumes_from_a_cursor_and_reports_progress():
    calls: list[str] = []
    stats: dict = {}

    def _counting(item):
        calls.append(item["institution"])
        return []

    bcb_ifdata.map_to_entities(_SHARES, resolver=_counting, start=1, limit=5, stats=stats)

    assert calls == ["BANCO DO BRASIL", "SOME UNTRACKED BANK"]
    assert stats["processed"] == 2


def test_map_to_entities_stops_at_a_soft_deadline_and_keeps_partial_work():
    """A hard SIGALRM kill mid-window would lose the window AND leave the cursor
    unmoved -> the incremental walk would livelock on the same slice forever."""
    import time as _t

    stats: dict = {}
    recs = bcb_ifdata.map_to_entities(
        _SHARES, resolver=_resolver, deadline=_t.monotonic() - 1, stats=stats
    )
    assert stats["processed"] == 0 and recs == []


def test_resolve_progress_tracks_the_quarter():
    # no store yet -> start at 0, not complete
    assert bcb_ifdata.resolve_progress({}, 202606) == (0, False)
    # mid-walk
    idx = {"base_date": 202606, "cursor": 150, "universe": 1422}
    assert bcb_ifdata.resolve_progress(idx, 202606) == (150, False)
    # walked out -> the source no-ops for the rest of the quarter
    assert bcb_ifdata.resolve_progress({**idx, "cursor": 1422}, 202606) == (1422, True)
    # BCB published a new quarter -> restart
    assert bcb_ifdata.resolve_progress({**idx, "cursor": 1422}, 202609) == (0, False)


def test_merge_carries_progress_forward():
    idx = {"records": {}, "base_date": 202606, "cursor": 150, "universe": 1422}
    out = bcb_ifdata.merge(idx, [], progress={"cursor": 300, "base_date": 202606})
    assert out["cursor"] == 300 and out["universe"] == 1422


def test_map_to_entities_does_not_swallow_the_wall_clock_budget_kill():
    """The per-source SIGALRM budget raises from an arbitrary line; the best-effort
    ``except Exception`` around the resolver must NOT eat it, or the source runs
    unbounded to the 900s Lambda ceiling (observed live)."""
    from src.ingest.budget import SourceBudgetExceeded

    def _killed(item):
        raise SourceBudgetExceeded("IF.data market exceeded 180s budget")

    import pytest

    with pytest.raises(SourceBudgetExceeded):
        bcb_ifdata.map_to_entities(_SHARES, resolver=_killed)
