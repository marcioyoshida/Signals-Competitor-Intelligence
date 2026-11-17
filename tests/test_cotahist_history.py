"""#336 B3 COTAHIST daily prices for listed financial institutions, attributed by date."""
import io
import zipfile

from src.ingest import cotahist_history as ch


def line(date, ticker, isin, name="BANCO X", close=2550, fatcot=1, bdi="02", merc="010", volume=123456789):
    """A 245-character COTAHIST quote line (prices in centavos)."""
    p = lambda v: str(v).rjust(13, "0")  # noqa: E731
    s = ("01" + date.replace("-", "") + bdi + ticker.ljust(12) + merc + name.ljust(12) + "PN".ljust(10)
         + " " * 3 + "R$".ljust(4) + p(close - 50) + p(close + 100) + p(close - 100) + p(close) + p(close)
         + p(close) + p(close) + "00042" + "1000".rjust(18, "0") + str(volume).rjust(18, "0")
         + p(0) + "0" + "99991231" + str(fatcot).rjust(7, "0") + p(0) + isin + "000")
    assert len(s) == 245, len(s)
    return s


def zipped(lines):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("COTAHIST_A2007.TXT", ("00HEADER\r\n" + "\r\n".join(lines) + "\r\n").encode("latin-1"))
    return out.getvalue()


def test_parse_line_per_share_and_filters():
    r = ch.parse_line(line("2007-06-01", "UBBR11", "BRUBBRCDAM14", close=2550))
    assert (r["date"], r["ticker"], r["issuer"], r["close"], r["trades"], r["volume"]) == (
        "2007-06-01", "UBBR11", "UBBR", 25.5, 42, 1234567.89)
    assert ch.parse_line(line("2001-06-01", "ITAU4", "BRITAUACNPR3", close=25000, fatcot=1000))["close"] == 0.25
    assert ch.parse_line(line("2007-06-01", "UBBR11", "BRUBBRCDAM14", bdi="96")) is None    # odd lot
    assert ch.parse_line(line("2007-06-01", "UBBR11", "BRUBBRCDAM14", merc="070")) is None  # options


def test_parse_year_keeps_only_in_scope_issuers():
    rows = ch.parse_year(zipped([line("2007-06-01", "UBBR11", "BRUBBRCDAM14"),
                                 line("2007-06-01", "VALE3", "BRVALEACNOR0")]), ["UBBR"])
    assert [r["ticker"] for r in rows] == ["UBBR11"]


def test_issuers_from_fca_and_supplement_never_guess():
    pairs = {"BBDC": {"60746948"}, "ODDX": {"11111111", "22222222"}, "VALE": {"33592510"}}
    got = ch.issuers(pairs, {"UBBR": "33700394"}, {"60746948", "33700394", "11111111", "22222222"})
    assert got == {"BBDC": "60746948", "UBBR": "33700394"}    # VALE out of scope, ODDX ambiguous
    sup = ch.load_supplement()
    assert sup["ITAU"] == "60872504" and sup["UBBR"] == "33700394" and "UBHD" not in sup


def test_fca_pairs_reads_trading_codes():
    csv = ("CNPJ_Companhia;Codigo_Negociacao\n60.746.948/0001-12;BBDC4\n60.746.948/0001-12;\n"
           "00.000.000/0001-91;bbas3\n").encode("latin-1")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("fca_cia_aberta_valor_mobiliario_2015.csv", csv)
    assert ch.fca_pairs(out.getvalue()) == {"BBDC": {"60746948"}, "BBAS": {"00000000"}}


EMAP = {"quarters": [200003, 202606], "roots": {
    "33700394": [{"from": 200003, "to": 200812, "group_entity_id": "unibanco", "entity_id": "unibanco"},
                 {"from": 200903, "to": 201606, "group_entity_id": "itau", "entity_id": "unibanco"}],
    "60746948": [{"from": 200003, "to": 202606, "group_entity_id": "bradesco", "entity_id": "bradesco"}]}}


def rows():
    return [ch.parse_line(line(d, t, i, close=c)) for d, t, i, c in (
        ("2008-11-03", "UBBR11", "BRUBBRCDAM14", 2000), ("2008-11-04", "UBBR11", "BRUBBRCDAM14", 2100),
        ("2009-04-01", "UBBR11", "BRUBBRCDAM14", 2200),
        ("2008-11-03", "BBDC4", "BRBBDCACNPR8", 3000), ("2008-11-04", "BBDC4", "BRBBDCACNPR8", 3030),
        ("2008-11-05", "BBDC4", "BRBBDCACNPR8", 6060),                        # a 2:1-like jump
        ("2008-11-03", "BBSE3", "BRBBSEACNOR5", 1000), ("2008-11-04", "BBSE3", "BRBBSEACNOR5", 990))]


def test_series_attributes_by_date_registry_fallback_and_flags_jumps():
    codes = {"UBBR": "33700394", "BBDC": "60746948", "BBSE": "17344597"}
    s = ch.build_series(rows(), codes, EMAP, {"17344597": "bb_seguridade"}, "t")
    assert [p[0] for p in s["entities"]["unibanco"]["UBBR11"]] == ["2008-11-03", "2008-11-04"]
    assert [p[0] for p in s["entities"]["itau"]["UBBR11"]] == ["2009-04-01"]      # quarter 2009-03 on: Itaú
    assert s["entities"]["bradesco"]["BBDC4"][-1][7] == "suspect_corporate_action"
    assert s["tickers"]["BBSE3"]["attribution"] == "registry" and "bb_seguridade" in s["entities"]


def test_reaction_against_the_sector_median_and_skips_flagged_windows():
    codes = {"UBBR": "33700394", "BBDC": "60746948", "BBSE": "17344597"}
    s = ch.build_series(rows(), codes, EMAP, {"17344597": "bb_seguridade"}, "t")
    r = ch.reaction(s, "unibanco", "2008-11-04", before=0, after=0)
    assert r["return_pct"] == 5.0 and r["peers"] == 2                    # 20.00 -> 21.00
    assert r["sector_median_pct"] == 0.0 and r["abnormal_pct"] == 5.0   # median of +1%, -1%
    assert ch.reaction(s, "bradesco", "2008-11-05", before=0, after=0) is None   # flagged day
