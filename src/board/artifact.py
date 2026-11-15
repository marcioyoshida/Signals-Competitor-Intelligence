"""Board pack artifact (#327): the brief a board secretary reads, in pt-BR Markdown.

Rendered from the run item alone (synthesis, dissents, gaps, cited facts, provenance), so
``board_run_status`` and /exec can show it without reading the pack from S3. The pack itself
stays the source of truth: its sha256 is printed, and every number in the brief points to a
cited fact listed in the appendix.

The artifact is a *proposal* for the human gate. Nothing in it is a decision until the board
secretary records one (``decide_board_brief`` → ``record_decision``).
"""
from __future__ import annotations

from typing import Any, Mapping

BOARD_NAMES = {"cso": "Estratégia", "risk": "Risco", "cco": "Compliance", "cpo": "Produto",
               "cro": "Regulatório"}
IMPACT = {"low": "baixo", "medium": "médio", "high": "alto"}
PHASE_NAMES = {"diagnosis": "Diagnóstico", "challenge": "Contestação", "quantify": "Quantificação",
               "synthesize": "Síntese"}
STATUS_NAMES = {"complete": "completo", "partial": "parcial", "failed": "falhou",
                "refused": "recusado", "running": "em andamento", "queued": "na fila"}
NOTICE = ("Briefing externo (outside-in), construído só com dados públicos sobre a instituição e "
          "seus pares. Não é decisão de capital, liquidez ou preço, e não vê dados internos. "
          "Nenhuma opção vale como decisão até o conselho registrá-la.")


