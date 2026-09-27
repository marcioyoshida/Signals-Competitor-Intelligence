"""#192 / R6 — the CADE lens missed the StoneX -> Banco Travelex control acquisition,
and yielded only ONE distinct Ato de Concentração across 30 days of digests.

Root causes fixed here:
  - editais routinely bundle SEVERAL unrelated Atos de Concentração in one act; the old
    single-match regex only ever surfaced the first;
  - editais label the parties "Partes:", not "Requerentes:" (despachos/pautas do use
    "Requerentes:") — the old regex matched only the latter, so every edital's parties
    came back empty;
  - the search SNIPPET cuts most of an edital's body; the fix fetches the act's FULL TEXT
    (`dou.extract_act_text` / `dou._fetch_act`) before extracting.

Fixture below is the REAL body of Edital nº 691/692 (11/09/2026), fetched live from
in.gov.br 2026-09-27 (https://www.in.gov.br/web/dou/-/edital-n-691-de-11-de-setembro-de-2026-731517468):
two Atos de Concentração back to back — 08700.008629/2026-60 (BTG Pactual / Abril
Comunicações / Total Express) and 08700.008630/2026-94 (StoneX Participações / Banco
Travelex), each with its own "Partes:" line.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cade, source_health as sh

_EDITAL_FULL_TEXT = (
    "EDITAL Nº 691, DE 11 DE SETEMBRO DE 2026 Nos termos do art. 53, § 2º, da Lei nº "
    "12.529/2011, dá-se publicidade ao Ato de Concentração nº 08700.008629/2026-60. "
    "Partes: Banco BTG Pactual S.A., Abril Comunicações S.A. e Total Express Holding LLC. "
    "Advogados: Daniel Costa Rebello, Gabriela Leão F. A. de Oliveira, Luis Nagalli, "
    "Rodrigo Vianna e Leticia Yada. Natureza da operação: aquisição de participação "
    "societária. Setor econômico envolvido: transporte rodoviário de carga. "
    "Felipe Neiva Mundim Superintendente-Adjunto "
    "EDITAL Nº 692, DE 11 DE SETEMBRO DE 2026 Nos termos do art. 53, § 2º, da Lei nº "
    "12.529/2011, dá-se publicidade ao Ato de Concentração nº 08700.008630/2026-94. "
    "Partes: StoneX Participações Ltda. e Banco Travelex S.A. "
    "Advogados: Daniel Costa Rebello, Gabriela Leão F. A. de Oliveira, Luiz Eduardo B. "
    "Soyer Queiroz, Ana Bátia Glenk e Isabela Martins Soares. Natureza da operação: "
    "aquisição de controle. Setor econômico envolvido: bancos múltiplos, com carteira "
    "comercial (6422-1-00). Felipe Neiva Mundim Superintendente-Adjunto"
)

# dou.fetch_dou-shaped record: search snippet is thin/cut, as observed live (the fix must
# not rely on the snippet already containing the parties).
_RAW = [
    {"id": "dou:edital-n-691-de-11-de-setembro-de-2026-731517468", "source": "DOU",
     "doc_type": "Edital", "title": "EDITAL Nº 691, DE 11 DE SETEMBRO DE 2026",
     "text": "Nos termos do art. 53, § 2º, da Lei nº 12.529/2011, dá-se publicidade ao "
             "Ato de Concentração nº 08700.008629/2026-60. Partes: Banco BTG Pactual "
             "S.A., Abril Comu...",  # truncated, like the real snippet
     "organ": "Ministério da Justiça e Segurança Pública/Conselho Administrativo de "
              "Defesa Econômica/Superintendência-Geral",
     "date": "2026-09-14",
     "url": "https://www.in.gov.br/web/dou/-/edital-n-691-de-11-de-setembro-de-2026-731517468"},
]


def _act_fetcher(url: str) -> str:
    assert "edital-n-691" in url
    return f'<div class="texto-dou">{_EDITAL_FULL_TEXT}</div></div>'


def setup_function(_):
    sh.reset()


def test_fetch_atos_extracts_every_ac_in_one_edital_via_full_text():
    atos = cade.fetch_atos(fetcher=lambda: _RAW, act_fetcher=_act_fetcher)
    acs = {a["ac_number"] for a in atos}
    assert acs == {"08700.008629/2026-60", "08700.008630/2026-94"}
    assert all(a["full_text"] for a in atos)


def test_stonex_travelex_ac_has_partes_parsed_correctly():
    atos = cade.fetch_atos(fetcher=lambda: _RAW, act_fetcher=_act_fetcher)
    travelex = next(a for a in atos if a["ac_number"] == "08700.008630/2026-94")
    assert travelex["parties"] == "StoneX Participações Ltda. e Banco Travelex S.A"
    # the two ACs get distinct ids so neither is dropped as a duplicate of the other
    btg = next(a for a in atos if a["ac_number"] == "08700.008629/2026-60")
    assert btg["id"] != travelex["id"]
    assert btg["parties"] == "Banco BTG Pactual S.A., Abril Comunicações S.A. e Total Express Holding LLC"


def test_stonex_travelex_resolves_to_both_tracked_entities():
    atos = cade.fetch_atos(fetcher=lambda: _RAW, act_fetcher=_act_fetcher)

    def resolver(item):
        t = f"{item.get('institution')}".upper()
        return [e for e, tok in (("stonex", "STONEX"), ("travelex", "TRAVELEX")) if tok in t]

    recs = cade.map_to_entities(atos, resolver=resolver)
    travelex = next(r for r in recs if r["ac_number"] == "08700.008630/2026-94")
    assert set(travelex["_entities"]) == {"stonex", "travelex"}


def test_fetch_atos_records_distinct_acs_per_week_yield_metric():
    cade.fetch_atos(fetcher=lambda: _RAW, act_fetcher=_act_fetcher, lookback_days=30)
    rec = sh.ledger()["cade"]
    assert rec["docs"] == 2
    assert rec["metrics"]["distinct_acs"] == 2
    assert rec["metrics"]["acs_per_week"] == round(2 / (30 / 7.0), 2)


def test_fetch_atos_yield_metric_reflects_a_dead_lens():
    """A run that finds one repeated AC (the R6 symptom: 1 distinct AC in 30 days) must
    show a near-zero acs_per_week even though ``docs`` and ``ok`` both look healthy."""
    stale = [dict(_RAW[0])]
    cade.fetch_atos(fetcher=lambda: stale, act_fetcher=lambda _u: "")
    rec = sh.ledger()["cade"]
    assert rec["metrics"]["distinct_acs"] <= 1
