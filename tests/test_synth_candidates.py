import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth.candidates import extract_candidates


def test_inf_diario_drops_subfloor_aum_even_when_new():
    """Near-zero / negative-AUM funds yield meaningless |pct_change|>100% and
    must be filtered by the absolute-PL floor regardless of is_new."""
    digest = {
        "inf_diario_moves": {
            "items": [
                {
                    "id": "cvm-inf:shell",
                    "fund_name": "ITAÚ SMALL CAP VALUATION II",
                    "admin": "ITAU UNIBANCO S.A.",
                    "pl": -34210.63,
                    "pct_change": -104.51,
                    "url": "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario",
                    "is_new": True,
                },
                {
                    "id": "cvm-inf:real",
                    "fund_name": "ITAÚ SOBERANO RF",
                    "admin": "ITAU UNIBANCO S.A.",
                    "pl": 5e10,
                    "pct_change": 2.1,
                    "url": "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario",
                    "is_new": True,
                },
            ],
            "context": [],
        },
    }
    cands = extract_candidates(digest, max_candidates=10, min_lenses=1, min_score=0.0)
    src_ids = {s.get("id") for c in cands for s in (c.get("sources") or [])}
    assert "cvm-inf:shell" not in src_ids
    assert "cvm-inf:real" in src_ids


def test_extract_regulatory_and_competitor_candidates():
    digest = {
        "regulatory": {
            "items": [
                {
                    "id": "bcb:1",
                    "doc_type": "Resolução",
                    "subject": "Pix payment institution rules",
                    "url": "https://www.bcb.gov.br/r1",
                    "is_new": True,
                }
            ],
            "context": [],
        },
        "ofertas": {
            "items": [],
            "context": [
                {
                    "id": "of:1",
                    "issuer": "Demo payment company",
                    "security": "Debêntures",
                    "url": "https://dados.cvm.gov.br/o1",
                }
            ],
        },
        "pix_moves": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=5)
    assert cands
    assert cands[0]["sources"]


def test_entity_fusion_from_context_when_items_empty():
    """Context-only fusion still works when emit-on-change is opted out.

    (By default require_change suppresses steady-state context restatements —
    see test_change_only_drops_steady_state_cluster — but the fusion capability
    itself must still function when a caller opts out.)"""
    digest = {
        "regulatory": {"items": [], "context": [], "count": 49, "new_count": 0},
        "sec_filings": {
            "items": [],
            "context": [
                {
                    "id": "sec:nu:1",
                    "ticker": "NU",
                    "form": "6-K",
                    "company": "Nu Holdings Ltd.",
                    "url": "https://www.sec.gov/nu",
                    "source": "SEC-EDGAR",
                },
                {
                    "id": "sec:nu:pix",
                    "ticker": "NU",
                    "form": "6-K",
                    "company": "Nu Holdings Ltd.",
                    "url": "https://www.sec.gov/nu2",
                    "source": "SEC-EDGAR",
                },
            ],
            "count": 117,
            "new_count": 0,
        },
        "ofertas": {
            "items": [],
            "context": [
                {
                    "id": "of:btg:1",
                    "issuer": "Some Issuer SA",
                    "leader": "BTG PACTUAL INVESTMENT BANKING",
                    "security": "Debêntures",
                    "url": "https://dados.cvm.gov.br/of",
                    "source": "CVM-Ofertas",
                }
            ],
            "count": 91,
            "new_count": 0,
        },
        "market": {
            "items": [
                {"institution": "ITAU", "value": 1e12, "share_pct": 14.8},
                {"institution": "NU PAGAMENTOS", "value": 1e11, "share_pct": 2.0},
            ]
        },
        "inf_diario_moves": {
            "items": [],
            "context": [
                {
                    "id": "cvm-inf:1:2026-07-16",
                    "fund_name": "ITAÚ SOBERANO RF",
                    "admin": "ITAU UNIBANCO S.A.",
                    "pl": 5e10,
                    "url": "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario",
                    "cnpj": "123",
                }
            ],
            "as_of": "2026-07-16",
        },
        "pix_moves": {
            "items": [],
            "context": [
                {
                    "id": "pix:nu",
                    "institution": "NU PAGAMENTOS - IP",
                    "ispb": "18236120",
                    "tx_value": 1e9,
                }
            ],
        },
        "juros_moves": {"items": [], "context": []},
        "new_entrants": {"items": [], "context": []},
        "competitor": {"items": [], "context": []},
    }
    cands = extract_candidates(
        digest, max_candidates=10, min_lenses=2, min_score=0.4, require_change=False
    )
    assert cands, "context-only multi-lens digest must yield candidates"
    # Quality bar: multi-lens entity fusions
    assert any(c.get("kind") == "entity_fusion" for c in cands)
    assert all(len(c.get("lenses") or []) >= 2 or c.get("is_alert") for c in cands)
    entities = {e for c in cands for e in (c.get("entities") or [])}
    assert entities & {"itau", "btg", "nubank"}
    assert any(c.get("as_of") == "2026-07-16" for c in cands)
    # And by default (emit-on-change) this steady-state digest yields nothing.
    assert extract_candidates(digest, max_candidates=10, min_lenses=2, min_score=0.4) == []


