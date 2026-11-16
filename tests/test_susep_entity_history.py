"""#389 dated insurer ownership map from SUSEP SES economic groups."""
import json

from src.ingest import bcb_entity_history as eh
from src.ingest import susep_entity_history as sh

CSV = """damesano;coenti;noenti;cogrupo;nogrupo
201512;05185;LIBERTY SEGUROS SA   ;01232;LIBERTY
202312;05185;LIBERTY SEGUROS SA   ;01232;LIBERTY
202403;05185;YLM SEGUROS S.A.     ;01230;TALANX AG
202406;05185;YLM SEGUROS S.A.     ;01230;TALANX AG
202402;05185;YLM SEGUROS S.A.     ;01230;TALANX AG
201512;05011;Chubb do Brasil      ;01225;INDEPENDENTE
202312;05011;Chubb do Brasil      ;01225;INDEPENDENTE
202403;05011;Chubb do Brasil      ;99999;OUTROS GRUPOS
202406;05011;Chubb do Brasil      ;;
201512;09999;GONE SEGUROS         ;00027;BRADESCO
;00001;NO MONTH;00027;BRADESCO
"""
ROOTS_BY_CODE = {"05185": "61550141", "05011": "33170085"}
ROOT_MAP = {"61550141": "liberty-twin", "33170085": "chubb"}
CANON = {"liberty-twin": "liberty"}
GROUPS = {"01232": "liberty", "01230": "hdi", "00027": "bradesco_seguros"}


def build():
    return sh.build_map(sh.parse_groups(CSV.encode("latin-1")), ROOTS_BY_CODE, ROOT_MAP, GROUPS, CANON)


def test_parse_drops_rows_without_a_month_and_trims():
    rows = sh.parse_groups(CSV.encode("latin-1"))
    assert len(rows) == 10 and rows[0]["noenti"] == "LIBERTY SEGUROS SA"


def test_a_change_of_owner_is_dated_and_works_with_the_bcb_lookups():
    m = build()
    assert m["quarters"] == [201512, 202406]
    assert eh.entity_at(m, "61550141", "2023-12-31") == "liberty"
    assert eh.entity_at(m, "61550141", "2024-05-02") == "hdi"            # not today's owner for 2023
    assert eh.entity_at(m, "61550141", 202403, level="direct") == "liberty"   # canonical twin
    assert sh.code_entity_at(m, "05185", 201512) == "liberty"


def test_placeholder_groups_attribute_to_the_company_itself():
    m = build()
    for q in (201512, 202403, 202406):
        assert eh.entity_at(m, "33170085", q) == "chubb"
    assert [iv["group"] for iv in m["codes"]["05011"]] == [None]        # one interval, no flapping


def test_intervals_split_on_owner_change_and_on_an_absent_quarter():
    m = build()
    assert [(iv["from"], iv["to"]) for iv in m["codes"]["05185"]] == [(201512, 202312), (202403, 202406)]
    # 09999 is only in 201512: absent from 202312 on, so its interval closes there
    assert [(iv["from"], iv["to"]) for iv in m["codes"]["09999"]] == [(201512, 201512)]
    assert "09999" in m["codes"] and m["codes"]["09999"][0]["cnpj_root"] is None
    assert m["codes"]["09999"][0]["group_entity_id"] == "bradesco_seguros"
    assert sh.code_entity_at(m, "09999", 202406) is None                 # left the market


def test_changes_list_only_named_group_moves():
    ch = sh.changes(build())
    assert [(c["susep_code"], c["at"], c["from_entity"], c["to_entity"]) for c in ch] == [
        ("05185", 202403, "liberty", "hdi")]


def test_code_roots_and_group_config():
    assert sh.code_roots([{"codigo_fip": "05185", "cnpj": "61550141000172"},
                          {"codigo_fip": "", "cnpj": "1"}]) == {"05185": "61550141"}
    g = sh.load_group_config()
    assert g["01230"] == "hdi" and "01233" not in g          # BB-Mapfre JV deliberately unmapped
    raw = json.loads(sh.GROUPS_CONFIG.read_text(encoding="utf-8"))
    assert not set(raw["groups"]) & {"99999", "01225"}       # placeholders are never owners


def test_a_partial_newest_month_is_not_read_as_exits():
    rows = [{"damesano": m, "coenti": f"{i:05d}", "noenti": "X", "cogrupo": "", "nogrupo": ""}
            for m, n in (("202606", 20), ("202609", 15)) for i in range(n)]
    assert sh.complete_through(rows) == "202606"
    m = sh.build_map(rows, {}, {}, {})
    assert m["quarters"] == [202606, 202606] and len(m["codes"]) == 20
    assert sh.code_interval_at(m, "00019", 202609)["from"] == 202606    # last state stays in force
