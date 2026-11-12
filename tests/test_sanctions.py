"""#381: international sanctions lists (UN/OFAC/EU/UK) on the CCO board.

Every name, CNPJ and person below is INVENTED (public repo) — fixtures only mirror the live XML
shapes verified 2026-11-12."""
import datetime as dt
import io
import json

import pytest

from src.dashboard import feed_builder
from src.ingest import sanctions as S
from src.synth import executive

TODAY = dt.date(2026, 11, 12)

OFAC_XML = b"""<?xml version="1.0"?>
<sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
  <sdnEntry><uid>900001</uid><lastName>EXEMPLO PAGAMENTOS LTDA</lastName><sdnType>Entity</sdnType>
    <programList><program>SDGT</program></programList>
    <idList><id><uid>1</uid><idType>Tax ID No.</idType><idNumber>11.222.333/0001-44</idNumber>
      <idCountry>Brazil</idCountry></id></idList>
    <akaList><aka><lastName>EXEMPLO PAY</lastName></aka></akaList>
    <addressList><address><city>Sao Paulo</city><country>Brazil</country></address></addressList>
  </sdnEntry>
  <sdnEntry><uid>900002</uid><firstName>FULANO</firstName><lastName>DE TAL</lastName>
    <sdnType>Individual</sdnType><programList><program>SDNTK</program></programList>
    <placeOfBirthList><placeOfBirthItem><placeOfBirth>Recife, Brazil</placeOfBirth></placeOfBirthItem></placeOfBirthList>
  </sdnEntry>
  <sdnEntry><uid>900003</uid><lastName>OUTRA TRADING COMPANY LIMITED</lastName><sdnType>Entity</sdnType>
    <programList><program>DPRK</program></programList>
    <addressList><address><country>Korea, North</country></address></addressList>
  </sdnEntry>
</sdnList>"""

UN_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<CONSOLIDATED_LIST><INDIVIDUALS><INDIVIDUAL><DATAID>1</DATAID><FIRST_NAME>CICLANO</FIRST_NAME>
<SECOND_NAME>EXEMPLO</SECOND_NAME><UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE><REFERENCE_NUMBER>QDi.900</REFERENCE_NUMBER>
<LISTED_ON>2020-01-02</LISTED_ON><NATIONALITY><VALUE>Brazil</VALUE></NATIONALITY>
<INDIVIDUAL_ALIAS><QUALITY/><ALIAS_NAME/></INDIVIDUAL_ALIAS></INDIVIDUAL></INDIVIDUALS>
<ENTITIES><ENTITY><DATAID>2</DATAID><FIRST_NAME>FRENTE EXEMPLO ARMADA</FIRST_NAME>
<UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE><REFERENCE_NUMBER>QDe.900</REFERENCE_NUMBER><LISTED_ON>2021-03-04</LISTED_ON>
<ENTITY_ALIAS><QUALITY>a.k.a.</QUALITY><ALIAS_NAME>FEA</ALIAS_NAME></ENTITY_ALIAS>
<ENTITY_ADDRESS><COUNTRY>Brazil</COUNTRY></ENTITY_ADDRESS></ENTITY></ENTITIES></CONSOLIDATED_LIST>"""

EU_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="http://eu.europa.ec/fpi/fsd/export">
 <sanctionEntity euReferenceNumber="EU.900.1" logicalId="9">
  <regulation publicationDate="2024-05-06" programme="RUS"/>
  <subjectType code="enterprise" classificationCode="E"/>
  <nameAlias wholeName="Exemplo Energia Comercial Ltda"/>
  <nameAlias wholeName="Exemplo Energia"/>
  <address countryIso2Code="BR"/>
 </sanctionEntity>
 <sanctionEntity euReferenceNumber="EU.900.2" logicalId="10">
  <regulation publicationDate="2024-05-06" programme="RUS"/>
  <subjectType code="person" classificationCode="P"/>
  <nameAlias wholeName="Beltrano Exemplo"/>
  <citizenship countryIso2Code="BR"/>
 </sanctionEntity>
</export>"""

