"""#104 (#14 Stage 2): Receita CNPJ bulk → FS-CNAE candidate parse + propose."""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import receita_bulk as rb


def test_cnae_mapping_and_fs_filter():
    assert rb.cnae_to_industry("6422-1/00") == "banking"
    assert rb.cnae_to_industry("6550-2/00") == "insurance"
    assert rb.cnae_to_industry("6630-4/00") == "asset-management"
    assert rb.cnae_to_industry("4711-3/02") is None      # retail — not FS
    assert rb.is_fs_cnae("6499-9/99") and not rb.is_fs_cnae("0111-3/01")
    assert rb.cnae_to_industry("6600-0/00") is None       # FS division but unmapped group


# CNPJ_BASICO;ORDEM;DV;MATRIZ;FANTASIA;SITUACAO;DTSIT;MOTIVO;CIDEXT;PAIS;DTINI;CNAE;CNAE2;...;UF;...
def _row(basico, fantasia, situacao, matriz, cnae, uf="SP"):
    return ";".join([basico, "0001", "00", matriz, fantasia, situacao, "", "", "", "",
                     "20200101", cnae, "", "RUA X", "1", "", "CENTRO", "1234", uf, "01001000"])


def test_parse_keeps_active_matriz_fs_dedups():
    text = "\n".join([
        _row("11111111", "NEOBANK", "02", "1", "6422-1/00"),          # active FS matriz → keep
        _row("22222222", "LOJA DE ROUPA", "02", "1", "4711-3/02"),    # non-FS → drop
        _row("33333333", "SEGURADORA Z", "04", "1", "6550-2/00"),     # inactive → drop
        _row("44444444", "FILIAL FIN", "02", "2", "6499-9/99"),       # filial → drop
        _row("11111111", "NEOBANK DUP", "02", "1", "6422-1/00"),      # dup base → drop
    ]) + "\n"
    recs = rb.parse_estabelecimentos(text)
    assert [r["cnpj"] for r in recs] == ["11111111000100"]
    r = recs[0]
    assert r["name"] == "NEOBANK" and r["industry"] == "banking" and r["uf"] == "SP"
    assert r["discovery_source"] == "receita_bulk"


class _FakeTable:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)


def test_propose_dedups_by_cnpj_and_skips_nameless():
    t = _FakeTable({"CNPJ#55555555": {"entity_id": "known"}})  # already registered
    rows = [
        {"cnpj": "55555555000199", "name": "KNOWN BANK", "cnae": "6422", "industry": "banking"},
        {"cnpj": "66666666000199", "name": "NEW FINTECH", "cnae": "6499", "industry": "fintech"},
        {"cnpj": "77777777000199", "name": "", "cnae": "6499", "industry": "fintech"},  # no name
    ]
    rep = rb.propose_candidates(rows, table=t)
    assert rep["already"] == 1 and rep["no_name"] == 1 and len(rep["proposed"]) == 1
    assert any(k.startswith("REVIEW#discovery:receita_66666666") for k in t.items)


def test_propose_respects_budget():
    t = _FakeTable()
    rows = [{"cnpj": f"{80000000 + i:08d}000199", "name": f"CO {i}", "cnae": "6499"}
            for i in range(5)]
    rep = rb.propose_candidates(rows, max_propose=2, table=t)
    assert len(rep["proposed"]) == 2 and rep["seen"] == 5


# --- live shard fetch (#104 unblock) -----------------------------------------------------
def test_shard_for_day_rotates_over_shards_1_to_9_excluding_0():
    seen = {rb.shard_for_day(d) for d in range(0, 30)}
    assert seen <= set(range(1, 10)) and 0 not in seen
    assert rb.shard_for_day(0) == rb.shard_for_day(9)  # cycles every 9 days


def test_latest_dump_date_parses_index_html_picks_max():
    html = '<a href="2026-06-06/">..</a><a href="2026-08-09/">..</a><a href="2026-07-01/">..</a>'
    assert rb.latest_dump_date(fetcher=lambda url: html) == "2026-08-09"


def test_latest_dump_date_fail_closed_on_network_error():
    def _boom(url):
        raise RuntimeError("mirror unreachable")
    assert rb.latest_dump_date(fetcher=_boom) is None


def test_latest_dump_date_fail_closed_when_no_dates_found():
    assert rb.latest_dump_date(fetcher=lambda url: "<html>no listing here</html>") is None


def _fake_shard_zip(path, rows_text):
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        # RFB names the member an opaque code, not the shard's own name — use one to prove
        # fetch_shard doesn't hardcode a filename.
        zf.writestr("K3241.K03200Y#.D260809.ESTABELE", rows_text.encode("latin-1"))


def test_fetch_shard_downloads_parses_and_cleans_up(tmp_path):
    rows_text = _row("11111111", "NEOBANK", "02", "1", "6422-1/00") + "\n"

    def _downloader(url, path, *, deadline=None):
        assert "Estabelecimentos3.zip" in url and "2026-08-09" in url
        _fake_shard_zip(path, rows_text)

    cands = rb.fetch_shard(3, dump_date="2026-08-09", dest_dir=str(tmp_path), downloader=_downloader)
    assert len(cands) == 1 and cands[0]["name"] == "NEOBANK" and cands[0]["industry"] == "banking"
    # the downloaded zip is cleaned up afterward — no leftover multi-hundred-MB files in /tmp
    assert list(tmp_path.iterdir()) == []


def test_fetch_shard_returns_empty_when_no_dump_date_discoverable():
    assert rb.fetch_shard(3, index_fetcher=lambda url: "<html></html>",
                          downloader=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not download"))) == []


def test_fetch_shard_best_effort_on_download_failure(tmp_path):
    def _boom(url, path, *, deadline=None):
        raise TimeoutError("exceeded budget")
    assert rb.fetch_shard(3, dump_date="2026-08-09", dest_dir=str(tmp_path), downloader=_boom) == []
