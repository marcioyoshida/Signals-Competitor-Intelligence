# LGPD posture

Onça's subject matter is companies, not people. Where a person unavoidably appears
(a director named in a public filing, a controlling shareholder), the product
applies the same guardrails everywhere that data can enter — they are enforced in
the ingestion/synthesis code itself, not as a downstream filter.

## The person-graph guardrails

- **No full CPF, ever.** Only a **masked** CPF (`***XXXXXX**`) is read from source
  registries (e.g. Receita/QSA shareholder data), and the ingestion code
  (`operatives.py`) **re-masks defensively** on every read — a full CPF cannot be
  persisted even if an upstream source were to leak one.
- **Name + role + public document only.** A person is only recorded with
  name, professional role, and the public, already-masked identifying reference
  their role appears under (e.g. company officer, listed on a public cadastral
  registry) — never a private communication, never a home address, never contact
  details.
- **Institutional names excluded.** The person-graph never conflates a company name
  appearing in a director/shareholder field with an actual natural person.
- **Cross-entity cohorting is a resolved fact, not a guess.** Two entities sharing
  the same (name, masked-CPF) pair are treated as genuinely the same controlling
  person — this lets Onça surface common-control clusters without ever
  reconstructing or storing the full CPF that would make the match "clean" in a
  privacy-invasive way.

## Defamation / accuracy discipline for sensitive claims

Any claim that could damage a company's or person's reputation (distress/RJ status,
integrity/compliance findings, sanctions) is held to a stricter bar than an
ordinary competitive-intelligence card:

- The underlying event must be **independently observed in a public role** — a
  court filing reported in the press, a sanctions-registry entry, a regulator
  action — never an inference or a single ambiguous mention.
- Entity resolution for these claims is **anchored**, not fuzzy: a distress or
  integrity finding only attaches to an entity when the resolver can confirm it
  with the same confidence bar used everywhere else in the registry, not a looser
  one because the story is more "interesting."
- An empty compliance/integrity panel for a sector is explicitly labeled
  "no findings ingested for this scope" — the product never lets an absence of
  data read as "audited and clean."

## What we deliberately do not hold

- No CPF (full), no RG, no home address, no phone number, no private
  communications, no biometric data, no health data, no data about consumers or
  end-users of the tracked companies — only about the companies themselves and
  their public officers/controllers acting in a public capacity.
- No scraping of anything behind a login wall (see
  [sources-and-licensing](sources-and-licensing.md)) — every person-adjacent fact
  Onça holds was already public before Onça touched it.

## Data subject rights

Because the product does not hold private personal data as defined above, most
LGPD data-subject-request categories (correction, portability, erasure of personal
data) do not apply to the corpus in the way they would to a consumer product. Where
a named individual's public-role information is wrong (e.g. a stale directorship),
the correction path is the same as any other registry correction: through
`OncaCurationLog`'s governed, provenance-tracked curation flow (ADR 018).
