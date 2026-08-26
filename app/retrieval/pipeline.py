"""The retrieval pipeline: rewrite → hybrid search → fuse → rerank → compress.

This is the piece that decides answer quality more than the model choice does,
so every stage is individually toggleable and individually measured.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import Cache
from app.core.config import settings
from app.core.logging import get_logger
from app.core.observability import RETRIEVAL_HITS, span
from app.llm import client as llm
from app.llm.prompts import COMPRESS, QUERY_REWRITE, RERANK
from app.retrieval import opensearch_store, pgvector_store

log = get_logger(__name__)
_query_cache = Cache("retrieval:query", ttl=600)

RRF_K = 60


@dataclass
class RetrievalResult:
    hits: list[dict[str, Any]]
    rewritten_queries: list[str] = field(default_factory=list)
    strategy: str = "hybrid"
    took_ms: int = 0
    total_candidates: int = 0


# ------------------------------------------------------------------ rewrite
async def rewrite_query(question: str, history: str = "", tenant_id: str = "default", n: int = 3) -> list[str]:
    cache_key = f"rw:{tenant_id}:{hash((question, history))}"
    cached = await _query_cache.get(cache_key)
    if cached:
        return cached
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": QUERY_REWRITE.format(n=n, history=history[-2000:] or "(none)", question=question)}],
            purpose="query_rewrite",
            tenant_id=tenant_id,
            temperature=0.0,
            max_tokens=300,
        )
        queries = [q for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
    except Exception as exc:
        log.warning("query_rewrite_failed", error=str(exc))
        queries = []
    result = list(dict.fromkeys([question, *queries]))[: n + 1]
    await _query_cache.set(cache_key, result)
    return result


# ------------------------------------------------------------------ fusion
def reciprocal_rank_fusion(rankings: list[list[dict]], k: int = RRF_K) -> list[dict]:
    """RRF: robust to score scales that BM25 and cosine will never share."""
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking):
            cid = doc.get("chunk_id") or doc.get("_id")
            if not cid:
                continue
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in docs:
                docs[cid] = doc
            else:
                # keep per-strategy scores for debugging / eval
                docs[cid].setdefault("_scores", {})[doc.get("_kind", "?")] = doc.get("_score")
    fused = []
    for cid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        doc = docs[cid]
        doc["_fused_score"] = score
        fused.append(doc)
    return fused


# ------------------------------------------------------------------ rerank
async def rerank(question: str, hits: list[dict], top_n: int, tenant_id: str) -> list[dict]:
    if not hits:
        return []
    window = hits[: min(len(hits), 20)]
    passages = "\n\n".join(
        f"[id={h.get('chunk_id')}] {h.get('title','')} :: {h.get('content','')[:700]}" for h in window
    )
    try:
        data = await llm.complete_json(
            [{"role": "user", "content": RERANK.format(question=question, passages=passages)}],
            purpose="rerank",
            tenant_id=tenant_id,
            model=settings.rerank_model,
            temperature=0.0,
            max_tokens=800,
        )
        scored = {s["id"]: float(s.get("score", 0)) for s in data.get("scores", []) if "id" in s}
    except Exception as exc:
        log.warning("rerank_failed_falling_back_to_fusion_order", error=str(exc))
        return hits[:top_n]

    for h in window:
        h["_rerank_score"] = scored.get(h.get("chunk_id"), 0.0)
    ordered = sorted(window, key=lambda h: h.get("_rerank_score", 0.0), reverse=True)
    kept = [h for h in ordered if h.get("_rerank_score", 0) >= 3][:top_n]
    return kept or ordered[:top_n]


# ------------------------------------------------------------------ compress
async def compress(question: str, hits: list[dict], tenant_id: str) -> list[dict]:
    """Contextual compression - drop the parts of each chunk that do not matter."""

    async def _one(hit: dict) -> dict:
        try:
            result = await llm.complete(
                [{"role": "user", "content": COMPRESS.format(question=question, passage=hit["content"][:4000])}],
                purpose="context_compression",
                tenant_id=tenant_id,
                temperature=0.0,
                max_tokens=500,
            )
            text = result.text.strip()
            if text:
                hit["content_full"] = hit["content"]
                hit["content"] = text
        except Exception as exc:
            log.warning("compression_failed", error=str(exc))
        return hit

    compressed = await asyncio.gather(*[_one(h) for h in hits])
    return [h for h in compressed if h.get("content", "").strip()]


# ------------------------------------------------------------------ main entry
async def retrieve(
    *,
    question: str,
    tenant_id: str,
    session: AsyncSession | None = None,
    top_k: int | None = None,
    top_n: int | None = None,
    strategy: str = "hybrid",
    filters: dict | None = None,
    do_rewrite: bool = True,
    do_rerank: bool = True,
    do_compress: bool = False,
    history: str = "",
) -> RetrievalResult:
    started = time.perf_counter()
    top_k = top_k or settings.retrieval_top_k
    top_n = top_n or settings.rerank_top_n

    with span("retrieval.pipeline", **{"retrieval.strategy": strategy, "retrieval.top_k": top_k}):
        queries = await rewrite_query(question, history, tenant_id) if do_rewrite else [question]

        rankings: list[list[dict]] = []

        if strategy in ("hybrid", "keyword"):
            bm25 = await asyncio.gather(
                *[opensearch_store.bm25_search(tenant_id, q, top_k, filters) for q in queries]
            )
            rankings.extend(bm25)

        if strategy in ("hybrid", "vector"):
            vectors = await llm.embed(queries, tenant_id=tenant_id)
            knn = await asyncio.gather(
                *[opensearch_store.knn_search(tenant_id, v, top_k, filters) for v in vectors]
            )
            rankings.extend(knn)

        if strategy == "pgvector":
            if session is None:
                raise ValueError("pgvector strategy needs a database session")
            vector = await llm.embed_one(question, tenant_id=tenant_id)
            rankings.append(
                await pgvector_store.similarity_search(
                    session, tenant_id, vector, top_k, (filters or {}).get("doc_class")
                )
            )

        total_candidates = sum(len(r) for r in rankings)
        fused = reciprocal_rank_fusion(rankings)
        RETRIEVAL_HITS.labels(strategy).observe(len(fused))

        hits = await rerank(question, fused, top_n, tenant_id) if do_rerank else fused[:top_n]
        if do_compress and hits:
            hits = await compress(question, hits, tenant_id)

    took_ms = int((time.perf_counter() - started) * 1000)
    log.info("retrieval_complete", strategy=strategy, candidates=total_candidates,
             returned=len(hits), took_ms=took_ms)
    return RetrievalResult(hits=hits, rewritten_queries=queries, strategy=strategy,
                           took_ms=took_ms, total_candidates=total_candidates)


def to_citations(hits: list[dict]) -> list[dict[str, Any]]:
    out = []
    for i, h in enumerate(hits, start=1):
        out.append(
            {
                "marker": f"[{i}]",
                "document_id": str(h.get("document_id", "")),
                "chunk_id": str(h.get("chunk_id", "")),
                "title": h.get("title", "Untitled"),
                "heading_path": h.get("heading_path"),
                "page_no": h.get("page_no"),
                "source_uri": h.get("source_uri"),
                "score": float(h.get("_rerank_score") or h.get("_fused_score") or h.get("_score") or 0.0),
                "snippet": (h.get("_highlight") or h.get("content", ""))[:300],
            }
        )
    return out


def format_passages(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        header = h.get("title", "Untitled")
        if h.get("heading_path"):
            header += f" › {h['heading_path']}"
        blocks.append(f"[{i}] {header}\n{h.get('content','')}")
    return "\n\n---\n\n".join(blocks) if blocks else "(no passages retrieved)"
