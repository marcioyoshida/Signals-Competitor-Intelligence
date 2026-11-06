"""#347 (BYOK story 2, ADR 031, #345): a tenant's model API key, sealed.

One row per tenant in ``OncaModelKeys`` (pk ``tenant_id``):

- ``sealed``: the key encrypted under Onça's model-key KMS key, with encryption context
  ``{"onca-tenant": <tenant>, "onca-purpose": "model-key"}``. The IAM grants are conditioned on
  that purpose, and the tenant in the context means one tenant's ciphertext can't be decrypted
  as another's.
- Non-secret metadata only beside it: provider, model, the last four characters, who connected
  it and when, the last verification and the last error.

The plaintext lives only between ``kms.decrypt`` and the SDK client (``client_model``). It is
never returned, stored, logged or journaled. Nothing is cached, so a disconnect stops the next
call at once.

#350 adds a second kind of row, ``provider = bedrock_role``: the client's own Amazon Bedrock,
reached by assuming an IAM role in the client's account. Nothing in it is secret (role ARN,
region, the tenant's external id), so it carries no ``sealed`` blob and needs no KMS.

Ported from CORAX ``src/corax/byom.py`` ``ModelKeys`` (live since 2026-10-13).
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from src.synth import client_model as cm

PURPOSE = "model-key"
TABLE_ENV = "ONCA_MODEL_KEYS_TABLE"
KMS_ENV = "ONCA_MODEL_KEY_KMS"

#: What the tenant agrees to before a key is stored (pt-BR, shown in the account sheet, #348).
ACKNOWLEDGEMENT = (
    "Ao conectar o seu próprio modelo, as perguntas da sua equipe e os dados de contexto "
    "selecionados pela Onça para respondê-las são enviados ao provedor do modelo (Anthropic) e "
    "processados por ele, nos termos do contrato da sua organização com esse provedor. As "
    "chamadas são cobradas na sua conta do provedor.")
#: #350: the same agreement for the client's own Bedrock, where the processor is AWS under the
#: client's own AWS contract.
ACKNOWLEDGEMENT_BEDROCK = (
    "Ao conectar o Amazon Bedrock da sua conta AWS, as perguntas da sua equipe e os dados de "
    "contexto selecionados pela Onça para respondê-las são enviados ao Amazon Bedrock na conta AWS "
    "da sua organização e processados ali, nos termos do contrato da sua organização com a AWS. "
    "As chamadas são cobradas na sua conta AWS.")
ACKNOWLEDGEMENTS = {cm.ANTHROPIC_KEY: ACKNOWLEDGEMENT, cm.BEDROCK_ROLE: ACKNOWLEDGEMENT_BEDROCK}


@dataclass
class Connection:
    """What an API response may show about a tenant's key. The key itself is never included."""

    provider: str
    model: str
    last4: str
    connected_by: str
    connected_at: str
    verified_at: str = ""
    last_error: str = ""
    last_error_at: str = ""
    role_arn: str = ""      # #350 bedrock_role only
    region: str = ""

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "label": cm.MODELS.get(self.model, {}).get("label", self.model),
                "last_error_message": cm.MESSAGES.get(self.last_error, "") if self.last_error else ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _context(tenant: str) -> dict[str, str]:
    return {"onca-tenant": tenant, "onca-purpose": PURPOSE}


def configured() -> bool:
    return bool(os.environ.get(TABLE_ENV) and os.environ.get(KMS_ENV))


def _table(table: Any | None) -> Any:
    if table is not None:
        return table
    import boto3
    return boto3.resource("dynamodb").Table(os.environ[TABLE_ENV])


def _kms(kms: Any | None) -> Any:
    if kms is not None:
        return kms
    import boto3
    return boto3.client("kms")


_FIELDS = tuple(Connection.__dataclass_fields__)


def connection(tenant: str, *, table: Any | None = None) -> Connection | None:
    row = _table(table).get_item(Key={"tenant_id": tenant}).get("Item")
    if not row:
        return None
    return Connection(**{k: str(row[k]) for k in _FIELDS if k in row})


def connect(tenant: str, *, api_key: str, model: str, actor: str, acknowledged: bool,
            table: Any | None = None, kms: Any | None = None,
            verify: Callable[[cm.Target], None] | None = None) -> Connection:
    """Verify with a real call FIRST, then seal and store. Raises ValueError (bad input) or
    ``client_model.ModelError`` (the provider refused the key); nothing is stored either way."""
    api_key = (api_key or "").strip()
    if not tenant:
        raise ValueError("a model belongs to an organisation account")
    if model not in cm.MODELS:
        raise ValueError("escolha um dos modelos permitidos")
    if not api_key:
        raise ValueError("informe a chave de API")
    if not acknowledged:
        raise ValueError("conectar exige o aceite: o provedor do modelo, não a Onça, processa "
                         "as perguntas e os dados de contexto enviados a ele")
    target = cm.Target(provider=cm.MODELS[model]["provider"], model=model, tenant=tenant,
                       api_key=api_key)
    (verify or cm.verify)(target)
    sealed = _kms(kms).encrypt(KeyId=os.environ.get(KMS_ENV, ""), Plaintext=api_key.encode(),
                               EncryptionContext=_context(tenant))["CiphertextBlob"]
    now = _now()
    conn = Connection(provider=target.provider, model=model, last4=api_key[-4:],
                      connected_by=actor, connected_at=now, verified_at=now)
    _table(table).put_item(Item={"tenant_id": tenant, **asdict(conn),
                                 "sealed": base64.b64encode(sealed).decode()})
    return conn


