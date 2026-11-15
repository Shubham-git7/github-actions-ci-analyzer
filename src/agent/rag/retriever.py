"""Runbook retrieval seam. Prod: Bedrock Knowledge Base. Dev/tests: in-memory keyword match."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunbookHit:
    section: str
    text: str
    score: float


class Retriever(Protocol):
    def retrieve(self, query: str, k: int = 3) -> list[RunbookHit]: ...


class BedrockKBRetriever:
    def __init__(self, kb_id: str, region: str, min_score: float = 0.4):
        import boto3

        self._c = boto3.client("bedrock-agent-runtime", region_name=region)
        self._kb, self._min = kb_id, min_score

    def retrieve(self, query: str, k: int = 3) -> list[RunbookHit]:
        r = self._c.retrieve(
            knowledgeBaseId=self._kb,
            retrievalQuery={"text": query[:1000]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}},
        )
        hits = [
            RunbookHit(
                x.get("location", {}).get("s3Location", {}).get("uri", "runbook"),
                x["content"]["text"],
                x.get("score", 0.0),
            )
            for x in r["retrievalResults"]
        ]
        return [h for h in hits if h.score >= self._min]


class KeywordRetriever:
    """Local runbook dir of .md files; sections split on '## '. For dev and tests only."""

    def __init__(self, sections: dict[str, str]):
        self._sections = sections

    @classmethod
    def from_dir(cls, path: str) -> "KeywordRetriever":
        import pathlib

        out = {}
        for f in pathlib.Path(path).glob("*.md"):
            for chunk in f.read_text().split("\n## ")[1:]:
                title, _, body = chunk.partition("\n")
                out[f"{f.name}#{title.strip()}"] = body
        return cls(out)

    def retrieve(self, query: str, k: int = 3) -> list[RunbookHit]:
        words = {w for w in query.lower().split() if len(w) > 3}
        scored = []
        for sec, text in self._sections.items():
            hay = (sec + " " + text).lower()
            score = sum(w in hay for w in words) / max(len(words), 1)
            if score > 0.15:
                scored.append(RunbookHit(sec, text, score))
        return sorted(scored, key=lambda h: -h.score)[:k]
