# CI/CD (GitOps) — AWS-native, CodePipeline + CodeBuild — 2026-08-25 (issue #6)

Replaces the GitHub-hosted-runner Actions workflow (slow, out-of-account) with an
in-account **CodePipeline + CodeBuild** pipeline. CodeDeploy is *not* used — it
targets EC2/Lambda/ECS application revisions, not CloudFormation stacks, so for a
CDK/CFN app the deployer is CodeBuild running `cdk deploy`.

```
GitHub (main)  ──CodeStar Connection──▶  CodePipeline "onca-cicd"
                                          ├─ Source:  GitHub main (trigger on push)
                                          └─ Deploy:  CodeBuild "onca-cicd-deploy"
                                                      runs buildspec.yml →
                                                      pytest → stage build/lambda →
                                                      cdk deploy OncaPrototypeStack
```

- Pipeline/build defined in **`infra/cicd.py`** as its own stack **`OncaCicdStack`**
  (separate from the app stack it deploys — no self-mutation).
- Build steps are versioned in the repo: **`buildspec.yml`**.
- CodeBuild image **STANDARD_7_0** (Ubuntu 22.04, Python 3.11, Node 20, rsync).
- The CodeBuild role is least-privilege for CDK: it only **assumes the CDK bootstrap
  roles** (`cdk-hnb659fds-*`) + reads the bootstrap version + `DescribeStacks`.

## One-time activation

### 1. Deploy the CI/CD stack (once, from an admin/dev machine)

```bash
cd infra && cdk deploy OncaCicdStack --require-approval never --app "python app.py"
```

### 2. Authorize the GitHub connection (once, in the console)

The `onca-github` CodeStar connection is created in **PENDING** state. Complete the
OAuth handshake so AWS can read the repo:

- Console → **Developer Tools → Settings → Connections** → `onca-github` →
  **Update pending connection** → install/authorize the AWS Connector GitHub app on
  the `marcioyoshida/Signals-Competitor-Intelligence` repo. Status → **Available**.

That's it — no PAT, no secret stored. After the connection is Available, every push
to `main` runs the pipeline: tests → stage → `cdk deploy OncaPrototypeStack`.

## Operating it

- **Trigger:** push to `main` (or *Release change* on the `onca-cicd` pipeline).
- **Logs:** CodePipeline console → `onca-cicd`, or CodeBuild → `onca-cicd-deploy`.
- **What deploys:** only `OncaPrototypeStack`. `OncaCicdStack` itself is managed out
  of band (re-run step 1 if you change `infra/cicd.py` or `buildspec.yml`).

## Notes / follow-ups

- Tests run `pytest -q --ignore=tests/test_lambda_port.py`; a `@pytest.mark.network`
  marker on the live-upstream suites would let CI use `-m "not network"` instead.
- PR checks: add a second CodeBuild project with a GitHub PR webhook (synth + tests,
  no deploy) if you move to a PR-based flow.
- Manual `cdk deploy` from a laptop still works unchanged.
