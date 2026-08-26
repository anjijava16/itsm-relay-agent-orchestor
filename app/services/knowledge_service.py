"""Direct search / grounded answer surface, independent of the agent graph."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.llm import client as llm
from app.llm.prompts import ANSWER
from app.retrieval import pipeline
from app.schemas.knowledge import (
    AnswerRequest,
    AnswerResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

log = get_logger(__name__)


async def search(session: AsyncSession, tenant_id: str, req: SearchRequest) -> SearchResponse:
    result = await pipeline.retrieve(
        question=req.query,
        tenant_id=tenant_id,
        session=session,
        top_k=req.top_k * 3,
        top_n=req.top_k,
        strategy=req.strategy,
        filters=req.filters.model_dump(exclude_none=True),
        do_rewrite=req.rewrite_query,
        do_rerank=req.rerank,
        do_compress=req.compress,
    )
    hits = [
        SearchHit(
            chunk_id=str(h.get("chunk_id", "")),
            document_id=str(h.get("document_id", "")),
            title=h.get("title", "Untitled"),
            content=h.get("content", ""),
            heading_path=h.get("heading_path"),
            page_no=h.get("page_no"),
            score=float(h.get("_rerank_score") or h.get("_fused_score") or h.get("_score") or 0.0),
            bm25_score=(h.get("_scores") or {}).get("bm25"),
            vector_score=(h.get("_scores") or {}).get("knn"),
            rerank_score=h.get("_rerank_score"),
            metadata={k: v for k, v in h.items() if k in ("doc_class", "ci_name", "category", "source_uri")},
        )
        for h in result.hits
    ]
    return SearchResponse(
        query=req.query, rewritten_queries=result.rewritten_queries, strategy=result.strategy,
        hits=hits, took_ms=result.took_ms, total_candidates=result.total_candidates,
    )


async def answer(session: AsyncSession, tenant_id: str, req: AnswerRequest) -> AnswerResponse:
    result = await pipeline.retrieve(
        question=req.question, tenant_id=tenant_id, session=session,
        top_n=req.top_k, filters=req.filters.model_dump(exclude_none=True),
    )
    if not result.hits:
        return AnswerResponse(
            question=req.question,
            answer="I could not find this in our knowledge base.",
            citations=[], grounded=False, confidence=0.0,
        )

    completion = await llm.complete(
        [
            {"role": "system", "content": "Answer strictly from the passages. Cite as [n]."},
            {"role": "user", "content": ANSWER.format(
                passages=pipeline.format_passages(result.hits), question=req.question)},
        ],
        purpose="kb_answer", tenant_id=tenant_id, temperature=0.1, max_tokens=900,
    )
    top = max((h.get("_rerank_score", 0) for h in result.hits), default=0)
    return AnswerResponse(
        question=req.question,
        answer=completion.text,
        citations=pipeline.to_citations(result.hits),
        grounded="could not find" not in completion.text.lower(),
        confidence=round(min(top / 10.0, 1.0), 3),
    )
