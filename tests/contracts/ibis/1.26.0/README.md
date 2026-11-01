# Ibis contract, pinned at 1.26.0 (#293)

Copied from Ibis `contracts/v1/` (Signals-License-Intelligence) at contract version 1.26.0:
`WebhookEvent.schema.json`, `Answer.schema.json` and `openapi.json` (its `servers` entry removed:
the Ibis endpoint lives in SSM `/onca/ibis/base-url`, never in this public repo).

`recorded_webhook_event.json` was built by Ibis's own payload builder from its recorded public
register fixtures, then the entity was replaced with a fictitious one ("Exemplo Cripto GmbH",
LEI `529900EXEMPLOFAKE042`, example.org link): the event is synthetic and must not name a real
company. `recorded_answer.json` is a live `/v1/check` answer recorded on 2026-11-01.

To move to a new Ibis MINOR version: copy the three files again, bump this directory, rerun
`tests/test_ibis_contract.py`. A MAJOR version is a new `contracts/v<major>/` on Ibis's side.