def connect_role(tenant: str, *, role_arn: str, region: str, external_id: str, model: str,
                 actor: str, acknowledged: bool, table: Any | None = None,
                 verify: Callable[[cm.Target], None] | None = None) -> Connection:
    """#350: connect the client's own Bedrock. Verify FIRST (assume the role, one minimal call),
    then store. ``external_id`` comes from the tenant's own config, never from the request: that
    is what stops one tenant from pointing Onça at another tenant's role (confused deputy)."""
    role_arn, region = (role_arn or "").strip(), (region or "").strip()
    if not tenant:
        raise ValueError("a model belongs to an organisation account")
    if cm.MODELS.get(model, {}).get("provider") != cm.BEDROCK_ROLE:
        raise ValueError("escolha um dos modelos permitidos")
    if not re.fullmatch(cm.ROLE_ARN_RE, role_arn):
        raise ValueError("informe o ARN do papel (arn:aws:iam::<conta>:role/<nome>)")
    broker = os.environ.get(cm.BROKER_ROLE_ENV, "")
    if broker and role_arn.split(":")[4] == broker.split(":")[4]:
        raise ValueError("o papel precisa estar na conta AWS da sua organização")
    if region not in cm.BEDROCK_REGIONS:
        raise ValueError("escolha uma região em que o Claude no Amazon Bedrock é servido")
    if not external_id:
        raise ValueError("external id ausente: abra de novo a tela do modelo próprio")
    if not acknowledged:
        raise ValueError("conectar exige o aceite: o Amazon Bedrock da sua conta, não a Onça, "
                         "processa as perguntas e os dados de contexto enviados a ele")
    target = cm.Target(provider=cm.BEDROCK_ROLE, model=model, tenant=tenant, role_arn=role_arn,
                       external_id=external_id, region=region)
    (verify or cm.verify)(target)
    now = _now()
    conn = Connection(provider=cm.BEDROCK_ROLE, model=model, last4="", connected_by=actor,
                      connected_at=now, verified_at=now, role_arn=role_arn, region=region)
    _table(table).put_item(Item={"tenant_id": tenant, **asdict(conn), "external_id": external_id})
    return conn


def disconnect(tenant: str, *, table: Any | None = None) -> bool:
    """Remove the key. Returns whether one was there."""
    old = _table(table).delete_item(Key={"tenant_id": tenant}, ReturnValues="ALL_OLD")
    return bool(old.get("Attributes"))


def load_target(tenant: str, *, table: Any | None = None, kms: Any | None = None
                ) -> cm.Target | None:
    """The tenant's Target with its decrypted key, or None when no key is connected. A KMS or
    DynamoDB failure RAISES: returning None would answer on hosted Nova for a tenant that chose
    its own model."""
    row = _table(table).get_item(Key={"tenant_id": tenant}).get("Item")
    if not row:
        return None
    if str(row.get("provider")) == cm.BEDROCK_ROLE:
        return cm.Target(provider=cm.BEDROCK_ROLE, model=str(row["model"]), tenant=tenant,
                         role_arn=str(row["role_arn"]), external_id=str(row["external_id"]),
                         region=str(row["region"]))
    plain = _kms(kms).decrypt(CiphertextBlob=base64.b64decode(str(row["sealed"])),
                              EncryptionContext=_context(tenant))["Plaintext"]
    return cm.Target(provider=str(row["provider"]), model=str(row["model"]), tenant=tenant,
                     api_key=plain.decode())


def record_check(tenant: str, *, error: str = "", table: Any | None = None) -> None:
    """Stamp the outcome of a call: a success sets ``verified_at`` and clears the error; a failure
    sets ``last_error``. It updates an EXISTING row only, so a disconnect racing a call can't
    resurrect the row. Best-effort."""
    now = _now()
    if error:
        expr, vals = "SET last_error = :e, last_error_at = :t", {":e": error, ":t": now}
    else:
        expr, vals = "SET verified_at = :t, last_error = :e, last_error_at = :e", {":t": now, ":e": ""}
    try:
        _table(table).update_item(Key={"tenant_id": tenant}, UpdateExpression=expr,
                                  ExpressionAttributeValues=vals,
                                  ConditionExpression="attribute_exists(tenant_id)")
    except Exception as exc:  # noqa: BLE001 - a stamp never breaks an answer
        print(f"Warning: model-key stamp skipped: {type(exc).__name__}")