def test_quality_gate_drops_single_lens_context_noise():
    digest = {
        "sec_filings": {
            "context": [
                {
                    "id": "sec:only",
                    "ticker": "XP",
                    "form": "6-K",
                    "company": "XP Inc",
                    "url": "https://sec/xp",
                }
            ]
        },
        "market": {"items": []},
        "ofertas": {"context": []},
        "regulatory": {"items": [], "context": []},
        "pix_moves": {"items": [], "context": []},
        "juros_moves": {"items": [], "context": []},
        "inf_diario_moves": {"items": [], "context": []},
        "competitor": {"items": [], "context": []},
        "new_entrants": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=10, min_lenses=2, min_score=0.45)
    # Single-lens non-alert context should not pass the quality gate
    assert cands == []


def test_max_candidates_cap():
    digest = {
        "regulatory": {
            "items": [
                {
                    "id": f"bcb:{i}",
                    "subject": f"topic unique{i}zzzz",
                    "url": f"https://x/{i}",
                    "is_new": True,
                }
                for i in range(20)
            ]
        }
    }
    cands = extract_candidates(digest, max_candidates=3)
    assert len(cands) == 3


def test_multi_lens_entity_scores_higher_than_single():
    digest = {
        "sec_filings": {
            "context": [
                {
                    "id": "sec:stne",
                    "ticker": "STNE",
                    "form": "6-K",
                    "company": "StoneCo",
                    "url": "https://sec/stne",
                    "is_new": True,
                }
            ]
        },
        "market": {
            "items": [
                # won't match stone
                {"institution": "ITAU", "share_pct": 10, "value": 1}
            ]
        },
        "ofertas": {
            "context": [
                {
                    "id": "of:stone",
                    "issuer": "Stone Sociedade de Crédito",
                    "leader": "XP INVESTIMENTOS",
                    "security": "Debêntures",
                    "url": "https://cvm/stone",
                }
            ]
        },
    }
    cands = extract_candidates(digest, max_candidates=5)
    stone = next((c for c in cands if c.get("entity") == "stone" or "stone" in (c.get("entities") or [])), None)
    assert stone is not None
    assert len(stone.get("lenses") or []) >= 2
    assert stone["threat_score"] >= 0.4


def _steady_btg_digest():
    """A 2-lens BTG cluster built purely from steady-state context (no is_new)."""
    return {
        "ofertas": {"items": [], "context": [
            {"id": "of1", "issuer": "BTG PACTUAL", "security": "Debêntures",
             "leader": "BTG PACTUAL", "url": "https://dados.cvm.gov.br/o"}]},
        "inf_diario_moves": {"items": [], "context": [
            {"id": "inf1", "fund_name": "BTG PACTUAL TESOURO SELIC",
             "admin": "BTG PACTUAL", "pl": 5e10, "url": "https://dados.cvm.gov.br/i"}]},
    }


