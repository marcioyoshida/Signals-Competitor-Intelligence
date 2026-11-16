"""#333 prudential history: IF.data / COSIF / KM1 per entity with as_of, attributed by date."""
import io
import zipfile

from src.ingest import prudential_history as ph

# HSBC Bank Brasil (01701201): HSBC's conglomerate to 2016-06, Bradesco's from 2016-09.
EMAP = {"quarters": [200003, 202606], "roots": {
    "01701201": [
        {"from": 200003, "to": 201606, "leader": "01701201", "group_entity_id": "hsbc", "entity_id": "hsbc"},
        {"from": 201609, "to": 202606, "leader": "60746948", "group_entity_id": "bradesco",
         "entity_id": "hsbc-bank"}],
    "60746948": [{"from": 200003, "to": 202606, "leader": "60746948", "group_entity_id": "bradesco",
                  "entity_id": "bradesco"}],
}}
SNAP_2016 = [{"CodInst": "C0080075", "CodConglomeradoPrudencial": "C0080075",
              "CodConglomeradoFinanceiro": "C0010045", "CnpjInstituicaoLider": "60746948"},
             {"CodInst": "01701201", "CodConglomeradoPrudencial": "C0080075",
              "CodConglomeradoFinanceiro": "C0010045", "CnpjInstituicaoLider": "60746948"}]


def resumo(code, ativo, cart, lucro, pl, capt=100.0):
    return [{"CodInst": code, "NomeColuna": n, "Saldo": v} for n, v in (
        ("Ativo Total", ativo), ("Carteira de Crédito Classificada", cart), ("Captações", capt),
        ("Lucro Líquido", lucro), ("Patrimônio Líquido", pl))]


def test_basis_switches_at_2014_03():
    assert ph.basis_for(201312) == "financeiro" and ph.basis_for(201403) == "prudencial"
    assert "@T=2" in ph.resumo_url(201312, "financeiro") and "@T=1" in ph.resumo_url(201403, "prudencial")


def test_conglomerate_codes_resolve_through_the_quarters_register():
    lead = ph.leader_roots(SNAP_2016, "prudencial")
    assert lead == {"C0080075": "60746948"}
    assert ph.leader_roots(SNAP_2016, "financeiro") == {"C0010045": "60746948"}
    assert ph.root_of("C0080075", lead) == "60746948" and ph.root_of("01701201", lead) == "01701201"
    assert ph.root_of("C9999999", lead) is None


def test_attribution_is_by_date_never_todays_owner():
    a15, a16 = ph.attribute(EMAP, "01701201", 201512), ph.attribute(EMAP, "01701201", 201609)
    assert (a15["entity_id"], a15["lead"]) == ("hsbc", True)
    assert (a16["entity_id"], a16["direct_entity_id"], a16["lead"]) == ("bradesco", "hsbc-bank", False)
    assert ph.attribute(EMAP, None, 201609)["entity_id"] is None


def test_ifdata_points_carry_the_live_ratios_and_basis():
    rows = resumo("C0080075", 1.0e12, 5.0e11, 1.5e10, 1.0e11) + resumo("01701201", 2.0e11, 1.0e11, 1.0e9, 2.0e10)
    pts = {p["code"]: p for p in ph.ifdata_points(rows, 201612, "prudencial",
                                                  ph.leader_roots(SNAP_2016, "prudencial"), EMAP)}
    b = pts["C0080075"]
    assert b["entity_id"] == "bradesco" and b["conglomerate"] and b["ativo_bi"] == 1000.0
    assert b["roe_pct"] == 15.0 and b["basis"] == "prudencial" and b["as_of"] == 201612
    assert pts["01701201"]["entity_id"] == "bradesco" and not pts["01701201"]["conglomerate"]


def test_capital_points_scale_fractions_to_percent():
    rows = [{"CodInst": "C0080075", "NomeColuna": "Índice de Basileia", "Saldo": 0.151}]
    p = ph.capital_points(rows, 201612, ph.leader_roots(SNAP_2016, "prudencial"), EMAP)[0]
    assert p["entity_id"] == "bradesco" and p["indice_basileia"] == 15.1 and p["band"]


