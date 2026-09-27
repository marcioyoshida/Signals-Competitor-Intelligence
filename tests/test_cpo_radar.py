"""#159 CPO Product Radar — ingester, classifier gates, clustering, baseline alert, surfaces.

No network: Apple RSS / YouTube / Bedrock / Secrets Manager are all stubbed. Fixtures in
tests/fixtures/cpo_radar/ are trimmed cuts of the #158 spike's real pulls.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import agent_ask, feed_builder, weekly_digest
from src.ingest import cpo_radar as cr
from src.synth import executive

FIX = Path(__file__).resolve().parent / "fixtures" / "cpo_radar"
SUBJ = {s["id"]: s for s in cr.load_seed_subjects()}


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_cached_key(monkeypatch):
    monkeypatch.setattr(cr, "_YT_KEY", {})
    monkeypatch.delenv("ONCA_YOUTUBE_API_KEY", raising=False)


# --- Apple RSS ------------------------------------------------------------------------------

def _page(entries):
    return {"feed": {"entry": entries}}


def _entries(start_id, n, ts):
    base = _load("appstore_rss_inter_page1.json")["feed"]["entry"][0]
    out = []
    for k in range(n):
        e = copy.deepcopy(base)
        e["id"]["label"] = str(start_id + k)
        e["updated"]["label"] = ts
        out.append(e)
    return out


def test_parse_rss_page_real_fixture_drops_author_and_app_entry():
    doc = _load("appstore_rss_inter_page1.json")
    app_entry = {"im:name": {"label": "Inter"}, "id": {"label": "839711154"}}   # page-1 lead entry
    doc["feed"]["entry"] = [app_entry] + doc["feed"]["entry"]
    rows = cr.parse_rss_page(doc)
    assert len(rows) == 6
    r = rows[0]
    assert r["id"] == "14592409228" and r["rating"] == 5 and r["version"] == "26.16.1"
    assert r["date"] == "2026-09-25"                      # 09:56 PDT → same BRT day
    assert "author" not in r and all("author" not in x for x in rows)
    # a single-review page is a dict, not a list
    one = _load("appstore_rss_inter_page1.json")
    one["feed"]["entry"] = one["feed"]["entry"][0]
    assert len(cr.parse_rss_page(one)) == 1
    assert cr.parse_rss_page({}) == []


def test_brt_day_converts_pacific_late_evening_to_next_day():
    assert cr.brt_day("2026-09-23T21:30:00-07:00") == "2026-09-24"
    assert cr.brt_day("2026-09-23T10:00:00-07:00") == "2026-09-23"
    assert cr.brt_day("2026-09-24T02:00:00Z") == "2026-09-23"


def test_pull_reviews_stops_on_overlap_with_stored_reviews():
    calls = []

    def fetch(url, params=None):
        calls.append(url)
        return _load("appstore_rss_inter_page1.json")

    page = cr.parse_rss_page(_load("appstore_rss_inter_page1.json"))
    seen = {page[3]["id"]}
    new, cov = cr.pull_reviews("839711154", seen=seen, since="2026-09-01", fetch=fetch)
    assert len(calls) == 1 and cov["stop"] == "overlap" and not cov["cap_hit"]
    assert {r["id"] for r in new} == {r["id"] for r in page} - seen


def test_pull_reviews_stops_on_empty_page_and_on_since():
    def fetch_empty(url, params=None):
        return _page(_entries(1000, 50, "2026-09-25T10:00:00-07:00")) if "page=1/" in url else {"feed": {}}

    new, cov = cr.pull_reviews("1", seen=set(), since="2026-09-01", fetch=fetch_empty)
    assert cov["pages"] == 1 and cov["stop"] == "empty" and len(new) == 50

    def fetch_old(url, params=None):
        p = int(url.split("page=")[1].split("/")[0])
        ts = "2026-09-25T10:00:00-07:00" if p == 1 else "2026-08-01T10:00:00-07:00"
        return _page(_entries(p * 100, 50, ts))

    new, cov = cr.pull_reviews("1", seen=set(), since="2026-09-01", fetch=fetch_old)
    assert cov["pages"] == 2 and cov["stop"] == "since" and len(new) == 50  # old page not kept


def test_pull_reviews_records_cap_hit_when_ten_pages_never_overlap():
    def fetch(url, params=None):
        p = int(url.split("page=")[1].split("/")[0])
        return _page(_entries(p * 100, 50, "2026-09-25T10:00:00-07:00"))

    new, cov = cr.pull_reviews("1", seen=set(), since="2026-09-20", fetch=fetch)
    assert cov["pages"] == 10 and cov["cap_hit"] is True and cov["stop"] == "cap"
    assert len(new) == 500


def test_pull_reviews_error_page_ends_pull_without_raising():
    def fetch(url, params=None):
        raise RuntimeError("HTTP 503 for https://itunes.apple.com/br/rss/...")

    new, cov = cr.pull_reviews("1", seen=set(), since="2026-09-20", fetch=fetch)
    assert new == [] and cov["stop"] == "error" and "503" in cov["error"]


# --- YouTube key + quota ------------------------------------------------------------------------

class _SM:
    def __init__(self, doc=None, fail=False):
        self.doc, self.fail, self.n = doc or {}, fail, 0

    def get_secret_value(self, SecretId):
        self.n += 1
        assert SecretId == "signalscompetitor/onca/api-key"
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"SecretString": json.dumps(self.doc)}


def test_youtube_key_absent_in_secret_is_none_and_cached():
    sm = _SM({"GOV_DADOS_TOKEN": "x"})
    assert cr.youtube_key(secrets_client=sm) is None
    assert cr.youtube_key(secrets_client=sm) is None
    assert sm.n == 1                                     # resolved once per container


def test_youtube_key_env_wins_and_secret_field_is_used(monkeypatch):
    monkeypatch.setenv("ONCA_YOUTUBE_API_KEY", "env-key")
    sm = _SM({"YOUTUBE_API_KEY": "secret-key"})
    assert cr.youtube_key(secrets_client=sm) == "env-key" and sm.n == 0
    monkeypatch.setattr(cr, "_YT_KEY", {})
    monkeypatch.delenv("ONCA_YOUTUBE_API_KEY")
    assert cr.youtube_key(secrets_client=sm) == "secret-key"
    monkeypatch.setattr(cr, "_YT_KEY", {})
    assert cr.youtube_key(secrets_client=_SM(fail=True)) is None


def test_redact_strips_key_from_error_text(monkeypatch):
    monkeypatch.setattr(cr, "_REDACT", ["SECRETKEY123"])
    assert "SECRETKEY123" not in cr._redact("giving up on ...?key=SECRETKEY123")


def test_youtube_client_enforces_unit_budget_before_calling():
    made = []

    def fetch(url, params=None):
        made.append(url)
        return {"items": [], "nextPageToken": "t"}

    yt = cr.YouTubeClient("k", budget=150, fetch=fetch)
    yt.creator_search("Nubank", "2026-09-24", pages=1)
    with pytest.raises(cr.QuotaExceeded):
        yt.creator_search("Inter", "2026-09-24", pages=1)
    assert yt.units == 100 and len(made) == 1


def test_default_daily_youtube_plan_fits_1000_units():
    # 5 products × (1 playlist page + 1 search page) + hydration ≈ 5×101 + ~5 ≪ 1,000
    per_product = cr.YT_COST["playlistItems"] + cr.YT_COST["search"] * 1 + cr.YT_COST["videos"]
    assert per_product * len(SUBJ) <= 1000


# --- language + is_new_change gates -----------------------------------------------------------

def _videos():
    return {v["video_id"]: dict(v, source="youtube", date=cr.brt_day(v["published"]))
            for v in _load("youtube_videos.json")["videos"]}


def test_language_gate_drops_spanish_mx_ar_videos_by_tag_and_by_text():
    vs = _videos()
    spanish = ["l1B7c4KCR_4", "WyGRFf9t2OA", "Lv6J3_0ZkOw", "n4WrR1OU0F8"]
    for vid in spanish:
        assert cr.language_gate(vs[vid])[0] is False
        assert cr.language_gate(dict(vs[vid], lang=None))[0] is False     # untagged → text
    # Portuguese content mis-tagged en-US / pt-PT / None is kept
    assert cr.language_gate(vs["3rje5HRwMjY"])[0] is True
    for vid in ("CPhwWfB-0Ao", "bgYZNFwgzAI", "DT8-ZFSdMWM", "jxWVqjqANoE"):
        assert cr.language_gate(dict(vs[vid], lang=None))[0] is True


LABELS = {   # what a correct Nova Lite answer looks like for each fixture video
    "CPhwWfB-0Ao": dict(event="price", is_new_change=True, change="fim do Priority Pass", feature="Priority Pass"),
    "FR2fTROyFmw": dict(event="price", is_new_change=True, change="Priority Pass restrito às salas VIP", feature="Priority Pass"),
    "UgP13Q6RnEs": dict(event="complaint", is_new_change=True, change="corte do Priority Pass", feature="Priority Pass Inter"),
    "bgYZNFwgzAI": dict(event="price", is_new_change=True, change="Ultravioleta Protegido pago", feature="proteção Ultravioleta"),
    "DT8-ZFSdMWM": dict(event="launch", is_new_change=True, change="Central de Cashback", feature="cashback em limite"),
    "jxWVqjqANoE": dict(event="feature", is_new_change=False, change="", feature="aumentar limite"),
    "4_okfjSX-vk": dict(event="feature", is_new_change=False, change="", feature="limite"),
    "l1B7c4KCR_4": dict(event="price", is_new_change=True, change="rendimento 15%", feature="Meli+"),
}


def _stub_converse(labels=LABELS, prose=True, calls=None):
    vs = _videos()
    by_title = {v["title"][:60]: vid for vid, v in vs.items()}

    def converse(prompt, max_tokens):
        if calls is not None:
            calls.append(prompt)
        items = [ln for ln in prompt.split("ITEMS:\n")[1].split("\n\nReturn ONLY")[0].split("\n") if ln[:1].isdigit()]
        out = []
        for ln in items:
            i = int(ln.split(".")[0])
            vid = next((v for t, v in by_title.items() if t in ln), None)
            if "app review" in ln:
                ev = "outage" if ("login" in ln.lower() or "entrar" in ln.lower()) else "praise"
                out.append({"i": i, "relevant": True, "event": ev, "sentiment": "neutral", "feature": "",
                            "is_new_change": False, "change": "", "pt_br": True, "why": "avaliação"})
                continue
            lab = labels.get(vid, dict(event="other", is_new_change=False, change="", feature=""))
            out.append(dict({"i": i, "relevant": True, "sentiment": "neutral", "pt_br": True,
                             "why": f"resumo {vid}"}, **lab))
        body = json.dumps(out, ensure_ascii=False)
        return (f"Aqui está:\n{body}\nFim." if prose else body), {"input_tokens": 100, "output_tokens": 50}

    return converse


def test_parse_response_handles_prose_and_salvages_truncated_arrays():
    assert cr.parse_response('ok [{"i": 0, "relevant": true}] done') == [{"i": 0, "relevant": True}]
    truncated = '[{"i": 0, "relevant": true, "event": "outage"}, {"i": 1, "relevant": false}, {"i": 2, "rel'
    got = cr.parse_response(truncated)
    assert [x["i"] for x in got] == [0, 1]
    assert cr.parse_response(None) == [] and cr.parse_response("no json") == []


def test_classify_with_stubbed_bedrock_applies_labels_and_marks_unscored():
    vs = list(_videos().values())
    usage = {}
    cr.classify(SUBJ["inter"], vs, _stub_converse(), usage=usage)
    assert usage["calls"] == 2 and usage["input_tokens"] == 200
    m = next(v for v in vs if v["video_id"] == "CPhwWfB-0Ao")
    assert m["provenance"] == "llm" and m["event"] == "price" and m["is_new_change"] is True
    assert m["pt_br"] is True and m["change"] == "fim do Priority Pass"

    # model returns only item 0 → the rest stay unscored (retried next run)
    def partial(prompt, n):
        return '[{"i": 0, "relevant": "true", "event": "BOGUS", "is_new_change": "false"}]', {}

    batch = [dict(v) for v in vs[:3]]
    cr.classify(SUBJ["inter"], batch, partial)
    assert batch[0]["provenance"] == "llm" and batch[0]["event"] == "other"   # unknown event → other
    assert batch[0]["relevant"] is True and batch[0]["is_new_change"] is False
    assert batch[1]["provenance"] == "unscored" and batch[2]["provenance"] == "unscored"


def test_prompt_keeps_bluefin_shape_plus_the_three_additions():
    p = cr.build_prompt(SUBJ["mercado_pago"], [dict(_videos()["DT8-ZFSdMWM"], kind="creator")])
    for k in ('"relevant"', '"event"', '"sentiment"', '"feature"', '"why"',
              '"is_new_change"', '"pt_br"', '"change"'):
        assert k in p
    assert "@mercadopagobrasiloficial" in p and "Mexico/Argentina" in p


def test_surfaceable_gate_excludes_evergreen_tutorials_and_spanish():
    vs = list(_videos().values())
    cr.classify(SUBJ["inter"], vs, _stub_converse())
    ok = {v["video_id"] for v in vs if cr.surfaceable(v)}
    assert {"CPhwWfB-0Ao", "FR2fTROyFmw", "UgP13Q6RnEs", "bgYZNFwgzAI", "DT8-ZFSdMWM"} <= ok
    assert "jxWVqjqANoE" not in ok and "4_okfjSX-vk" not in ok          # evergreen how-to
    assert "l1B7c4KCR_4" not in ok        # model said pt_br=True, the deterministic gate still drops it
    v = next(v for v in vs if v["video_id"] == "bgYZNFwgzAI")
    assert cr.surfaceable(dict(v, pt_br=False)) is False
    assert cr.surfaceable(dict(v, relevant=False)) is False


def test_corporate_news_is_classified_but_never_surfaced():
    # Live 2026-09-26: "Nubank negocia compra da Monzo" came out as a "launch". Corporate news
    # now has its own type, which the parser keeps and the card gate drops.
    assert "corporate" in cr.EVENTS and "corporate" not in cr.SURFACE_EVENTS
    m = {"video_id": "UygrItpHdGQ", "title": "Nubank negocia compra da Monzo", "description": ""}
    cr.apply_result(m, {"relevant": True, "event": "corporate", "is_new_change": True, "pt_br": True,
                        "change": "compra da Monzo", "why": "negociação de aquisição"})
    assert m["event"] == "corporate" and cr.surfaceable(m) is False
    p = cr.build_prompt(SUBJ["nubank"], [m]) if hasattr(cr, "build_prompt") else ""
    if p:
        assert '"corporate"' in p and "acquisition talks or rumours" in p


# --- clustering ----------------------------------------------------------------------------------

def test_three_creators_on_priority_pass_cluster_into_one_event():
    vs = [v for v in _videos().values() if v["product"] == "inter"]
    cr.classify(SUBJ["inter"], vs, _stub_converse())
    cand = [v for v in vs if cr.surfaceable(v)]
    groups = cr.cluster_mentions(SUBJ["inter"], cand)
    assert len(groups) == 1 and len(groups[0]) == 3
    ev = cr.cluster_to_event(SUBJ["inter"], groups[0])
    # two channels (one posted twice) → corroborated, three sources
    assert ev["n_sources"] == 3 and ev["n_channels"] == 2 and ev["confidence"] == "corroborado"
    assert ev["date"] == "2026-09-21" and ev["last_seen"] == "2026-09-24"
    assert ev["entity"] == "inter" and ev["type"] == "price"
    assert all(s["url"].startswith("https://www.youtube.com/watch?v=") for s in ev["sources"])
    # stable id: same cluster → same id across runs
    assert ev["id"] == cr.cluster_to_event(SUBJ["inter"], list(reversed(groups[0])))["id"]


def test_unrelated_changes_do_not_merge_and_far_apart_dates_split():
    a = {"change": "Central de Cashback", "feature": "", "date": "2026-09-22"}
    b = {"change": "Ultravioleta Protegido", "feature": "", "date": "2026-09-22"}
    c = {"change": "Central de Cashback", "feature": "", "date": "2026-08-01"}
    groups = cr.cluster_mentions(SUBJ["picpay"], [a, b, c])
    assert len(groups) == 3
    # brand words never glue two changes together
    d = {"change": "PicPay novo cartão", "feature": "", "date": "2026-09-22"}
    e = {"change": "PicPay nova conta", "feature": "", "date": "2026-09-22"}
    assert len(cr.cluster_mentions(SUBJ["picpay"], [d, e])) == 2


# --- baseline alert: the Inter 2026-09-23 v26.16 login outage --------------------------------

def _inter_reviews():
    rows = _load("inter_reviews_classified.json")["reviews"]
    return [dict(r, date=cr.brt_day(r["ts"])) for r in rows]


def test_inter_2026_09_23_outage_fires_with_version_and_error_strings():
    reviews = _inter_reviews()
    alerts = cr.detect_alerts(SUBJ["inter"], reviews, eval_days=["2026-09-23"],
                              coverage_since="2026-08-28",
                              app={"version": "26.16.1", "version_date": "2026-09-23T20:02:05Z"})
    assert len(alerts) == 1
    a = alerts[0]
    assert a["type"] == "outage" and a["source"] == "appstore" and a["date"] == "2026-09-23"
    out = a["metrics"]["outage"]
    assert out["count"] >= 10 and out["z"] >= 3 and out["baseline_range"][1] <= 9
    # 48 low-star reviews that day (BRT) vs ≤10/day before — carried as context
    assert a["context"]["low_star"]["count"] >= 40 and "low_star" not in a["metrics"]
    assert a["dominant_version"]["version"] == "26.16"
    texts = {e["text"].lower() for e in a["error_strings"]}
    assert any("test mode" in t for t in texts)
    assert a["url"] == "https://apps.apple.com/br/app/id839711154?see-all=reviews"
    assert a["app"]["version"] == "26.16.1" and "v26.16" in a["reason"]


def test_inter_baseline_is_quiet_on_ordinary_days():
    reviews = _inter_reviews()
    days = sorted({r["date"] for r in reviews if "2026-09-05" <= r["date"] <= "2026-09-22"})
    assert cr.detect_alerts(SUBJ["inter"], reviews, eval_days=days, coverage_since="2026-08-28") == []


def test_alert_needs_both_count_and_z_and_enough_baseline():
    def rows(day, n, rating=1):
        return [{"id": f"{day}-{k}", "date": day, "rating": rating, "version": "1.0",
                 "title": "", "text": ""} for k in range(n)]

    def cls(rs, ev="complaint"):
        return [dict(r, provenance="llm", relevant=True, event=ev) for r in rs]

    noisy = []   # a product that routinely has ~12 complaints/day: 13 is not a spike
    for d in range(1, 31):
        noisy += cls(rows(f"2026-08-{d:02d}", 12 if d % 2 else 8))
    noisy += cls(rows("2026-08-31", 13))
    assert cr.detect_alerts(SUBJ["c6"], noisy, eval_days=["2026-08-31"], coverage_since="2026-08-01") == []
    # a big z on a tiny count (C6 2026-09-10: 4 reviews, z=4.8 in the spike) does not fire
    quiet = cls(rows("2026-09-10", 4), "outage")
    assert cr.detect_alerts(SUBJ["c6"], quiet, eval_days=["2026-09-10"], coverage_since="2026-08-10") == []
    # low_star is a trigger only when the classifier mostly failed that day
    unscored = []
    for d in range(1, 31):
        unscored += rows(f"2026-08-{d:02d}", 2)
    unscored += rows("2026-08-31", 30)
    got = cr.detect_alerts(SUBJ["c6"], unscored, eval_days=["2026-08-31"], coverage_since="2026-08-01")
    assert len(got) == 1 and "low_star" in got[0]["metrics"]
    scored = [dict(r, provenance="llm", relevant=True, event="praise") for r in unscored]
    assert cr.detect_alerts(SUBJ["c6"], scored, eval_days=["2026-08-31"], coverage_since="2026-08-01") == []
    # cold start: <7 covered prior days → no verdict
    assert cr.detect_alerts(SUBJ["c6"], cls(rows("2026-09-10", 40), "outage"), eval_days=["2026-09-10"],
                            coverage_since="2026-09-07") == []


def test_top_error_strings_extracts_codes_and_quoted_messages():
    got = cr.top_error_strings([
        {"title": "Erro AL-903", "text": "aparece “Test Mode em produção” ao logar"},
        {"title": "", "text": "erro AL 903 de novo, iOS 26"},
        {"title": "", "text": "Pix fora (PIX 2026?)"},
    ])
    texts = [g["text"] for g in got]
    assert texts[0] == "AL-903" and got[0]["count"] == 2
    assert "Test Mode em produção" in texts
    assert not any(t.startswith("PIX") for t in texts)


# --- end-to-end run (local store, stubs) --------------------------------------------------------

def _stub_fetch(yt_items=None):
    """RSS: the Inter fixture page then an empty page. Lookup: a version. YouTube: canned."""
    vs = _videos()
    yt_items = yt_items or ["CPhwWfB-0Ao", "FR2fTROyFmw", "UgP13Q6RnEs", "jxWVqjqANoE"]

    def fetch(url, params=None):
        if "customerreviews" in url:
            return _load("appstore_rss_inter_page1.json") if "page=1/" in url else {"feed": {}}
        if "lookup" in url:
            return {"results": [{"version": "26.16.1", "currentVersionReleaseDate": "2026-09-23T20:02:05Z"}]}
        assert params and params.get("key") == "test-key"
        if url.endswith("/playlistItems"):
            return {"items": []}
        if url.endswith("/search"):
            return {"items": [{"id": {"videoId": v}} for v in yt_items]}
        if url.endswith("/videos"):
            return {"items": [{"id": v, "snippet": {"channelId": vs[v]["channel_id"], "channelTitle": vs[v]["channel"],
                                                   "title": vs[v]["title"], "description": vs[v]["description"],
                                                   "publishedAt": vs[v]["published"],
                                                   "defaultAudioLanguage": vs[v]["lang"]},
                               "statistics": {"viewCount": str(vs[v]["views"])}}
                              for v in params["id"].split(",") if v in vs]}
        raise AssertionError(url)

    return fetch


def test_run_without_youtube_key_skips_leg_and_says_so(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(cr, "youtube_key", lambda **kw: None)
    store = cr.LocalStore(tmp_path)
    out = cr.run(store, today=dt.date(2026, 9, 25), products=["inter"], fetch=_stub_fetch(),
                 converse=_stub_converse())
    radar = out["radar"]
    assert radar["sources"]["youtube"] == "disabled: no key"
    assert radar["youtube_quota"] is None and "youtube" not in radar["coverage"]["inter"]
    assert radar["coverage"]["inter"]["appstore"]["new"] == 6
    latest = json.loads((tmp_path / "product_radar" / "latest.json").read_text())
    assert latest["as_of"] == "2026-09-25"
    assert (tmp_path / "product_radar" / "2026-09-25.json").exists()
    st = json.loads((tmp_path / "product_radar" / "state.json").read_text())
    assert len(st["appstore"]["inter"]["reviews"]) == 6
    assert "test-key" not in json.dumps(latest) and "<redacted>" not in json.dumps(latest)

    # second run: overlap on page 1 → nothing new, nothing re-classified
    calls = []
    out2 = cr.run(store, today=dt.date(2026, 9, 25), products=["inter"], fetch=_stub_fetch(),
                  converse=_stub_converse(calls=calls))
    assert out2["radar"]["coverage"]["inter"]["appstore"]["stop"] == "overlap"
    assert out2["radar"]["coverage"]["inter"]["appstore"]["new"] == 0 and calls == []


def test_run_with_key_clusters_creators_and_meters_quota(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setenv("ONCA_CPO_YT_LOOKBACK_DAYS", "3")
    out = cr.run(cr.LocalStore(tmp_path), today=dt.date(2026, 9, 24), products=["inter"],
                 fetch=_stub_fetch(), converse=_stub_converse(), yt_key="test-key")
    radar = out["radar"]
    assert radar["sources"]["youtube"] == "ok"
    yt_events = [e for e in radar["events"] if e["source"] == "youtube"]
    # lookback 3 days (since 09-21 BRT): the three Priority Pass videos → ONE event; the
    # evergreen "como aumentar limite" tutorial never becomes a card
    assert len(yt_events) == 1 and yt_events[0]["n_sources"] == 3
    assert radar["youtube_quota"]["units"] <= 1000
    assert radar["youtube_quota"]["by_endpoint"]["search"] == 100


def test_reclassify_youtube_rescores_stored_videos_only(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setenv("ONCA_CPO_YT_LOOKBACK_DAYS", "3")
    store = cr.LocalStore(tmp_path)
    kw = dict(today=dt.date(2026, 9, 24), products=["inter"], fetch=_stub_fetch(), yt_key="test-key")
    cr.run(store, converse=_stub_converse(), **kw)
    prompts: list[str] = []

    def spy(p, n):
        prompts.append(p)
        return _stub_converse()(p, n)

    cr.run(store, converse=spy, **kw)
    n_plain = len(prompts)
    prompts.clear()
    out = cr.run(store, converse=spy, reclassify_youtube=True, **kw)
    assert len(prompts) > n_plain                     # stored videos went back through Nova
    assert out["radar"]["sources"]["classifier"] == "ok"
    st = json.loads((tmp_path / "product_radar" / "state.json").read_text())
    assert all(v["provenance"] == "llm" for v in st["youtube"]["inter"])


def test_run_bedrock_unavailable_keeps_items_unscored(tmp_path, monkeypatch):
    import datetime as dt

    monkeypatch.setattr(cr, "youtube_key", lambda **kw: None)
    out = cr.run(cr.LocalStore(tmp_path), today=dt.date(2026, 9, 25), products=["inter"],
                 fetch=_stub_fetch(), converse=lambda p, n: (None, {}))
    assert out["radar"]["sources"]["classifier"] == "unavailable"
    st = json.loads((tmp_path / "product_radar" / "state.json").read_text())
    assert all(r["provenance"] == "unscored" for r in st["appstore"]["inter"]["reviews"])


def test_prune_dated_removes_only_old_snapshots(tmp_path):
    import datetime as dt

    store = cr.LocalStore(tmp_path)
    for day in ("2026-08-01", "2026-08-30", "2026-09-20"):
        store.put(f"product_radar/{day}.json", {})
    store.put(cr.LATEST_KEY, {})
    store.put(cr.STATE_KEY, {})
    gone = cr.prune_dated(store, dt.date(2026, 9, 26))
    assert gone == ["product_radar/2026-08-01.json"] or sorted(gone) == ["product_radar/2026-08-01.json"]
    assert (tmp_path / "product_radar" / "latest.json").exists()


def test_lambda_handler_local_out(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "run", lambda store, **kw: {"ok": True, "store": type(store).__name__,
                                                          "kw": sorted(kw), "radar": {"x": 1}})
    res = cr.lambda_handler({"local_out": str(tmp_path), "products": ["inter"], "youtube": False}, None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 200 and body["store"] == "LocalStore" and "radar" not in body


# --- surfaces: feed / executive / ask / digest ------------------------------------------------

def _radar():
    reviews = _inter_reviews()
    alert = cr.detect_alerts(SUBJ["inter"], reviews, eval_days=["2026-09-23"], coverage_since="2026-08-28")[0]
    vs = [v for v in _videos().values() if v["product"] in ("inter", "picpay")]
    for pid in ("inter", "picpay"):
        cr.classify(SUBJ[pid], [v for v in vs if v["product"] == pid], _stub_converse())
    events = []
    for pid in ("inter", "picpay"):
        cand = [v for v in vs if v["product"] == pid and cr.surfaceable(v)]
        events += [cr.cluster_to_event(SUBJ[pid], g) for g in cr.cluster_mentions(SUBJ[pid], cand)]
    return {"as_of": "2026-09-26", "sources": {"appstore": "ok", "youtube": "ok", "classifier": "ok"},
            "coverage": {"inter": {"appstore": {"pages": 1}}}, "nova": {"calls": 3},
            "products": [{"id": "inter", "name": "Inter", "entity": "inter", "app": {}, "reviews_held": 9}],
            "events": sorted(events + [alert], key=lambda e: e["date"], reverse=True), "alerts": [alert]}


def _feed_with_radar():
    from tests.test_executive import _feed

    f = _feed()
    f["entity_attrs"]["inter"] = {"label": "Inter&Co", "industries": ["banking", "fintech"]}
    f["entity_attrs"]["picpay"] = {"label": "PicPay", "industries": ["fintech"]}
    f["product_radar"] = feed_builder.attach_product_radar(_radar())
    return f


def test_attach_product_radar_keeps_events_drops_operator_telemetry():
    fr = feed_builder.attach_product_radar(_radar())
    assert fr["events"] and fr["alerts"] and fr["sources"]["youtube"] == "ok"
    assert "coverage" not in fr and "nova" not in fr and "reviews_held" not in fr["products"][0]
    assert feed_builder.attach_product_radar({}) == {}


def test_build_cpo_product_radar_block_has_links_date_product_type_reason():
    ex = executive.build_executive(_feed_with_radar())
    pr = ex["cpo"]["panels"]["product_radar"]
    assert pr["as_of"] == "2026-09-26" and pr["n_alerts"] == 1
    types = {(r["product"], r["type"]) for r in pr["events"]}
    assert ("inter", "outage") in types and ("picpay", "launch") in types
    for r in pr["events"]:
        assert r["date"] and r["product_label"] and r["type_label"] and r["reason"]
        assert r["sources"] and all(s["url"].startswith("https://") for s in r["sources"])
        assert r["industries"]                 # bound entity → sector filter works
    out = next(r for r in pr["events"] if r["type"] == "outage")
    assert out["dominant_version"]["version"] == "26.16"


def test_product_radar_is_entity_scoped_for_tenants_and_hidden_from_sample():
    f = _feed_with_radar()
    banking = feed_builder.scope_feed_to_modules(f, ["banking"])
    assert {e["entity"] for e in banking["product_radar"]["events"]} == {"inter"}
    none = feed_builder.scope_feed_to_modules(f, ["insurance"])
    assert none["product_radar"]["events"] == []


def test_ask_grounds_on_radar_cards():
    f = _feed_with_radar()
    cards = agent_ask.product_radar_cards(f)
    alert = next(c for c in cards if c["is_alert"])
    assert alert["entity"] == "inter" and "26.16" in alert["narrative"]
    assert alert["citations"] and alert["citations"][0]["url"].startswith("https://apps.apple.com/")
    assert any("Priority Pass" in c["narrative"] for c in cards)


def test_weekly_digest_renders_last_seven_days_only():
    radar = _radar()
    old = dict(radar["events"][0], id="radar:old", date="2026-09-01", last_seen="2026-09-01", title="velho")
    radar["events"].append(old)
    d = cr.render_weekly_digest(radar, as_of="2026-09-26")
    assert d["period"] == ["2026-09-20", "2026-09-26"]
    assert all(e["id"] != "radar:old" for e in d["events"])
    assert d["lines"][0].startswith("[instabilidade] Inter — 2026-09-23")   # outages first
    assert "apps.apple.com" in d["lines"][0]
    assert "alerta" in d["headline"]
    empty = cr.render_weekly_digest({"as_of": "2026-09-26", "events": []})
    assert empty["lines"] == [] and "Sem mudanças" in empty["headline"]


def test_send_cpo_digest_uses_existing_channels_and_is_silent_without_radar():
    posted = []
    f = _feed_with_radar()
    rep = weekly_digest.send_cpo_digest(f, as_of="2026-09-26", slack_poster=lambda u, p: posted.append(p),
                                        teams_poster=lambda u, p: None, email_sender=lambda *a: None)
    # no channel configured in tests → every channel None, nothing sent
    assert set(rep) == {"teams", "slack", "email"}
    w = cr.render_weekly_digest(f["product_radar"], as_of="2026-09-26")
    slack = weekly_digest.format_slack({"title": w["title"], "headline": w["headline"], "lines": w["lines"],
                                        "lines_heading": "Eventos da semana"})
    txt = json.dumps(slack, ensure_ascii=False)
    assert "Radar de produto semanal do CPO" in txt and "Movimentos" not in txt
    subj, text, html = weekly_digest.format_email({"title": w["title"], "headline": w["headline"],
                                                   "lines": w["lines"]})
    assert subj.startswith("Radar de produto semanal do CPO") and "Inter" in text
    assert weekly_digest.send_cpo_digest({}, as_of="2026-09-26") == {"teams": None, "slack": None, "email": None}


# --- #159 follow-up: subjects live in the entity registry -----------------------------------------

def _registry_with_entities():
    from src.synth import entity_registry as er
    from tests.test_entity_registry import FakeTable

    t = FakeTable()
    for s in cr.load_seed_subjects():
        for eid in s["onca_entities"]:
            er.put_entity(eid, s["name"], [s["name"]], table=t)
    return er, t


def test_seed_registry_round_trips_every_seed_subject():
    er, t = _registry_with_entities()
    changed = cr.seed_registry(table=t)
    assert set(changed) == {"nubank", "inter", "picpay", "mercado_pago", "c6"} and all(changed.values())
    assert not any(cr.seed_registry(table=t).values())                 # idempotent
    reg = {s["id"]: s for s in er.list_product_radar_subjects(table=t)}
    for s in cr.load_seed_subjects():
        r = reg[s["id"]]
        for k in ("name", "aliases", "search_query", "namesake_risk", "apple_app_ids",
                  "youtube_channels", "onca_entities"):
            assert r[k] == s[k], (s["id"], k)
    assert er.get_entity("inter", table=t)["_prov"]["product_radar"]["source"].startswith("seed:")


def test_load_subjects_prefers_registry_and_falls_back_loudly():
    er, t = _registry_with_entities()
    assert cr.load_subjects(table=t) and cr.SUBJECTS_SOURCE["v"] == "seed_fallback"   # empty registry
    cr.seed_registry(table=t)
    er.set_product_radar("c6", None, table=t)                           # removed from the radar
    subs = cr.load_subjects(table=t)
    assert cr.SUBJECTS_SOURCE["v"] == "registry" and {s["id"] for s in subs} == {
        "nubank", "inter", "picpay", "mercado_pago"}
    cfg = dict(er.get_entity("picpay", table=t)["product_radar"], active=False)
    er.set_product_radar("picpay", cfg, table=t)                        # paused, config kept
    assert "picpay" not in {s["id"] for s in cr.load_subjects(table=t)}


def test_set_product_radar_rejects_a_subject_with_nothing_to_fetch():
    er, t = _registry_with_entities()
    import pytest

    with pytest.raises(ValueError):
        er.set_product_radar("nubank", {"name": "Nubank", "aliases": ["Nubank"]}, table=t)
    assert er.set_product_radar("ghost", {"apple_app_ids": [{"id": "1"}]}, table=t) is False
