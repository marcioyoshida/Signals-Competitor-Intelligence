import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt

from src.ingest import datajud


def _hit(num, code, name, ajuiz, trib="TJSP", orgao="1ª Vara"):
    return {"_source": {"numeroProcesso": num, "classe": {"codigo": code, "nome": name},
                        "tribunal": trib, "orgaoJulgador": {"nome": orgao},
                        "dataAjuizamento": ajuiz}}


def _fetcher(hits):
    def f(trib, body):
        return {"hits": {"hits": hits}}
    return f


def test_parses_and_filters_by_window():
    today = dt.date(2026, 8, 24)
    hits = [
        _hit("111", 129, "Recuperação Judicial", "20260820140000"),   # in window
        _hit("222", 130, "Falência", "20260101090000"),               # too old
    ]
    rows = datajud.fetch_recuperacao_judicial(
        tribunals=["tjsp"], lookback_days=30, today=today, fetcher=_fetcher(hits))
    assert [r["numero_processo"] for r in rows] == ["111"]
    r = rows[0]
    assert r["id"] == "datajud:tjsp:111"
    assert r["kind"] == "recuperacao_judicial"
    assert r["classe"] == "Recuperação Judicial"
    assert r["date"] == "2026-08-20"
    assert "Recuperação Judicial" in r["subject"] and "TJSP" in r["subject"]


def test_iso_date_and_dedup():
    today = dt.date(2026, 8, 24)
    hits = [
        _hit("900", 131, "Recuperação Extrajudicial", "2026-08-22T10:00:00"),
        _hit("900", 131, "Recuperação Extrajudicial", "2026-08-22T10:00:00"),  # dup id
    ]
    rows = datajud.fetch_recuperacao_judicial(
        tribunals=["tjsp"], lookback_days=30, today=today, fetcher=_fetcher(hits))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-22"
    assert rows[0]["classe"] == "Recuperação Extrajudicial"


def test_bad_response_degrades_to_empty():
    def boom(trib, body):
        raise RuntimeError("HTTP 503")
    rows = datajud.fetch_recuperacao_judicial(
        tribunals=["tjsp", "tjrj"], fetcher=boom)
    assert rows == []


def test_summarize_counts_by_class_and_tribunal():
    rows = [
        {"classe": "Recuperação Judicial", "tribunal": "TJSP"},
        {"classe": "Falência", "tribunal": "TJSP"},
        {"classe": "Recuperação Judicial", "tribunal": "TJRJ"},
    ]
    s = datajud.summarize(rows)
    assert s["total"] == 3
    assert s["by_class"] == {"Recuperação Judicial": 2, "Falência": 1}
    assert s["by_tribunal"] == {"TJSP": 2, "TJRJ": 1}
