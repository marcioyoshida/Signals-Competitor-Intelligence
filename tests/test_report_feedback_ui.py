"""Client defect / suggestion reports from the /exec Ask chat (UI half; the report_feedback act is built in
parallel). Greps the buildless pages: chat Reportar control + cue list + form copy, payload shape (no
top-level officer), /v2/admin #relatos, and the privacy paragraph."""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
EXEC = (SITE / "v3" / "index.html").read_text(encoding="utf-8")
ADMIN = (SITE / "v2" / "admin" / "index.html").read_text(encoding="utf-8")
PRIVACY = (SITE / "docs" / "privacy.html").read_text(encoding="utf-8")


def _fn(src, head):
    return src.split(head, 1)[1].split("\n}\n", 1)[0]


def test_chat_answers_carry_a_quiet_reportar_control():
    assert 'data-rpt-slot="${i}"' in EXEC and "data-rpt-open" in EXEC
    assert ">${cue ? \"\" : RPT_ICON}Reportar</button>" in EXEC
    # only answered turns of a signed-in client; the operator preview (?admin=1) has no report intent
    assert "function rptReportable(t) { return !ADMIN && !!t && !t.pending && !t.error && !!t.res && !!t.res.answer; }" in EXEC
    # 44px on touch, like every other chat control
    coarse = EXEC.split("@media (pointer: coarse) {", 1)[1].split("</style>", 1)[0]
    assert ".rptbtn, .rpt__kind, .rpt .btn, .rpt .askbtn { min-height:44px; }" in coarse
    assert ".rpt textarea { font-size:16px; }" in coarse                       # no iOS focus zoom


def test_cue_list_is_fixed_and_deterministic():
    cues = _fn(EXEC, "const RPT_CUES = {")
    for w in ("está errado", "errado", "incorreto", "não funciona", "não funcionou", "bug", "erro", "problema",
              "deveria", "seria bom", "sugiro", "sugestão", "poderia ter"):
        assert f'"{w}"' in cues, w
    assert "Algo errado ou uma ideia?" in EXEC
    # no model call: the cue path never touches fetch/ask
    cue_fn = _fn(EXEC, "function rptCue(q) {")
    assert "fetch" not in cue_fn and "askOnca" not in cue_fn
    # a suggestion word alone pre-selects Sugestão, otherwise Defeito; the field is prefilled when a cue fired
    assert 'kind: cue === "sugestao" ? "sugestao" : "defeito", text: cue ? t.q : ""' in EXEC


def test_report_form_copy_and_keyboard():
    for copy in ("Defeito</button>", "Sugestão</button>", "O que aconteceu ou o que você sugere?",
                 "Seu relato vai só para a equipe da Onça, junto com esta pergunta e resposta. Guardado por 90 dias.",
                 ">Cancelar</button>", "Recebido — obrigado. A equipe da Onça vai analisar.",
                 "Limite de relatos de hoje atingido."):
        assert copy in EXEC, copy
    assert 'aria-pressed="${st.kind === "defeito"}"' in EXEC                    # toggle state is not colour alone
    assert 'e.key === "Escape"' in EXEC and "e.stopPropagation()" in _fn(EXEC, "function rptRender(slot, focus) {")
    assert 'rptRender(slot, "text")' in EXEC and 'rptRender(slot, "open")' in EXEC   # focus in on open / back on cancel
    assert "maxlength=\"1000\"" in EXEC
    # the report text is never persisted: chatSave() writes CHAT only, and the form state is a separate in-memory Map
    assert "const RPT = new Map();" in EXEC
    assert not re.search(r"sessionStorage\.setItem\([^)]*RPT", EXEC)


def test_report_payload_has_no_top_level_officer():
    send = _fn(EXEC, "async function rptSend(slot, t, st) {")
    assert 'intent: "report_feedback", idempotency_key: st.key, args: {' in send
    body = send.split("const body = {", 1)[1].split("let r, j", 1)[0]
    # top level of the body is intent / idempotency_key / args only
    assert "officer: OFFICER, sector: SECTOR === \"__all__\" ? \"todos\" : SECTOR, view: VIEW" in body                # officer lives in args.context
    assert not re.search(r"intent: \"report_feedback\"[^{]*officer\s*:", body.split("args: {", 1)[0])
    assert 'surface: "chat"' in body and "feed_as_of" in body
    assert "actFetch(JSON.stringify(body))" in send                              # same helper as record_engagement -> /api/me/act
    assert 'fetch("/api/me/act"' in _fn(EXEC, "function actFetch(body, extra) {")
    assert "slice(0, 1200)" in send and "cited:" in send
    assert "r.status === 429" in send and "r.status === 400" in send


def test_admin_relatos_section():
    assert 'id="relatos"' in ADMIN and ">Relatos</span>" in ADMIN
    for st in ("Novos", "Ticket", "Descartados", "Todos"):
        assert f">{st}</button>" in ADMIN, st
    assert 'data-st="novo" aria-pressed="true"' in ADMIN                         # Novos is the default
    assert 'prAct("list_feedback"' in ADMIN and 'prAct("set_feedback_status"' in ADMIN
    for label in ("Copiar rascunho do ticket", "Marcar como ticket", "Descartar", "Reabrir"):
        assert label in ADMIN, label
    assert "ticket_draft" in ADMIN and '`${d.title || ""}\\n\\n${d.body || ""}`' in ADMIN
    assert "não cole dados do cliente no ticket — o repositório é público" in ADMIN
    assert "args.ticket_url = url" in ADMIN and "<summary>Resposta</summary>" in ADMIN   # answer collapsed
    assert 'id="rlCount"' in ADMIN


def test_privacy_page_discloses_reports_with_90_day_retention():
    assert "Relatos de defeito e sugestão" in PRIVACY
    para = PRIVACY.split("<li><b>Relatos de defeito e sugestão.</b>", 1)[1].split("</li>", 1)[0]
    for part in ("chat da Onça", "a pergunta e a resposta daquele turno", "melhorar o serviço", "90 dias",
                 "apagamos automaticamente", "nunca são publicados"):
        assert part in para, part
    retention = PRIVACY.split("<h2>Por quanto tempo guardamos</h2>", 1)[1].split("</ul>", 1)[0]
    assert "Relatos de defeito e sugestão:</b> 90 dias" in retention
