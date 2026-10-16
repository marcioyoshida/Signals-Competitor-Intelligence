# Ask → chat panel: requirements and cost (2026-10-16)

Owner request: "make the ask bar a button that opens a chat panel, just like Anteater. The chat
should continue and not be limited to one question. That assumes having a context, so raise
requirements and cost related."

Reference: Anteater's phone pattern, a floating **Perguntar** button that opens a full-screen
sheet. Anteater is still single-question with no history, so multi-turn is new in both products.

## What changes for the user

- **/exec** gets a launcher button where the ask bar was. Phones also get a floating **Perguntar**
  button above the tab bar, which also appears on the Hoje view.
- The button opens a **chat panel**: docked on the right on desktop, so the board stays visible,
  and full-screen on a phone.
  - Messages show as bubbles; each answer keeps its cited sources (links to the original
    filing/news).
  - Suggestion chips appear while the conversation is empty.
  - Enter sends, Shift+Enter adds a new line, Esc closes, and `/` opens it on desktop.
  - **Nova conversa** clears the history.
- **Follow-ups work.** "e o Bradesco?", "por quê?" and "detalhe o item 2" are answered in the
  context of the conversation.
- The conversation survives a reload within the tab. It is cleared on sign-out or when the tab
  closes.
- **Same tiers as today.** Entry has no Ask on /exec, and gated or signed-out users can't send.
  Chat does not open the model to anyone who can't ask today. Under the owner's "Bedrock costs
  are for paying clients only" rule (2026-10-12), only the SaaS and sovereign officer suite asks,
  on Nova Lite.

## How context works (requirements)

1. **History is held by the client and treated as untrusted.**
   - Each turn sends at most the last **3 exchanges**: the question (≤500 chars), the answer
     (≤1,200 chars, citation markers stripped), and the cited card ids.
   - Nothing is stored server-side in v1: no transcript table and no new personal data at rest
     (LGPD). The only server-side record of a question text is still the coverage-gap capture
     for in-domain questions that have no grounding, and it now skips follow-ups.
2. **Grounding is re-done every turn** from the asker's tenant-scoped feed, the same read boundary
   as a single question. Prior answers are **conversation context, never evidence**: the
   contract says to cite only ids in this turn's DADOS block.
3. **Follow-up resolution is deterministic**, with no extra model call to rewrite the question:
   - **Scope gate:** a short question with no domain cue inherits the conversation's domain. The
     hard refuse cues (code, recipes, prompt injection) still refuse.
   - **Retrieval:** a follow-up that selects nothing on its own is retrieved together with the
     previous question.
   - **Carried citations:** the previous answer's cited cards stay in the pool, so "detalhe o
     item 2" can be answered.
4. **Bounded:**
   - **Per conversation:** 20 questions; the UI then offers a new conversation.
   - **Per user:** **150 questions/day** (server-enforced, with the counter in DynamoDB
     `OncaAskQuota`, TTL 3 days); over the limit returns a polite 429.
   - **Per answer:** 700 output tokens and a 60 s timeout, as today.
5. **Metered:** every turn logs `ask_usage` (tenant, tier, input/output tokens, history length),
   so real cost can be read from the logs instead of estimated.

## Cost (Nova Lite on-demand, us-east-1: $0.06 / 1M input, $0.24 / 1M output tokens)

Measured offline from the live feed on 2026-10-16: a single question's prompt is about
**2.3–3.5k input tokens** (12 cards plus the contract), and answers run about 350–500 output
tokens.

| | input tok | output tok | model $/turn |
|---|---|---|---|
| single question (today) | ~3.5k | ~450 | ~$0.0003 |
| chat turn (+3 exchanges of history) | ~4.6k | ~450 | ~$0.0004 |

Everything else per turn is negligible:
- KB retrieval (Titan v2 embedding plus an S3 Vectors query): < $0.00002.
- Lambda (512 MB × ~6 s): ~$0.00005.
- Quota counter write: ~$0.0000013.

**All-in: about $0.0005 per chat turn.**

| usage | turns/month | $/month |
|---|---|---|
| 20 seats × 15 turns/day × 22 days | 6,600 | ~$3 |
| 100 seats × 20 turns/day × 22 days | 44,000 | ~$22 |
| one user at the 150/day cap, every day | 4,500 | ~$2.30 (worst case per seat) |

The cost driver is **turns per user**, not the context: history adds about 30% per turn. The
daily cap bounds a runaway or scripted client to about $2.30 per seat per month. The OncaAskQuota
table and the EMF-free log line add ~$0.

## Decisions left to the owner

- **Caps:** 20 questions per conversation and 150 per user per day. Both are environment
  variables (`ONCA_ASK_DAILY_TURNS`; the client constant `CHAT_MAX_TURNS`).
- **Server-side transcripts** (history across devices, analytics on what officers ask) are NOT
  in v1. Adding them means a retention policy and an LGPD note in the privacy policy (see
  `onca-privacy-policy-retention`).
- **Streaming answers:** not in v1. The Lambda/API Gateway path returns the full answer; the
  thinking bubble shows elapsed seconds.
