# Pattern: client defect / suggestion reports from the chat ("Reportar")

**Status:** reference implementation in Onça (2026-10-18). Fleet pattern: every product with a
signed-in chat or Ask surface should offer it. Owner decisions: build it; 90-day retention; the
owner triages.

## What the user gets

- A quiet **"Reportar"** control on every answered chat bubble (≥44px tap on touch).
- When the user's own message reads like a problem or an idea ("está errado", "não funciona",
  "bug", "seria bom", "sugiro"…), the chat offers it once under the next answer: *"Algo errado ou
  uma ideia? Reportar"*. The detection is a **fixed cue list**, never a model call, and the model
  never files anything on its own: the user always confirms.
- An inline form: **Defeito / Sugestão**, a text box (prefilled with the user's message when the
  cue fired), one line of consent copy ("goes only to the team, with this question and answer;
  kept 90 days"), Enviar / Cancelar. Then "Recebido — obrigado".

## Why reports never go straight to GitHub

A report carries the client's tenant, the entities they watch and the strategic question they
asked. If the product repo is **public** (Onça's is), filing it as an issue publishes client
interest; even a private repo copies client text to a third party with no retention. So the report
lands in a **private store** and the operator promotes it to a ticket **written without client
data**.

## Building blocks

| piece | Onça | notes for a fork |
|---|---|---|
| write path | self-service act `report_feedback` on `POST /api/me/act` (JWT); tenant and user come from the **verified claims**, never from the client args | reuse the fork's authenticated write endpoint; never trust a client-supplied tenant |
| store | DynamoDB `OncaFeedback`, `pk = FB#<id>`, **TTL 90 days**, no point-in-time recovery (PITR backups would stretch retention), RemovalPolicy RETAIN | same; one table per product |
| record | kind, text (≤1000), the chat turn (q ≤500, a ≤1200, cited ids ≤12), context (officer, sector, view, feed date, surface), status `novo/ticket/descartado`, created_at | keep the turn: a report without the answer it is about cannot be triaged |
| abuse bound | per-user daily cap (10), atomic counter with TTL, fails open | same |
| journal / logs | the action is in `_NO_JOURNAL`; the handler never prints the text | audit logs and CloudWatch outlive the 90 days |
| owner alert | content-free e-mail via the contact-form SES path: kind + link to triage, **no client text** | mailboxes outlive the retention too |
| triage | operator-only acts `list_feedback`, `set_feedback_status`; `/v2/admin#relatos` list with filter, "Copiar rascunho do ticket", "Marcar como ticket" (+ GitHub URL), Descartar / Reabrir | the fork's existing operator page |
| ticket draft | `ticket_draft()`: date, surface/officer/view, feed date, and placeholders. No tenant, user, question, answer, entities or card ids (ids name the entities the client watches) | same rule |
| privacy | a paragraph in the privacy policy: what a report holds, used only to improve the service, 90 days, never published | required before launch |
| account deletion | `feedback.delete_for_user(sub)`, for the self-service deletion flow | call it from the fork's deletion path |
| tiers | wherever the chat exists (Onça: SaaS + sovereign; Entry has no chat) | |

## Cost

Negligible: one DynamoDB write and one SES e-mail per report, no model call.

## Files (Onça)

- `src/dashboard/feedback.py`: store, cap, triage, draft, deletion, owner e-mail
- `src/dashboard/act_api.py`: `report_feedback` (self-service), `list_feedback`, `set_feedback_status`
- `infra/app.py`: `OncaFeedback` table, act Lambda env and IAM (SES limited to `contato@onssa.org`)
- `src/dashboard/site/v3/index.html`: chat "Reportar" + cue suggestion + inline form
- `src/dashboard/site/v2/admin/`: "Relatos" triage section
- `src/dashboard/site/docs/privacy.html`: retention paragraph
- `tests/test_feedback.py`: store, cap, draft has no client data, client can report but not
  triage, nothing journaled