UK_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Designations><DateGenerated>12/11/2026</DateGenerated>
 <Designation><DateDesignated>24/02/2026</DateDesignated><UniqueID>RUS9001</UniqueID>
  <Names><Name><Name6>EXEMPLO PETROLEO TRADING LTDA</Name6><NameType>Primary Name</NameType></Name>
   <Name><Name6>Exemplo Petro</Name6><NameType>Alias</NameType></Name></Names>
  <RegimeName>The Russia (Sanctions) (EU Exit) Regulations 2019</RegimeName>
  <IndividualEntityShip>Entity</IndividualEntityShip>
  <Addresses><Address><AddressCountry>Brazil</AddressCountry></Address></Addresses>
 </Designation>
</Designations>"""

FIXTURES = {"ofac": OFAC_XML, "un": UN_XML, "eu": EU_XML, "uk": UK_XML}


class _Resp:
    def __init__(self, body):
        self.content = body

    def raise_for_status(self):
        pass


def _get(fixtures, fail=()):
    by_url = {S.LISTS[k]["url"]: k for k in S.LISTS}

    def get(url, **_):
        lst = by_url[url]
        if lst in fail:
            raise RuntimeError("503 upstream")
        return _Resp(fixtures[lst])
    return get


class _S3:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objs[Key])}

    def put_object(self, Bucket, Key, Body, **_):
        self.objs[Key] = Body


# --- parsers -------------------------------------------------------------------------------
def test_ofac_brazil_entity_carries_cnpj_root_and_people_are_flagged_not_named():
    recs = list(S.parse_ofac(io.BytesIO(OFAC_XML)))
    ent = recs[0]
    assert ent["id"] == "ofac:900001" and ent["kind"] == "entity" and ent["brazil"]
    assert ent["cnpj_roots"] == ["11222333"] and ent["aliases"] == ["EXEMPLO PAY"]
    assert recs[1]["kind"] == "individual" and recs[1]["brazil"]     # "Recife, Brazil"
    assert not recs[2]["brazil"]


@pytest.mark.parametrize("lst,first", [("un", "QDi.900"), ("eu", "EU.900.1"), ("uk", "RUS9001")])
def test_each_parser_reads_its_live_shape(lst, first):
    recs = list(S.PARSERS[lst](io.BytesIO(FIXTURES[lst])))
    assert recs[0]["id"] == f"{lst}:{first}" and all(r["brazil"] for r in recs)


def test_uk_date_and_eu_listing_date():
    uk = next(S.parse_uk(io.BytesIO(UK_XML)))
    assert uk["listed_on"] == "2026-02-24" and uk["aliases"] == ["Exemplo Petro"]
    eu = next(S.parse_eu(io.BytesIO(EU_XML)))
    assert eu["listed_on"] == "2024-05-06" and eu["kind"] == "entity"


def test_snapshot_keeps_brazil_entities_and_only_counts_individuals():
    snap = S.snapshot("ofac", list(S.parse_ofac(io.BytesIO(OFAC_XML))), as_of="2026-11-12")
    assert [r["name"] for r in snap["brazil_entities"]] == ["EXEMPLO PAGAMENTOS LTDA"]
    assert snap["brazil_individuals"] == 1 and snap["total"] == 3
    assert "FULANO" not in json.dumps({k: v for k, v in snap.items() if k != "_entity_index"})


# --- matching ------------------------------------------------------------------------------
def test_name_key_is_conservative():
    assert S.name_key("Banco Exemplo S.A.") == "BANCO EXEMPLO"
    assert S.name_key("Exemplo Ltda") == ""          # one significant token → never matches
    assert S.name_key("AB CD") == ""                 # too short
    assert S.name_key("Pagamentos Ação Ltda") == S.name_key("PAGAMENTOS ACAO LTDA")


def test_roster_match_cnpj_first_then_exact_name():
    recs = list(S.parse_ofac(io.BytesIO(OFAC_XML)))
    idx = S.snapshot("ofac", recs, as_of="x")["_entity_index"]
    by_cnpj = S.roster_matches(idx, recs, cnpj_to_entity={"11222333": "exemplo_pay"},
                               name_to_entity={})
    assert by_cnpj[0]["basis"] == "cnpj" and by_cnpj[0]["entity"] == "exemplo_pay"
    by_name = S.roster_matches(idx, recs, cnpj_to_entity={},
                               name_to_entity={S.name_key("Outra Trading Co. Ltd"): "outra"})
    assert [(m["entity"], m["basis"]) for m in by_name] == [("outra", "name")]
    # a one-word brand never matches, and individuals are not in the entity index
    assert S.roster_matches(idx, recs, cnpj_to_entity={}, name_to_entity={"EXEMPLO": "x"}) == []
    assert all(e["id"] != "ofac:900002" for e in idx)


# --- state ---------------------------------------------------------------------------------
def test_first_run_is_a_baseline_then_new_ids_are_dated():
    s3 = _S3()
    out = S.update_state("bkt", s3=s3, get=_get(FIXTURES), today=TODAY,
                         cnpj_to_entity={"11222333": "exemplo_pay"}, name_to_entity={"Banco Z": "z"})
    doc = json.loads(s3.objs[S.STATE_KEY])
    assert out["errors"] == [] and out["matches"] == 1
    assert all(d["baseline"] and d["new"] == 0 for d in doc["new_log"])
    assert doc["lists"]["ofac"]["brazil_entities"][0]["first_seen"] is None
    assert "_entity_index" not in doc["lists"]["ofac"]
    for name in ("FULANO", "CICLANO", "Beltrano"):            # no person is ever stored by name
        assert name not in s3.objs[S.STATE_KEY].decode()

    more = OFAC_XML.replace(b"</sdnList>", b"""<sdnEntry><uid>900004</uid>
      <lastName>NOVA EXEMPLO CAMBIO LTDA</lastName><sdnType>Entity</sdnType>
      <addressList><address><country>Brazil</country></address></addressList></sdnEntry></sdnList>""")
    S.update_state("bkt", s3=s3, get=_get({**FIXTURES, "ofac": more}),
                   today=TODAY + dt.timedelta(days=1), cnpj_to_entity={"11222333": "exemplo_pay"})
    doc = json.loads(s3.objs[S.STATE_KEY])
    last = [d for d in doc["new_log"] if d["list"] == "ofac"][-1]
    assert last["new"] == 1 and last["new_brazil"] == 1 and not last["baseline"]
    rows = {r["id"]: r for r in doc["lists"]["ofac"]["brazil_entities"]}
    assert rows["ofac:900004"]["first_seen"] == "2026-11-13" and rows["ofac:900001"]["first_seen"] is None


def test_failed_list_keeps_previous_snapshot_never_reads_as_delisting():
    s3 = _S3()
    S.update_state("bkt", s3=s3, get=_get(FIXTURES), today=TODAY,
                   cnpj_to_entity={"11222333": "exemplo_pay"})
    before = json.loads(s3.objs[S.STATE_KEY])
    out = S.update_state("bkt", s3=s3, get=_get(FIXTURES, fail=("ofac",)),
                         today=TODAY + dt.timedelta(days=1), cnpj_to_entity={"11222333": "exemplo_pay"})
    doc = json.loads(s3.objs[S.STATE_KEY])
    assert out["errors"] == ["ofac"] and "503" in doc["errors"]["ofac"]
    assert doc["lists"]["ofac"] == before["lists"]["ofac"]
    assert [m["id"] for m in doc["matches"]] == ["ofac:900001"]


def test_registry_unavailable_carries_previous_matches():
    s3 = _S3()
    S.update_state("bkt", s3=s3, get=_get(FIXTURES), today=TODAY,
                   cnpj_to_entity={"11222333": "exemplo_pay"})
    out = S.update_state("bkt", s3=s3, get=_get(FIXTURES), today=TODAY)
    assert out["entity_map"] == "carried" and out["matches"] == 1


def test_zero_records_is_an_error_not_an_empty_list():
    s3 = _S3()
    out = S.update_state("bkt", s3=s3, get=_get({**FIXTURES, "un": b"<CONSOLIDATED_LIST/>"}),
                         today=TODAY, lists=("un",))
    assert out["errors"] == ["un"]


# --- feed + scoping ------------------------------------------------------------------------
ATTRS = {"exemplo_pay": {"label": "Exemplo Pay", "industries": ["fintech", "acquiring"]}}


def _doc():
    s3 = _S3()
    S.update_state("bkt", s3=s3, get=_get(FIXTURES), today=TODAY,
                   cnpj_to_entity={"11222333": "exemplo_pay"})
    return json.loads(s3.objs[S.STATE_KEY])


def test_for_feed_binds_roster_rows_and_drops_ids_and_errors():
    block = S.for_feed(_doc(), entity_attrs=ATTRS, today=TODAY)
    assert set(block) == {"as_of", "lists", "brazil_entities", "brazil_individuals", "matches"}
    assert [l["list"] for l in block["lists"]] == list(S.ORDER)
    assert next(l for l in block["lists"] if l["list"] == "un")["binding_in_brazil"]
    assert "ids" not in json.dumps(block["lists"])
    pay = next(r for r in block["brazil_entities"] if r["id"] == "ofac:900001")
    assert pay["entity"] == "exemplo_pay" and pay["label"] == "Exemplo Pay"
    unbound = next(r for r in block["brazil_entities"] if r["id"] == "uk:RUS9001")
    assert unbound["entity"] is None and "banking" in unbound["industries"]
    assert block["matches"][0]["label"] == "Exemplo Pay" and block["matches"][0]["basis"] == "cnpj"
    assert block["brazil_individuals"]["ofac"] == 1


def test_scope_fail_closed():
    block = S.for_feed(_doc(), entity_attrs=ATTRS, today=TODAY)
    adv = S.scope(block, ["advisory"], lambda r: False)
    assert adv["brazil_entities"] == [] and adv["matches"] == [] and adv["brazil_individuals"] == {}
    bank = S.scope(block, ["banking"], lambda r: False)
    assert bank["matches"] == [] and all(r["entity"] is None for r in bank["brazil_entities"])
    fin = S.scope(block, ["fintech"], lambda r: r.get("entity") == "exemplo_pay")
    assert fin["matches"] and any(r["entity"] == "exemplo_pay" for r in fin["brazil_entities"])


def test_entry_and_sample_feeds_never_carry_sanctions():
    src = open(feed_builder.__file__, encoding="utf-8").read()
    assert src.count('"sanctions": _scope_sanctions(') == 1                      # tenant scope
    assert '"sanctions": {},  # #381' in src                                      # static Entry
    assert '"product_radar", "sanctions",  # #381' in src                         # Entry tenant
    assert '"bcb_supervision", "sanctions", "standard_setters"):' in src                             # sample
    assert "sanctions" in feed_builder.ENTRY_TENANT_WITHHELD_STORES


def test_cco_board_carries_matches_brazil_rows_and_a_verify_recommendation():
    block = S.for_feed(_doc(), entity_attrs=ATTRS, today=TODAY)
    feed = {"dates": ["2026-11-12"], "industry_options": [{"slug": "fintech", "label": "Fintech"}],
            "entity_attrs": ATTRS, "feed": [], "sanctions": block}
    cco = executive.build_executive(feed)["cco"]
    P = cco["panels"]["sanctions_lists"]
    assert P["matches"] and P["brazil"] and P["lists"]
    assert cco["by_industry"]["fintech"]["n_sanction_matches"] == 1
    rec = [r for r in cco["panels"]["recommendations"] if "sanções" in r.get("text", "")]
    assert rec and "verificar" in rec[0]["text"].lower()


def test_sanctions_runs_on_its_own_parallel_branch():
    lp = open("src/ingest/lambda_port.py", encoding="utf-8").read()
    assert 'if mode == "sanctions":' in lp and 'shard="sanctions"' in lp
    app = open("infra/app.py", encoding="utf-8").read()
    assert '{"mode": "sanctions"}' in app and "ingest_task.branch(sanctions_ingest)" in app
    assert "sanctions_ingest.add_catch(" in app
