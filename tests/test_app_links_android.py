"""#316: Android App Links — assetlinks.json only once a real signing fingerprint is configured."""
import json

from src.dashboard import app_links

FP = ":".join(["AB"] * 32)


def test_no_fingerprint_no_file(monkeypatch):
    monkeypatch.delenv("ONCA_ANDROID_CERT_SHA256", raising=False)
    assert app_links.assetlinks_response()["status"] == 404


def test_fingerprints_are_validated_and_served(monkeypatch):
    monkeypatch.setenv("ONCA_ANDROID_CERT_SHA256", f"{FP.lower()}, not-a-fingerprint")
    r = app_links.assetlinks_response()
    body = json.loads(r["body"])
    assert r["status"] == 200 and body[0]["target"]["package_name"] == "org.onssa.onca"
    assert body[0]["target"]["sha256_cert_fingerprints"] == [FP]


def test_package_matches_app_json():
    from pathlib import Path
    app = json.loads((Path(__file__).resolve().parents[1] / "mobile/apps/onca/app.json").read_text())
    assert app["expo"]["android"]["package"] == app_links.ANDROID_PACKAGE
    flt = app["expo"]["android"]["intentFilters"][0]
    assert flt["autoVerify"] and flt["data"][0]["pathPrefix"] == "/c/"   # never /exec (sign-in return)
