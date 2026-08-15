# Job Application Pipeline — requirements spec (2026-08-15)

Status: **Draft for review.** This is a requirements specification, not an
implementation plan. It defines *what* the pipeline must do, the constraints it
must respect, and the decisions still open. No code has been written.

> **Relationship to Onça.** This is a separate product/tool from the Onça
> competitor-intelligence platform that owns this repo. It is spec'd here at the
> user's request. Where it reuses house conventions (AWS-native serverless,
> ~cost ceiling, Python 3.11+, source-cited outputs, "no login-gated scraping")
> that is called out explicitly. It could equally live as a standalone repo.

---

## 1. Goal

An assistive pipeline that, given a candidate profile, finds relevant job
postings, extracts and normalizes their requirements, matches them against the
candidate's stack, produces a **tailored** resume-highlight set and cover letter
per posting, submits (or prepares for submission) the application, and tracks
each application's status over time.

Non-goal: a fully autonomous bot that mass-applies without human review. See
§7 (Legal & platform constraints) and §8 (Human-in-the-loop gate) — the default
posture is **human-approves-before-submit**.

---

## 2. Actors & primary use case

- **Candidate (user)** — owns the profile, resume master, and preferences;
  reviews and approves tailored materials before anything is submitted.
- **Pipeline** — the six-stage system below.
- **External sources** — job boards / ATS platforms (LinkedIn, Greenhouse,
  Lever, Ashby, Indeed, company career pages), a licensed data aggregator, and
  an LLM provider for extraction and drafting.

Primary flow: user defines a **search profile** → pipeline gathers matching
postings daily → per posting it extracts requirements, matches stack, and drafts
tailored materials → user reviews a queue → approved items are submitted →
pipeline monitors status and surfaces updates.

---

## 3. Pipeline stages (the six requested capabilities)

### Stage 1 — Job gathering ("gather job posts, starting with LinkedIn")

**FR-1.1** Ingest postings from configurable sources. Priority order for the
MVP, ranked by legal/maintenance risk (lowest first):
  1. **ATS/job-board APIs that are public or partner** — Greenhouse, Lever,
     Ashby, Workday feeds; Indeed/Adzuna/USAJOBS-style APIs. Structured, stable.
  2. **A licensed jobs aggregator** (e.g. an API that resells LinkedIn/Indeed
     listings under license) for breadth including LinkedIn-sourced posts.
  3. **Company career pages** — logged-out, robots.txt-respecting fetch only.
  4. **LinkedIn directly** — see **CON-1**: LinkedIn's ToS prohibit scraping and
     automated access; direct scraping risks account bans and legal exposure.
     Access LinkedIn postings **only** via LinkedIn's official APIs (limited) or
     a licensed aggregator — never by scraping a logged-in session. (This
     mirrors the repo's existing rule: "LinkedIn-derived data ONLY via a
     licensed aggregator, never scraped.")

**FR-1.2** A **search profile** drives gathering: target titles/keywords,
seniority, location(s) + remote policy, industry, company size, min/max comp (if
available), languages, must-exclude terms/companies, and posting recency window.

**FR-1.3** Dedupe postings across sources (same role reposted / cross-listed)
via a stable key (normalized company + title + location + JD hash).

**FR-1.4** Persist raw posting + normalized fields + source URL + first-seen /
last-seen timestamps. Every downstream claim must trace back to the source URL
(house rule: no uncited synthesized output).

**FR-1.5** Incremental / diff behavior: only surface **new** postings since the
last run (a diff engine analogous to `src/diff/engine.py` in this repo).

### Stage 2 — Requirements extraction ("check requirements and pull them")

**FR-2.1** From each posting's JD, extract a structured requirements object:
  - `must_have_skills[]`, `nice_to_have_skills[]`
  - `years_experience` (min), `education`, `certifications[]`
  - `responsibilities[]`, `domain`/industry
  - `location`, `remote_policy`, `work_authorization`, `languages[]`
  - `comp` (if stated), `seniority`
  - `application_method` (ATS platform + apply URL, or email), and any
    **screening questions** the form will ask.

**FR-2.2** Extraction is LLM-assisted but must **not invent** fields not present
in the JD; unstated fields are `null`, not guessed. Keep the JD span/quote that
each extracted requirement came from (evidence) so the user can verify.

**FR-2.3** Classify each requirement as **hard** (disqualifying if unmet, e.g.
work authorization) vs **soft**. Use this in Stage 3 fit scoring.

### Stage 3 — Stack/technology matching ("pull its most-close platform stack")

**FR-3.1** Normalize posting skills to a **canonical technology taxonomy**
(e.g. map "React.js", "ReactJS", "React" → `react`; group into families:
language / framework / cloud / data / infra / tooling). Maintain a synonym map.

**FR-3.2** Represent the candidate's stack the same way (from the master
profile), then compute a **fit score**: coverage of `must_have`, coverage of
`nice_to_have`, and gaps. Weight hard requirements heavily; a missing hard
requirement caps the score.