def cited_facts(synthesis: Mapping[str, Any] | None, pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The pack facts the synthesis cites (options + triggers), in pack order."""
    if not synthesis:
        return []
    ids: set[str] = set()
    for o in synthesis.get("options") or []:
        ids.update(o.get("citations") or [])
    for t in synthesis.get("monitoring_triggers") or []:
        ids.update(t.get("citations") or [])
    return [{k: f.get(k) for k in ("id", "source", "as_of", "text")}
            for f in pack.get("facts") or [] if f.get("id") in ids]


def phase_status(result: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {ph: {off: str(r.get("status")) for off, r in by.items()}
            for ph, by in (result.get("phases") or {}).items()}


def _boards(ids: list[str] | None) -> str:
    return ", ".join(BOARD_NAMES.get(i, i) for i in ids or []) or "—"


def _cell(s: Any) -> str:
    return " ".join(str(s or "").split()).replace("|", "/")


def render_markdown(run: Mapping[str, Any]) -> str:
    """The board pack. Works on partial runs too: gaps are printed, never hidden."""
    syn = run.get("synthesis") or {}
    focal = run.get("focal") or run.get("target")
    subject = run.get("subject") or focal
    out = [f"# Briefing de conselho — {focal}",
           "",
           f"> {NOTICE}",
           "",
           f"**Conselho de:** {focal}  ",
           f"**Assunto:** {subject if subject != focal else 'a própria instituição'}  ",
           f"**Pergunta:** {run.get('task')}  ",
           f"**Situação do processo:** {STATUS_NAMES.get(str(run.get('status')), run.get('status'))}"
           f" · dados do feed de {run.get('feed_as_of') or 's/d'}"
           + (f" · concorrentes: {', '.join(run.get('peers') or [])}" if run.get("peers") else ""),
           ""]
    if syn.get("summary"):
        out += ["## Situação", "", syn["summary"], ""]
    prox = run.get("proximity") or []
    if prox:
        dims = ("size", "market", "business_model", "prudential", "pressure")
        names = ("porte", "mercado", "modelo", "prudencial", "pressão")
        out += [f"## Concorrentes por proximidade a {focal}", "",
                "| Concorrente | Proximidade | " + " | ".join(names) + " | Cobertura |",
                "|---|---|" + "---|" * len(names) + "---|"]
        for x in prox:
            comps = x.get("components") or {}
            cells = [f"{comps[d]:.2f}" if comps.get(d) is not None else "s/d" for d in dims]
            score = f"{x['score']:.2f}" if x.get("score") is not None else "s/d"
            out.append(f"| {x.get('label') or x.get('peer')} | {score} | " + " | ".join(cells) +
                       f" | {x.get('coverage', 0):.0%}{' (baixa)' if x.get('low_coverage') else ''} |")
        out.append("")
    opts = syn.get("options") or []
    if opts:
        out += ["## Opções", "",
                "| # | Opção | Impacto no cliente | Sinais de concorrentes (proximidade) | Apoiada por | Contestada por |",
                "|---|---|---|---|---|---|"]
        for o in opts:
            sig = ", ".join(f"{x['entity']} ({x['proximity']:.2f})" if x.get("proximity") is not None
                            else f"{x['entity']} (s/d)" for x in o.get("competitor_signals") or []) or "—"
            out.append(f"| {o.get('id')} | {_cell(o.get('description'))} | "
                       f"{IMPACT.get(o.get('customer_impact'), o.get('customer_impact'))} | {sig} | "
                       f"{_boards(o.get('supported_by'))} | {_boards(o.get('contested_by'))} |")
        out.append("")
        for o in opts:
            eff = _cell(o.get("capital_liquidity_effect"))
            if eff:
                out.append(f"- **{o.get('id')} — efeito em capital e liquidez:** {eff} "
                           f"[{', '.join(o.get('citations') or [])}]")
        out.append("")
    dissents = run.get("dissents") or []
    if dissents:
        out += ["## Dissidências (texto integral)", ""]
        for d in dissents:
            out += [f"### {BOARD_NAMES.get(d.get('officer'), d.get('officer'))} contesta "
                    f"{_boards(d.get('challenged'))}", "", str(d.get("text") or "").strip(), ""]
            if d.get("red_lines"):
                out += ["Linhas vermelhas:", ""] + [f"- {r}" for r in d["red_lines"]] + [""]
    if syn.get("residual_regulatory_risk"):
        out += ["## Risco regulatório residual", "", syn["residual_regulatory_risk"], ""]
    trig = syn.get("monitoring_triggers") or []
    if trig:
        out += ["## Gatilhos de monitoramento", ""]
        out += [f"- {t.get('text')} [{', '.join(t.get('citations') or [])}]" for t in trig] + [""]
    gaps = [g.get("gap") if isinstance(g, dict) else g for g in run.get("gaps") or []]
    gaps += list(run.get("pack_gaps") or [])
    dropped = list(run.get("dropped") or [])
    if gaps or dropped:
        out += ["## Lacunas e itens descartados", ""]
        out += [f"- {g}" for g in gaps] + [f"- descartado: {d}" for d in dropped] + [""]
    facts = run.get("cited_facts") or []
    if facts:
        out += ["## Apêndice quantitativo — fatos citados", "",
                "| Fato | Fonte | Data | Conteúdo |", "|---|---|---|---|"]
        out += [f"| {f.get('id')} | {_cell(f.get('source'))} | {f.get('as_of') or 's/d'} | "
                f"{_cell(f.get('text'))} |" for f in facts] + [""]
    out += ["## Proveniência", "",
            f"- Execução: `{run.get('run_id')}`",
            f"- Pacote de contexto (sha256): `{run.get('pack_sha') or 's/d'}`"]
    for ph, by in (run.get("phase_status") or {}).items():
        out.append(f"- {PHASE_NAMES.get(ph, ph)}: " + ", ".join(
            f"{BOARD_NAMES.get(o, 'secretaria' if o == 'secretary' else o)} {s}" for o, s in by.items()))
    u = run.get("usage") or {}
    if u:
        out.append(f"- Uso: {u.get('calls')} chamadas, {u.get('tokens_in')} tokens de entrada, "
                   f"{u.get('tokens_out')} de saída (limite {u.get('token_cap')})"
                   + (" · modelo próprio da organização" if run.get("client_billed") else ""))
    return "\n".join(out).rstrip() + "\n"
