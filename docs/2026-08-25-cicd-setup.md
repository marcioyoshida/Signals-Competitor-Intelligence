# CI/CD (GitOps) — setup — 2026-08-25 (issue #6)

Two GitHub Actions workflows implement GitOps deploys from this repo:

- **`.github/workflows/ci.yml`** — on every PR to `main`: run tests + `cdk synth`
  (template compiles). No AWS credentials, no deploy.
- **`.github/workflows/deploy.yml`** — on push to `main` (paths `src/**`, `infra/**`,
  `requirements.txt`): tests → stage the Lambda asset (`build/lambda` = deps + `src/`)
  → **`cdk deploy OncaPrototypeStack`**. Manual `workflow_dispatch` can also **start the
  pipeline** after deploy. Auth is **OIDC** — no long-lived AWS keys in GitHub.

This replaces the manual loop we ran all along (rsync → cdk synth → background cdk
deploy → poll). On the Linux CI runner, CDK runs natively — no WSL/Windows friction.

## One-time AWS setup (account 668449743071, us-east-1)

Least-privilege pattern: the GitHub OIDC role does **not** get admin — it only assumes
the CDK v2 bootstrap roles (already created by `cdk bootstrap`), which hold the real
deploy permissions. Run these once with an admin profile.

### 1. GitHub OIDC identity provider (skip if it already exists)

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

### 2. Deploy role trusting only this repo's `main`

`trust.json` (restrict to the repo + main branch; PRs never assume it):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::668449743071:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike": { "token.actions.githubusercontent.com:sub": "repo:marcioyoshida/Signals-Competitor-Intelligence:ref:refs/heads/main" }
    }
  }]
}
```

`perms.json` — assume the CDK bootstrap roles + start the pipeline (for the optional step):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::668449743071:role/cdk-hnb659fds-*-668449743071-us-east-1"
    },
    {
      "Effect": "Allow",
      "Action": "cloudformation:DescribeStacks",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "states:StartExecution",
      "Resource": "arn:aws:states:us-east-1:668449743071:stateMachine:OncaPipeline*"
    }
  ]
}
```

```bash
aws iam create-role --role-name onca-github-deploy \
  --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name onca-github-deploy \
  --policy-name onca-cdk-deploy --policy-document file://perms.json
```

## GitHub repo configuration (Settings → Secrets and variables → Actions → Variables)

| Variable | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::668449743071:role/onca-github-deploy` |
| `ONCA_PIPELINE_ARN` | `arn:aws:states:us-east-1:668449743071:stateMachine:OncaPipeline64AA66FE-tGEFHlEqVym4` |

(These are non-secret ARNs, so repo **Variables** are fine; no secrets needed thanks to OIDC.)

## Notes / follow-ups

- **Tests in CI** run `pytest -q --ignore=tests/test_lambda_port.py`. A few suites hit
  live upstreams (BCB/CVM/news) and are slow/flaky for CI; the clean fix is a
  `@pytest.mark.network` marker on those + `-m "not network"` here (follow-up).
- The deploy job is serialized via a `concurrency` group so it never interrupts an
  in-flight CloudFormation update (the SIGKILL/foreground problem we hit manually is
  moot on CI, but concurrency still prevents overlapping stack updates).
- Manual deploy from a laptop still works exactly as before; CI just automates it.
