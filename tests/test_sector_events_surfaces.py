"""#177 surfaces: CRO (reg_threat floor, n_changes, "Eventos setoriais"), tenant scoping, the
weekly digest and /api/ask grounding — on the real pre-fix CRO slice (betting reg_threat 25.0)."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import agent_ask as aa
from src.dashboard import feed_builder, weekly_digest
from src.synth import executive
from src.synth import sector_events as se

FIX = Path(__file__).resolve().parent / "fixtures" / "sector_events"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


SLICE = _load("feed_cro_slice.json")
RUNS = _load("news_runs_2026-09-25_26.json")["runs"]
DOU = _load("dou_2026-09-25.json")


def _events(with_dou: bool):
    store = {}
    for i, run in enumerate(RUNS):
        digest = {"news": copy.deepcopy(run["news"])}
        if with_dou and i == 0:
            digest["dou"] = copy.deepcopy(DOU["dou"])
        off, news = se.split_digest(digest)
        store, _ = se.build_events(store, off, news, today=dt.date.fromisoformat(run["run"][:10]))
    return se.for_feed(store, entity_attrs=SLICE["entity_attrs"], today=dt.date(2026, 9, 27))


def _feed(with_dou=True, events=True):
    f = copy.deepcopy(SLICE)
    f["sector_events"] = _events(with_dou) if events else []
    return f


# --- CRO -------------------------------------------------------------------------------------
def test_cro_before_matches_live_25():
    cro = executive.build_executive(_feed(events=False))["cro"]
    assert cro["by_industry"]["betting"]["reg_threat"] == SLICE["live_cro_betting"]["reg_threat"] == 25.0
    assert cro["panels"]["sector_events"] == []


def test_cro_after_critical_event_floors_reg_threat_and_counts_change():
    before = executive.build_executive(_feed(events=False))["cro"]["by_industry"]["betting"]
    ex = executive.build_executive(_feed(with_dou=True))
    b = ex["cro"]["by_industry"]["betting"]
    assert b["reg_threat"] == executive.SECTOR_EVENT_FLOOR["critical"] == 90.0
    assert b["reg_threat_cards"] == 25.0  # the card average is still shown, not overwritten
    assert b["n_changes"] == before["n_changes"] + 1 and b["n_sector_events"] == 1
    assert b["sector_event_severity"] == "critical"
    row = ex["cro"]["panels"]["sector_events"][0]
    assert row["industry"] == "betting" and row["sources"][0]["kind"] == "official"
    assert row["sources"][0]["title"].startswith("MEDIDA PROVISÓRIA Nº 1.394")
    assert row["n_affected"] == 83
    rec = ex["cro"]["panels"]["recommendations"][0]
    assert rec["evidence_id"] == row["id"] and "Evento setorial (critical)" in rec["text"]
    traj = [t for t in ex["flow"] if t["trigger"] == "evento_setorial"]
    assert traj and traj[0]["severity"] == "crit" and traj[0]["handoff"] == "cco"
    # CSO's regulatory tile reads the same floor (no two numbers for one sector)
    assert ex["cso"]["by_industry"]["betting"]["reg_threat"] == 90.0


def test_cro_after_press_only_event_is_high_not_critical():
    b = executive.build_executive(_feed(with_dou=False))["cro"]["by_industry"]["betting"]
    assert b["reg_threat"] == executive.SECTOR_EVENT_FLOOR["high"] == 70.0
    assert b["sector_event_severity"] == "high"


def test_other_industries_are_not_floored():
    ex = executive.build_executive(_feed(with_dou=True))["cro"]["by_industry"]
    assert ex["banking"]["n_sector_events"] == 0
    assert ex["banking"]["reg_threat"] == ex["banking"]["reg_threat_cards"]


# --- tenant scoping ----------------------------------------------------------------------------
def test_scoped_feed_shows_event_only_to_licensed_tenants():
    f = _feed()
    assert feed_builder.scope_feed_to_modules(f, ["betting"])["sector_events"]
    scoped = feed_builder.scope_feed_to_modules(f, ["banking"])
    assert scoped["sector_events"] == []
    assert scoped["executive"]["cro"]["panels"]["sector_events"] == []


def test_entry_feed_keeps_betting_event_and_sample_withholds_it():
    f = _feed()
    assert feed_builder.derive_entry_feed(f)["sector_events"]  # betting is an entry industry
    sample = feed_builder.derive_sample_feed(f, industry="betting")
    assert sample["sector_events"] == [] and "sector_events" in sample["withheld"]["sections"]


# --- weekly digest -----------------------------------------------------------------------------
def test_weekly_digest_mentions_the_event():
    f = _feed()
    lines = weekly_digest.sector_event_lines(f, as_of="2026-09-28")
    assert len(lines) == 1 and lines[0].startswith("[crítico] Betting & iGaming — proibição")
    assert "MEDIDA PROVISÓRIA Nº 1.394" in lines[0]
    w = weekly_digest.with_sector_events({"headline": "Semana"}, f, as_of="2026-09-28")
    subject, text, html = weekly_digest.format_email(w)
    assert "Eventos setoriais" in text and "Eventos setoriais" in html
    slack = json.dumps(weekly_digest.format_slack(w), ensure_ascii=False)
    assert "Eventos setoriais" in slack
    assert weekly_digest.sector_event_lines(f, as_of="2026-11-30") == []  # out of the week


# --- /api/ask grounding ------------------------------------------------------------------------
Q = "Houve mudança regulatória no setor de apostas?"


def test_ask_grounds_on_the_sector_event_and_cites_the_dou():
    f = _feed()
    seen = {}

    def conv(user, system=None, max_tokens=None):
        seen["user"] = user
        return "Sim: a MP 1.394 proibiu as apostas de quota fixa [sector_event:betting:mp-1394]."

    r = aa.answer(Q, feed=f, converser=conv)
    assert not r["refused"] and r["grounded"]
    assert r["considered"][0] == "sector_event:betting:mp-1394"
    cite = r["citations"][0]
    assert cite["id"] == "sector_event:betting:mp-1394"
    assert cite["sources"][0]["url"] == ("https://www.in.gov.br/web/dou/-/"
                                         "medida-provisoria-n-1.394-de-25-de-setembro-de-2026-734808521")
    assert "MEDIDA PROVISÓRIA Nº 1.394" in seen["user"]


def test_ask_short_question_about_bets_is_in_domain():
    r = aa.answer("As bets foram proibidas?", feed=_feed(), converser=lambda *a, **k: "ok [sector_event:betting:mp-1394]")
    assert not r["refused"] and r["considered"][0] == "sector_event:betting:mp-1394"


def test_ask_scoped_tenant_without_betting_never_sees_the_card():
    r = aa.answer(Q, feed=_feed(), converser=lambda *a, **k: "x", modules=["banking"])
    assert "sector_event:betting:mp-1394" not in (r.get("considered") or [])


def test_ask_without_event_still_declines_honestly():
    r = aa.answer(Q, feed=_feed(events=False), converser=lambda *a, **k: None)
    assert "sector_event:betting:mp-1394" not in (r.get("considered") or [])


def test_cross_sector_aggregate_is_not_floored_but_single_sector_scope_is():
    f = _feed()
    ex = executive.build_executive(f)["cro"]["by_industry"]["__all__"]
    assert ex["reg_threat"] == ex["reg_threat_cards"] and ex["n_sector_events"] == 1
    only_bet = feed_builder.scope_feed_to_modules(f, ["betting"])
    assert only_bet["executive"]["cro"]["by_industry"]["__all__"]["reg_threat"] == 90.0


def test_sector_event_outranks_the_b3_narrative_that_mentions_the_ban():
    """Pre-fix, retrieval found only B3's narrative ("A proibição das bets no Brasil pode impactar
    as ações da B3") and the agent answered with B3 facts. The sector-event card must rank first."""
    f = _feed()
    assert any(c["id"] == "cand-ent-b3" for c in f["feed"])
    pool = aa.sector_event_cards(f) + f["feed"]
    ids = [c["id"] for c in aa.select_grounding(Q, pool, limit=12)]
    assert ids[0] == "sector_event:betting:mp-1394"
    # the B3 card shares "bets" with this phrasing — still second to the event card
    ids = [c["id"] for c in aa.select_grounding("A proibição das bets afetou o setor?", pool, limit=12)]
    assert ids[0] == "sector_event:betting:mp-1394" and "cand-ent-b3" in ids[1:]