def _cosif_zip(header, rows, *, upper=True):
    body = "Balancete\nData\nFonte\n" + header + "\n" + "\n".join(rows) + "\n"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("201412BANCOS.CSV", body.encode("latin-1"))
    return out.getvalue()


def test_cosif_parses_all_three_layouts_and_both_charts():
    y2000 = _cosif_zip("DATA;CNPJ;NOME INSTITUICAO;ATRIBUTO;DOCUMENTO;CONTA;NOME CONTA;SALDO", [
        "200012;60746948;BCO BRADESCO;L;4010;0016000001;OPERACOES DE CREDITO; 0000002000000,00",
        "200012;60746948;BCO BRADESCO;L;4010;0016900008;(-) PROVISOES;-0000000100000,00",
        "200012;60746948;BCO BRADESCO;L;4016;0016000001;OPERACOES DE CREDITO; 0000009999999,00"])
    y2014 = _cosif_zip("#DATA_BASE;DOCUMENTO;CNPJ;AGENCIA;NOME_INSTITUICAO;COD_CONGL;NOME_CONGL;TAXONOMIA;CONTA;NOME_CONTA;SALDO", [
        "201412;4010;60746948;;BCO BRADESCO;;;;41000007;DEPOSITOS;3000000,50",
        "201412;4010;60746948;;BCO BRADESCO;;;;60000002;PATRIMONIO LIQUIDO;500000,00"])
    y2025 = _cosif_zip("#DATA_BASE;DOCUMENTO;CNPJ;AGENCIA;NOME_INSTITUICAO;COD_CONGL;NOME_CONGL;TAXONOMIA;CONTA;NOME_CONTA;SALDO", [
        "202501;4010;60746948;;BCO BRADESCO;;;;1600000007;OPERACOES DE CREDITO;8000000,00",
        "202501;4010;60746948;;BCO BRADESCO;;;;1899900004;(-) PROVISAO;-40000,00",
        "202501;4010;60746948;;BCO BRADESCO;;;;16000001;NOT A NEW-CHART CODE;1,00"])
    a = ph.parse_cosif(y2000)["60746948"]
    assert a["chart"] == "legacy" and a["lines"] == {"credito": 2000.0, "pdd": 100.0}     # R$ mil, 4016 skipped
    assert ph.parse_cosif(y2014)["60746948"]["lines"] == {"depositos": 3000.0, "patrimonio_liquido": 500.0}
    c = ph.parse_cosif(y2025)["60746948"]
    assert c["chart"] == "res4966" and c["lines"]["credito"] == 8000.0 and c["lines"]["pdd"] == 40.0


def test_cosif_points_attribute_by_the_months_quarter():
    pts = ph.cosif_points({"01701201": {"name": "HSBC", "chart": "legacy", "lines": {"credito": 1.0}}}, 201511, EMAP)
    assert pts[0]["entity_id"] == "hsbc" and pts[0]["as_of"] == 201511 and pts[0]["credito"] == 1.0
    assert ph.cosif_points({"01701201": {"lines": {}}}, 201610, EMAP)[0]["entity_id"] == "bradesco"


def test_quarter_arithmetic_and_km1_reference_formats():
    assert ph.quarter_back(202503, 1) == 202412 and ph.quarter_back(202503, 4) == 202403
    assert ph.quarter_back(202512, 0) == 202512
    assert [ph.km1_quarter({"km1_trimestreReferencia": t}) for t in ("2025-3", "2025/2", "3T25", "202509")] == [
        202509, 202506, 202509, 202509]
    assert ph.km1_quarter({}) is None