def test_change_only_drops_steady_state_cluster():
    d = _steady_btg_digest()
    # min_score=0 isolates the change-gate from the (now stricter) score threshold
    assert not any(
        c["kind"] == "entity_fusion"
        for c in extract_candidates(d, require_change=True, min_score=0.0)
    )
    # opting out keeps the steady-state cluster (old behavior)
    assert any(
        c["kind"] == "entity_fusion"
        for c in extract_candidates(d, require_change=False, min_score=0.0)
    )


def test_change_only_keeps_changed_cluster():
    d = _steady_btg_digest()
    # a genuine new offering (is_new) for the same entity -> emitted even change-only
    d["ofertas"]["items"] = [
        {"id": "of-new", "issuer": "BTG PACTUAL", "security": "Debêntures",
         "leader": "BTG PACTUAL", "url": "https://dados.cvm.gov.br/new", "is_new": True}
    ]
    assert any(
        c["kind"] == "entity_fusion"
        for c in extract_candidates(d, require_change=True, min_score=0.0)
    )


def test_threat_score_model_ranks_and_spreads():
    """Real scoring: strategic+novel+big-move outranks routine; bounded, explainable."""
    from src.synth.candidates import _score_threat

    routine, rf = _score_threat([
        {"_lens": "funds", "id": "f1"},  # steady routine filing, no is_new/move
    ])
    strategic, sf = _score_threat([
        {"_lens": "regulatory", "id": "r1", "is_new": True},
        {"_lens": "sec", "id": "s1", "is_new": True},
        {"_lens": "inf_diario", "id": "i1", "is_new": True, "pct_change": 45},
    ])
    assert 0.0 <= routine <= 1.0 and 0.0 <= strategic <= 1.0
    assert strategic > routine + 0.2            # meaningfully separated, not saturated
    assert strategic < 1.0                      # weighted blend doesn't peg at 1.0
    # factor breakdown is present and reflects inputs
    assert set(sf) == {"signal", "magnitude", "novelty", "breadth"}
    assert sf["signal"] == 1.0                  # regulatory present
    assert sf["novelty"] == 1.0 and rf["novelty"] == 0.3
    assert sf["magnitude"] > 0                  # the 45% move registers


def test_threat_score_magnitude_scales_with_move():
    from src.synth.candidates import _score_threat

    small, _ = _score_threat([{"_lens": "inf_diario", "id": "a", "is_new": True, "pct_change": 5}])
    big, _ = _score_threat([{"_lens": "inf_diario", "id": "b", "is_new": True, "pct_change": 80}])
    assert big > small


def test_entrant_license_class_drives_score():
    """A new credit fintech (SCD) outscores a new consórcio — same lens, diff class."""
    from src.synth.candidates import _score_threat

    scd, sf = _score_threat([
        {"_lens": "entrants", "id": "e1", "is_new": True,
         "license_class": "Crédito Direto (SCD)"},
    ])
    consorcio, cf = _score_threat([
        {"_lens": "entrants", "id": "e2", "is_new": True,
         "license_class": "Consórcio"},
    ])
    assert scd > consorcio + 0.2
    assert sf["signal"] >= 0.9 and cf["signal"] <= 0.3


def test_entrant_scd_beats_consorcio_end_to_end():
    scd = extract_candidates({
        "new_entrants": {"items": [{
            "id": "bcb-auth:1", "name": "NOVA SCD LTDA",
            "license_class": "Crédito Direto (SCD)", "entity_type": "Sociedade de Crédito Direto",
            "is_new": True}], "context": []}},
        max_candidates=5, min_lenses=1, min_score=0.0)
    con = extract_candidates({
        "new_entrants": {"items": [{
            "id": "bcb-auth:2", "name": "NOVO CONSORCIO SA",
            "license_class": "Consórcio", "entity_type": "Administradora de Consórcio",
            "is_new": True}], "context": []}},
        max_candidates=5, min_lenses=1, min_score=0.0)
    assert scd and con
    assert scd[0]["threat_score"] > con[0]["threat_score"]