# --- KB snippets keep their provenance (live finding 2026-09-27) --------------------------------
MP_URL = "https://www.in.gov.br/web/dou/-/medida-provisoria-n-1.394-de-25-de-setembro-de-2026-734808521"


def _kb_results():
    mid_chunk = ("Art. 5º Os saldos existentes nas contas dos apostadores serão devolvidos por meio de "
                 "instituições financeiras entre 9 e 14 de outubro de 2026 ... " * 30)
    meta = {"source": "DOU", "kind": "regulatory", "doc_type": "Medida Provisória", "date": "2026-09-25",
            "url": MP_URL, "name": "MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026",
            "industries": "betting",
            "x-amz-bedrock-kb-source-uri": "s3://raw/DOU/dou:medida-provisoria-n-1.394.txt"}
    loc = {"type": "S3", "s3Location": {"uri": "s3://raw/DOU/dou:medida-provisoria-n-1.394.txt"}}
    other = {"content": {"text": "Resolução BCB nº 588 altera a Circular 3.978"}, "score": 0.5,
             "metadata": {"source": "BCB", "url": "https://www.bcb.gov.br/x"},
             "location": {"s3Location": {"uri": "s3://raw/BCB/588.txt"}}}
    return [{"content": {"text": mid_chunk}, "score": 0.71, "metadata": meta, "location": loc},
            {"content": {"text": "Art. 1º Fica proibida a exploração ..."}, "score": 0.70, "metadata": meta, "location": loc},
            {"content": {"text": "Art. 9º Revogam-se ..."}, "score": 0.69, "metadata": meta, "location": loc},
            other]


