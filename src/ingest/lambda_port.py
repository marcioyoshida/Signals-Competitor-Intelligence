"""Lambda-style prototype for the Phase 1.5 ingestion pipeline.

This keeps the fetch functions pure and exposes a single event handler that
can later be wired to EventBridge + Lambda with minimal changes.

Sources:
  - BCB normativos (regulatory) — detect_new
  - CVM funds RCVM 175 registry (competitor launches) — detect_new
  - BCB IF.data market share — snapshot (no id-diff)
  - BCB autorizações (new entrants) — detect_new, first-run seed suppressed
  - BCB Pix (traction moves) — detect_moves via DynamoDB value state
  - BCB juros médios (pricing moves) — detect_moves via DynamoDB value state
  - CVM ofertas de distribuição — detect_new, first-run seed suppressed
  - SEC EDGAR (US-listed fintechs) — detect_new, first-run seed suppressed
  - CVM Informe Diário (fund AUM moves) — detect_moves via DynamoDB value state
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from typing import Any

import boto3

from src.diff.engine import DynamoDbState, DynamoDbValueState, detect_moves, detect_new


from src.ingest.budget import SourceBudgetExceeded as _SourceBudgetExceeded  # noqa: E402


def _dou_with_stats(recs: list) -> list:
    """#191: publish the DOU run's paging telemetry (queries, pages, SATURATED terms — a full
    page limit still inside the lookback) to source_health as metrics of "DOU saturation"."""
    try:
        from src.ingest import dou as _dou
        from src.ingest import source_health

        st = dict(_dou.LAST_STATS or {})
        sat_recs = st.get("saturated") or []
        sat = list(dict.fromkeys(x["term"] for x in sat_recs))
        # #199: a truncation leaving ≤ SATURATION_WARN_DAYS of the lookback unfetched is a
        # warning; a larger (or unmeasured) gap is an error
        worst = {}
        for x in sat_recs:
            d = x.get("missing_days")
            worst[x["term"]] = max(worst.get(x["term"], -1), 10**6 if d is None else int(d))
        hard = [t for t in sat if worst[t] > _dou.SATURATION_WARN_DAYS]
        soft = [t for t in sat if t not in hard]
        msg = lambda ts: f"{len(ts)} saturated: " + ", ".join(ts[:8])  # noqa: E731
        source_health.record("DOU saturation", ok=not hard, docs=len(recs),
                             error=msg(hard) if hard else None,
                             warning=msg(soft) if soft else None,
                             metrics={"queries": st.get("queries", 0), "pages": st.get("pages", 0),
                                      "saturated_terms": sat,
                                      "missing_days": {t: worst[t] for t in sat},
                                      "paged_terms": list(dict.fromkeys(
                                          x["term"] for x in st.get("paged") or [])),
                                      # #199: "todos" full at the cap → older span via DO1/DO2 only
                                      "split_terms": list(dict.fromkeys(
                                          x["term"] for x in st.get("split") or [])),
                                      # #197: stem-matched entity queries (not paged, not volume)
                                      "fuzzy_terms": list(dict.fromkeys(
                                          x["term"] for x in st.get("fuzzy") or []))})
    except Exception:  # pragma: no cover - telemetry must never affect ingestion
        pass
    return recs


def _record_health(label: str, *, ok: bool, error: str | None = None,
                   idle: bool = False) -> None:
    """#76: best-effort per-source run telemetry into the source-health ledger. Never raises."""
    try:
        from src.ingest import source_health

        source_health.record(label, ok=ok, error=error, idle=idle)
    except Exception:  # pragma: no cover - telemetry must never affect ingestion
        pass


@contextmanager
def _source_budget(label: str, deadline: float, per_source: int):
    """Bound one source's wall-clock time so a slow endpoint can't eat the run.

    Enforces both a per-source cap and the overall ingest deadline. If the
    deadline has already passed, the source is skipped. Uses SIGALRM (main
    thread only); elsewhere it degrades to a no-op and relies on the caller's
    deadline checks. The raised error is an Exception, so each source's existing
    ``except Exception`` handler catches it and falls back to an empty result.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        # #76: a source skipped for the deadline is a reliability event, not silence.
        _record_health(label, ok=False, error="skipped (ingest deadline reached)")
        raise _SourceBudgetExceeded(f"{label} skipped (ingest deadline reached)")

    use_alarm = hasattr(signal, "SIGALRM") and (
        threading.current_thread() is threading.main_thread()
    )
    if not use_alarm:
        try:
            yield
        except Exception as exc:
            _record_health(label, ok=False, error=str(exc))
            raise
        else:
            _record_health(label, ok=True)
        return

    secs = max(1, int(min(per_source, remaining)))
    # SIGALRM is ONE-SHOT, and a budget exception raised inside a per-item
    # ``except Exception`` (e.g. bcb_ifdata.map_to_entities' best-effort resolver
    # loop) or inside botocore (which re-wraps it as a retryable HTTPClientError)
    # is SWALLOWED — after which the source runs completely unbounded and eats the
    # whole 900s Lambda budget. Observed live 2026-09-08..12: structured ingest
    # timed out at 900s with zero log output. So RE-ARM the alarm on every fire:
    # whoever swallows it gets hit again a few seconds later, until the with-block
    # exits and the ``finally`` below disarms it for good.
    retry_every = max(1, int(os.environ.get("ONCA_SOURCE_BUDGET_REARM_SEC", "5")))

    def _fire(signum, frame):
        signal.alarm(retry_every)
        raise _SourceBudgetExceeded(f"{label} exceeded {secs}s budget")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(secs)
    ok, err = True, None
    try:
        yield
    except Exception as exc:  # includes _SourceBudgetExceeded (timeout)
        ok, err = False, str(exc)
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        _record_health(label, ok=ok, error=err)  # #76 per-source run telemetry


def _ingest_deadline(context: Any) -> float:
    """Monotonic time by which ingestion must stop starting new work.

    Derived from the Lambda's actual remaining time (minus a reserve for the
    S3 digest write + return), so it adapts to the configured timeout. Falls
    back to a fixed budget when no Lambda context is available (local/tests).
    """
    reserve = int(os.environ.get("ONCA_INGEST_RESERVE_SEC", "25"))
    fallback = int(os.environ.get("ONCA_INGEST_BUDGET_SEC", "780"))
    now = time.monotonic()
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        remaining_s = context.get_remaining_time_in_millis() / 1000.0
        return now + max(5.0, remaining_s - reserve)
    return now + fallback
from src.ingest import (
    bcb_autorizacoes,
    bcb_ifdata,
    bcb_soundness,
    bcb_juros,
    bcb_macro,
    bcb_normativos,
    bcb_pix,
    cvm_fiagro,
    cvm_fundos,
    cvm_inf_diario,
    cvm_ipe,
    cvm_normas,
    cvm_ofertas,
    ans_igr,
    bcb_reclamacoes,
    cade,
    ceis_cnep,
    consumidor_gov,
    datajud,
    dou,
    federal_acts,
    pncp_contratos,
    raw_writer,
    registry,
    receita_cnpj,
    reclame_aqui,
    sec_filings,
    trade_press,
)


def _new_since_last_run(
    source: str,
    docs: list[dict[str, Any]],
    *,
    seed_if_empty: bool = False,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Diff docs against DynamoDB-backed state; degrade gracefully on failure.

    When seed_if_empty is True (autorizações registry), the first run with
    an empty state table seeds the baseline and reports nothing — otherwise
    every authorized institution would appear as a "new entrant".

    ``commit=False`` computes the fresh set without marking anything seen; the
    caller must commit later via ``diff.engine.commit_seen`` once the items are
    actually consumed (the news → synth deferred-commit path, issue #23).
    """
    try:
        state = DynamoDbState(source)
        if hasattr(state, "load"):
            state.load()
        was_empty = len(state.seen) == 0
        fresh = detect_new(source, docs, state=state, commit=commit)
        if seed_if_empty and was_empty and docs:
            print(
                f"Info: {source} baseline seeded ({len(docs)} items); "
                "new items will surface from the next run on."
            )
            return []
        return fresh
    except Exception as exc:  # pragma: no cover - defensive handling for state-table issues
        print(f"Warning: {source} diff state unavailable: {exc}")
        # Never dump a full authorization registry as "new" when state is broken.
        if seed_if_empty:
            return []
        return docs


def _moves_since_last_run(
    source: str,
    items: list[dict[str, Any]],
    key_field: str,
    value_field: str,
    min_pct: float,
) -> list[dict[str, Any]]:
    """Compare numeric series against DynamoDB value state; no inventing moves on failure."""
    try:
        state = DynamoDbValueState(source)
        return detect_moves(
            source,
            items,
            key_field=key_field,
            value_field=value_field,
            min_pct=min_pct,
            state=state,
        )
    except Exception as exc:  # pragma: no cover - defensive handling for state-table issues
        print(f"Warning: {source} value state unavailable, skipping moves: {exc}")
        return []


