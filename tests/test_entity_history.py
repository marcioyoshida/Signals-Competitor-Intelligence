"""#330: dated CNPJ ↔ entity map from BCB IF.data's quarterly register."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.ingest import bcb_entity_history as eh  # noqa: E402

HSBC, BRADESCO, HSBC_BI = "01701201", "60746948", "53518684"


def row(code, name, *, status="A", fin=None, pru=None, leader=None):
    return {"CodInst": code, "NomeInstituicao": name, "Situacao": status,
            "CodConglomeradoFinanceiro": fin, "CodConglomeradoPrudencial": pru,
            "CnpjInstituicaoLider": leader}


# Shapes taken from the live register (2015-12, 2016-09, 2016-12).
SNAPS = {
    201512: [
        row("C0051152", "HSBC", fin="C0051152", pru="C0080525", leader=HSBC),
        row(HSBC, "HSBC BANK BRASIL S.A. - BANCO MULTIPLO", fin="C0051152", pru="C0080525", leader=HSBC),
        row(HSBC_BI, "HSBC BRASIL S.A. - BANCO DE INVESTIMENTO", fin="C0051152", pru="C0080525", leader=HSBC),
        row(BRADESCO, "BANCO BRADESCO S.A.", fin="C0010045", pru="C0080075", leader=BRADESCO),
        row("33852567", "BANCO HSBC S.A.", status="I"),
    ],
    201603: [
        row(HSBC, "HSBC BANK BRASIL S.A. - BANCO MULTIPLO", fin="C0051152", pru="C0080525", leader=HSBC),
        row(BRADESCO, "BANCO BRADESCO S.A.", fin="C0010045", pru="C0080075", leader=BRADESCO),
    ],
    201606: [
        row(HSBC, "HSBC BANK BRASIL S.A. - BANCO MULTIPLO", fin="C0010045", pru="C0080075", leader=BRADESCO),
        row(BRADESCO, "BANCO BRADESCO S.A.", fin="C0010045", pru="C0080075", leader=BRADESCO),
    ],
    201609: [
        row(HSBC, "HSBC BANK BRASIL S.A. - BANCO MULTIPLO", fin="C0010045", pru="C0080075", leader=BRADESCO),
        row(BRADESCO, "BANCO BRADESCO S.A.", fin="C0010045", pru="C0080075", leader=BRADESCO),
    ],
    201612: [
        row(HSBC, "KIRTON BANK S.A. - BANCO MULTIPLO", fin="C0010045", pru="C0080075", leader=BRADESCO),
        row(BRADESCO, "BANCO BRADESCO S.A.", fin="C0010045", pru="C0080075", leader=BRADESCO),
        row(HSBC_BI, "HSBC BRASIL S.A. - BANCO DE INVESTIMENTO", pru="C0084741", leader=HSBC_BI),
    ],
}
# Registry today: Bradesco owns both roots; "hsbc-brasil" is a merged duplicate of bradesco.
ROOT_MAP = {BRADESCO: "bradesco", HSBC: "hsbc-brasil", HSBC_BI: "hsbc-bi"}
CANON = {"hsbc-brasil": "bradesco"}


@pytest.fixture
def m():
    return eh.build_map(SNAPS, ROOT_MAP, {})


def test_quarter_of_is_the_last_quarter_end_on_or_before_the_date():
    assert eh.quarter_of("2016-09-30") == 201609
    assert eh.quarter_of("2016-09-29") == 201606
    assert eh.quarter_of("2016-10-05") == 201609
    assert eh.quarter_of("2016-01-10") == 201512
    assert eh.quarter_of(201608) == 201606
    assert eh.quarters(201509, 201606) == [201509, 201512, 201603, 201606]


def test_group_owner_follows_the_conglomerate_leader_through_the_acquisition(m):
    assert eh.entity_at(m, HSBC, "2015-12-31") == "hsbc-brasil"
    assert eh.entity_at(m, HSBC, "2016-03-31") == "hsbc-brasil"
    assert eh.entity_at(m, HSBC, "2016-06-30") == "bradesco"
    assert eh.entity_at(m, HSBC, "2016-12-31") == "bradesco"
    # The direct owner of the root never changes; only the group does.
    assert eh.entity_at(m, HSBC, "2016-12-31", level="direct") == "hsbc-brasil"


def test_rename_opens_a_new_interval_and_keeps_the_old_name(m):
    ivs = m["roots"][HSBC]
    assert [(iv["from"], iv["to"], iv["name"][:5]) for iv in ivs] == [
        (201512, 201603, "HSBC "), (201606, 201609, "HSBC "), (201612, 201612, "KIRTO")]


def test_absent_quarter_closes_the_interval_and_is_not_bridged(m):
    # HSBC BI is missing from 2016-03 and 2016-06 in the fixture, so those quarters are not in force.
    assert eh.entity_at(m, HSBC_BI, "2015-12-31") == "hsbc-brasil"
    assert eh.entity_at(m, HSBC_BI, "2016-06-30") is None
    assert eh.entity_at(m, HSBC_BI, "2016-12-31") == "hsbc-bi"
    assert [iv["from"] for iv in m["roots"][HSBC_BI]] == [201512, 201612]


def test_inactive_and_conglomerate_rows_are_not_institutions(m):
    assert "33852567" not in m["roots"]
    assert not any(k.startswith("C") for k in m["roots"])


def test_canonical_chains_apply_to_both_levels():
    m = eh.build_map(SNAPS, ROOT_MAP, CANON)
    assert eh.entity_at(m, HSBC, "2015-12-31") == "bradesco"
    assert eh.entity_at(m, HSBC, "2015-12-31", level="direct") == "bradesco"


def test_dates_outside_the_register(m):
    assert eh.entity_at(m, HSBC, "2014-01-01") is None          # before the first quarter
    assert eh.entity_at(m, BRADESCO, "2026-11-10") == "bradesco"  # after the last: last state holds
    assert eh.entity_at(m, "99999999", "2016-09-30") is None
    with pytest.raises(ValueError):
        eh.entity_at(m, HSBC, "2016-09-30", level="brand")


def test_members_lists_every_root_of_the_group_at_that_date(m):
    assert eh.members(m, "bradesco", "2015-12-31") == [BRADESCO]
    assert eh.members(m, "bradesco", "2016-09-30") == [HSBC, BRADESCO]
    assert eh.members(m, "hsbc-brasil", "2015-12-31") == [HSBC, HSBC_BI]


def test_unregistered_root_is_kept_with_its_name_but_no_entity():
    m = eh.build_map({201512: [row("12345678", "BANCO X S.A.", leader="12345678")]}, {}, {})
    iv = m["roots"]["12345678"][0]
    assert iv["name"] == "BANCO X S.A." and iv["entity_id"] is None and iv["group_entity_id"] is None


def test_liquidation_keeps_the_entity_in_force_and_records_the_status():
    snaps = {201512: [row("11111111", "BANCO Y", leader="11111111")],
             201603: [row("11111111", "BANCO Y", status="LE", leader="11111111")]}
    m = eh.build_map(snaps, {"11111111": "banco-y"}, {})
    assert eh.entity_at(m, "11111111", "2016-03-31") == "banco-y"
    assert [iv["status"] for iv in m["roots"]["11111111"]] == ["A", "LE"]


def test_latest_published_steps_back_over_unpublished_quarters():
    import datetime as dt
    seen = []

    def fetch(q):
        seen.append(q)
        return [] if q > 202506 else [{"CodInst": "x"}]

    assert eh.latest_published(fetch=fetch, today=dt.date(2026, 1, 15)) == 202506
    assert seen == [202512, 202509, 202506]


def test_fetch_retries_then_slims_rows(monkeypatch):
    monkeypatch.setattr(eh.time, "sleep", lambda s: None)
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"value": [dict(row(HSBC, "HSBC"), Uf="PR", Municipio="Curitiba")]}

    class Session:
        def get(self, url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectionError("reset")
            return Resp()

    rows = eh.fetch_cadastro(201512, session=Session())
    assert len(calls) == 2 and "@AnoMes=201512" in calls[0]
    assert rows == [row(HSBC, "HSBC")]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            class NoSuchKey(Exception):
                pass
            raise NoSuchKey(Key)
        data = self.objects[(Bucket, Key)]

        class Body:
            def read(self):
                return data

        return {"Body": Body()}


def test_snapshot_and_map_round_trip_through_s3(m):
    s3 = FakeS3()
    assert eh.get_snapshot(s3, "b", 201512) is None and eh.load_map(s3, "b") is None
    eh.put_snapshot(s3, "b", 201512, SNAPS[201512])
    assert json.loads(gzip.decompress(s3.objects[("b", "entity_history/cadastro/201512.json.gz")])) == SNAPS[201512]
    assert eh.get_snapshot(s3, "b", 201512) == SNAPS[201512]
    eh.put_map(s3, "b", m)
    assert eh.entity_at(eh.load_map(s3, "b"), HSBC, "2016-09-30") == "bradesco"


def test_canonical_map_follows_registry_chains():
    ents = [{"entity_id": "a", "canonical_id": "b"}, {"entity_id": "b", "canonical_id": "c"},
            {"entity_id": "c"}]
    assert eh.canonical_map(ents) == {"a": "c", "b": "c"}
