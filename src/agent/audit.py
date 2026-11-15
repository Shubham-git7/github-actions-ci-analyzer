"""Append-only audit trail: in memory, structured JSON on stdout (CloudWatch) and optionally DynamoDB.
Events never contain log bodies, diffs or secrets: only ids, actors, decisions and reasons."""
import json
import time
import uuid


class AuditLog:
    def __init__(self, path: str | None = None):
        self._path, self.events = path, []

    def record(self, **fields) -> dict:
        e = {"ts": round(time.time(), 3), **fields}
        self.events.append(e)
        if self._path:
            with open(self._path, "a") as f:
                f.write(json.dumps(e) + "\n")
        self._sink(e)
        return e

    def _sink(self, e: dict) -> None:  # overridden by persistent implementations
        pass

    def for_run(self, rid: str) -> list[dict]:
        return [e for e in self.events if e.get("rid") == rid]


class DynamoAuditLog(AuditLog):
    """Persistent sink in the shared state table: PK=AUDIT#<rid>, SK=<ts>#<nonce>."""

    def __init__(self, table: str, region: str, client=None, stream=print):
        super().__init__()
        import boto3

        self._c = client or boto3.client("dynamodb", region_name=region)
        self._t, self._stream = table, stream

    def _sink(self, e: dict) -> None:
        self._stream(json.dumps({"audit": e}))  # one structured line per event for CloudWatch
        self._c.put_item(TableName=self._t, Item={
            "PK": {"S": f"AUDIT#{e.get('rid') or 'none'}"},
            "SK": {"S": f"{e['ts']:016.3f}#{uuid.uuid4().hex[:6]}"},
            "data": {"S": json.dumps(e)},
        })

    def for_run(self, rid: str) -> list[dict]:
        r = self._c.query(TableName=self._t, KeyConditionExpression="PK = :p", ConsistentRead=True,
                          ExpressionAttributeValues={":p": {"S": f"AUDIT#{rid}"}})
        return [json.loads(i["data"]["S"]) for i in r["Items"]]