def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _tag_new(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark delta rows for Stage B prioritization without mutating originals."""
    out: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        row = {k: v for k, v in d.items() if k != "raw"}
        row["is_new"] = True
        out.append(row)
    return out


def _strip_raw(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact rows for digest context (drop bulky raw payloads).

    SEC primary documents can be 100k+ chars; digests keep subject + a short
    excerpt so Stage B fusion stays light. Full text is on the raw S3 object.
    """
    max_text = int(os.environ.get("ONCA_DIGEST_TEXT_CHARS", "2000"))
    out: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        row = {k: v for k, v in d.items() if k != "raw"}
        row.setdefault("is_new", False)
        text = row.get("text")
        if isinstance(text, str) and len(text) > max_text:
            row["text"] = text[:max_text].rstrip() + "…"
        out.append(row)
    return out


# --- ADR 019 Phase 2 — registry-driven source runner ---------------------------------------
# The (gate → budget → fetch → delta → standard digest section) pattern, driven by a
# SourceSpec, replacing the per-source hand-coded blocks. Migration is source-by-source;
# the FS-core sources with interleaved side effects move over incrementally.

_VERTICAL_WARNED: set[str] = set()


def _active_vertical() -> str:
    """The market this deployment serves (ADR 019 Phase 3). Onça = financial-services;
    the Anteater sectorial deployment sets ONCA_VERTICAL to a sector."""
    v = os.environ.get("ONCA_VERTICAL", registry.VERTICAL_FS)
    if not registry.is_known_vertical(v) and v not in _VERTICAL_WARNED:
        _VERTICAL_WARNED.add(v)
        print(f"Warning: ONCA_VERTICAL={v!r} is not a known vertical "
              f"{registry.KNOWN_VERTICALS}; failing closed (only sector-agnostic sources run).")
    return v


def _vertical_ok(spec: "registry.SourceSpec") -> bool:
    """Whether ``spec`` applies to the active vertical (sector-agnostic = ``all``)."""
    return registry.ALL in spec.verticals or _active_vertical() in spec.verticals


def _fs_jobs_ok() -> bool:
    """C5 (#303): the registry jobs that grow the financial-services entity universe (prudential
    solvency, Receita bulk CNAE, entity discovery, the regulated roster promote) are not
    SourceSpecs but are FS-only: they run only under the FS vertical."""
    return _active_vertical() == registry.VERTICAL_FS


def _source_enabled(spec: "registry.SourceSpec") -> bool:
    """Gate a source on its vertical applicability (ADR 019 Phase 3) then its env flag."""
    if not _vertical_ok(spec):
        return False
    default = "true" if spec.default_on else "false"
    flag = spec.env_flag or f"ONCA_{spec.id.upper()}"
    return os.environ.get(flag, default).lower() in ("1", "true", "yes")


def _lens_section(
    records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    spec: "registry.SourceSpec",
    **extra: Any,
) -> dict[str, Any]:
    """The standard {count, new_count, items, context} digest section, with per-source
    limits taken from the spec (declarative). ``extra`` adds source-specific keys."""
    return {
        "count": len(records),
        "new_count": len(new_records),
        **extra,
        "items": _tag_new(new_records[: spec.items_limit]),
        "context": _strip_raw(records[: spec.context_limit]),
    }


def _gated_source(
    spec: "registry.SourceSpec",
    *,
    deadline: float,
    per_source: int,
    fetch: "Any",
    store: "Any" = None,
    post: "Any" = None,
    applicable: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one registry source: gate → budget → fetch → delta → optional store → optional post.

    ``fetch()`` returns the source's records; ``store(records)`` persists a durable index
    when the source is store-integrated. ``post(records, new_records)`` (C2, #303) is the
    source's own side effect on this run's result (SEC content enrichment, the fatos
    governance sort, the BCB register certifications), run inside the same budget so its
    timing and telemetry are what they were. ``applicable=False`` (an empty watchlist) skips
    the source before its budget. Returns (records, new_records). Best-effort — a failure
    degrades to ([], []) like every other source here."""
    if not applicable or not _source_enabled(spec):
        return [], []
    records: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    try:
        with _source_budget(spec.label or spec.id, deadline, per_source):
            records = fetch() or []
            new_records = _new_since_last_run(
                spec.state_key or spec.id, records, seed_if_empty=spec.seed_if_empty
            )
            if store is not None and records:
                store(records)
            if post is not None:
                post(records, new_records)
    except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
        print(f"Warning: {spec.label or spec.id} fetch failed: {exc}")
    return records, new_records


def _moves_section(
    tracked: list[dict[str, Any]],
    moves: list[dict[str, Any]],
    spec: "registry.SourceSpec",
    **extra: Any,
) -> dict[str, Any]:
    """C1 (#303): the numeric-moves digest section — ``{<tracked_key>, …extra, move_count,
    items, context}`` (key order is part of the digest bytes)."""
    return {
        spec.tracked_key or "tracked": len(tracked),
        **extra,
        "move_count": len(moves),
        "items": _tag_new(moves[: spec.items_limit]),
        "context": _strip_raw(tracked[: spec.context_limit]),
    }


def _gated_moves(
    spec: "registry.SourceSpec",
    *,
    deadline: float,
    per_source: int,
    fetch: "Any",
    min_pct: float | None = None,
    applicable: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """C1 (#303): run one delta="moves" source: gate → budget → fetch → moves diff.

    ``fetch()`` returns ``(tracked, series, aux)``. ``applicable=False`` (e.g. an empty
    watchlist) skips the source before its budget, so no state or health row is touched, as
    before the loop. When ``spec.key_field`` is set,
    ``series`` is diffed by ``_moves_since_last_run(spec.state_key, …)``; otherwise the fetcher
    already returns the moves (FIAGRO's two series + new registrations). Returns
    (tracked, moves, aux); best-effort, a failure degrades to empty."""
    if not applicable or not _source_enabled(spec):
        return [], [], {}
    tracked: list[dict[str, Any]] = []
    moves: list[dict[str, Any]] = []
    aux: dict[str, Any] = {}
    try:
        with _source_budget(spec.label or spec.id, deadline, per_source):
            tracked, series, aux = fetch()
            if spec.key_field:
                moves = _moves_since_last_run(
                    spec.state_key or spec.id, series, key_field=spec.key_field,
                    value_field=spec.value_field, min_pct=float(min_pct or 0.0),
                )
            else:
                moves = series
    except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
        print(f"Warning: {spec.label or spec.id} fetch failed: {exc}")
    return tracked, moves, aux


def _gated_static(
    spec: "registry.SourceSpec",
    *,
    deadline: float,
    per_source: int,
    fetch: "Any",
) -> list[dict[str, Any]]:
    """C1 (#303): a delta="none" source (a snapshot, e.g. the IF.data market top): gate →
    budget → fetch, no seen-state."""
    if not _source_enabled(spec):
        return []
    try:
        with _source_budget(spec.label or spec.id, deadline, per_source):
            return fetch() or []
    except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
        print(f"Warning: {spec.label or spec.id} fetch failed: {exc}")
    return []


def _populate_corpus_and_sync(new_docs: list[dict[str, Any]]) -> None:
    """Write new docs to the raw corpus bucket and trigger a KB ingestion sync.

    A corpus/sync failure must never break the digest response, matching the
    graceful-degradation pattern used for every other external call here.
    """
    raw_bucket = os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        return

    # Defense-in-depth: never write an unbounded number of objects in one run.
    # A broken diff state once marked every fund "new"; capping keeps a single
    # run's corpus writes (and the ingest Lambda's wall clock) bounded.
    max_docs = int(os.environ.get("ONCA_MAX_CORPUS_DOCS", "300"))
    if len(new_docs) > max_docs:
        print(
            f"Info: corpus write capped at {max_docs} of {len(new_docs)} new docs"
        )
        new_docs = new_docs[:max_docs]

    try:
        written = raw_writer.write_raw_documents(raw_bucket, new_docs)
    except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
        print(f"Warning: raw corpus write failed: {exc}")
        return

    if not written:
        return

    kb_id = os.environ.get("ONCA_KB_ID")
    data_source_id = os.environ.get("ONCA_KB_DATA_SOURCE_ID")
    if not kb_id or not data_source_id:
        return

    try:
        boto3.client("bedrock-agent").start_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=data_source_id
        )
    except Exception as exc:  # pragma: no cover - defensive handling for KB sync failures
        print(f"Warning: KB ingestion sync failed: {exc}")


def _promote_decisions_to_kb() -> None:
    """ADR 021 §H/§F — continuous, seen-set-gated promotion of closed decisions (+ the officer
    reference) into the KB, so an officer's next grounded read retrieves its own precedents.
    Gated by ONCA_DECISION_KB; best-effort — never breaks the ingest run."""
    if os.environ.get("ONCA_DECISION_KB", "").lower() not in ("1", "true", "yes"):
        return
    raw_bucket = os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        return
    try:
        from src.synth import decision_kb, decision_log, executive

        promoted = decision_kb.promote_decisions(decision_log.list_decisions(), bucket=raw_bucket)
        seeded = decision_kb.seed_reference(executive.build_reference(), bucket=raw_bucket)
    except Exception as exc:  # pragma: no cover - promotion best-effort
        print(f"Warning: decision KB promotion failed: {exc}")
        return
    if not promoted and not seeded:
        return
    print(f"Decision KB: promoted={len(promoted)} decisions, reference seeded={seeded}")
    kb_id = os.environ.get("ONCA_KB_ID")
    data_source_id = os.environ.get("ONCA_KB_DATA_SOURCE_ID")
    if kb_id and data_source_id:
        try:
            boto3.client("bedrock-agent").start_ingestion_job(
                knowledgeBaseId=kb_id, dataSourceId=data_source_id
            )
        except Exception as exc:  # pragma: no cover
            print(f"Warning: decision KB ingestion sync failed: {exc}")


def _news_slice(context: Any) -> dict[str, Any]:
    """Trade-press (Google News RSS + outlet feeds) news slice of the digest.

    Factored out so it can run as its OWN parallel Step Functions branch: news
    is the only source that scales linearly with the registry (one HTTP per
    term), so it no longer shares a wall-clock budget with the fixed-cost
    structured sources. News is not written to the corpus, so this path has no
    corpus/state coupling with the structured branch (disjoint diff sources)."""
    # C5 (#303): the news lens is a registry source like any other; a vertical it does not
    # serve gets the empty slice and runs no fetch.
    if not _source_enabled(registry.by_id("news")):
        return _empty_news_slice()
    NP = registry.resolve_params(registry.by_id("news"))   # C3 (#303): the news knobs
    news_lookback = NP["lookback_days"]
    competitors = registry.resolve_params(registry.by_id("competitor"))["admins"]
    news_terms = NP["watchlist"]
    if NP["use_competitors"]:
        news_terms = list(dict.fromkeys(news_terms + competitors))
    # Derive news terms from the registry (source of truth for tracked entities)
    # so the news set can't drift out of sync with it (the C6/PicPay silence bug).
    if os.environ.get("ONCA_ENTITIES_TABLE") and NP["use_registry"]:
        try:
            from src.synth import entity_registry

            reg_terms = entity_registry.news_terms()
            have = {t.lower() for t in news_terms}
            news_terms = news_terms + [t for t in reg_terms if t.lower() not in have]
            print(f"News terms: {len(reg_terms)} from registry, {len(news_terms)} total")
        except Exception as exc:  # pragma: no cover - best-effort, config still works
            print(f"Warning: registry news terms unavailable, using config: {exc}")

    def _news_excludes() -> dict[str, list[str]] | None:
        if not os.environ.get("ONCA_ENTITIES_TABLE"):
            return None
        try:
            from src.synth import entity_registry

            return entity_registry.news_excludes()
        except Exception as exc:  # pragma: no cover - best-effort, the veto is optional
            print(f"Warning: registry news excludes unavailable: {exc}")
            return None

    deadline = _ingest_deadline(context)
    per_source = int(os.environ.get("ONCA_SOURCE_TIMEOUT_SEC", "90"))
    news_items: list[dict[str, Any]] = []
    new_news: list[dict[str, Any]] = []
    outlet_items: list[dict[str, Any]] = []

    # #176 sector topic queries: per covered industry (registry.INDUSTRY_TOPICS, ≤3 each),
    # OUTSIDE ONCA_NEWS_MAX_TERMS and in their OWN wall-clock budget — the entity loop already
    # runs close to its per-source cap at registry scale, and a sector-wide measure (MP 1.394)
    # must not be the thing that gets cut. Items: query_kind "sector", industries, no entity.
    sector_items: list[dict[str, Any]] = []
    sector_timing: dict[str, Any] = {}
    # (Runs whenever the news lens is configured — news_terms non-empty, as in every deployment.)
    if news_terms and NP["sector_queries"]:
        queries = registry.news_topic_queries(_active_vertical())
        t0 = time.monotonic()
        try:
            with _source_budget("Sector news", deadline, per_source):
                sector_items = trade_press.fetch_sector_news(
                    queries, registry.industry_vocabulary(_active_vertical()),
                    lookback_days=news_lookback)
        except Exception as exc:  # pragma: no cover - best-effort, like every source
            print(f"Warning: sector news fetch failed: {exc}")
        sector_timing = {"queries": sum(len(v) for v in queries.values()),
                         "seconds": round(time.monotonic() - t0, 1), "items": len(sector_items)}
        print(f"Sector news: {sector_timing}")

    if news_terms:
        try:
            with _source_budget("Trade press", deadline, per_source):
                news_items = trade_press.fetch_news(
                    news_terms,
                    lookback_days=news_lookback,
                    max_terms=NP["max_terms"],
                    # #135 mode 2: curated per-entity disqualifying phrases for
                    # true homonyms. Best-effort — a registry hiccup must not
                    # take the whole news lens down, it only loses the veto.
                    excludes=_news_excludes(),
                    direct_sink=outlet_items,
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: trade-press fetch failed: {exc}")
    # dedupe sector items against entity items (a duplicate keeps the entity item + gains the
    # industries), then the deferred seen-set diff over the merged set
    if sector_items and outlet_items:
        n = trade_press.prefer_direct_links(sector_items, outlet_items)
        print(f"Sector news: {n} Google News link(s) swapped for the outlet's own")
    news_items = trade_press.merge_sector_news(news_items, sector_items)
    # #354 distress sweep: every fetched outlet headline + the distress topic queries, kept when
    # it classifies as distress EVEN IF it names no tracked entity (resolved in synth's distress
    # watch). Its own small budget; never touches the seen-set (the distress store dedups).
    distress_items: list[dict[str, Any]] = []
    if news_terms and os.environ.get("ONCA_DISTRESS_SWEEP", "on") != "off":
        try:
            with _source_budget("Distress sweep", deadline, min(per_source, 45)):
                d_lookback = max(news_lookback, trade_press.DISTRESS_LOOKBACK_DAYS)
                queried = trade_press.fetch_distress_queries(lookback_days=d_lookback)
                distress_items = trade_press.distress_sweep(
                    outlet_items + queried, known=news_items, lookback_days=d_lookback)
            print(f"Distress sweep: {len(distress_items)} headline(s) "
                  f"from {len(outlet_items)} outlet + {len(queried)} query items")
        except Exception as exc:  # pragma: no cover - best-effort, like every source
            print(f"Warning: distress sweep failed: {exc}")
    if news_items:
        try:
            # Deferred commit (issue #23): compute the fresh set but do NOT
            # mark anything seen here. Synth commits ``fetched_ids`` only
            # after it has consumed this slice, so a fetch-only run or a
            # failed/retried synth never burns the news — the items simply
            # re-surface next run instead of leaving the entity falsely
            # silent. Seed suppression on a truly-empty state still holds.
            new_news = _new_since_last_run(
                "trade_press", news_items, seed_if_empty=True, commit=False
            )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: trade-press delta failed: {exc}")
    return {
        "count": len(news_items),
        "new_count": len(new_news),
        # These caps bound what SYNTH can see. The old 12/15 were sized for a
        # ~15-entity watchlist; at registry scale (60+ entities) the most-recent
        # slice is monopolised by a few high-volume names (BTG, BB, Bradesco),
        # starving low-volume entities (crypto, consórcio, advisory) of synth
        # visibility even when their news clears the corroboration gate. Persist
        # ALL new items (env-tunable) + a broad context sample; rows are compact.
        # #176: sector-query items get their OWN caps on top — measured live 2026-09-27, the
        # first run returned 147 new sector items and, in one shared cap, crowded 18 of 38 new
        # entity items out of `items`. Entity items keep exactly their pre-#176 budget.
        "items": (_tag_new(_entity_rows(new_news)[: NP["digest_items"]])
                  + _tag_new(_sector_rows(new_news)[: NP["digest_sector_items"]])),
        "context": (_strip_raw(_entity_rows(news_items)[: NP["digest_context"]])
                    + _strip_raw(_sector_rows(news_items)[: NP["digest_sector_context"]])),
        # Every fetched id (not just the capped items/context) so synth commits
        # the exact set it saw to the trade_press seen-set — the second phase of
        # the deferred diff above. Compact: bare id strings.
        "fetched_ids": [d["id"] for d in news_items if isinstance(d, dict) and d.get("id")],
        # #354: compact distress headlines (any subject), read only by the distress miner.
        "distress_items": distress_items,
    }


def _sector_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if isinstance(r, dict) and r.get("query_kind") == "sector"]


def _entity_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if isinstance(r, dict) and r.get("query_kind") != "sector"]


def _empty_news_slice() -> dict[str, Any]:
    return {"count": 0, "new_count": 0, "items": [], "context": [], "fetched_ids": []}


def _write_news_digest(slice_: dict[str, Any], context: Any) -> dict[str, Any]:
    """Persist the news slice to its own S3 prefix so synth can overlay it onto
    the structured base digest (the two branches write disjoint objects)."""
    payload = {"news": slice_, "source": "news_ingest"}
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if bucket:
        try:
            s3 = boto3.client("s3")
            key = f"lambda-digests/news/{getattr(context, 'aws_request_id', 'local')}.json"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
            print(f"Warning: S3 news upload failed: {exc}")
    return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False)}



def concurrency_guard(event: dict[str, Any], sfn_client: Any | None = None) -> dict[str, Any]:
    """#196: ``{"busy": bool, "blocking": arn|None}`` — is an EARLIER execution of this state
    machine still running? Every downstream task reads the *latest* digest in S3, so two
    overlapping executions swap digests: one run's synth consumed the other's 0-new digest
    after its own ingest had committed the seen-state, and the new acts reached no feed
    (2026-09-27). The later execution skips BEFORE ingesting — nothing is consumed; the
    earlier one produces the fresh feed. Ties (same start) break on the execution ARN.
    Fails OPEN: if the check itself errors, the run proceeds (never block the pipeline)."""
    sm = (event or {}).get("state_machine")
    me = (event or {}).get("execution")
    if not sm or not me:
        return {"busy": False, "blocking": None}
    try:
        client = sfn_client or boto3.client("stepfunctions")
        running = client.list_executions(stateMachineArn=sm, statusFilter="RUNNING",
                                          maxResults=20).get("executions") or []
        mine = next((e for e in running if e.get("executionArn") == me), None)
        my_key = (mine or {}).get("startDate"), me
        for e in running:
            arn = e.get("executionArn")
            if arn == me:
                continue
            if mine is None or (e.get("startDate"), arn) < my_key:
                print(f"Warning: concurrent execution {arn} still running; skipping this run")
                return {"busy": True, "blocking": arn}
    except Exception as exc:  # pragma: no cover - defensive: fail open
        print(f"Warning: concurrency guard failed open: {exc}")
    return {"busy": False, "blocking": None}


_OFFICIAL_ACT_FIELDS = ("id", "source", "kind", "organ", "doc_type", "title", "date", "url",
                        "industries", "compliance_tags", "severity", "severity_reason")


def _official_acts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """#198: compact, uncapped list of new official acts rated medium+ with a classification."""
    acts: dict[str, dict[str, Any]] = {}
    for r in records:
        if not isinstance(r, dict) or not r.get("id") or r.get("kind") != "regulatory":
            continue
        if str(r.get("severity") or "").lower() not in ("medium", "high", "critical"):
            continue
        if not (r.get("industries") or r.get("compliance_tags")):
            continue
        a = {f: r.get(f) for f in _OFFICIAL_ACT_FIELDS if r.get(f) is not None}
        a["text"] = str(r.get("text") or r.get("subject") or "")[:1500]
        acts.setdefault(str(r["id"]), a)
    return {"count": len(acts), "acts": list(acts.values())}


# C1 (#303): the structured digest's established section order. Registry-loop sources come
# first in registry order in the pre-C1 layout; this list pins that layout now that more
# sections are built in the loop.
_PAYLOAD_ORDER = (
    "regulatory", "competitor", "ofertas", "dou", "cvm_normas", "sanctions", "cade", "contracts",
    "market", "new_entrants", "pix_moves", "juros_moves", "sec_filings", "official_acts",
    "cvm_ofertas_status", "fatos", "news", "macro", "reputation", "bcb_reclamacoes", "ans_igr",
    "consumidor_gov", "inf_diario_moves", "fiagro_moves",
)

# Step Functions caps a task result at 256 KB even with result_path=DISCARD
# (States.DataLimitExceeded). A failed attempt has already committed the seen-state, so the
# retry saw 0 new items and the run's new acts never reached a digest (2026-09-27, #188–#195
# volume). Once the digest is in S3 (synth reads it from there), return a pointer instead.
MAX_INLINE_BODY_BYTES = 200_000


def _response_body(payload: dict, digest_key: str | None) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if digest_key is None or len(body.encode("utf-8")) <= MAX_INLINE_BODY_BYTES:
        return body
    counts = {k: {f: v[f] for f in ("count", "new_count") if f in v}
              for k, v in payload.items() if isinstance(v, dict) and ("count" in v or "new_count" in v)}
    return json.dumps({"source": payload.get("source"), "digest_key": digest_key,
                       "inline": False, "counts": counts}, ensure_ascii=False)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small digest payload for downstream Lambda/CDK wiring.

    ``mode`` (event or ONCA_INGEST_MODE) splits the work into parallel branches:
      - "news":       fetch only trade-press → write the news slice to S3.
      - "structured": fetch every structured source (+ corpus) → write the base
                      digest to S3 with an empty news slice (news arrives via the
                      parallel news branch and is overlaid at synth time).
      - "all" (default): both, in one invocation (local / back-compat)."""
    mode = (event or {}).get("mode") or os.environ.get("ONCA_INGEST_MODE", "all")
    if mode == "guard":
        return concurrency_guard(event)
    from src.ingest import source_health as _source_health

    if mode == "news":
        # This branch used to return here WITHOUT ever persisting its telemetry ledger
        # (`merge_and_publish` lives at the end of the structured path only), so every
        # news-branch run was recorded in-process and then thrown away with the
        # invocation. Trade press last persisted a run on 2026-09-09 and then read as
        # "11 days stale" on the Fontes tab while it was in fact fetching fine 3x/day —
        # a MONITORING outage that looked exactly like an ingestion outage.
        _source_health.reset()
        out = _write_news_digest(_news_slice(context), context)
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        if bucket:
            _source_health.merge_and_publish(bucket, shard="news")
        return out

    _source_health.reset()  # #76: fresh per-run telemetry ledger for the structured path

    # C3 (#303): every per-source knob (lookbacks, watchlists, thresholds) is a Param on its
    # SourceSpec, resolved here from the same deploy-time env vars as before.
    P = registry.all_params()
    lookback_days = P["regulatory"]["lookback_days"]
    competitors = P["competitor"]["admins"]
    competitor_ispb = P["pix_moves"]["watchlist_ispb"]
    pix_threshold = P["pix_moves"]["min_pct"]
    juros_competitors = P["juros_moves"]["institutions"]
    juros_modalities = P["juros_moves"]["modalities"]
    juros_use_defaults = P["juros_moves"]["use_default_modalities"]
    juros_threshold = P["juros_moves"]["min_pct"]
    ofertas_lookback = P["ofertas"]["lookback_days"]
    ofertas_watch = P["ofertas"]["watchlist"]
    if not ofertas_watch and P["ofertas"]["use_competitors"]:
        ofertas_watch = competitors
    sec_tickers = P["sec_filings"]["tickers"]
    sec_lookback = P["sec_filings"]["lookback_days"]
    inf_diario_watch = P["inf_diario_moves"]["watchlist"]
    if not inf_diario_watch and P["inf_diario_moves"]["use_competitors"]:
        inf_diario_watch = competitors
    inf_diario_threshold = P["inf_diario_moves"]["min_pct"]
    inf_diario_top_n = P["inf_diario_moves"]["top_n"]

    # CVM material facts (Fato Relevante / Comunicado ao Mercado). Own watchlist
    # (B3-listed FS names); optionally unioned with the competitors list.
    fatos_lookback = P["fatos"]["lookback_days"]
    fatos_watch = P["fatos"]["watchlist"]
    if P["fatos"]["use_competitors"]:
        fatos_watch = list(dict.fromkeys(fatos_watch + competitors))
    # Derive the structured (Fato Relevante / Comunicado ao Mercado) watchlist from
    # the registry too, so a newly curated B3-listed entity (one carrying a
    # fatos_term) gets a STRUCTURED lens with no redeploy — mirrors the news-terms
    # derivation below. Structured identity means such an entity resolves from its
    # CVM filing and does not depend on the fragile news-only corroboration gate
    # (nor on an ambiguous place-name brand like "Porto Seguro").
    if (os.environ.get("ONCA_ENTITIES_TABLE") and P["fatos"]["use_registry"]
            and _source_enabled(registry.by_id("fatos"))):
        try:
            from src.synth import entity_registry

            reg_fatos = entity_registry.fatos_terms()
            have = {t.lower() for t in fatos_watch}
            fatos_watch = fatos_watch + [t for t in reg_fatos if t.lower() not in have]
            print(f"Fatos terms: {len(reg_fatos)} from registry, {len(fatos_watch)} total")
        except Exception as exc:  # pragma: no cover - best-effort, config still works
            print(f"Warning: registry fatos terms unavailable, using config: {exc}")
    fatos_categories = P["fatos"]["categories"]

    # Diário Oficial (DOU) — SUSEP / CADE / BACEN acts mentioning a competitor.
    dou_lookback = P["dou"]["lookback_days"]
    dou_terms = P["dou"]["watchlist"]
    if P["dou"]["use_competitors"]:
        dou_terms = list(dict.fromkeys(dou_terms + fatos_watch))
    # Sector-wide DOU topics (#173/#175): per-industry phrases from the declarative industry
    # topic registry (registry.INDUSTRY_TOPICS) — TOPIC terms (industry-tagged, own budget,
    # searched first, scoped to the normative issuers), not competitor names: an act they match
    # carries industries=[...] and no entity. Betting's phrases were the pre-#175 hard-coded
    # set (dead config until #174 fixed the organ filter, which is how MP 1.394 — the
    # 2026-09-25 online-betting ban — was missed). Per-industry toggles stay env flags
    # (ONCA_DOU_BETTING); ONCA_DOU_SECTOR_TOPICS=false turns every topic phrase off.
    def _topic_enabled(spec: "registry.IndustryTopicSpec") -> bool:
        return not spec.env_flag or os.environ.get(spec.env_flag, "true").lower() in ("1", "true", "yes")

    dou_topics: dict[str, list[str]] = {}
    dou_topic_organs: dict[str, list[str]] = {}
    if P["dou"]["sector_topics"]:
        dou_topics = registry.dou_topic_terms(_active_vertical(), enabled=_topic_enabled)
        dou_topic_organs = {p: o for p, o in registry.dou_topic_organs(_active_vertical()).items()
                            if p in dou_topics}
    # #188: watched primary acts (MP 1.394/1.393) — their conversion law / expiry arrives months
    # later, outside the lookback: search for acts citing them, and pass them as known critical
    # instruments so those acts inherit the industries. Searched FIRST (ahead of topic phrases).
    dou_known: dict[str, list[str]] = {}
    if dou_topics or dou_terms:
        _watched = {}
        for _key, _phrase, _inds in registry.watched_acts():
            dou_known[_key] = _inds
            _watched[_phrase] = _inds
            dou_topic_organs[_phrase] = list(registry.NORMATIVE_ISSUERS)
        dou_topics = {**_watched, **dou_topics}
    # 19 → 40+ phrases after #188/#189/#195; each is one query (+ walked-back pages, #191).
    dou_max_topics = P["dou"]["max_topic_terms"]

    # Trade-press news is fetched by _news_slice (its own parallel branch); see
    # the mode dispatch at the top of lambda_handler.

    # Wall-clock guards: bound each source and stop starting new work before the
    # Lambda times out, so ingest always returns a (possibly partial) digest.
    deadline = _ingest_deadline(context)
    per_source = int(os.environ.get("ONCA_SOURCE_TIMEOUT_SEC", "90"))

    # regulatory (bcb_normativos) + competitor (cvm_fundos) are now produced by the registry
    # loop below (ADR 019 — the FETCHERS registry), together with ofertas/dou/cade/sanctions/
    # contracts. Their outputs are extracted from `loop_results` right before the digest.

    # IF.data is QUARTERLY data on a 3x/day pipeline. Until 2026-09-13 this block
    # re-did the identical work every single run: a 58MB/153k-row download TWICE
    # (latest_base_date() probed with full fetches) plus ~1,422 resolve_entities
    # calls at ~200-600ms each. Measured live: ~320-840s — the single source that
    # consumed the whole 900s Lambda budget and produced the ExecutionTimedOut runs.
    # Now: cheap $top=1 date probe, skip entirely once the quarter is fully walked,
    # and otherwise resolve one bounded window per run (cursor in the store).
    # C1 (#303): the IF.data market snapshot is a registry-loop source (delta="none"); its
    # budget and failure handling come from the loop's _gated_static.
    def _fetch_market() -> list[dict[str, Any]]:
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        index = bcb_ifdata.load_index(bucket) if bucket else {}
        base_date = bcb_ifdata.latest_base_date()
        cursor, complete = bcb_ifdata.resolve_progress(index, base_date)
        if complete and index.get("top"):
            market = index.get("top") or []
            print(
                f"IF.data market: base_date {base_date} already fully resolved "
                f"({index.get('count', 0)} entities) — reusing cached top{len(market)}"
            )
        else:
            rows = bcb_ifdata.fetch_institutions(base_date=base_date)
            names = bcb_ifdata.fetch_institution_names(base_date)
            shares = bcb_ifdata.market_share(rows, institution_names=names)
            market = shares[:10]
            del rows  # ~58MB / 153k rows — free it before the resolver pass
            # ADR 015 §3: resolve institution names -> entity_id and persist a durable
            # bcb_ifdata/index.json store so feed_builder can emit entities[].market_
            # share_pct. Best-effort, mirrors the bcb_reclamacoes store below.
            if bucket:
                from src.synth.entities import resolve_entities

                window = max(1, P["market"]["max_resolve"])
                stats: dict[str, Any] = {}
                recs = bcb_ifdata.map_to_entities(
                    shares,
                    resolver=resolve_entities,
                    base_date=base_date,
                    start=cursor,
                    limit=window,
                    # Stop cleanly a little before the SIGALRM budget would kill
                    # the source, so the window's work is persisted and the cursor
                    # advances (a hard kill here would livelock the walk).
                    deadline=min(
                        deadline, time.monotonic() + max(10, int(per_source * 0.7))
                    ),
                    stats=stats,
                )
                nxt = min(cursor + int(stats.get("processed") or window), len(shares))
                bcb_ifdata.update_store(
                    recs,
                    bucket,
                    index=index,
                    progress={
                        "base_date": base_date,
                        "cursor": nxt,
                        "universe": len(shares),
                        "top": market,
                    },
                )
                print(
                    f"IF.data market: base_date {base_date} resolved "
                    f"{cursor}-{nxt}/{len(shares)} (+{len(recs)} entities)"
                )
        return market

    # ADR 022 Tier A — prudential SOLVENCY (Índice de Basileia et al.) from IF.data Relatório 5.
    # Gated OFF by default: the ADR §4 home for this quarterly data is the separate MONTHLY
    # OncaFinancialsPipeline (Phase 3), not this 3×/day cycle. When enabled it writes the durable
    # soundness/index.json the feed_builder already reads (harmless/no-op when the quarter is
    # unchanged). Best-effort — a failure never touches the rest of the ingest.
    if os.environ.get("ONCA_SOUNDNESS", "").lower() == "true" and _fs_jobs_ok():
        try:
            with _source_budget("prudential solvency", deadline, per_source):
                bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
                if bucket:
                    from src.synth.entities import resolve_entities

                    base_date = bcb_soundness.latest_base_date()
                    rows = bcb_soundness.fetch_capital(base_date)
                    names = bcb_soundness.fetch_institution_names(base_date)
                    solvency = bcb_soundness.extract_solvency(rows)
                    recs = bcb_soundness.map_to_entities(
                        solvency, names, resolver=resolve_entities, base_date=base_date
                    )
                    if recs:
                        bcb_soundness.update_store(recs, bucket)
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: prudential solvency fetch failed: {exc}")


    # Financial statements (#7) moved OUT of this handler by #145 / ADR 028. They now run
    # as CvmStatementsTask on the monthly OncaFinancialsPipeline
    # (`cvm_financials.lambda_handler`). CVM publishes quarterly; downloading a ~30MB
    # package three times a day against this handler's source budget bought nothing, and
    # the gate here (`ONCA_FINANCIALS`) was never set in CDK, so it never ran at all.
    # Deliberately no fallback: two writers to financials/index.json is how a fresh store
    # gets overwritten by a stale one.

    # FIAGRO agri-funds — PL / cotista moves + newly-registered classes as
    # narrative-ready signal (task b). Entity discovery above only populates the
    # registry; without this, 0 agri-funds narratives ever reach synth. Reuses
    # the generic value-state move engine (same primitives as Pix/juros/Informe
    # Diário) — first run seeds a baseline only. Independently gated from
    # ONCA_ENTITY_DISCOVERY so either can be disabled without the other.
    def _fetch_fiagro() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        fiagro_min_pl = P["fiagro_moves"]["min_pl"]
        pl_threshold = P["fiagro_moves"]["pl_min_pct"]
        cotista_threshold = P["fiagro_moves"]["cotista_min_pct"]
        reg_lookback = P["fiagro_moves"]["new_reg_lookback_days"]
        fiagro_rows = cvm_fiagro.fetch_fiagro(min_pl=fiagro_min_pl)
        fiagro_pl_moves = _moves_since_last_run(
            "cvm_fiagro_pl",
            cvm_fiagro.for_pl_moves(fiagro_rows),
            key_field="cnpj",
            value_field="pl",
            min_pct=pl_threshold,
        )
        fiagro_cotista_moves = _moves_since_last_run(
            "cvm_fiagro_cotistas",
            cvm_fiagro.for_cotista_moves(fiagro_rows),
            key_field="cnpj",
            value_field="cotistas",
            min_pct=cotista_threshold,
        )
        reg_candidates = cvm_fiagro.for_new_registrations(
            fiagro_rows, lookback_days=reg_lookback
        )
        fiagro_new_regs = _new_since_last_run(
            "cvm_fiagro_newreg", reg_candidates, seed_if_empty=True
        )
        fiagro_moves_all = fiagro_pl_moves + fiagro_cotista_moves + fiagro_new_regs
        # Attribute each FIAGRO move to exactly ONE entity by CNPJ — a structured id
        # that is authoritative. This bypasses the free-text alias matcher, which
        # over-resolves FIAGRO funds via their shared *administrator* name (dozens of
        # funds share one DTVM/corretora admin, so a single move would otherwise fan
        # out to every co-administered fund). Drop a move whose CNPJ isn't yet a
        # registry entity — there's no subject to attribute it to (discovery adds the
        # fund later, after which its next move resolves).
        if fiagro_moves_all:
            try:
                from src.synth import entity_registry as _fiagro_reg

                _resolved: list[dict[str, Any]] = []
                for _ev in fiagro_moves_all:
                    _eid = _fiagro_reg.resolve_by_cnpj(str(_ev.get("cnpj") or ""))
                    if _eid:
                        _ev["_entities"] = [_eid]
                        _resolved.append(_ev)
                fiagro_moves_all = _resolved
            except Exception as exc:  # pragma: no cover - never emit unattributed fan-out
                print(f"Warning: FIAGRO entity resolution failed, dropping signal: {exc}")
                fiagro_moves_all = []
        return fiagro_rows, fiagro_moves_all, {
            "pl_move_count": len(fiagro_pl_moves),
            "cotista_move_count": len(fiagro_cotista_moves),
            "new_registration_count": len(fiagro_new_regs),
        }

    # Pix traction — month-over-month volume moves (first run seeds baseline only).
    def _fetch_pix() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        pix_rows = bcb_pix.fetch_recent()
        pix_by_inst = bcb_pix.by_institution(
            pix_rows, watchlist_ispb=competitor_ispb or None
        )
        return pix_by_inst, pix_by_inst, {}

    # Juros médios — relative rate moves by institution × modality.
    def _fetch_juros() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        juros_rows = bcb_juros.fetch_daily()
        modalities = juros_modalities
        if not modalities and juros_use_defaults:
            modalities = list(bcb_juros.DEFAULT_MODALITY_FILTERS)
        juros_focus = bcb_juros.filter_rates(
            juros_rows,
            institutions=juros_competitors or None,
            modalities=modalities or None,
        )
        return juros_focus, bcb_juros.for_moves(juros_focus), {}

    # CVM ofertas — produced by the registry loop below.


    # Diário Oficial (dou) — produced by the registry loop below.

    # CADE antitrust (#61) — produced by the registry loop below (fetch resolves the merger
    # parties by name; see the "antitrust" fetcher).
    from src.synth.entities import resolve_entities as _resolve_entities

    # BCB macro — Copom/Selic decision + weekly Focus expectations (market-wide,
    # not entity-tied; surfaces as standalone "macro" cards). Best-effort.
    macro_selic: dict[str, Any] | None = None
    macro_focus: list[dict[str, Any]] = []
    if _source_enabled(registry.by_id("macro")):
        try:
            with _source_budget("BCB macro", deadline, per_source):
                macro_selic = bcb_macro.fetch_selic()
                macro_focus = bcb_macro.fetch_focus()
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: BCB macro fetch failed: {exc}")

    # DataJud (CNJ) — corporate-distress filings (recuperação judicial / falência).
    # Party-name-scrubbed public API, so this is a MACRO sector-distress signal
    # (filing volume/trend), not entity-tied (issue #25). Best-effort; off via env.
    distress_summary: dict[str, Any] | None = None
    if _source_enabled(registry.by_id("datajud")):
        try:
            with _source_budget("DataJud RJ", deadline, per_source):
                tribs = P["datajud"]["tribunals"] or list(datajud.DEFAULT_TRIBUNALS)
                distress = datajud.fetch_recuperacao_judicial(
                    tribs, lookback_days=P["datajud"]["lookback_days"]
                )
                new_distress = _new_since_last_run("datajud_rj", distress, seed_if_empty=True)
                distress_summary = {
                    **datajud.summarize(distress),
                    "new_count": len(new_distress),
                    "items": _tag_new(new_distress[:12]),
                }
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: DataJud fetch failed: {exc}")

    # BCB complaints ranking — OFFICIAL quarterly consumer-complaints ranking per
    # institution (issue #31, the Reclame Aqui alternative). Public Olinda OData,
    # entity-tied; writes a durable bcb_reclamacoes/index.json store. Best-effort.
    bcb_reclamacoes_summary: dict[str, Any] | None = None
    if _source_enabled(registry.by_id("bcb_reclamacoes")):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("BCB reclamações", deadline, per_source):
                from src.synth.entities import resolve_entities

                rows = bcb_reclamacoes.fetch_ranking()
                recs = bcb_reclamacoes.map_to_entities(rows, resolver=resolve_entities)
                if recs and bucket:
                    bcb_reclamacoes.update_store(recs, bucket)
                bcb_reclamacoes_summary = {
                    **bcb_reclamacoes.summarize(recs),
                    "items": recs[:12],
                }
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: BCB reclamações fetch failed: {exc}")

    # ANS IGR (#140) — the same reputation shape for saúde suplementar, extending the
    # signal past BCB-supervised banks. Resolution is CNPJ-only via the ANS cadop bridge;
    # there is deliberately no name fallback (see ans_igr's docstring). DEFAULT-OFF until
    # the health-plan sub-entities (ADR-017) are registered, since until then almost
    # nothing resolves and the store would just be empty.
    ans_igr_summary: dict[str, Any] | None = None
    if _source_enabled(registry.by_id("ans_igr")):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("ANS IGR", deadline, per_source):
                ans_igr_summary = ans_igr.run(bucket)
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: ANS IGR fetch failed: {exc}")

    # consumidor.gov.br complaints (#63) — cross-industry consumer reputation, the general
    # form of bcb_reclamacoes. DEFAULT-OFF + token-gated: the source is resolved via the
    # dados.gov.br catalog ([[gov_dados]]), whose GOV_DADOS_TOKEN is currently rejected, so
    # this is inert until the token is regenerated. Durable store, like bcb_reclamacoes.
    consumidor_gov_summary: dict[str, Any] | None = None
    if _source_enabled(registry.by_id("consumidor_gov")):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("consumidor.gov", deadline, per_source):
                from src.synth.entities import resolve_entities

                rows = consumidor_gov.fetch_indicators(
                    min_complaints=P["consumidor_gov"]["min_complaints"])
                recs = consumidor_gov.map_to_entities(rows, resolver=resolve_entities)
                if recs and bucket:
                    consumidor_gov.update_store(recs, bucket)
                consumidor_gov_summary = {**consumidor_gov.summarize(recs), "items": recs[:12]}
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: consumidor.gov fetch failed: {exc}")

    # CEIS/CNEP federal sanctions (#60) — counterparty-integrity, sector-agnostic. Only
    # sanctions resolving (CNPJ-root first) to a tracked entity are kept; a durable
    # sanctions/index.json store holds state and _new_since_last_run drives the delta
    # (seed-suppressed so the historical backlog does not flood the first run).
    def _fetch_sanctions() -> list[dict[str, Any]]:
        from src.synth import entity_registry

        # CNPJ-only resolution — no registry, no signal (a sanction binds to a specific
        # legal person; see ceis_cnep docstring).
        idx = ceis_cnep.build_cnpj_index(
            entity_registry.list_entities()
        ) if os.environ.get("ONCA_ENTITIES_TABLE") else {}
        srows = ceis_cnep.fetch_sanctions() if idx else []
        return ceis_cnep.map_to_entities(srows, cnpj_index=idx)

    _bucket = os.environ.get("ONCA_DIGESTS_BUCKET")

    def _fetch_contracts() -> list[dict[str, Any]]:
        from src.synth import entity_registry

        idx = ceis_cnep.build_cnpj_index(
            entity_registry.list_entities()
        ) if os.environ.get("ONCA_ENTITIES_TABLE") else {}
        crows = pncp_contratos.fetch_contracts(
            days_back=P["contracts"]["lookback_days"],
            max_pages=P["contracts"]["max_pages"],
            min_valor=P["contracts"]["min_valor"],
        ) if idx else []
        return pncp_contratos.map_to_entities(crows, cnpj_index=idx)

    # ADR 019 — the registry-driven ingest loop. Each standard "document" lens source is one
    # entry in FETCHERS (its fetch, keyed by spec.id); the loop applies vertical gating + the
    # wall-clock budget + delta + section-building UNIFORMLY. Adding such a source = a
    # SourceSpec + one FETCHERS entry, with no bespoke handler block and no payload edit.
    # (The numeric "moves" sources and the special-shape/side-effecting ones remain bespoke
    # below — they have distinct mechanics; migrating them is the tracked follow-on.)
    _cade_lookback = P["cade"]["lookback_days"]
    cvm_normas_lookback = P["cvm_normas"]["lookback_days"]
    _FETCHERS: dict[str, Any] = {
        # #175: every regulatory item leaves ingest with severity (+ industries when a covered
        # industry applies) — BCB normativos here, DOU inside dou.fetch_dou.
        "regulatory": lambda: federal_acts.annotate(bcb_normativos.fetch_recent(days=lookback_days)),
        "competitor": lambda: cvm_fundos.fetch_funds(watchlist_admins=competitors),
        "ofertas": lambda: cvm_ofertas.fetch_recent(
            lookback_days=ofertas_lookback, watchlist=ofertas_watch or None),
        # #194 (audit R5): CVM legislação (Resoluções/Instruções/Ofícios-Circulares) + the
        # gov.br/cvm notícias listing (PAS judgments, SRE stop orders, Ofício-Circular
        # announcements) — the CVM had no normative/enforcement source at all before this.
        "cvm_normas": lambda: federal_acts.annotate(
            cvm_normas.fetch_recent(lookback_days=cvm_normas_lookback)),
        "dou": lambda: (_dou_with_stats(dou.fetch_dou(
                            dou_terms, lookback_days=dou_lookback, topic_terms=dou_topics,
                            topic_organs=dou_topic_organs, max_topic_terms=dou_max_topics,
                            known_instruments=dou_known))
                        if (dou_terms or dou_topics) else []),
        "cade": lambda: cade.map_to_entities(
            cade.fetch_atos(lookback_days=_cade_lookback), resolver=_resolve_entities),
        "sanctions": _fetch_sanctions,
        "contracts": _fetch_contracts,
        # C1 (#303): the numeric moves sources (delta="moves") and the IF.data snapshot.
        "pix_moves": _fetch_pix,
        "juros_moves": _fetch_juros,
        "inf_diario_moves": lambda: (
            lambda rows: (rows, cvm_inf_diario.for_moves(rows), {}))(
            cvm_inf_diario.fetch_latest(watchlist_admins=inf_diario_watch, top_n=inf_diario_top_n)),
        "fiagro_moves": _fetch_fiagro,
        "market": _fetch_market,
        # C2 (#303): the side-effecting lens sources; their side effects are _POST hooks.
        "new_entrants": lambda: bcb_autorizacoes.fetch_authorized(),
        "fatos": lambda: cvm_ipe.fetch_material_facts(
            lookback_days=fatos_lookback, watchlist=fatos_watch, categories=fatos_categories),
        "sec_filings": lambda: sec_filings.fetch_filings(sec_tickers, lookback_days=sec_lookback),
    }

    def _post_entrants(records: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
        # #74/R2: register-VERIFIED certifications for tracked entities (CNPJ-joined; structured
        # provenance, never demotes a curated cert). Pure-local over the already-fetched rows.
        try:
            _certd = bcb_autorizacoes.apply_verified_certifications(records)
            if _certd:
                print(f"R2 certifications: verified {len(_certd)} tracked entities from BCB register")
        except Exception as exc:  # pragma: no cover - never blocks the entrant scan
            print(f"Warning: R2 certification stamp failed: {exc}")
        # #358: register snapshot + EXIT diff (an institution leaving "em funcionamento"). The
        # snapshot is also the distress watch's wide index for untracked institutions (#355).
        if _bucket:
            try:
                print(f"BCB register state: {bcb_autorizacoes.update_register_state(records, _bucket)}")
            except Exception as exc:  # pragma: no cover - never blocks the entrant scan
                print(f"Warning: BCB register snapshot/exit diff failed: {exc}")
            # #362: official BCB resolution regimes (liquidação / intervenção / RAET) — the
            # distress watch turns this snapshot into regulatory records (the Will Bank miss).
            try:
                from src.ingest import bcb_regimes

                try:  # tracked names drive the (bounded) history detail fetch
                    from src.synth import entity_registry as _er

                    _names = {bcb_regimes.legal_key(a) for e in _er.list_entities()
                              for a in [e.get("display_name"), *(e.get("aliases") or [])] if a}
                except Exception as exc:  # pragma: no cover - registry best-effort
                    print(f"Warning: regime history names unavailable: {exc}")
                    _names = None
                print(f"BCB resolution regimes: {bcb_regimes.update_state(_bucket, match_names=_names)}")
            except Exception as exc:  # pragma: no cover - never blocks the entrant scan
                print(f"Warning: BCB resolution regimes fetch failed: {exc}")
            # #378: BCB supervision for the CRO board — PAS sanctions (legal entities only),
            # the Pix data-incident register and open public consultations (comment deadline).
            try:
                from src.ingest import bcb_supervision

                _cnpj: dict[str, str] = {}
                _legal: dict[str, str] = {}
                try:
                    from src.synth import entity_registry as _er2

                    for e in _er2.list_entities():
                        eid = e.get("entity_id")
                        if not eid:
                            continue
                        for root in e.get("cnpj_roots") or []:
                            _cnpj.setdefault(str(root)[:8], eid)
                        for a in [e.get("legal_name"), e.get("display_name"), *(e.get("aliases") or [])]:
                            if a:
                                _legal.setdefault(bcb_supervision._fold(a), eid)
                except Exception as exc:  # pragma: no cover - registry best-effort
                    print(f"Warning: BCB supervision entity map unavailable: {exc}")
                # bounded: the ingest run already peaks near its 900 s timeout
                with _source_budget("BCB supervision", deadline, min(per_source, 60)):
                    print(f"BCB supervision: {bcb_supervision.update_state(_bucket, cnpj_to_entity=_cnpj, name_to_entity=_legal)}")
            except Exception as exc:  # pragma: no cover - never blocks the entrant scan
                print(f"Warning: BCB supervision fetch failed: {exc}")

    def _post_fatos(records: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
        # Governance events are the strategic ones — float them to the
        # front so they survive the digest item cap and reach synth.
        new.sort(key=lambda f: not f.get("governance"))

    def _post_sec(records: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
        # Metadata first (submissions JSON), then primary-document bodies for the
        # diffed set + a small context sample so digests/corpus carry text not only URLs.
        # Corpus: full body for genuinely new filings.
        if new:
            try:
                sec_filings.enrich_with_content(new)
            except _SourceBudgetExceeded:
                raise
            except Exception as exc:  # pragma: no cover
                print(f"Warning: SEC primary-document fetch failed: {exc}")
        # Digest context: enrich a few recent rows still missing text
        # (same object refs as new are skipped via skip_existing).
        context_need = [r for r in records[:15] if not r.get("text")]
        if context_need:
            try:
                sec_filings.enrich_with_content(context_need)
            except _SourceBudgetExceeded:
                raise
            except Exception as exc:  # pragma: no cover
                print(f"Warning: SEC context content fetch failed: {exc}")

    _POST: dict[str, Any] = {"new_entrants": _post_entrants, "fatos": _post_fatos,
                             "sec_filings": _post_sec}
    _STORES: dict[str, Any] = {
        "sanctions": lambda recs: ceis_cnep.update_store(recs, _bucket) if _bucket else None,
        "contracts": lambda recs: pncp_contratos.update_store(recs, _bucket) if _bucket else None,
    }
    # per-source extra section keys, computed from (records, new) after the post hook
    _SECTION_EXTRA: dict[str, Any] = {
        "fatos": lambda recs, new: {"governance_count": sum(1 for f in new if f.get("governance"))},
    }
    # sources whose section has a bespoke shape built after the loop (new_entrants: its count
    # is the BCB register, its items include the supplementary registries)
    _DEFERRED_SECTION = {"new_entrants"}
    # moves: the threshold per source, and whether it has anything to do this run (a source
    # that is not applicable is skipped before its budget: no state, no health row).
    _MOVE_MIN_PCT = {"pix_moves": pix_threshold, "juros_moves": juros_threshold,
                     "inf_diario_moves": inf_diario_threshold}
    _APPLICABLE = {"inf_diario_moves": bool(inf_diario_watch), "fatos": bool(fatos_watch),
                   "sec_filings": bool(sec_tickers)}
    # section extras computed from a moves run: (tracked, aux) -> keys between the tracked
    # count and move_count
    _MOVES_EXTRA: dict[str, Any] = {
        "inf_diario_moves": lambda t, aux: {"as_of": (t[0].get("date") if t else None)},
        "fiagro_moves": lambda t, aux: {
            "pl_move_count": aux.get("pl_move_count", 0),
            "cotista_move_count": aux.get("cotista_move_count", 0),
            "new_registration_count": aux.get("new_registration_count", 0)},
    }
    _STATIC_SECTION: dict[str, Any] = {
        "market": lambda recs: {"count": len(recs), "items": recs, "context": recs},
    }
    loop_results: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    loop_sections: dict[str, dict[str, Any]] = {}
    for _spec in registry.active(_active_vertical()):
        _fetch = _FETCHERS.get(_spec.id)
        if _fetch is None:  # not (yet) loop-migrated — handled by a bespoke block
            continue
        if _spec.delta == "moves":
            _tracked, _moves, _aux = _gated_moves(
                _spec, deadline=deadline, per_source=per_source, fetch=_fetch,
                min_pct=_MOVE_MIN_PCT.get(_spec.id), applicable=_APPLICABLE.get(_spec.id, True),
            )
            loop_results[_spec.id] = (_tracked, _moves)
            _extra = _MOVES_EXTRA.get(_spec.id)
            loop_sections[_spec.id] = _moves_section(
                _tracked, _moves, _spec, **(_extra(_tracked, _aux) if _extra else {}))
            continue
        if _spec.delta == "none":
            _records = _gated_static(_spec, deadline=deadline, per_source=per_source, fetch=_fetch)
            loop_results[_spec.id] = (_records, [])
            loop_sections[_spec.id] = _STATIC_SECTION[_spec.id](_records)
            continue
        _records, _new = _gated_source(
            _spec, deadline=deadline, per_source=per_source,
            fetch=_fetch, store=_STORES.get(_spec.id), post=_POST.get(_spec.id),
            applicable=_APPLICABLE.get(_spec.id, True),
        )
        loop_results[_spec.id] = (_records, _new)
        if _spec.id in _DEFERRED_SECTION:
            continue
        _sx = _SECTION_EXTRA.get(_spec.id)
        loop_sections[_spec.id] = _lens_section(
            _records, _new, _spec, **(_sx(_records, _new) if _sx else {}))

    # Extract for the payload/corpus. A vertical-gated-out source yields ([], []).
    normativos, new_normativos = loop_results.get("regulatory", ([], []))
    funds, new_funds = loop_results.get("competitor", ([], []))
    offerings, new_ofertas = loop_results.get("ofertas", ([], []))
    dou_acts, new_dou = loop_results.get("dou", ([], []))
    cvm_normas_acts, new_cvm_normas = loop_results.get("cvm_normas", ([], []))
    cade_records, new_cade = loop_results.get("cade", ([], []))
    sanctions_records, new_sanctions = loop_results.get("sanctions", ([], []))
    contracts_records, new_contracts = loop_results.get("contracts", ([], []))
    fatos, new_fatos = loop_results.get("fatos", ([], []))
    sec_filings_rows, new_sec = loop_results.get("sec_filings", ([], []))

    # New entrants (C2, #303): the BCB autorizações fetch, its delta and its register
    # certifications run in the loop (the "new_entrants" source). The supplementary registries
    # and every registry side effect below consume that result, in their established order.
    authorized, new_entrants = loop_results.get("new_entrants", ([], []))
    new_entrants = list(new_entrants)
    # Full rosters kept for the #103 SPA+SUSEP roster BACKFILL (below); the delta lists
    # above feed the entrant SIGNAL path, these feed Entity-Master promotion.
    susep_rows: list[dict[str, Any]] = []
    spa_rows: list[dict[str, Any]] = []
    _entrants_on = _source_enabled(registry.by_id("new_entrants"))

    # CVM market-participant registries (E5) — consultores/adm de carteira new-entrant scan.
    # Gated (heavy CVM zip fetch) + seed-suppressed. Non-fintech → surfaces as entrant SIGNALS
    # (entrants lens + Receita enrich), not auto-created, so a fuzzy advisory set never floods.
    if _entrants_on and P["new_entrants"]["ingest_cvm_participantes"]:
        try:
            from src.ingest import cvm_participantes

            with _source_budget("CVM participantes", deadline, per_source):
                parts = cvm_participantes.fetch_participants()
                new_entrants += _new_since_last_run(
                    "cvm_participantes", parts, seed_if_empty=True
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: CVM participantes fetch failed: {exc}")

    # SUSEP supervised-entities registry (#77 E1) — insurance/EAPC/capitalização/resseguro new
    # entrants. Gated + seed-suppressed. Insurers aren't fintech → surface as review-gated entrant
    # SIGNALS (entrants lens + Receita enrich), not auto-created, like the CVM-participantes path.
    if _entrants_on and P["new_entrants"]["ingest_susep"]:
        try:
            from src.ingest import susep_entidades

            with _source_budget("SUSEP entidades", deadline, per_source):
                susep_rows = susep_entidades.fetch_entities()
                new_entrants += _new_since_last_run(
                    "susep_entidades", susep_rows, seed_if_empty=True
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: SUSEP entidades fetch failed: {exc}")

    # SPA/MF authorized betting operators (#78 E3) — for iGaming the authorized list IS the entrant
    # feed (~82 companies / 188 brands). Gated + seed-suppressed → entrants lens + Receita enrich.
    if _entrants_on and P["new_entrants"]["ingest_spa"]:
        try:
            from src.ingest import spa_apostas

            with _source_budget("SPA apostas", deadline, per_source):
                spa_rows = spa_apostas.fetch_authorized()
                new_entrants += _new_since_last_run(
                    "spa_apostas", spa_rows, seed_if_empty=True
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: SPA apostas fetch failed: {exc}")

    # PREVIC EFPC base-cadastral (#79 E2) — closed-pension new entrants. Direct XLSX (not the dead
    # dados.gov.br token). Gated + seed-suppressed → entrants lens + Receita enrich.
    if _entrants_on and P["new_entrants"]["ingest_previc"]:
        try:
            from src.ingest import previc_efpc

            with _source_budget("PREVIC EFPC", deadline, per_source):
                previc_rows = previc_efpc.fetch_entities()
                new_entrants += _new_since_last_run(
                    "previc_efpc", previc_rows, seed_if_empty=True
                )
        except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
            print(f"Warning: PREVIC EFPC fetch failed: {exc}")

    # Receita Federal enrichment: resolve the brand + controllers behind each new
    # entrant's (otherwise anonymous) CNPJ. Own budget so a slow lookup can't lose
    # the entrants list; only new entrants (bounded volume) are enriched.
    _enrich_on = P["new_entrants"]["receita_enrich"]
    if new_entrants and _enrich_on:
        try:
            with _source_budget("Receita QSA", deadline, per_source):
                receita_cnpj.enrich_entrants(new_entrants)
        except Exception as exc:  # pragma: no cover - enrichment is best-effort
            print(f"Warning: Receita enrichment skipped: {exc}")
    elif _enrich_on:
        # No new entrants this cycle. This source is EVENT-DRIVEN — it fires only when an
        # official register adds an institution — so staying silent here made it drift into
        # `warn`/`stale` on the Fontes tab while working perfectly. Record the idle outcome
        # so "the registers were quiet" stops reading as "the ingester is degraded".
        _record_health("Receita QSA", ok=True, idle=True)

    # Entities registry auto-create (ADR step 3): give each new fintech entrant's
    # CNPJ a registry record so future signals about it (CVM/news/DOU) resolve and
    # cluster with no redeploy. CNPJ-keyed + idempotent; best-effort, never blocks.
    _autocreate_on = bool(os.environ.get("ONCA_ENTITIES_TABLE")) and P["new_entrants"]["autocreate"]
    if not new_entrants and _autocreate_on:
        # Event-driven, same as Receita QSA above: no new entrants means nothing to create.
        _record_health("entities auto-create", ok=True, idle=True)
    if new_entrants and _autocreate_on:
        fintech_only = P["new_entrants"]["autocreate_fintech_only"]
        # #103: promote NEW non-fintech entrants (insurers/bookmakers/EFPCs/CVM participants)
        # that classify to a confident industry — else they stay signal-only. Off by default.
        promote_regulated = P["new_entrants"]["autocreate_regulated"]
        try:
            with _source_budget("entities auto-create", deadline, per_source):
                from src.synth import entity_registry

                review_on = P["new_entrants"]["review"]
                created = 0
                for e in new_entrants:
                    is_ft = bool(e.get("is_fintech"))
                    if not is_ft:
                        _inds, _needs = entity_registry.classify_industries(e)
                        # fintech_only skips non-fintech UNLESS regulated-promotion is on and
                        # the entrant classifies confidently; fintech_only=false keeps the old
                        # "create everything (ambiguous → review)" escape hatch.
                        if fintech_only and not (promote_regulated and not _needs):
                            continue
                    # An official-register non-fintech entrant is structured-provenance
                    # (radar tier `registry`); a BCB fintech stays `cnpj` as before.
                    eid = entity_registry.auto_create_from_entrant(
                        e, confidence="cnpj" if is_ft else "structured"
                    )
                    if eid:
                        e["registry_entity_id"] = eid
                        created += 1
                        # New entity's bare brand stays structured-only until a
                        # curator vets it (ADR 002). Queue a news-safe promotion
                        # review when the brand is a single token (multi-token
                        # legal names are already distinctive — no gate to lift).
                        brand = str(e.get("trade_name") or e.get("name") or "").strip()
                        if review_on and brand and " " not in brand:
                            entity_registry.propose_news_safe(eid, brand)
                        # Ambiguous license → industry couldn't be auto-assigned.
                        # Queue a curator pick so an add-on for this module still
                        # aggregates the entrant's signals (ADR 002 Phase B).
                        if review_on:
                            _inds, needs = entity_registry.classify_industries(e)
                            if needs:
                                entity_registry.propose_industry(
                                    eid, brand or str(e.get("name") or "")
                                )
                if created:
                    print(f"entities: auto-created {created} from new entrants")
                    # ADR step 5: a fresh entity may share a QSA controller with
                    # an existing one — queue group-merge proposals for review
                    # (never auto-merged). Only when something was created, so
                    # the table scan stays off the common no-new-entrant runs.
                    if review_on:
                        queued = entity_registry.propose_group_merges()
                        if queued:
                            print(f"entities: queued {queued} group-merge reviews")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity auto-create skipped: {exc}")

    # #103 roster BACKFILL — the SPA (betting) + SUSEP (insurance) licensed rosters ARE their
    # competitive sets and are bounded (~82 / ~233), so the WHOLE roster is promoted into the
    # Entity Master (not just deltas): resolve→enrich / clean-brand auto-create / propose,
    # budget-capped per run (idempotent by CNPJ, fills over runs). Gated OFF by default.
    if _fs_jobs_ok() and os.environ.get("ONCA_PROMOTE_REGULATED", "false").lower() in ("1", "true", "yes") \
            and os.environ.get("ONCA_ENTITIES_TABLE"):
        try:
            with _source_budget("regulated roster promote", deadline, per_source):
                from src.synth import entity_discovery

                _budget = int(os.environ.get("ONCA_PROMOTE_MAX_NEW", "40"))
                for _label, _rows, _industry, _pfn in (
                    ("SPA/betting", spa_rows, "betting", entity_discovery._profile_from_spa),
                    ("SUSEP/insurance", susep_rows, "insurance", entity_discovery._profile_from_susep),
                ):
                    if not _rows:
                        continue
                    _rep = entity_discovery.promote_roster(
                        _rows, industry=_industry, profile_fn=_pfn, max_new=_budget)
                    print(
                        f"roster promote {_label}: fetched={_rep.get('fetched')} "
                        f"created={len(_rep.get('created') or [])} "
                        f"enriched={len(_rep.get('enriched') or [])} "
                        f"already={_rep.get('already')} "
                        f"proposed={len(_rep.get('proposed') or [])} "
                        f"skipped={len(_rep.get('skipped') or [])}"
                    )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: regulated roster promote skipped: {exc}")

    # #104 (#14 Stage 2) — Receita CNPJ bulk → FS-CNAE candidate proposals. Gated OFF by default
    # (ONCA_INGEST_RECEITA_BULK). Default path (2026-09-08, unblocked): a LIVE shard fetch —
    # the dump is 10 numbered shards, not one monolith; shards 1-9 fit a single invocation,
    # rotating by day-of-year. A pre-staged partition (ONCA_RECEITA_BULK_KEY/URL — e.g. a future
    # Athena/Glue step over the full dataset) overrides the live fetch if configured. This is a
    # genuinely heavy fetch (a few hundred MB), so it gets its OWN larger source budget rather
    # than the shared ONCA_SOURCE_TIMEOUT_SEC. Propose-only (ADR 011 §4) throughout.
    if _fs_jobs_ok() and os.environ.get("ONCA_INGEST_RECEITA_BULK", "false").lower() in ("1", "true", "yes") \
            and os.environ.get("ONCA_ENTITIES_TABLE"):
        try:
            _rb_budget = int(os.environ.get("ONCA_RECEITA_SOURCE_TIMEOUT_SEC", "240"))
            with _source_budget("Receita bulk CNAE", deadline, _rb_budget):
                from src.ingest import receita_bulk

                _rb_key = os.environ.get("ONCA_RECEITA_BULK_KEY")
                _rb_url = os.environ.get("ONCA_RECEITA_BULK_URL")
                if _rb_key:
                    # NB: boto3 is already imported at module scope (top of file) — a local
                    # `import boto3` HERE would make Python treat the name as local to the
                    # WHOLE `lambda_handler` function (Python's scoping is function-wide, not
                    # block-wide, and applies at compile time regardless of whether this branch
                    # ever runs), breaking every unrelated bare `boto3.` reference later in this
                    # same giant function — this exact bug silently broke the final digest S3
                    # upload (`Warning: S3 upload failed: cannot access local variable 'boto3'`)
                    # on every ingest invocation since it was introduced (2026-09-08, live-
                    # confirmed via CloudWatch). Do not reintroduce a local import of a name
                    # already imported at module scope inside this function.
                    _body = boto3.client("s3").get_object(
                        Bucket=os.environ["ONCA_DIGESTS_BUCKET"], Key=_rb_key)["Body"].read()
                    _cands = receita_bulk.parse_estabelecimentos(_body.decode("latin-1"))
                    _src_label = f"staged:{_rb_key}"
                elif _rb_url:
                    _text = requests.get(_rb_url, timeout=120).content.decode("latin-1")
                    _cands = receita_bulk.parse_estabelecimentos(_text)
                    _src_label = _rb_url
                else:
                    import datetime as _dt
                    _shard = receita_bulk.shard_for_day(_dt.date.today().timetuple().tm_yday)
                    _cands = receita_bulk.fetch_shard(
                        _shard, deadline=min(deadline, time.monotonic() + _rb_budget))
                    _src_label = f"live shard {_shard}"
                _rep = receita_bulk.propose_candidates(
                    _cands, max_propose=int(os.environ.get("ONCA_RECEITA_BULK_MAX_PROPOSE", "200")))
                print(f"Receita bulk CNAE ({_src_label}): candidates={len(_cands)} "
                      f"already={_rep['already']} proposed={len(_rep['proposed'])} no_name={_rep['no_name']}")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: Receita bulk CNAE skipped: {exc}")

    # Entity discovery — structured CVM FIAGRO universe → registry (ADR 011 / #14).
    # High-precision path: every row has a CNPJ. Auto-creates / enriches under
    # industry agri-funds. Gated OFF by default until the first live validation
    # (set ONCA_ENTITY_DISCOVERY=true). Weekly-ish cost is fine; runs best-effort
    # inside the same budget window as the rest of ingest.
    if (
        _fs_jobs_ok()
        and os.environ.get("ONCA_ENTITIES_TABLE")
        and os.environ.get("ONCA_ENTITY_DISCOVERY", "false").lower()
        in ("1", "true", "yes")
    ):
        try:
            with _source_budget("entity discovery FIAGRO", deadline, per_source):
                from src.synth import entity_discovery

                min_pl = float(os.environ.get("ONCA_FIAGRO_MIN_PL", "50000000"))
                auto = os.environ.get(
                    "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                ).lower() in ("1", "true", "yes")
                report = entity_discovery.discover_fiagro(
                    min_pl=min_pl, auto_create=auto, max_new=40
                )
                print(
                    "entity discovery FIAGRO: "
                    f"fetched={report.get('fetched')} "
                    f"created={len(report.get('created') or [])} "
                    f"enriched={len(report.get('enriched') or [])} "
                    f"already={report.get('already')} "
                    f"proposed={len(report.get('proposed') or [])}"
                )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity discovery skipped: {exc}")

        # FII (Fundos Imobiliários) — structured CVM universe → registry (issue #102,
        # #14 Stage 1). Same master gate + industry-parametric engine as FIAGRO; every row
        # has a CNPJ (and a B3 ticker for listed funds). Funds nest under their manager as
        # sub-entities (ADR 017). ONCA_FII_MIN_PL floors on the Total_Investido size proxy.
        try:
            with _source_budget("entity discovery FII", deadline, per_source):
                from src.ingest import cvm_fii
                from src.synth import entity_discovery

                fii_min = float(os.environ.get("ONCA_FII_MIN_PL", "100000000"))
                auto = os.environ.get(
                    "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                ).lower() in ("1", "true", "yes")
                freport = entity_discovery.discover_fiagro(
                    industry="real-estate-funds",
                    rows=cvm_fii.fetch_fii(min_pl=fii_min),
                    auto_create=auto,
                    max_new=int(os.environ.get("ONCA_FII_MAX_NEW", "40")),
                )
                print(
                    "entity discovery FII: "
                    f"fetched={freport.get('fetched')} "
                    f"created={len(freport.get('created') or [])} "
                    f"enriched={len(freport.get('enriched') or [])} "
                    f"already={freport.get('already')} "
                    f"proposed={len(freport.get('proposed') or [])}"
                )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity discovery FII skipped: {exc}")

        # Consórcio administradoras — structured BCB universe → registry (issue #46,
        # ADR 017). Same master gate; every row has a CNPJ. Conglomerate arms
        # (Itaú/Bradesco/Santander/Porto Seguro) nest as sub-entities of the tier-1 parent.
        try:
            with _source_budget("entity discovery consórcio", deadline, per_source):
                from src.synth import entity_discovery

                auto = os.environ.get(
                    "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                ).lower() in ("1", "true", "yes")
                creport = entity_discovery.discover_consorcio(auto_create=auto, max_new=100)
                print(
                    "entity discovery consórcio: "
                    f"fetched={creport.get('fetched')} "
                    f"created={len(creport.get('created') or [])} "
                    f"enriched={len(creport.get('enriched') or [])} "
                    f"already={creport.get('already')} "
                    f"proposed={len(creport.get('proposed') or [])}"
                )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: consórcio discovery skipped: {exc}")

        # BCB-authorized institutions → registry (#14 Official Registry Sync). Behind an
        # EXTRA sub-gate (ONCA_DISCOVER_BCB, default off even when discovery is on): the
        # BCB registry is huge, so this stays dark until a dry-run validates brand quality.
        # Relevance-gated to banks/IP/SCD-SEP-SCFI/corretoras; cooperativas need their own
        # flag (thousands of singulars). Conglomerate arms nest under the tier-1 parent.
        if os.environ.get("ONCA_DISCOVER_BCB", "false").lower() in ("1", "true", "yes"):
            try:
                with _source_budget("entity discovery BCB", deadline, per_source):
                    from src.synth import entity_discovery

                    auto = os.environ.get(
                        "ONCA_ENTITY_DISCOVERY_AUTOCREATE", "true"
                    ).lower() in ("1", "true", "yes")
                    coops = os.environ.get(
                        "ONCA_DISCOVER_BCB_COOPS", "false"
                    ).lower() in ("1", "true", "yes")
                    # #67 prominence gate: override which license classes are proposed
                    # (human-curated) vs auto-created. Unset -> default micro-lender tail;
                    # "" -> auto-create everything.
                    _pc = os.environ.get("ONCA_DISCOVER_BCB_PROPOSE_CLASSES")
                    propose_only = (
                        None if _pc is None
                        else frozenset(c.strip() for c in _pc.split(",") if c.strip())
                    )
                    breport = entity_discovery.discover_bcb_institutions(
                        auto_create=auto, include_coops=coops, max_new=40,
                        propose_only_classes=propose_only)
                    print(
                        "entity discovery BCB: "
                        f"fetched={breport.get('fetched')} "
                        f"created={len(breport.get('created') or [])} "
                        f"enriched={len(breport.get('enriched') or [])} "
                        f"already={breport.get('already')} "
                        f"proposed={len(breport.get('proposed') or [])} "
                        f"skipped={len(breport.get('skipped') or [])}"
                    )
            except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
                print(f"Warning: BCB institutions discovery skipped: {exc}")

        # #14 general NER harvest — company candidates from the narrative corpus →
        # review-queue proposals (never auto-creates). Sub-gate ONCA_NER_HARVEST (default
        # off). Reads recent narratives (previous runs) via the feature store.
        if os.environ.get("ONCA_NER_HARVEST", "false").lower() in ("1", "true", "yes"):
            try:
                with _source_budget("NER harvest", deadline, per_source):
                    from src.synth import entity_discovery, feature_store

                    _bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
                    _win = int(os.environ.get("ONCA_NER_WINDOW_DAYS", "30"))
                    _narrs = feature_store.load_history(_bucket, _win) if _bucket else []
                    _mm = int(os.environ.get("ONCA_NER_MIN_MENTIONS", "3"))
                    _cands = entity_discovery.harvest_ner(_narrs, min_mentions=_mm)
                    # #107: optionally extend discovery with live web-search results (Tavily) so
                    # NER reaches firms the daily ingest never mentions. Harvested in a SEPARATE
                    # pass at its own (lower) threshold — the curated queries + reputable-domain
                    # filter make each hit high-precision, so a single authoritative mention is
                    # enough to propose; the corpus pass keeps #105's min_mentions=3 noise gate.
                    # Inert (returns []) unless ONCA_SEARCH_EXPANSION=true AND a token is set.
                    _search = []
                    if os.environ.get("ONCA_SEARCH_EXPANSION", "false").lower() in ("1", "true", "yes"):
                        from src.ingest import search_expansion
                        _q = os.environ.get("ONCA_SEARCH_QUERIES")
                        _queries = ([q.strip() for q in _q.split("||") if q.strip()] if _q else None)
                        _dom = os.environ.get("ONCA_SEARCH_DOMAINS")
                        _domains = (None if _dom is None
                                    else [d.strip() for d in _dom.split(",") if d.strip()])
                        _raw = os.environ.get("ONCA_SEARCH_RAW_CONTENT", "text") or None
                        _search = search_expansion.expand(
                            _queries, per_query=int(os.environ.get("ONCA_SEARCH_PER_QUERY", "8")),
                            include_domains=_domains, include_raw_content=_raw)
                        if _search:
                            _seen = {c["surface"] for c in _cands}
                            _smm = int(os.environ.get("ONCA_SEARCH_MIN_MENTIONS", "1"))
                            for c in entity_discovery.harvest_ner(_search, min_mentions=_smm):
                                if c["surface"] not in _seen:
                                    _seen.add(c["surface"])
                                    c["keyword"] = "web_search"  # provenance in the review queue
                                    _cands.append(c)
                    _pids = entity_discovery.propose_news_candidates(
                        _cands, max_propose=int(os.environ.get("ONCA_NER_MAX_PROPOSE", "20")))
                    print(f"NER harvest: narratives={len(_narrs)} search={len(_search)} "
                          f"candidates={len(_cands)} proposed={len(_pids)}")
            except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
                print(f"Warning: NER harvest skipped: {exc}")

    # CVM ofertas status-change detection (#194, audit R5 B3) — `offerings` above is the
    # id-existence diff (`detect_new`); an offering whose status FLIPS to suspensa/revogada
    # after it was already seen never re-alerts there. This is a second, categorical diff
    # over the same fetch, durable-stored (`cvm_ofertas/status_index.json`) like `ceis_cnep`.
    new_ofertas_status: list[dict[str, Any]] = []
    if offerings and os.environ.get("ONCA_DIGESTS_BUCKET"):
        try:
            with _source_budget("CVM ofertas status", deadline, per_source):
                new_ofertas_status = cvm_ofertas.update_status_changes(
                    offerings, os.environ["ONCA_DIGESTS_BUCKET"]
                )
        except Exception as exc:  # pragma: no cover - defensive; upstream best-effort
            print(f"Warning: CVM ofertas status-change detection failed: {exc}")

    # Entities registry alias accumulation (ADR step 4): a structured CVM signal
    # (offering issuer / fato relevante company) that carries a CNPJ already
    # resolving to a known entity contributes its razão social to that entity's
    # aliases — so a later name-only signal (news, DOU) about it resolves too.
    # Data-derived + CNPJ-gated is the auto-safe case; best-effort, never blocks.
    # NB relocated here (2026-09-08, live bug fix): `new_ofertas` is only assigned once the
    # ADR-019 registry-driven loop above completes — this block used to sit BEFORE that loop
    # (a pre-ADR-019 layout) and referenced `new_ofertas` before its assignment, which Python's
    # function-wide scoping turns into an `UnboundLocalError` on every run (silently swallowed
    # by the surrounding try/except as "Warning: entity alias accumulation skipped"). Moving it
    # here fixes it; the one behavior change is that THIS run's own DOU/CADE resolution (which
    # happens inside that same loop, above) no longer benefits from aliases accumulated from
    # THIS run's ofertas/fatos — only from prior runs. Given the block has been fully broken
    # (never accumulating anything) since whichever change introduced the ordering bug, this is
    # a strict improvement, not a regression.
    if os.environ.get("ONCA_ENTITIES_TABLE") and os.environ.get(
        "ONCA_ENTITIES_ACCUMULATE", "true"
    ).lower() in ("1", "true", "yes"):
        try:
            with _source_budget("entities alias accumulation", deadline, per_source):
                from src.synth import entity_registry

                named = [(o.get("issuer"), o.get("issuer_cnpj")) for o in new_ofertas]
                named += [(f.get("company"), f.get("cnpj")) for f in new_fatos]
                acc = 0
                seen_pairs: set[tuple[str, str]] = set()
                for name, cnpj in named:
                    root = "".join(ch for ch in str(cnpj or "") if ch.isdigit())[:8]
                    if not name or len(root) < 8:
                        continue
                    pair = (root, str(name))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    eid = entity_registry.resolve_by_cnpj(root)
                    if eid and entity_registry.accumulate_aliases(eid, [name]):
                        acc += 1
                if acc:
                    print(f"entities: accumulated aliases for {acc} CVM signals")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks ingest
            print(f"Warning: entity alias accumulation skipped: {exc}")

    # Reclame Aqui — consumer-reputation snapshots for retail-facing banks/fintechs
    # (issue #31). Unofficial source, so best-effort + off via env; writes a durable
    # reputation/index.json (entity-tied), surfaced in the feed + agent. Requires the
    # digests bucket (already available for the corpus/digest writes).
    # Default OFF: the public RA origin is Cloudflare-gated (403). Enable only with
    # an authorized/proxied endpoint via ONCA_RA_IOSEARCH (see reclame_aqui docstring).
    reputation_summary: dict[str, Any] | None = None
    if _source_enabled(registry.by_id("reclame_aqui")):
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        try:
            with _source_budget("Reclame Aqui", deadline, per_source):
                ids = P["reclame_aqui"]["entity_ids"] or list(reclame_aqui.DEFAULT_ENTITY_IDS)
                comps = reclame_aqui.companies_from_registry(ids)
                snaps = reclame_aqui.fetch_reputation(comps)
                if snaps and bucket:
                    reclame_aqui.update_store(snaps, bucket)
                reputation_summary = {
                    **reclame_aqui.summarize(snaps),
                    "items": snaps[:12],
                }
        except Exception as exc:  # pragma: no cover - defensive; unofficial upstream
            print(f"Warning: Reclame Aqui fetch failed: {exc}")


    # CVM Informe Diário — fund AUM moves: a registry-loop source (C1, _fetch_inf_diario).

    # Corpus gets document-like signals only (not numeric Pix/juros/AUM moves).
    _populate_corpus_and_sync(
        new_normativos + new_funds + new_entrants + new_ofertas + new_sec
        + new_fatos + new_dou + new_cvm_normas + new_ofertas_status
    )

    # ADR 021 §H/§F — promote closed decisions (+ officer reference) into the KB (gated).
    _promote_decisions_to_kb()

    # Stage B fusion needs more than deltas: after seeding, items[] is often
    # empty while pulls still succeed. Attach compact context samples and
    # tag delta rows with is_new for prioritization.
    sections = {
        # ADR 019 — sections for the registry-loop sources are built IN the loop; the bespoke
        # sources below still build their own (special shapes / side effects).
        **loop_sections,
        "new_entrants": {
            "count": len(authorized),
            "new_count": len(new_entrants),
            "items": _tag_new(new_entrants[:8]),
            "context": _strip_raw(new_entrants[:8] or authorized[:5]),
        },
        # CVM ofertas status changes (#194, audit R5 B3) — categorical diff, not a registry-loop
        # source (it re-diffs the "ofertas" fetch above rather than fetching independently).
        # #198: EVERY new classified official act (medium+), uncapped and compact — the lens
        # sections above keep only items_limit, so the 11th+ act of a busy DOU week (LC 237)
        # never reached synth. Key "acts" (not items/context) so lens consumers ignore it.
        "official_acts": _official_acts(new_dou + new_cvm_normas + new_normativos),
        "cvm_ofertas_status": {
            "count": len(new_ofertas_status),
            "new_count": len(new_ofertas_status),
            "items": _tag_new(new_ofertas_status[:8]),
            "context": _strip_raw(new_ofertas_status[:12]),
        },
        # In "all" mode news is fetched inline here; in "structured" mode the news
        # slice is empty and the parallel news branch supplies it (overlaid at synth).
        "news": _news_slice(context) if mode == "all" else _empty_news_slice(),
        # Market-wide macro (Copom/Selic + Focus) — standalone macro cards.
        "macro": {"selic": macro_selic, "focus": macro_focus, "distress": distress_summary},
        # Consumer reputation (#31) — entity-tied; stores are authoritative.
        "reputation": reputation_summary,
        "bcb_reclamacoes": bcb_reclamacoes_summary,
        "ans_igr": ans_igr_summary,
        "consumidor_gov": consumidor_gov_summary,  # #63 (default-off, token-gated)
    }
    # The digest's key order is part of its bytes (and what synth has always read): assemble
    # it in the established order, whichever path built each section. A section not in the
    # list (a newly registered source) goes before the trailing "source" marker.
    payload = {k: sections.pop(k) for k in _PAYLOAD_ORDER if k in sections}
    payload.update(sections)
    payload["source"] = "lambda_port"

    # ADR 019 Phase 3 — vertical gating: drop the lens sections of sources not applicable to
    # the active vertical (each digest section key IS the source id). Sector-agnostic sources
    # ({all}: sanctions/cade/contracts) survive every vertical; FS-only sources vanish under a
    # sectorial vertical, so synth never fuses them into that vertical's cards. Non-lens
    # sections (macro/reputation/stores) are untouched. Since C1–C5 (#303) every source skips
    # its FETCH via _source_enabled; this drop only keeps the digest free of empty FS sections.
    _active_ids = {s.id for s in registry.active(_active_vertical())}
    for _sid in [s.id for s in registry.lens_sources()]:   # store sections stay (C4)
        if _sid not in _active_ids:
            payload.pop(_sid, None)

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    digest_key = None
    if bucket:
        try:
            s3 = boto3.client("s3")
            key = f"lambda-digests/{getattr(context, 'aws_request_id', 'local')}.json"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            digest_key = key
        except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
            print(f"Warning: S3 upload failed: {exc}")

    # #76: fold this run's per-source telemetry into the durable source-health store.
    # Sharded by branch: `StructuredIngest` and `NewsIngest` are parallel Lambda
    # invocations and this is a read-modify-write, so a shared key would let whichever
    # finished last silently drop the other's sources.
    if bucket:
        _source_health.merge_and_publish(
            bucket, shard=("structured" if mode == "structured" else None))

    return {"statusCode": 200, "body": _response_body(payload, digest_key)}
