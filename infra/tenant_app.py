"""Synth-only entry point for `OncaTenantStack` (ADR 016 addendum, Decision 4
step 1). A SEPARATE `App()`/entry point from `infra/app.py` on purpose — the
vendor stack's synth must never be able to accidentally pull in or instantiate
the tenant stack, and vice versa; keeping them in different CDK apps makes that
a structural fact, not a discipline.

Validate with:
    cdk synth --app "python infra/tenant_app.py"
    cdk synth --app "python infra/tenant_app.py" -c tenant_id=acme -c vendor_resolve_api_url=https://...

Do not `cdk deploy` this against the vendor's own AWS credentials/account — there
is no real tenant account yet, and deploying it there would create a confusing,
costly, pointless duplicate stack with no tenant to serve it (see
`tenant_stack.py`'s module docstring).

Every synth also runs `egress_audit.assert_single_account_egress` (ADR 016
addendum Decision 5, check 2) — this app EXITS NON-ZERO, before writing the
cloud assembly, if the template grants IAM access to any AWS account other
than the tenant's own. That is deliberate: this is the one entry point every
real tenant deploy goes through, so it is the one place the check is
guaranteed to run rather than trusted to a runbook step.
"""
from aws_cdk import App

from egress_audit import assert_single_account_egress
from tenant_stack import OncaTenantStack

app = App()
stack = OncaTenantStack(app, "OncaTenantStack")
assert_single_account_egress(stack)
app.synth()