def test_fatos_material_fact_high_value_and_resolves_entity():
    from src.synth.synthesize import _describe_signal
    d = {"fatos": {"items": [{
        "id": "cvm-fato:P1", "company": "BANCO BRADESCO S.A.", "name": "BANCO BRADESCO S.A.",
        "doc_type": "Fato Relevante", "subject": "Aquisição de participação relevante",
        "date": "2026-08-13", "url": "https://rad.cvm.gov.br/x", "is_new": True}],
        "context": []}}
    cands = extract_candidates(d, max_candidates=5, min_lenses=1, min_score=0.0)
    assert cands
    c = cands[0]
    assert "fatos" in c["lenses"]
    assert "bradesco" in (c.get("entities") or [])
    assert c["threat_score"] >= 0.5  # strategic disclosure + novel
    # narrative uses the Fato Relevante branch, not the regulatory one
    txt = _describe_signal({**d["fatos"]["items"][0], "_lens": "fatos"})
    assert txt.startswith("Fato Relevante")  # not the "Regulatório" branch
    assert "BANCO BRADESCO" in txt and "Aquisição de participação" in txt


def test_dou_act_is_high_value_and_resolves_entity():
    from src.synth.synthesize import _describe_signal
    d = {"dou": {"items": [{
        "id": "dou:despacho-cade-941", "company": "BANCO BTG PACTUAL", "name": "BANCO BTG PACTUAL",
        "doc_type": "Despacho", "title": "DESPACHO SG Nº 941 - Ato de Concentração",
        "organ": "Ministério da Justiça/Conselho Administrativo de Defesa Econômica",
        "date": "2026-08-13", "url": "https://www.in.gov.br/web/dou/-/despacho-cade-941",
        "is_new": True}], "context": []}}
    cands = extract_candidates(d, max_candidates=5, min_lenses=1, min_score=0.0)
    assert cands
    c = cands[0]
    assert "dou" in c["lenses"]
    assert "btg" in (c.get("entities") or [])
    assert c["threat_score"] >= 0.5
    txt = _describe_signal({**d["dou"]["items"][0], "_lens": "dou"})
    assert txt.startswith("Diário Oficial") and "(CADE)" in txt


def _c6_news(title, publisher, url, *, is_new=True):
    return {
        "id": f"news:{publisher}:{title[:8]}", "source": "News", "kind": "competitor",
        "_lens": "news", "company": "C6 Bank", "name": "C6 Bank",
        "title": title, "subject": title, "publisher": publisher, "url": url,
        "is_new": is_new,
    }


def test_corroborated_news_surfaces_for_private_entity():
    # C6 is privately held (no Fato Relevante) — earnings arrive only via press.
    # Six distinct outlets on the same result => material, must surface.
    items = [
        _c6_news("C6 Bank tem lucro de R$ 1,25 bi", "NeoFeed", "https://neofeed.com.br/a"),
        _c6_news("C6 Bank ve inadimplencia subir", "InfoMoney", "https://infomoney.com.br/b"),
        _c6_news("C6 Bank receita recorde", "Valor", "https://valor.globo.com/c"),
        _c6_news("C6 Bank lucro liquido R$1,3bi", "Exame", "https://exame.com/d"),
        _c6_news("C6 Bank amplia lucro", "Brazil Journal", "https://braziljournal.com/e"),
        _c6_news("No C6 Bank, salto de 20%", "NeoFeed", "https://neofeed.com.br/f"),
    ]
    cands = extract_candidates({"news": {"items": items, "context": []}}, max_candidates=10)
    assert len(cands) == 1
    c = cands[0]
    assert c["kind"] == "news_corroborated" and c["entity"] == "c6"
    assert c["lenses"] == ["news"]
    assert c["is_alert"] is False          # news is color, never a red alert
    assert 0.3 < c["threat_score"] < 0.6   # surfaces, but below an official delta


def test_lone_promo_headline_stays_out():
    # One outlet, two promo headlines -> not corroborated -> dropped (default gate).
    items = [
        _c6_news("C6 Bank cashback em dobro", "NeoFeed", "https://neofeed.com.br/p1"),
        _c6_news("C6 Bank promo de cartao", "NeoFeed", "https://neofeed.com.br/p2"),
    ]
    assert extract_candidates({"news": {"items": items, "context": []}}, max_candidates=10) == []


