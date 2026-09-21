"""Source-coverage roadmap (the war room's "Fontes" tab)."""
from src.synth import source_coverage as sc


def _feed(entities=None, health=None, runs=None):
    return {"entities": entities or [], "source_health": health or [],
            "source_runs": runs or []}


def test_every_declared_row_is_well_formed():
    assert sc.ROADMAP, "roadmap must not be empty"
    ids = [r["id"] for r in sc.ROADMAP]
    assert len(ids) == len(set(ids)), "duplicate source ids"
    for r in sc.ROADMAP:
        assert r["status"] in sc.STATUSES, r["id"]
        assert r["group"] in sc.GROUPS, r["id"]
        assert r["label"]


def test_rejected_routes_carry_the_measurement_that_killed_them():
    """The whole point of listing a dead route is the number next to it — a bare
    'rejected' invites re-proposal, which is what the ADRs keep having to prevent."""
    rejected = [r for r in sc.ROADMAP if r["status"] == "rejected"]
    assert len(rejected) >= 3
    for r in rejected:
        assert r["metric"], f"{r['id']} is rejected with no measurement"
        assert r["note"], f"{r['id']} is rejected with no reason"


def test_financial_coverage_is_counted_off_the_feed_not_asserted():
    feed = _feed(entities=[
        {"entity": "bb", "resultados": {"custo_credito_pct": 8.08}, "soundness": {"x": 1}},
        {"entity": "itau", "resultados": {"custo_credito_pct": 1.2}},
        {"entity": "quiet"},
    ])
    out = sc.build(feed)
    row = next(r for r in out["rows"] if r["id"] == "resultados")
    assert row["metric"] == "2 de 3 entidades"
    assert row["status"] == "live"


def test_a_financial_source_reaching_nobody_is_a_defect_not_quiet():
    """#145 found cvm_financials switched OFF in the deployed stack while every doc
    called it shipped; #150 found the monthly pipeline dead for weeks. A source that
    reaches zero entities must read as broken, never as 'live'."""
    out = sc.build(_feed(entities=[{"entity": "bb"}, {"entity": "itau"}]))
    row = next(r for r in out["rows"] if r["id"] == "resultados")
    assert row["status"] == "silent"
    assert row["status_label"] == sc.STATUS_LABEL["silent"]
    assert out["n_attention"] >= 1


def test_run_telemetry_outranks_the_declaration():
    feed = _feed(runs=[{"source": "Receita QSA", "band": "error", "staleness_days": None}])
    out = sc.build(feed)
    row = next(r for r in out["rows"] if r["id"] == "receita_qsa")
    assert row["status"] == "silent" and row["band"] == "error"


def test_lens_rows_take_their_number_from_source_health():
    feed = _feed(health=[{"lens": "news", "docs": 441, "staleness_days": 0}])
    out = sc.build(feed)
    row = next(r for r in out["rows"] if r["id"] == "news")
    assert row["metric"] == "441 narrativas"
    assert row["staleness_days"] == 0


def test_registry_size_is_injected_not_guessed():
    out = sc.build(_feed(), n_entities=1824)
    row = next(r for r in out["rows"] if r["id"] == "entity_discovery")
    assert row["metric"] == "1824 entidades no registro"


def test_build_is_total_and_groups_tally():
    out = sc.build(_feed())
    assert out["n_total"] == len(sc.ROADMAP)
    assert sum(out["tally"].values()) == out["n_total"]
    assert [g["key"] for g in out["groups"]] == list(sc.GROUPS)
    assert sum(g["n"] for g in out["groups"]) == out["n_total"]


def test_empty_feed_does_not_explode():
    out = sc.build({})
    assert out["rows"] and out["n_total"] == len(sc.ROADMAP)


def test_feed_list_sources_count_their_rows():
    out = sc.build({"financials": [{"entity_id": "bb"}, {"entity_id": "itau"}]})
    row = next(r for r in out["rows"] if r["id"] == "cvm_financials")
    assert row["metric"] == "2 registros" and row["status"] == "live"


def test_an_empty_feed_list_is_also_a_defect():
    """#145: cvm_financials was switched off in the deployed stack while the docs all
    said it shipped. Zero rows must read as broken."""
    out = sc.build({"financials": []})
    row = next(r for r in out["rows"] if r["id"] == "cvm_financials")
    assert row["status"] == "silent"


def test_lens_freshness_never_masks_a_stalled_ingester():
    """#76's whole reason for existing: the lens proxy measures how fresh a source's
    NARRATIVES are, so a source that stopped running looks fresh while its old
    narratives sit in the window. Trade press was 0 days stale by lens and 11 days by
    telemetry. The telemetry must win."""
    feed = _feed(health=[{"lens": "news", "docs": 442, "staleness_days": 0}],
                 runs=[{"source": "Trade press", "band": "stale", "staleness_days": 11}])
    out = sc.build(feed)
    row = next(r for r in out["rows"] if r["id"] == "news")
    assert row["staleness_days"] == 11
    assert row["band"] == "stale"


def test_a_gated_source_is_not_demoted_to_silent_by_stale_telemetry():
    """`Receita bulk CNAE` is switched off (ONCA_INGEST_RECEITA_BULK=false), so its
    last_error is a historical record that can never clear — it must not render as a
    live incident."""
    feed = _feed(runs=[{"source": "Receita bulk CNAE", "band": "error",
                        "staleness_days": None}])
    out = sc.build(feed)
    row = next(r for r in out["rows"] if r["id"] == "receita_cnae")
    assert row["status"] == "gated"      # not "silent" — it is off, not broken
    assert row["band"] == "error"        # the history is still recorded, just not alarming
