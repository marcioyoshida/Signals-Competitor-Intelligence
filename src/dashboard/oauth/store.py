"""Persistence for the authorization server (D8): one DynamoDB table, pk/sk strings.

Item layout (only hashes of codes and refresh tokens are ever stored — T1/T7):

  req#<id>        -            pending authorization (TTL 10 min)
  code#<sha256>   -            authorization code (TTL 60 s, taken atomically — T1)
  rt#<sha256>     -            refresh token: family, used flag (rotated — T12)
  org#<org>       fam#<fid>    refresh family: client, created, revoked (T14)
  org#<org>       consent#<c>  consent record (T3)
  client#<id>     -            pre-registered client (D3)
  cimd#<sha256>   -            cached client metadata document (T8, ≤ 24 h)

Two backends with one interface: ``DynamoStore`` in Lambda, ``MemoryStore`` in tests.
The two security-critical operations are atomic in both: ``take`` (a code can be
redeemed once) and ``mark_used`` (a refresh token can be rotated once).
"""
from __future__ import annotations

import time
from typing import Any

TTL_ATTR = "ttl"


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def _live(self, key):
        it = self.items.get(key)
        if it and it.get(TTL_ATTR) and it[TTL_ATTR] < time.time():
            self.items.pop(key, None)
            return None
        return it

    def put(self, pk: str, sk: str, item: dict[str, Any], ttl: int | None = None) -> None:
        rec = dict(item, pk=pk, sk=sk)
        if ttl:
            rec[TTL_ATTR] = int(time.time()) + ttl
        self.items[(pk, sk)] = rec

    def get(self, pk: str, sk: str = "-") -> dict[str, Any] | None:
        it = self._live((pk, sk))
        return dict(it) if it else None

    def delete(self, pk: str, sk: str = "-") -> None:
        self.items.pop((pk, sk), None)

    def take(self, pk: str, sk: str = "-") -> dict[str, Any] | None:
        it = self._live((pk, sk))
        self.items.pop((pk, sk), None)
        return dict(it) if it else None

    def mark_used(self, pk: str, sk: str = "-") -> bool:
        it = self._live((pk, sk))
        if not it or it.get("used"):
            return False
        it["used"] = True
        return True

    def update(self, pk: str, sk: str, fields: dict[str, Any]) -> None:
        it = self._live((pk, sk))
        if it:
            it.update(fields)

    def query(self, pk: str, sk_prefix: str) -> list[dict[str, Any]]:
        return [dict(v) for (p, s), v in list(self.items.items())
                if p == pk and s.startswith(sk_prefix) and self._live((p, s))]


class DynamoStore:
    def __init__(self, table_name: str, client: Any = None) -> None:
        if client is None:
            import boto3
            client = boto3.resource("dynamodb").Table(table_name)
        self.t = client

    def put(self, pk, sk, item, ttl=None):
        rec = dict(item, pk=pk, sk=sk)
        if ttl:
            rec[TTL_ATTR] = int(time.time()) + ttl
        self.t.put_item(Item=rec)

    def get(self, pk, sk="-"):
        it = self.t.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        # DynamoDB TTL deletion is lazy (up to days): expiry is enforced HERE, not trusted
        # to the sweeper.
        if it and it.get(TTL_ATTR) and int(it[TTL_ATTR]) < time.time():
            return None
        return it

    def delete(self, pk, sk="-"):
        self.t.delete_item(Key={"pk": pk, "sk": sk})

    def take(self, pk, sk="-"):
        it = self.t.delete_item(Key={"pk": pk, "sk": sk}, ReturnValues="ALL_OLD").get("Attributes")
        if it and it.get(TTL_ATTR) and int(it[TTL_ATTR]) < time.time():
            return None
        return it

    def mark_used(self, pk, sk="-"):
        from botocore.exceptions import ClientError
        try:
            self.t.update_item(
                Key={"pk": pk, "sk": sk}, UpdateExpression="SET used = :t",
                ConditionExpression="attribute_exists(pk) AND (attribute_not_exists(used) OR used = :f)",
                ExpressionAttributeValues={":t": True, ":f": False})
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def update(self, pk, sk, fields):
        if not fields:
            return
        names = {"#k%d" % i: k for i, k in enumerate(fields)}
        values = {":v%d" % i: v for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join("#k%d = :v%d" % (i, i) for i in range(len(fields)))
        self.t.update_item(Key={"pk": pk, "sk": sk}, UpdateExpression=expr,
                           ConditionExpression="attribute_exists(pk)",
                           ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def query(self, pk, sk_prefix):
        from boto3.dynamodb.conditions import Key
        out, kw = [], {"KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix)}
        while True:
            r = self.t.query(**kw)
            out += r.get("Items") or []
            if not r.get("LastEvaluatedKey"):
                return out
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
