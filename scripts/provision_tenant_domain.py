"""#306 / ADR 016 SaaS: give a tenant its own domain on the CloudFront multi-tenant distribution.

Two phases, re-runnable (each step is idempotent):

  cert   python -m scripts.provision_tenant_domain cert --tenant qa-internal-admin --domain tenant-test.onssa.org
         Request (or find) the us-east-1 ACM certificate, upsert its DNS-validation CNAME in Cloudflare
         (token read in-process from Secrets Manager, never printed), and, once ISSUED, write
         {tenant, domain, cert_arn} into infra/cdk.json `tenant_domains` and `custom_domain` onto the
         tenant's onca-tenant-config row. Then deploy: the stack creates the distribution tenant and
         adds the domain to the Cognito callbacks and the session broker's allowed hosts.
  route  python -m scripts.provision_tenant_domain route --tenant qa-internal-admin --domain tenant-test.onssa.org
         After the deploy: point the domain (DNS-only CNAME) at the tenant's connection-group endpoint.

When the Cloudflare token is missing or rejected, the record to add by hand is printed instead.
Only SaaS/Sovereign tenants get a domain (ADR 016: Entry is the shared portal).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CDK_JSON = Path(__file__).resolve().parents[1] / "infra" / "cdk.json"
CF_SECRET = "shared/cloudflare/api-token"
CF_API = "https://api.cloudflare.com/client/v4"
REGION = "us-east-1"          # CloudFront certificates must live in us-east-1


def _zone_of(domain: str) -> str:
    return ".".join(domain.split(".")[-2:])


def upsert_context(cdk: dict[str, Any], tenant: str, domain: str, cert_arn: str) -> dict[str, Any]:
    """Pure: the cdk.json dict with this tenant's entry added or replaced (one entry per tenant)."""
    ctx = cdk.setdefault("context", {})
    rows = [r for r in ctx.get("tenant_domains") or [] if r.get("tenant") != tenant and r.get("domain") != domain]
    rows.append({"tenant": tenant, "domain": domain, "cert_arn": cert_arn})
    ctx["tenant_domains"] = sorted(rows, key=lambda r: r["tenant"])
    return cdk


class Cloudflare:
    def __init__(self, token: str):
        import requests
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    def _get(self, path: str, **params: Any) -> Any:
        r = self._s.get(f"{CF_API}{path}", params=params, timeout=20).json()
        if not r.get("success"):
            raise PermissionError(f"Cloudflare: {[e.get('message') for e in r.get('errors') or []]}")
        return r["result"]

    def upsert_cname(self, zone: str, name: str, target: str, comment: str) -> str:
        zid = self._get("/zones", name=zone)[0]["id"]
        name, target = name.rstrip("."), target.rstrip(".")
        have = self._get(f"/zones/{zid}/dns_records", name=name)
        body = {"type": "CNAME", "name": name, "content": target, "proxied": False, "ttl": 300, "comment": comment}
        if have and have[0]["type"] == "CNAME" and have[0]["content"] == target:
            return "unchanged"
        if have:
            raise RuntimeError(f"{name} already has a {have[0]['type']} record; refusing to overwrite it")
        r = self._s.post(f"{CF_API}/zones/{zid}/dns_records", json=body, timeout=20).json()
        if not r.get("success"):
            raise PermissionError(f"Cloudflare: {[e.get('message') for e in r.get('errors') or []]}")
        return "created"


def _cloudflare() -> Cloudflare | None:
    import boto3
    try:
        raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=CF_SECRET)["SecretString"]
        tok = json.loads(raw).get("api_token")
    except Exception:
        return None
    return Cloudflare(tok) if tok else None


def _dns(zone: str, name: str, target: str, comment: str) -> None:
    cf = _cloudflare()
    try:
        if cf is None:
            raise PermissionError("no Cloudflare token")
        print(f"DNS {name}: {cf.upsert_cname(zone, name, target, comment)}")
    except PermissionError as exc:
        print(f"Cloudflare unavailable ({exc}). Add this record by hand (DNS only, not proxied):\n"
              f"  CNAME {name.rstrip('.')}  ->  {target.rstrip('.')}")


def cmd_cert(tenant: str, domain: str) -> int:
    import time

    import boto3
    from src.dashboard.tenant_config import get_tenant_config
    cfg = get_tenant_config(tenant)
    if not cfg or cfg.get("tier") not in ("saas", "sovereign"):
        print(f"{tenant}: no SaaS/Sovereign tenant row; Entry tenants use the shared portal (ADR 016)")
        return 2
    acm = boto3.client("acm", region_name=REGION)
    arn = next((c["CertificateArn"] for c in acm.list_certificates()["CertificateSummaryList"]
                if c.get("DomainName") == domain), None)
    if arn is None:
        arn = acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                      Tags=[{"Key": "project", "Value": "onssa"}, {"Key": "tenant", "Value": tenant}])[
            "CertificateArn"]
        time.sleep(8)                                   # the validation record appears shortly after
    cert = acm.describe_certificate(CertificateArn=arn)["Certificate"]
    print(f"certificate {arn}: {cert['Status']}")
    if cert["Status"] != "ISSUED":
        rr = cert["DomainValidationOptions"][0].get("ResourceRecord")
        if rr:
            _dns(_zone_of(domain), rr["Name"], rr["Value"], f"ACM validation, Onça tenant {tenant}")
        print("Re-run this command once the certificate is ISSUED.")
        return 1
    cdk = upsert_context(json.loads(CDK_JSON.read_text(encoding="utf-8")), tenant, domain, arn)
    CDK_JSON.write_text(json.dumps(cdk, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    boto3.resource("dynamodb", region_name=REGION).Table(
        __import__("os").environ.get("ONCA_TENANT_CONFIG_TABLE", "onca-tenant-config")).update_item(
        Key={"tenant_id": tenant}, UpdateExpression="SET custom_domain = :d",
        ConditionExpression="attribute_exists(tenant_id)", ExpressionAttributeValues={":d": domain})
    print(f"infra/cdk.json tenant_domains updated; {tenant}.custom_domain = {domain}. Deploy, then run `route`.")
    return 0


def cmd_route(tenant: str, domain: str) -> int:
    import boto3
    cf = boto3.client("cloudfront", region_name=REGION)
    t = cf.get_distribution_tenant_by_domain(Domain=domain)["DistributionTenant"]
    cg = cf.get_connection_group(Identifier=t["ConnectionGroupId"])["ConnectionGroup"]
    print(f"tenant {t['Name']} ({t['Status']}), connection group {cg['Name']} -> {cg['RoutingEndpoint']}")
    _dns(_zone_of(domain), domain, cg["RoutingEndpoint"], f"Onça SaaS tenant {tenant} (#306)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("phase", choices=("cert", "route"))
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--domain", required=True)
    a = ap.parse_args(argv)
    return (cmd_cert if a.phase == "cert" else cmd_route)(a.tenant, a.domain.lower().rstrip("."))


if __name__ == "__main__":
    sys.exit(main())
