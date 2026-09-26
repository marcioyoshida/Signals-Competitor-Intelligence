"""Nova Lite classification (#158 spike) — Bluefin analyze.py prompt shape, retargeted to CPO events.

Bluefin asks per mention: relevant (subject, not a namesake) / sentiment / controversy / why, with the
subject's official accounts as disambiguation anchors (#73 there). Here the same shape tags each
mention with: relevant, event (launch|feature|price|outage|complaint|praise|other), sentiment,
feature (the specific product/feature named, or ""), why (<=15 words).

Batched (BATCH items per Converse call) to keep tokens modest; <=4 worker threads. Token totals from
the Converse `usage` block go to out/nova_usage.json. Output: out/classified.json.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import threading

import boto3

import common as c

MODELS = ["amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0"]
BATCH = 10
EVENTS = {"launch", "feature", "price", "outage", "complaint", "praise", "other"}
_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failed_batches": 0, "model": None}
_lock = threading.Lock()
_rt = boto3.Session(profile_name=c.PROFILE).client("bedrock-runtime", region_name="us-east-1")


def _anchor(s: dict) -> str:
    parts = [f"YouTube {ch['handle']}" for ch in s["youtube_channels"]]
    parts += [f"iOS app id{a['id']} ({a['name']})" for a in s["apple_app_ids"]]
    return "; ".join(parts)


def _item_text(m: dict) -> str:
    if m["source"] == "youtube":
        return (f"[{m['kind']} video by {m['channel']}] TITLE: {m['title'][:200]} | "
                f"DESCRIPTION: {re.sub(r'\s+', ' ', m['description'])[:500]}")
    return f"[app review, {m['rating']} stars, app v{m.get('version')}] {m['title'][:120]} | {m['text'][:500]}"


def _prompt(s: dict, batch: list[dict]) -> str:
    items = "\n".join(f"{i}. {_item_text(m)}" for i, m in enumerate(batch))
    return (
        "You are a competitor product-monitoring classifier for a bank's Chief Product Officer.\n"
        f"SUBJECT PRODUCT: {s['name']} (Brazilian digital bank / fintech app)"
        f" (also: {', '.join(s['aliases'])})\nPUBLISHES AT: {_anchor(s)}\n"
        f"NAMESAKE NOTE: {s['namesake_risk']}\n\n"
        f"ITEMS (Portuguese):\n{items}\n\n"
        "Return ONLY a JSON array, one object per item in order, keys: "
        '"i" (item number), '
        '"relevant" (true only if the item is substantively about the SUBJECT\'s own products/app, '
        'not a namesake, not a passing mention in a list of banks), '
        '"event" ("launch" new product | "feature" new/changed feature | "price" fee/rate/cashback/limit '
        'change | "outage" app/service failure | "complaint" user problem | "praise" | "other"), '
        '"sentiment" ("positive"|"neutral"|"negative"), '
        '"feature" (the specific product or feature named, e.g. "Pix parcelado", else ""), '
        '"why" (<=15 words, English).'
    )


def _call(s: dict, batch: list[dict]) -> list[dict]:
    prompt = _prompt(s, batch)
    for model in ([_usage["model"]] if _usage["model"] else MODELS):
        try:
            r = _rt.converse(modelId=model, messages=[{"role": "user", "content": [{"text": prompt}]}],
                             inferenceConfig={"maxTokens": 90 * len(batch) + 100, "temperature": 0})
        except Exception as e:  # noqa: BLE001
            if "on-demand" in str(e).lower() or "inference profile" in str(e).lower():
                continue
            print("[classify] call failed:", str(e)[:200])
            break
        with _lock:
            _usage["model"] = model
            _usage["calls"] += 1
            _usage["input_tokens"] += r["usage"]["inputTokens"]
            _usage["output_tokens"] += r["usage"]["outputTokens"]
        txt = r["output"]["message"]["content"][0]["text"]
        m = re.search(r"\[.*\]", txt, re.DOTALL)
        try:
            return json.loads(m.group(0)) if m else []
        except json.JSONDecodeError:
            return []
    with _lock:
        _usage["failed_batches"] += 1
    return []


def main() -> None:
    subj = {s["id"]: s for s in c.subjects()}
    mentions = c.load("youtube.json") + c.load("appstore.json")
    jobs = []
    for pid, s in subj.items():
        for src in ("youtube", "appstore"):
            ms = [m for m in mentions if m["product"] == pid and m["source"] == src]
            jobs += [(s, ms[i:i + BATCH]) for i in range(0, len(ms), BATCH)]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_call, s, b): (s, b) for s, b in jobs}
        for f in cf.as_completed(futs):
            s, b = futs[f]
            res = {int(x.get("i", -1)): x for x in (f.result() or []) if isinstance(x, dict)}
            for i, m in enumerate(b):
                x = res.get(i)
                if x is None:
                    m.update(provenance="unscored", relevant=None, event=None, sentiment=None, feature="", why="")
                    continue
                ev = str(x.get("event", "other")).lower()
                m.update(provenance="llm", relevant=bool(x.get("relevant")),
                         event=ev if ev in EVENTS else "other", sentiment=str(x.get("sentiment", "neutral")),
                         feature=str(x.get("feature") or "")[:80], why=str(x.get("why") or "")[:140])
    c.dump("classified.json", mentions)
    # Nova Lite on-demand list price (us-east-1): $0.06 / 1M input, $0.24 / 1M output.
    _usage["usd"] = round(_usage["input_tokens"] * 0.06e-6 + _usage["output_tokens"] * 0.24e-6, 4)
    _usage["mentions"] = len(mentions)
    c.dump("nova_usage.json", _usage)
    print(_usage)


if __name__ == "__main__":
    main()