def test_kb_snippets_keep_act_metadata_and_dedupe_per_document():
    snips = aa.kb_snippets_from_results(_kb_results())
    assert [s["id"] for s in snips] == ["kb:0", "kb:1", "kb:2"]  # best 2 chunks of the MP + the BCB doc
    s0 = snips[0]
    assert s0["title"].startswith("MEDIDA PROVISÓRIA Nº 1.394") and s0["date"] == "2026-09-25"
    assert s0["url"] == MP_URL and s0["doc_type"] == "Medida Provisória"
    assert len(s0["subject"]) == aa.KB_CHUNK_CHARS


def test_kb_prompt_names_the_act_and_citation_resolves_to_the_dou_url():
    seen = {}

    def conv(user, system=None, max_tokens=None):
        seen["user"] = user
        return "Sim, a MP 1.394 proibiu as apostas de quota fixa [kb:1]."

    kb = lambda q: aa.kb_snippets_from_results(_kb_results())  # noqa: E731
    r = aa.answer(Q, feed=_feed(events=False), converser=conv, kb_retrieve=kb)
    line = next(ln for ln in seen["user"].splitlines() if ln.startswith("[kb:0]"))
    assert "MEDIDA PROVISÓRIA Nº 1.394" in line and "2026-09-25" in line and MP_URL in line
    assert "DOU · Medida Provisória" in line
    # kb:1 is another chunk of the same MP → merged into its first citation (one per act)
    assert [c["id"] for c in r["citations"]] == ["kb:0"]
    cite = r["citations"][0]
    assert cite["kb"] and cite["url"] == MP_URL and cite["sources"] == [{"url": MP_URL}]
    assert cite["entity_label"].startswith("MEDIDA PROVISÓRIA Nº 1.394")