**FR-3.3** Identify the **closest matching stack** the candidate has to what the
posting wants (the requested capability's literal reading), and surface the
**gap list** — skills the posting wants that the candidate lacks or under-
evidences — because that list drives Stage 4 tailoring and honest self-assessment.

**FR-3.4** Output per posting: `fit_score`, `matched_skills[]`, `gaps[]`,
`recommendation` (apply / stretch / skip), each with the evidence behind it.

### Stage 4 — Tailoring ("tailor resume highlights + cover letter")

**FR-4.1** Input: candidate **master resume** (superset of experience, projects,
skills, bullets) + the posting's requirements + the fit/gap analysis.

**FR-4.2** Produce **tailored resume highlights**: select and re-order the
master's bullets/projects to foreground what this posting values; optionally
rephrase bullets to mirror the JD's language — **without fabricating**
experience the candidate doesn't have. Fabrication is a hard prohibition
(§7 CON-4). Every tailored bullet must map to a real master-resume item.

**FR-4.3** Produce a **cover letter** draft: addresses the specific company/role,
references 2–3 concrete matched strengths, and honestly frames (not hides) key
gaps where appropriate. Configurable tone/length; templated with per-posting
fill-ins.

**FR-4.4** Draft answers to the posting's **screening questions** (from FR-2.1)
where they can be answered truthfully from the profile; flag any that need the
user's manual input (e.g. salary expectation, visa specifics).

**FR-4.5** All Stage-4 outputs are **drafts pending human approval** — never
auto-finalized.

### Stage 5 — Application submission ("apply with necessary posts")

**FR-5.1** After the user approves an item, submit the application through the
posting's `application_method`:
  - **ATS with an application API / partner integration** — preferred; submit
    structured payload (resume file, cover letter, field answers).
  - **Email applications** — send via the mail integration with resume + cover
    letter attached.
  - **Web forms without an API** — this is the hard case. Options: (a) generate a
    filled, ready-to-submit package and hand the user a deep link to finish in
    one click (**recommended default**, lowest ToS/CAPTCHA risk); (b) assisted
    browser automation the user watches. **Fully unattended form-filling on
    platforms that prohibit automation is out of scope** (CON-1, CON-3).

**FR-5.2** Idempotency: never submit the same application twice; record a
submission receipt (timestamp, method, confirmation id/screenshot, materials
version used).

**FR-5.3** Respect per-source rate limits and any "one application per role"
etiquette; throttle to human-plausible volumes.

**FR-5.4** Store exactly which resume/cover-letter version was sent to whom
(needed for Stage 6 and for the user's own records).

### Stage 6 — Progress monitoring ("monitor its progress")

**FR-6.1** Track each application through a **status lifecycle**:
`discovered → drafted → approved → submitted → acknowledged → screening →
interview → offer → rejected → withdrawn/expired`.

**FR-6.2** Status signals: ATS status APIs where available; **inbox parsing** of
confirmation/rejection/interview emails (via the mail integration) mapped back
to the application by company/role/thread; manual status override by the user.

**FR-6.3** Surface a dashboard/feed: pipeline funnel, per-application timeline,
items needing user action (approve draft, answer a recruiter, schedule an
interview), and stale-application follow-up reminders.

**FR-6.4** Notifications/digest on meaningful transitions (interview invite,
rejection, offer) — reuse an EventBridge→SNS / email digest pattern like the
Onça pipeline if built on the house stack.

---

## 4. Data model (core entities)

- **SearchProfile** — the Stage-1 query + user preferences.
- **CandidateProfile** — master resume, normalized skill set, prefs, PII.
- **Posting** — raw + normalized JD, source, dedupe key, first/last seen.
- **Requirements** — Stage-2 structured object + evidence spans.
- **FitAnalysis** — Stage-3 score, matched/gap lists, recommendation.
- **Application** — links Posting + the materials version sent + status +
  submission receipt + status-event history.
- **MaterialsVersion** — a specific tailored resume + cover letter + answers,
  immutable once submitted.

---

## 5. Architecture options

**Option A — reuse the house stack (recommended if kept in-account).**
AWS-native serverless: Lambda fetchers per source, EventBridge schedule, Step
Functions orchestration (`GatherTask → ExtractTask → MatchTask → TailorTask`,
then a human-approval wait, then `SubmitTask → MonitorTask`), DynamoDB for
application state, S3 for materials, Bedrock for extraction/tailoring, static
S3+CloudFront dashboard reading an aggregated `feed.json`. Fits the repo's
existing patterns and ~$100/mo ceiling.

**Option B — standalone lightweight app.** A single service + a queue + a small
DB + a hosted LLM API, if this should not be entangled with Onça infra.

The stage contracts in §3 are identical either way; the choice is deployment,
not requirements. **OPEN-1** below.

---

## 6. Non-functional requirements

- **NFR-1 Human-in-the-loop:** a mandatory approval gate between Stage 4 and
  Stage 5. Nothing is submitted without explicit per-application user approval
  (a bulk "approve all reviewed" is fine; silent auto-submit is not).
- **NFR-2 Truthfulness:** no fabricated experience, skills, or answers anywhere
  in generated materials (CON-4).
- **NFR-3 Traceability:** every extracted requirement and every generated claim
  links to its source (JD span or a real master-resume item).
- **NFR-4 Privacy/PII:** resume PII stored encrypted at rest, least-privilege
  access, retention policy, and easy full deletion. Do not send PII to sources
  or the LLM beyond what a given application requires.
- **NFR-5 Cost:** cheap models for extraction/classification, stronger models
  only for cover-letter synthesis; batch where non-real-time (mirrors the
  Onça Bedrock cost pattern). Keep to the prototype cost ceiling.
- **NFR-6 Auditability:** immutable log of what was submitted, where, and when.
- **NFR-7 Rate-limit/etiquette compliance** per source.

---

## 7. Legal & platform constraints (must-read)

- **CON-1 LinkedIn ToS.** LinkedIn prohibits scraping and automated access;
  doing so risks account termination and legal exposure. Get LinkedIn postings
  only via official APIs or a **licensed aggregator**. Do not automate a
  logged-in LinkedIn session. (Consistent with the repo's standing rule.)
- **CON-2 robots.txt / logged-out only.** Any direct web fetch (career pages)
  is logged-out and respects robots.txt (house rule).
- **CON-3 Auto-apply / anti-bot.** Many ATS/job platforms prohibit automated
  form submission and use CAPTCHA. Prefer official application APIs and the
  "prepare a one-click package for the human" pattern (FR-5.1a) over headless
  form automation. Unattended automation against platforms that forbid it is
  out of scope.
- **CON-4 No fabrication.** Generated resumes/cover letters/answers must never
  claim experience, credentials, authorization, or skills the candidate does
  not have. This is a hard product rule, not a preference.
- **CON-5 Honest identity.** Applications are submitted as the real candidate
  with their knowledge; the tool assists, it does not impersonate at scale.
- **CON-6 Data licensing.** Aggregator data used within its license terms only.

---

## 8. MVP cut vs later

**MVP:** Stages 1–4 end-to-end for **one or two API-friendly sources**
(e.g. Greenhouse + Lever + a licensed aggregator), the human-approval queue,
email-based submission (FR-5.1 email/one-click package), and Stage-6 status via
inbox parsing + manual override. Deliver the review queue as the primary UI.

**Later:** more sources incl. licensed LinkedIn breadth, ATS status APIs,
assisted browser automation for form-only postings, richer fit modeling, a
full funnel dashboard, follow-up automation.

---

## 9. Open decisions (resolve before implementation)

- **OPEN-1 Deployment:** house AWS stack (Option A) vs standalone (Option B)?
- **OPEN-2 LinkedIn access path:** which licensed aggregator (breadth, cost,
  license terms), or accept "no LinkedIn in MVP" and rely on ATS/board APIs?
- **OPEN-3 Submission posture:** one-click-package-for-human (safest) vs
  assisted browser automation — how far toward automation is acceptable given
  CON-3?
- **OPEN-4 Master-resume input format:** structured (JSON/YAML fields) vs a
  parsed PDF/DOCX master? Structured is far more reliable for tailoring.
- **OPEN-5 Screening-question answers:** how much may the tool auto-answer vs
  always defer to the user (salary, visa, demographic questions)?
- **OPEN-6 Volume/etiquette policy:** target applications-per-day cap.

---

## 10. Acceptance criteria (MVP)

1. Given a SearchProfile, the pipeline returns deduped, **new-since-last-run**
   postings from ≥2 API sources, each with a source URL.
2. For a posting, it emits a structured Requirements object with per-field
   evidence and no invented fields.
3. It emits a FitAnalysis (score, matched, gaps, recommendation) tied to the
   candidate's normalized stack.
4. It emits a tailored resume-highlight set and cover letter where **every**
   tailored bullet maps to a real master-resume item, plus drafted screening
   answers with unanswerable ones flagged.
5. No application is submitted without explicit user approval; each submission
   records an immutable receipt and the exact materials version.
6. Application status is tracked through the §3.6 lifecycle and updated from at
   least inbox parsing + manual override, with notifications on key transitions.
