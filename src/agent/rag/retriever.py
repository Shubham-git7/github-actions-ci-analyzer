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
        """Distinct-word overlap. Tokenised on word characters (so `build` and "package.json," match), and normalised by at most
        10 query words so a long model-written analysis does not dilute a real match."""
        import re

        words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 3}
        scored = []
        for sec, text in self._sections.items():
            hay = set(re.findall(r"\w+", (sec + " " + text).lower()))
            hits = sum(w in hay for w in words)
            score = hits / max(min(len(words), 10), 1)
            if hits >= 2 and score > 0.3:
                scored.append(RunbookHit(sec, text, score))
        return sorted(scored, key=lambda h: -h.score)[:k]


class DirRetriever:
    """Re-reads the runbook directory on every call, so editing runbooks/*.md changes behaviour without a restart.
    (The runbook is the agent's only source of fix authority; this makes that visible when learning.)"""

    def __init__(self, path: str):
        self._path = path

    def retrieve(self, query: str, k: int = 3) -> list[RunbookHit]:
        return KeywordRetriever.from_dir(self._path).retrieve(query, k)
