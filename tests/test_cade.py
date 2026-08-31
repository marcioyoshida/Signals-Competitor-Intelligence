"""CADE antitrust / merger-review ingester (#61)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cade

# DOU acts as dou.fetch_dou returns them (text carries highlight <span> tags to strip).
_RAW = [
    {"id": "dou:pauta-271", "source": "DOU", "doc_type": "Pauta",
     "title": "PAUTA DA 271ª SESSÃO ORDINÁRIA",
     "text": "<span class='highlight'>Ato</span> de Concentração nº 08700.012323/2025-27 "
             "Requerentes: B3 S.A. e Neurotech. Procedimento Administrativo ...",
     "organ": "Ministério da Justiça/CADE/Tribunal", "date": "2026-08-27",
     "url": "https://in.gov.br/web/dou/-/pauta-271"},
    {"id": "dou:despacho-1128", "source": "DOU", "doc_type": "Despacho",
     "title": "DESPACHO SG Nº 1.128",
     "text": "Ato de Concentração nº 08700.007688/2026-11. Aprovado sem restrições.",
     "organ": "Ministério da Justiça/CADE/Superintendência-Geral", "date": "2026-08-27",
     "url": "https://in.gov.br/web/dou/-/despacho-1128"},
    # a non-merger CADE act (no "concentra") — dropped by fetch_atos
    {"id": "dou:edital-x", "source": "DOU", "doc_type": "Edital",
     "title": "EDITAL DE INTIMAÇÃO", "text": "Processo administrativo sancionador.",
     "organ": "CADE", "date": "2026-08-25", "url": "u"},
]


def test_clean_ac_number_and_parties():
    blob = cade._clean("<span class='x'>Ato</span> de Concentração nº 08700.012323/2025-27 "
                       "Requerentes: B3 S.A. e Neurotech. Advogados: ...")
    assert "<span" not in blob
    assert cade._ac_number(blob) == "08700.012323/2025-27"
    assert cade._parties(blob) == "B3 S.A. e Neurotech"


def test_fetch_atos_keeps_only_mergers_and_extracts_fields():
    atos = cade.fetch_atos(fetcher=lambda: _RAW)
    assert len(atos) == 2                              # the non-merger edital is dropped
    a = next(a for a in atos if a["ac_number"] == "08700.012323/2025-27")
    assert a["id"] == "cade:pauta-271" and a["kind"] == "antitrust"
    assert a["parties"].startswith("B3 S.A.")
    assert "<span" not in a["text"]


def test_map_resolves_parties_by_name_and_stamps_all_entities():
    atos = cade.fetch_atos(fetcher=lambda: _RAW)

    def resolver(item):
        t = f"{item.get('title')} {item.get('text')} {item.get('institution')}".upper()
        return [e for e, tok in (("b3", "B3 S.A"), ("neurotech", "NEUROTECH")) if tok in t]

    recs = cade.map_to_entities(atos, resolver=resolver)
    merger = next(r for r in recs if r["ac_number"] == "08700.012323/2025-27")
    assert merger["id"] == "antitrust:cade:pauta-271"
    assert merger["entity"] == "b3" and set(merger["_entities"]) == {"b3", "neurotech"}


def test_map_drops_acts_with_no_tracked_party():
    atos = cade.fetch_atos(fetcher=lambda: _RAW)
    assert cade.map_to_entities(atos, resolver=lambda item: []) == []


def test_summarize_counts_all_parties():
    atos = cade.fetch_atos(fetcher=lambda: _RAW)
    recs = cade.map_to_entities(
        atos, resolver=lambda item: ["b3", "neurotech"]
        if "012323" in (item.get("text") or "") else [])
    s = cade.summarize(recs)
    assert s["total"] == 1 and s["entities"] == 2
    assert "08700.012323/2025-27" in s["acs"]
