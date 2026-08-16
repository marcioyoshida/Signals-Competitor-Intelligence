import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_autorizacoes as ba


def test_classify_license_maps_fintech_segments():
    cases = {
        "Instituição de Pagamento": "Instituição de Pagamento",
        "Sociedade de Crédito Direto": "Crédito Direto (SCD)",
        "Sociedade de Empréstimo entre Pessoas": "Empréstimo P2P (SEP)",
        "Sociedade de Crédito, Financiamento e Investimento": "Financeira (SCFI)",
        "Sociedade de Crédito ao Microempreendedor": "Microcrédito (SCMEPP)",
        "Banco Múltiplo": "Banco",
        "Sociedade Distribuidora de TVM": "Corretora/DTVM",
        "Cooperativa de Crédito": "Cooperativa",  # 'crédito' appears but 'cooperativa' rule wins order? see note
    }
    for seg, expected in cases.items():
        assert ba.classify_license(seg) == expected, seg


def test_classify_license_unknown_passthrough():
    assert ba.classify_license("Something Exotic") == "Something Exotic"
    assert ba.classify_license("") == "Outro"
    assert ba.classify_license(None) == "Outro"


def test_normalize_tags_fintech_flag():
    row = {"CNPJ": "123", "NOME_INSTITUICAO": "FOO IP LTDA", "SEGMENTO": "Instituição de Pagamento"}
    rec = ba._normalize(row, "SedesSociedades")
    assert rec["license_class"] == "Instituição de Pagamento"
    assert rec["is_fintech"] is True

    row2 = {"CNPJ": "9", "NOME_INSTITUICAO": "X CONSORCIO", "SEGMENTO": "Administradora de Consórcio"}
    rec2 = ba._normalize(row2, "SedesConsorcios")
    assert rec2["license_class"] == "Consórcio"
    assert rec2["is_fintech"] is False
