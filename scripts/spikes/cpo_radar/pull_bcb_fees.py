"""BCB bank-fee tables pull (#158 spike).

Live endpoint established 2026-09-26 (olinda OData, keyless, HTTP 200):
  https://olinda.bcb.gov.br/olinda/servico/Informes_ListaTarifasPorInstituicaoFinanceira/versao/v1/odata/
  ListaTarifasPorInstituicaoFinanceira(PessoaFisicaOuJuridica=@p,CNPJ=@c)
Each row carries `DataVigencia` (effective date) -> a fee row whose DataVigencia falls in the
window is a dated fee change. Pulls PF and PJ. Output: out/bcb_fees.json (all rows, flagged).
"""
from __future__ import annotations

import common as c

BASE = ("https://olinda.bcb.gov.br/olinda/servico/Informes_ListaTarifasPorInstituicaoFinanceira/"
        "versao/v1/odata/ListaTarifasPorInstituicaoFinanceira(PessoaFisicaOuJuridica=@p,CNPJ=@c)")


def _date(v: str) -> str:
    """DataVigencia is a string; normalise dd/mm/yyyy or yyyy-mm-dd to ISO."""
    v = (v or "").strip()
    if len(v) >= 10 and v[2] == "/" and v[5] == "/":
        return f"{v[6:10]}-{v[3:5]}-{v[0:2]}"
    return v[:10]


def main() -> None:
    rows = []
    for s in c.subjects():
        for inst in s["fee_table_cnpjs"]:
            for pf in ("F", "J"):
                url = f"{BASE}?@p='{pf}'&@c='{inst['cnpj']}'&$format=json"
                try:
                    vals = c.get_json(url, pace=1.0, timeout=90).get("value", [])
                except RuntimeError as e:
                    print(s["id"], inst["cnpj"], pf, "ERR", e)
                    continue
                for v in vals:
                    d = _date(v.get("DataVigencia"))
                    rows.append({"product": s["id"], "source": "bcb_fees", "cnpj": inst["cnpj"],
                                 "institution": inst["name"], "pessoa": pf, "date": d,
                                 "in_window": c.in_window(d), "url": url, "fetched_at": c.dt_now(), **v})
                n_in = sum(1 for r in rows if r["cnpj"] == inst["cnpj"] and r["pessoa"] == pf and r["in_window"])
                print(s["id"], inst["cnpj"], pf, "rows", len(vals), "in_window", n_in)
    c.dump("bcb_fees.json", rows)


if __name__ == "__main__":
    main()