def test_news_publisher_threshold_is_tunable():
    items = [
        _c6_news("C6 Bank lucro", "NeoFeed", "https://neofeed.com.br/a"),
        _c6_news("C6 Bank receita", "Valor", "https://valor.globo.com/b"),
    ]
    # default threshold (3) drops 2 outlets ...
    assert extract_candidates({"news": {"items": items, "context": []}}) == []
    # ... lowering it to 2 admits them.
    cands = extract_candidates(
        {"news": {"items": items, "context": []}}, min_news_publishers=2
    )
    assert len(cands) == 1 and cands[0]["kind"] == "news_corroborated"


def test_news_item_surfaces_but_scores_below_official():
    from src.synth.synthesize import _describe_signal
    d = {"news": {"items": [{
        "id": "news:abc", "company": "Nubank", "name": "Nubank", "publisher": "Valor Econômico",
        "title": "Nubank tem lucro recorde no trimestre", "subject": "Nubank tem lucro recorde",
        "date": "2026-08-13", "url": "https://news.google.com/x", "is_new": True}], "context": []}}
    cands = extract_candidates(d, max_candidates=5, min_lenses=1, min_score=0.0)
    assert cands and "news" in cands[0]["lenses"]
    assert "nubank" in (cands[0].get("entities") or [])
    news_score = cands[0]["threat_score"]
    # a regulatory delta for the same entity should outrank the news item
    d2 = {"regulatory": {"items": [{"id": "bcb:1", "doc_type": "Resolução",
        "subject": "Nubank Pix rule", "url": "https://bcb/1", "is_new": True}], "context": []}}
    reg_score = extract_candidates(d2, max_candidates=5, min_lenses=1, min_score=0.0)[0]["threat_score"]
    assert reg_score > news_score
    txt = _describe_signal({**d["news"]["items"][0], "_lens": "news"})
    assert txt.startswith("Imprensa") and "Valor Econômico" in txt


def test_news_only_item_is_suppressed_at_prod_defaults():
    # A lone news headline (no corroborating lens) must NOT surface as an alert
    # under prod gating (min_lenses=2) — news is color, not a solo signal.
    d = {"news": {"items": [{
        "id": "news:promo", "company": "Nomad", "name": "Nomad",
        "title": "Cartão Nomad Explorer oferece pontos em dobro",
        "subject": "Nomad Explorer pontos em dobro",
        "date": "2026-08-17", "url": "https://news.google.com/x", "is_new": True}],
        "context": []}}
    cands = extract_candidates(d, max_candidates=5)  # prod defaults
    assert not any("nomad" in (c.get("entities") or []) for c in cands)
    # but news corroborated by a real regulatory delta for the same entity surfaces
    d["regulatory"] = {"items": [{"id": "bcb:9", "doc_type": "Resolução",
        "subject": "Nomad autorizada", "url": "https://bcb/9", "is_new": True}], "context": []}
    cands2 = extract_candidates(d, max_candidates=5)
    assert any("nomad" in (c.get("entities") or []) for c in cands2)


def test_regulatory_fusion_does_not_absorb_unrelated_entity_news():
    """An entity-less regulatory card must not staple on an unrelated tracked
    entity's news via a coincidental shared token (the Oppens-cancellation card
    that pulled in Caixa Seguridade earnings as false 'color')."""
    digest = {
        "regulatory": {
            "items": [
                {
                    "id": "bcb:cancel",
                    "doc_type": "Comunicado",
                    "subject": "Pedido de cancelamento de autorizacao de sociedade de credito",
                    "url": "https://www.bcb.gov.br/x",
                    "is_new": True,
                }
            ],
            "context": [],
        },
        "news": {
            "items": [
                {
                    "id": "news:nubank",
                    "title": "Nubank amplia credito e lucro cresce no trimestre",
                    "publisher": "Valor",
                    "url": "https://valor.example/n",
                    "is_new": True,
                }
            ],
            "context": [],
        },
    }
    cands = extract_candidates(digest, max_candidates=10, min_lenses=2, min_score=0.0)
    # No entity-less card (e.g. the regulatory one) may carry the Nubank news.
    for c in cands:
        if not c.get("entities"):
            src_ids = {s.get("id") for s in (c.get("sources") or [])}
            assert "news:nubank" not in src_ids
