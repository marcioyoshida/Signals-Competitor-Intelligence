"""#334 BCB interest-rate history per institution and product, attributed by date."""
from src.ingest import juros_history as jh

# HSBC Bank Brasil (01701201): HSBC's to 2016-06, Bradesco's from 2016-09 (as #330 records it)
EMAP = {"quarters": [201203, 202606], "roots": {
    "01701201": [{"from": 201203, "to": 201606, "group_entity_id": "hsbc", "entity_id": "hsbc"},
                 {"from": 201609, "to": 202606, "group_entity_id": "bradesco", "entity_id": "hsbc-bank"}],
    "60746948": [{"from": 201203, "to": 202606, "group_entity_id": "bradesco", "entity_id": "bradesco"}],
}}
PNC = "Crédito pessoal não consignado - Prefixado"


def raw(cnpj8, seg, prod, pos, am, aa):
    return {"cnpj8": cnpj8, "Segmento": seg, "Modalidade": prod, "Posicao": pos,
            "TaxaJurosAoMes": am, "TaxaJurosAoAno": aa, "InstituicaoFinanceira": "X"}


def test_chain_takes_non_overlapping_windows():
    periods = [{"inicio": "2012-01-02", "fim": "2012-01-06"}, {"inicio": "2012-01-03", "fim": "2012-01-09"},
               {"inicio": "2012-01-09", "fim": "2012-01-13"}, {"inicio": "2012-01-06", "fim": "2012-01-12"}]
    assert [w["inicio"] for w in jh.chain(periods)] == ["2012-01-02", "2012-01-09"]


def test_compact_maps_segments_and_drops_bad_roots():
    rows = jh.compact([raw("60746948", "PESSOA FÍSICA", PNC, 3, 5.0, 79.59),
                       raw("", "PESSOA JURÍDICA", PNC, 1, 1, 1),
                       {"cnpj8": "60746948", "Modalidade": "Financiamento imobiliário", "Posicao": 2,
                        "TaxaJurosAoMes": 0.9, "TaxaJurosAoAno": 11.4}])
    assert rows == [["60746948", "PF", PNC, 3, 5.0, 79.59],
                    ["60746948", None, "Financiamento imobiliário", 2, 0.9, 11.4]]
    assert jh.product_key("PF", PNC) == f"PF|{PNC}" and jh.product_key(None, "X") == "X"


def windows():
    return [
        {"fim": "2015-12-04", "rows": jh.compact([raw("01701201", "PESSOA FÍSICA", PNC, 5, 4.0, 60.1),
                                                   raw("60746948", "PESSOA FÍSICA", PNC, 4, 4.5, 69.6),
                                                   raw("99999999", "PESSOA FÍSICA", PNC, 9, 9.0, 180.0)])},
        {"fim": "2016-10-07", "rows": jh.compact([raw("01701201", "PESSOA FÍSICA", PNC, 6, 4.2, 63.9),
                                                   raw("60746948", "PESSOA FÍSICA", PNC, 5, 5.0, 79.6),
                                                   raw("99999999", "PESSOA FÍSICA", PNC, 9, 9.0, 190.0)])},
    ]


def test_rows_are_attributed_to_the_owner_at_the_window_date():
    s = jh.build_series(windows(), EMAP, "t")
    key = f"PF|{PNC}"
    assert [p["as_of"] for p in jh.series_for(s, "hsbc", key)] == ["2015-12-04"]
    brad = jh.series_for(s, "bradesco", key)
    assert [(p["as_of"], p["cnpj8"]) for p in brad] == [("2015-12-04", "60746948"), ("2016-10-07", "01701201"),
                                                       ("2016-10-07", "60746948")]
    assert jh.series_for(s, "bradesco", key, direct="hsbc-bank")[0]["rate_aa"] == 63.9
    assert s["unattributed_rows"] == 2 and s["unattributed_roots"] == 1
    s2 = jh.build_series(windows(), EMAP, "t", names={"99999999": "BCO NOVO"})
    assert s2["unattributed"]["99999999"] == {"rows": 2, "first": "2015-12-04", "last": "2016-10-07",
                                              "name": "BCO NOVO"}
    assert s["market"][key] == [["2015-12-04", 69.6, 3], ["2016-10-07", 79.6, 3]]   # medians, all filers


def test_monthly_rows_are_dated_at_month_end():
    s = jh.build_series([], EMAP, "t", monthly=[{"anomes": "2016-02", "rows": [
        ["60746948", None, "Financiamento imobiliário", 2, 0.9, 11.4]]}])
    assert jh.series_for(s, "bradesco", "Financiamento imobiliário")[0]["as_of"] == "2016-02-29"


def test_selic_at():
    selic = [["2015-07-30", 14.25], ["2016-10-20", 14.0]]
    assert jh.selic_at(selic, "2015-12-04") == 14.25 and jh.selic_at(selic, "2016-10-21") == 14.0
    assert jh.selic_at(selic, "2015-01-01") is None


def test_pass_through_compares_each_entity_with_itself_not_whoever_filed():
    def w(fim, rows):
        return {"fim": fim, "rows": [[c, "PF", PNC, 1, 1.0, aa] for c, aa in rows]}
    # Bradesco's bank goes 90 -> 100; a cheap group card arm (10%) appears only at the end;
    # a market lender 50 -> 55 at both ends; a new cheap entrant (20%) only at the end.
    emap = {"quarters": [202103, 202212], "roots": {
        "60746948": [{"from": 202103, "to": 202212, "group_entity_id": "bradesco", "entity_id": "bradesco"}],
        "04184779": [{"from": 202103, "to": 202212, "group_entity_id": "bradesco", "entity_id": "bradescard"}]}}
    wins = [w("2021-03-05", [("60746948", 88.0), ("11111111", 50.0)]),
            w("2021-03-12", [("60746948", 92.0), ("11111111", 50.0)]),
            w("2022-09-30", [("60746948", 100.0), ("04184779", 10.0), ("11111111", 55.0), ("22222222", 20.0)])]
    s = jh.build_series(wins, emap, "t")
    assert s["panel"][f"PF|{PNC}"]["11111111"] == [["2021-03-05", 50.0], ["2021-03-12", 50.0], ["2022-09-30", 55.0]]
    pt = jh.pass_through(s, [["2021-03-01", 2.0], ["2022-08-04", 13.75]], "bradesco", f"PF|{PNC}",
                         "2021-03-12", "2022-09-30")
    assert pt["rate_change_pp"] == 10.0                     # 90 (4-week mean) -> 100, card arm ignored
    assert [e["cnpj8"] for e in pt["entities"]] == ["60746948"]
    assert pt["market_matched"] == 2 and pt["market_change_pp"] == 7.5   # median of +10, +5; entrant ignored
    assert pt["selic_change_pp"] == 11.75 and pt["vs_market_pp"] == 2.5
    assert jh.pass_through(s, [], "nobody", f"PF|{PNC}", "2021-03-12", "2022-09-30") is None
