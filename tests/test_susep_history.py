"""#390 SUSEP financial history per insurer: premiums, claims, solvency, provisions."""
from src.ingest import susep_history as su

SEG_HDR = ("damesano;coenti;cogrupo;coramo;premio_direto;premio_de_seguros;premio_retido;premio_ganho;"
           "sinistro_direto;sinistro_retido;desp_com;premio_emitido2;premio_emitido_cap;despesa_resseguros;"
           "sinistro_ocorrido;receita_resseguro;sinistros_ocorridos_cap;recuperacao_sinistros_ocorridos_cap;"
           "rvne;conveniodpvat;consorciosefundos")


def seg(m, code, ramo, direto, ganho, sin_ret, desp, sin_oc):
    return (f"{m};{code};00051;{ramo};{direto};{direto};0;{ganho};0;{sin_ret};{desp};0;0;0;{sin_oc};"
            "0;0;0;0;0;0")


SEGUROS = "\n".join([SEG_HDR,
    # 2013 Q4: claims in sinistro_retido until 2013-11, sinistro_ocorrido from 2013-12
    seg(201310, "05886", "0531", "100000000", "80000000", "40000000", "10000000", "0"),
    seg(201311, "05886", "0531", "100000000", "80000000", "40000000", "10000000", "0"),
    seg(201312, "05886", "0531", "100000000", "80000000", "0", "10000000", "40000000"),
    # 2014 Q1: claims in sinistro_ocorrido, two lines of business summed
    seg(201401, "05886", "0531", "50000000", "40000000", "0", "5000000", "20000000"),
    seg(201401, "05886", "0114", "50000000", "40000000", "0", "5000000", "28000000,5"),
    seg(201402, "05886", "0531", "100000000", "80000000", "0", "10000000", "48000000"),
    seg(201403, "05886", "0531", "100000000", "80000000", "0", "10000000", "48000000"),
    seg(201403, "09999", "0531", "1000", "500", "0", "0", "9000"),        # tiny book
]).encode("latin-1")

CAP = ("coenti;damesano;plajustado;margem;pl;AjustesContabeis;AjustesEconomicos;NovoPla;CMR\n"
       "05886;201402;0;0;1;0;0;0;0\n"
       "05886;201403;2000000000;0;5000000000;0;0;2400000000;2000000000\n").encode("latin-1")
PROV = "coenti;damesano;valor\n05886;201403;9000000000\n".encode("latin-1")

IMAP = {"quarters": [201312, 201403], "codes": {
    "05886": [{"from": 201312, "to": 201312, "name": "PORTO GERAIS", "cnpj_root": "61198164",
               "entity_id": "porto_seguro", "group_entity_id": "porto_seguro"},
              {"from": 201403, "to": 201403, "name": "PORTO GERAIS", "cnpj_root": "61198164",
               "entity_id": "porto_seguro", "group_entity_id": "newowner"}],
    "09999": [{"from": 201403, "to": 201403, "name": "TINY", "cnpj_root": None,
               "entity_id": None, "group_entity_id": "newowner"}]}}


def build():
    seg_ = su.parse_seguros(SEGUROS)
    cap = su.parse_stock(CAP, ("pl", "plajustado", "NovoPla", "CMR"))
    prov = su.parse_stock(PROV, ("valor",))
    return su.company_points(seg_, cap, prov, IMAP)


def test_quarters_and_basis():
    assert su.quarter_of_month(201401) == 201403 and su.quarter_of_month(201312) == 201312
    assert su.basis_for(201311) == "fip_pre2014" and su.basis_for(201312) == "fip_2014"
    assert su.quarter_basis([201310, 201311, 201312]) == "mixed"


def test_claims_column_follows_the_2014_break_and_flows_are_summed():
    pts = {(p["susep_code"], p["as_of"]): p for p in build()}
    q4 = pts[("05886", 201312)]
    assert q4["sinistros_mi"] == 120.0 and q4["premio_ganho_mi"] == 240.0
    assert q4["sinistralidade_pct"] == 50.0 and q4["comissionamento_pct"] == 12.5
    assert q4["basis"] == "mixed"
    q1 = pts[("05886", 201403)]
    assert q1["months"] == 3 and q1["sinistros_mi"] == 144.0 and q1["premio_ganho_mi"] == 240.0
    assert q1["sinistralidade_pct"] == 60.0 and q1["basis"] == "fip_2014"


def test_stocks_are_the_quarter_end_and_solvency_uses_the_new_pla():
    q1 = {(p["susep_code"], p["as_of"]): p for p in build()}[("05886", 201403)]
    assert (q1["pla_mi"], q1["cmr_mi"], q1["suficiencia_pct"]) == (2400.0, 2000.0, 120.0)
    assert q1["pl_mi"] == 5000.0 and q1["provisoes_mi"] == 9000.0


def test_attribution_is_by_date_through_the_insurer_map():
    pts = {(p["susep_code"], p["as_of"]): p for p in build()}
    assert pts[("05886", 201312)]["entity_id"] == "porto_seguro"
    assert pts[("05886", 201403)]["entity_id"] == "newowner"
    assert pts[("05886", 201403)]["direct_entity_id"] == "porto_seguro"


def test_group_sums_recompute_ratios_and_guards_blank_thin_books():
    s = su.build_series(build(), "t")
    g = s["groups"]["newowner"][0]
    assert g["companies"] == ["05886", "09999"] and g["premio_ganho_mi"] == 240.0005
    assert g["sinistros_mi"] == 144.009
    assert su.series_for(s, "porto_seguro")[0]["as_of"] == 201312
    tiny = [p for p in s["companies"]["09999"]][0]
    assert tiny["sinistralidade_pct"] == 1800.0 and su.guard(tiny)["sinistralidade_pct"] is None
    assert su.series_for(s, "porto_seguro", level="company")[-1]["suficiencia_pct"] == 120.0


def test_complete_through_drops_a_partial_newest_month():
    assert su.complete_through({202607: 100, 202608: 100, 202609: 80}) == 202608
    assert su.complete_through({202608: 100, 202609: 99}) == 202609
    assert len(su.parse_seguros(SEGUROS, last_month=201312)) == 1
