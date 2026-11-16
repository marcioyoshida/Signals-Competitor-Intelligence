"""2026-11-16 feed check: four ways today's acts reached the boards wrongly.

BCB Res. 605 (the BCB staff health plan) became a sector-wide card; a re-layout of its BCB page
read as "modifica Preâmbulo, Art. 1"; IN BCB 788 fused with an unrelated SPA/MF act; and
rule Threats were listed under Estratégia's Oportunidades.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import federal_acts
from src.synth import candidates as cand
from src.synth import reg_diff
from src.synth import executive

RES_605 = {"id": "bcb:Resolução BCB:605", "kind": "regulatory", "source": "BCB",
           "doc_type": "Resolução BCB", "number": "605",
           "subject": "Altera o Regulamento do Programa de Assistência à Saúde dos Servidores do "
                      "Banco Central – PASBC, anexo à Resolução BCB nº 415, de 24 de setembro de 2024."}
IN_788 = {"id": "bcb:Instrução Normativa BCB:788", "kind": "regulatory", "source": "BCB",
          "_lens": "regulatory", "is_new": True, "doc_type": "Instrução Normativa BCB", "number": "788",
          "subject": "Altera a Instrução Normativa nº 374, de 26 de abril de 2023, que divulga "
                     "procedimentos, prazos, documentos e informações necessários para a instrução "
                     "de pedidos de autorização relacionados aos Sistemas de Mercado Financeiro"}
SPA_32 = {"id": "dou:instrucao-normativa-spa/mf-n-32", "kind": "regulatory", "source": "DOU",
          "_lens": "dou", "is_new": True,
          "title": "INSTRUÇÃO NORMATIVA SPA/MF Nº 32, DE 6 DE NOVEMBRO DE 2026",
          "text": "Altera a Instrução Normativa que divulga procedimentos, prazos, documentos e "
                  "informações necessários para pedidos de autorização de apostas"}


def test_regulator_staff_health_plan_is_internal_and_gets_no_card():
    rec = dict(RES_605)
    federal_acts.annotate([rec])
    assert rec["internal_admin"] and rec["severity"] == "low" and not rec.get("industries")
    assert rec["severity_reason"] == "internal administrative act"
    digest = {"regulatory": {"items": [{**rec, "url": "https://www.bcb.gov.br/605", "is_new": True}],
                             "context": []}}
    assert [c for c in cand.extract_candidates(digest, max_candidates=10)
            if c["seed"]["id"] == rec["id"]] == []


def test_regimento_interno_as_legal_basis_is_not_internal():
    # every BCB resolution cites the Regimento Interno as its legal basis in the preamble
    rec = {"kind": "regulatory", "source": "BCB", "doc_type": "Resolução BCB", "number": "601",
           "subject": "Dispõe sobre a prestação de serviços de pagamento",
           "text": "A Diretoria Colegiada do Banco Central do Brasil, com fundamento no art. 11 do "
                   "Regimento Interno do Banco Central do Brasil, R E S O L V E: Art. 1º ..."}
    federal_acts.annotate([rec])
    assert not rec.get("internal_admin")


def test_two_different_acts_never_fuse_but_one_act_from_two_sources_does():
    assert cand._soft_related(dict(IN_788), [dict(SPA_32)], set()) == []
    dou_788 = {"id": "dou:in-bcb-788", "kind": "regulatory", "_lens": "dou", "is_new": True,
               "title": "INSTRUÇÃO NORMATIVA BCB Nº 788, DE 13 DE NOVEMBRO DE 2026",
               "text": IN_788["subject"]}
    assert [r["id"] for r in cand._soft_related(dict(IN_788), [dou_788], set())] == ["dou:in-bcb-788"]


def test_relayout_of_the_same_act_is_not_a_change():
    old = ("A Diretoria Colegiada do Banco Central do Brasil, no uso de suas atribuições, "
           "R E S O L V E :\nArt. 1º O Regulamento passa a vigorar com as seguintes alterações:\n"
           "7º ......................\nI - os filhos\nArt. 2º Esta Resolução entra em vigor.")
    new = ("RESOLUÇÃO BCB Nº 605, DE 13 DE NOVEMBRO DE 2026\nAltera o Regulamento do PASBC.\n"
           "A\nDiretoria Colegiada do Banco Central do Brasil, no uso de suas atribuições,\nR E S\nO L V E :\n"
           "Art. 1º O Regulamento passa\na vigorar com as seguintes alterações:\n"
           "7º ..........................................................\nI - os\nfilhos\n"
           "Art. 2º Esta Resolução entra em vigor.")
    d = reg_diff.diff_versions(old, new)
    assert d["summary"]["modified"] == 0 and reg_diff.summarize_diff(d) == "sem alteração estrutural"
    changed = new.replace("os\nfilhos", "os netos")
    assert reg_diff.diff_versions(old, changed)["modified"] == ["art-1"]


def test_rule_threat_is_not_an_opportunity_but_a_rival_setback_is():
    rule = {"id": "regulatory-rn-ans-683", "entities": [],
            "swot_hint": {"dimension": "T", "sign": "-", "instrument": "rn-ans-683"}}
    rival = {"id": "cand-ent-x", "entity": "x", "swot_hint": {"dimension": "W", "sign": "-"}}
    opp = {"id": "o", "swot_hint": {"dimension": "O", "sign": "+"}}
    assert not executive._is_opportunity(rule)
    assert executive._is_opportunity(rival) and executive._is_opportunity(opp)
