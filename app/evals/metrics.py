"""RAG quality metrics computed without a judge model where possible."""

from __future__ import annotations

import re

CITATION_RE = re.compile(r"\[(\d+)\]")


def citation_coverage(answer: str, n_passages: int) -> float:
    """Share of the answer's factual sentences carrying a citation marker."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer) if len(s.split()) > 4]
    if not sentences:
        return 0.0
    cited = sum(1 for s in sentences if CITATION_RE.search(s))
    return round(cited / len(sentences), 3)


def citation_validity(answer: str, n_passages: int) -> float:
    """Do the markers point at passages that actually exist?"""
    markers = [int(m) for m in CITATION_RE.findall(answer)]
    if not markers:
        return 0.0
    valid = sum(1 for m in markers if 1 <= m <= n_passages)
    return round(valid / len(markers), 3)


def context_precision(hits: list[dict], threshold: float = 5.0) -> float:
    """Fraction of retrieved passages the reranker considered actually relevant."""
    if not hits:
        return 0.0
    relevant = sum(1 for h in hits if float(h.get("_rerank_score", 0)) >= threshold)
    return round(relevant / len(hits), 3)


def deflection_rate(auto_resolved: int, total: int) -> float:
    return round(auto_resolved / total, 3) if total else 0.0
