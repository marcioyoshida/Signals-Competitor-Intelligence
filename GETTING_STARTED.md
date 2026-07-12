# Getting started — Signals / Onça

## Drop this kit into the repo

    cd Signals-Competitor-Intelligence
    # copy CLAUDE.md, GETTING_STARTED.md, infra/ from this kit into repo root
    git add CLAUDE.md GETTING_STARTED.md infra/
    git commit -m "Add project context and AWS bootstrap for my2027"

If the data-spine prototype (onca-data-spine.zip from the previous step)
isn't in the repo yet, unzip it into the root too — CLAUDE.md assumes
its module layout (src/ingest, src/diff, run.py).

## Wire up the AWS account (one time)

1. Configure the CLI profile (SSO preferred over long-lived keys):

       aws configure sso --profile my2027
       # or: aws configure --profile my2027

2. Run the bootstrap:

       chmod +x infra/bootstrap.sh
       ./infra/bootstrap.sh

   It verifies the profile resolves to 668449743071, CDK-bootstraps
   us-east-1, creates the two baseline buckets, and sets a $100/mo
   budget alarm (edit the email placeholder first).

3. Manually enable Bedrock model access in the console (step 4 of the
   script prints the exact URL) — this cannot be done via CLI.

## First working session

    pip install -r requirements.txt
    python run.py            # expect field-name mismatches on first run
    # align field names against live API responses (see CLAUDE.md caveats)
    python run.py            # second run: real deltas only

## Working with Claude Code in this repo

CLAUDE.md carries the full decision history: architecture choices,
what NOT to do, phase status. Start sessions with a concrete goal, e.g.:

    "Port bcb_normativos.py to a Lambda with an EventBridge daily cron,
     as a CDK stack targeting profile my2027, per CLAUDE.md conventions."

## Security notes

- Never commit AWS credentials; the profile lives in ~/.aws only
- Both buckets are created with full public-access block
- Prototype uses no customer data — public government sources only,
  which keeps LGPD exposure at zero for this phase
