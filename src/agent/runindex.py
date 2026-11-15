"""Index of known run ids, so any session (any approver) can list runs. Checkpoints hold the run state."""


class MemoryRunIndex:
    def __init__(self):
        self._ids: list[str] = []

    def add(self, run_id: str) -> None:
        if run_id not in self._ids:
            self._ids.append(run_id)

    def list(self) -> list[str]:
        return list(self._ids)


class DynamoRunIndex:
    """Same table as the checkpointer: PK='RUNS', SK=run id."""

    def __init__(self, table: str, region: str, client=None):
        import boto3

        self._c = client or boto3.client("dynamodb", region_name=region)
        self._t = table

    def add(self, run_id: str) -> None:
        self._c.put_item(TableName=self._t, Item={"PK": {"S": "RUNS"}, "SK": {"S": run_id}})

    def list(self) -> list[str]:
        out, kw = [], {"TableName": self._t, "KeyConditionExpression": "PK = :p",
                       "ExpressionAttributeValues": {":p": {"S": "RUNS"}}, "ConsistentRead": True}
        while True:
            r = self._c.query(**kw)
            out += [i["SK"]["S"] for i in r["Items"]]
            if "LastEvaluatedKey" not in r:
                return sorted(out)
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