def test_km1_points_unroll_five_quarters_and_drop_implausible_values():
    doc = {"km1_trimestreReferencia": "2025-3", "grupos": [{"linhas": [
        {"codigo": "km1_7", "t": 0.15, "t_1": 0.149, "t_2": 0.148, "t_3": 0.147, "t_4": 14.4},
        {"codigo": "km1_17", "t": 1.5, "t_1": 0, "t_2": None, "t_3": None, "t_4": None}]}]}
    from src.ingest import bcb_km1 as k
    if k._find_row(doc, "km1_7") is None:          # adapt to the live row-finder's layout
        doc = {"km1_trimestreReferencia": "2025-3",
               "km1_7": {"t": 0.15, "t_1": 0.149, "t_2": 0.148, "t_3": 0.147, "t_4": 14.4},
               "km1_17": {"t": 1.5, "t_1": 0}}
    pts = {p["as_of"]: p for p in ph.km1_points(doc, "60746948000112", "BRADESCO", "u", EMAP)}
    assert pts[202509]["basileia_pct"] == 15.0 and pts[202509]["lcr_pct"] == 150.0
    assert pts[202506]["lcr_pct"] is None                          # 0 = not applicable
    assert 202409 not in pts or pts[202409]["basileia_pct"] is None   # 1440% rejected
    assert all(p["entity_id"] == "bradesco" and p["reported_in"] == 202509 for p in pts.values())


def test_km1_merge_keeps_the_latest_report_of_each_quarter():
    old = {"root": "60746948", "as_of": 202506, "reported_in": 202506, "lcr_pct": 140.0}
    new = {"root": "60746948", "as_of": 202506, "reported_in": 202509, "lcr_pct": 141.0}
    assert ph.merge_km1([new, old]) == [new] and ph.merge_km1([old, new]) == [new]


def test_series_prefers_the_conglomerate_row_and_guards_on_read():
    pts = [{"as_of": 201612, "entity_id": "bradesco", "code": "01701201", "conglomerate": False,
            "lead": False, "ativo_bi": 200.0, "roe_pct": 10.0},
           {"as_of": 201612, "entity_id": "bradesco", "code": "C0080075", "conglomerate": True,
            "lead": True, "ativo_bi": 1000.0, "roe_pct": 15.0},
           {"as_of": 201512, "entity_id": "bradesco", "code": "C0080075", "conglomerate": True,
            "lead": True, "ativo_bi": 900.0, "roe_pct": 400.0},            # implausible ROE
           {"as_of": 201512, "entity_id": None, "code": "X"}]
    s = ph.build_series({"ifdata": pts}, "t")
    assert s["points"] == {"ifdata": 3} and list(s["entities"]) == ["bradesco"]
    got = ph.series_for(s, "bradesco", "ifdata")
    assert [(p["as_of"], p["code"]) for p in got] == [(201512, "C0080075"), (201612, "C0080075")]
    assert got[0]["roe_pct"] is None and got[1]["roe_pct"] == 15.0      # guard_fundamentals on read
    assert len(ph.series_for(s, "bradesco", "ifdata", lead_only=False)) == 3
    assert ph.series_for(s, "bradesco", "ifdata", start=201601) == got[1:]
    assert ph.series_for(s, "nobody", "ifdata") == []


def test_km1_v1_scalar_and_camelcase_layouts_in_percent():
    v1 = {"km1_trimestreReferencia": "2021-1", "km1_7": 14.79, "km1_14": 6.88, "km1_17": 0.0, "km1_20": 0.0}
    camel = {"km1TrimestreReferencia": "2022-1", "km17": 18.59, "km117": 0}
    p1 = ph.km1_points(v1, "60746948000112", "B", "u", EMAP)
    assert [(p["as_of"], p["basileia_pct"], p["leverage_pct"], p["lcr_pct"]) for p in p1] == [
        (202103, 14.79, 6.88, None)]                                   # LCR 0 = not applicable
    p2 = ph.km1_points(camel, "60746948000112", "B", "u", EMAP)
    assert [(p["as_of"], p["basileia_pct"]) for p in p2] == [(202203, 18.59)]


def test_km1_fraction_vs_percent_by_plausibility():
    assert ph._pct("basileia_pct", 0.1479) == 14.79 and ph._pct("basileia_pct", 14.79) == 14.79
    assert ph._pct("lcr_pct", 1.5) == 150.0 and ph._pct("lcr_pct", 150.0) == 150.0
    assert ph._pct("basileia_pct", 1444.0) is None and ph._pct("lcr_pct", 0) is None
