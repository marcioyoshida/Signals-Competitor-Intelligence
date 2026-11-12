"""#382: international standard-setters (BIS/BCBS/CPMI, FSB) on the CRO board.

Titles mirror the live feed shapes (probed 2026-11-12); they are public headlines."""
import datetime as dt
import io
import json

from src.dashboard import feed_builder
from src.ingest import standard_setters as T
from src.synth import executive

TODAY = dt.date(2026, 11, 12)

BIS = {"list": {
    "/media-releases/20261016-basel-g-sib": {
        "title": "Basel Committee finalises measures to address window-dressing in the G-SIB framework",
        "content_date": "2026-10-16", "sources": ["Bank for International Settlements", "BCBS"]},
    "/media-releases/20261029-cpmi": {
        "title": "CPMI publishes analysis on how central banks support payment system safety",
        "content_date": "2026-10-29", "sources": ["Bank for International Settlements", "CPMI"]},
    "/media-releases/20261110-appointments": {
        "title": "Appointments to BIS global payments standard-setting body",
        "content_date": "2026-11-10", "sources": ["Bank for International Settlements", "CPMI"]},
    "/media-releases/20261001-meets": {
        "title": "Basel Committee meets to advance supervisory and regulatory initiatives",
        "content_date": "2026-10-01", "sources": ["BCBS"]},
    "/media-releases/20240101-old": {
        "title": "Basel Committee publishes capital standard", "content_date": "2024-01-01",
        "sources": ["BCBS"]},
    "/media-releases/20260628-economy": {
        "title": "Global economic pressure points call for policy discipline: BIS",
        "content_date": "2026-06-28", "sources": ["Bank for International Settlements"]},
}}

FSB = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>FSB recommends Brazil further strengthens its framework for crypto-assets and stablecoins</title>
<link>https://www.fsb.org/2026/11/x/</link><pubDate>Wed, 11 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>FSB Regional Consultative Group for Europe meets in Bern</title>
<link>https://www.fsb.org/2026/11/y/</link><pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>FSB finalises sound practices for the responsible adoption of artificial intelligence (AI)</title>
<link>https://www.fsb.org/2026/10/z/</link><pubDate>Thu, 15 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


class _Resp:
    def __init__(self, body=None, js=None):
        self.content, self._js = body, js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


class _S3:
    def __init__(self):
        self.objs = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objs[Key])}

    def put_object(self, Bucket, Key, Body, **_):
        self.objs[Key] = Body


def _get(fail=()):
    def get(url, **_):
        if ("bis" in url and "bis" in fail) or ("fsb" in url and "fsb" in fail):
            raise RuntimeError("timeout")
        return _Resp(js=BIS) if "bis.org" in url else _Resp(body=FSB)
    return get


def test_bis_keeps_tagged_publications_and_drops_noise_and_old():
    rows = T.parse_bis(BIS, since="2025-11-12")
    titles = {r["title"][:20]: r for r in rows}
    assert set(titles) == {"Basel Committee fina", "CPMI publishes analy"}
    gsib = titles["Basel Committee fina"]
    assert gsib["body"] == "BCBS" and gsib["topics"] == ["capital"] and gsib["implementers"] == ["BCB"]
    assert gsib["url"].startswith("https://www.bis.org/media-releases/")
    assert "banking" in gsib["industries"]
    assert titles["CPMI publishes analy"]["body"] == "CPMI"


def test_fsb_parses_rss_tags_topics_and_flags_brazil():
    rows = T.parse_fsb(FSB, since="2025-11-12")
    assert [r["topics"] for r in rows] == [["crypto"], ["tech"]]       # "meets in Bern" dropped
    assert rows[0]["brazil"] and rows[0]["date"] == "2026-11-11" and "CVM" in rows[0]["implementers"]


def test_noise_filter():
    for t in ("Appointment: BIS Chair", "Statement on the appointment of X as Governor",
              "BIS extends term for Y as Chair", "Basel Committee meets to discuss risks"):
        assert T.is_noise(t)
    assert not T.is_noise("Basel Committee publishes principles for third-party risk")


def test_update_state_first_seen_and_failed_source_keeps_items():
    s3 = _S3()
    out = T.update_state("bkt", s3=s3, get=_get(), today=TODAY)
    assert out == {"items": 4, "fetched": {"bis": 2, "fsb": 2}, "errors": []}
    out = T.update_state("bkt", s3=s3, get=_get(fail=("fsb",)), today=TODAY + dt.timedelta(days=1))
    doc = json.loads(s3.objs[T.STATE_KEY])
    assert out["errors"] == ["fsb"] and len(doc["items"]) == 4
    assert all(i["first_seen"] == "2026-11-12" for i in doc["items"])


def test_for_feed_and_scope():
    s3 = _S3()
    T.update_state("bkt", s3=s3, get=_get(), today=TODAY)
    block = T.for_feed(json.loads(s3.objs[T.STATE_KEY]), today=TODAY)
    assert len(block["items"]) == 4 and "first_seen" not in block["items"][0]
    assert T.scope(block, ["advisory"])["items"] == []
    ins = T.scope(block, ["insurance"])["items"]
    assert [i["topics"] for i in ins] == [["tech"]]


def test_cro_panel_count_and_recommendation():
    s3 = _S3()
    T.update_state("bkt", s3=s3, get=_get(), today=TODAY)
    block = T.for_feed(json.loads(s3.objs[T.STATE_KEY]), today=TODAY)
    feed = {"dates": ["2026-11-12"], "run_date": "2026-11-12",
            "industry_options": [{"slug": "banking", "label": "Banking"}],
            "entity_attrs": {}, "feed": [], "standard_setters": block}
    cro = executive.build_executive(feed)["cro"]
    assert len(cro["panels"]["standard_setters"]) == 4
    assert cro["by_industry"]["banking"]["n_global_standards"] >= 2
    assert any("Padrão internacional recente" in r["text"] for r in cro["panels"]["recommendations"])


def test_feed_projections_scope_or_empty_the_block():
    src = open(feed_builder.__file__, encoding="utf-8").read()
    assert src.count('"standard_setters": _scope_standard_setters(') == 2   # tenant + entry
    assert '"sanctions", "standard_setters"):' in src                     # sample empties it


def test_runs_on_the_sanctions_branch():
    lp = open("src/ingest/lambda_port.py", encoding="utf-8").read()
    i = lp.index("def _sanctions_slice(")
    assert "standard_setters.update_state(bucket)" in lp[i:lp.index("\ndef ", i + 10)]
