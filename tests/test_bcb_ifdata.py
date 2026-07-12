import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_ifdata


def test_market_share_groups_by_codinst_and_resolves_names():
    # IfDataValores rows carry no institution name field (confirmed against
    # the live API) — only CodInst. Grouping by name instead of CodInst was
    # the bug: every row silently collapsed into one "?" bucket.
    rows = [
        {"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 300.0},
        {"CodInst": "00068987", "NomeColuna": "Carteira de Crédito", "Saldo": 999.0},
        {"CodInst": "00012345", "NomeColuna": "Ativo Total", "Saldo": 700.0},
    ]
    names = {"00068987": "Banco A", "00012345": "Banco B"}

    result = bcb_ifdata.market_share(rows, institution_names=names)

    assert result == [
        {"institution": "Banco B", "value": 700.0, "share_pct": 70.0},
        {"institution": "Banco A", "value": 300.0, "share_pct": 30.0},
    ]


def test_market_share_falls_back_to_code_when_names_missing():
    rows = [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}]

    result = bcb_ifdata.market_share(rows)

    assert result == [{"institution": "00068987", "value": 100.0, "share_pct": 100.0}]
