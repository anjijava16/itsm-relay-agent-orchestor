"""OpenSearch is the primary retrieval engine: BM25 + kNN in one index.

Index design notes
------------------
* `content` uses an english analyzer with a custom ITSM synonym set so that
  "vpn"/"virtual private network" and "mfa"/"2fa" behave the same.
* `embedding` is a knn_vector using HNSW + cosine.
* Everything the agent filters on (tenant, acl, doc_class, ci_name, is_active)
  is a keyword field so filters stay in the filter context and get cached.
* Tenant isolation is enforced in a mandatory filter clause, never in a query
  string - a caller cannot drop it.
"""

from __future__ import annotations

import time
from typing import Any

from opensearchpy import AsyncOpenSearch, NotFoundError

from app.core.config import settings
from app.core.errors import UpstreamError
from app.core.logging import get_logger

log = get_logger(__name__)
_client: AsyncOpenSearch | None = None

ITSM_SYNONYMS = [
    "vpn, virtual private network",
    "mfa, 2fa, two factor, multifactor",
    "sso, single sign on",
    "pw, pwd, password, passcode",
    "laptop, notebook, workstation",
    "outage, downtime, unavailable",
    "ad, active directory",
    "vdi, virtual desktop",
]


def index_body() -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 128,
                "number_of_shards": 3,
                "number_of_replicas": 1,
                "refresh_interval": "5s",
            },
            "analysis": {
                "filter": {
                    "itsm_synonyms": {"type": "synonym_graph", "synonyms": ITSM_SYNONYMS},
                    "english_stemmer": {"type": "stemmer", "language": "english"},
                },
                "analyzer": {
                    "itsm_text": {
                        "tokenizer": "standard",
                        "filter": ["lowercase", "itsm_synonyms", "english_stemmer"],
                    }
                },
            },
        },
        "mappings": {
            "properties": {
                "tenant_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "ordinal": {"type": "integer"},
                "title": {"type": "text", "analyzer": "itsm_text", "fields": {"raw": {"type": "keyword"}}},
                "content": {"type": "text", "analyzer": "itsm_text"},
                "heading_path": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "page_no": {"type": "integer"},
                "doc_class": {"type": "keyword"},
                "category": {"type": "keyword"},
                "ci_name": {"type": "keyword"},
                "acl": {"type": "keyword"},
                "source_uri": {"type": "keyword"},
                "is_active": {"type": "boolean"},
                "version": {"type": "integer"},
                "created_at": {"type": "date"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embedding_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                        "parameters": {"ef_construction": 256, "m": 24},
                    },
                },
            }
        },
    }


def get_client() -> AsyncOpenSearch:
    global _client
    if _client is None:
        _client = AsyncOpenSearch(
            hosts=settings.opensearch_hosts,
            http_auth=(settings.opensearch_user, settings.opensearch_password),
            verify_certs=settings.opensearch_verify_certs,
            ssl_show_warn=False,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ensure_index() -> None:
    client = get_client()
    try:
        exists = await client.indices.exists(index=settings.opensearch_index)
        if not exists:
            await client.indices.create(index=settings.opensearch_index, body=index_body())
            log.info("opensearch_index_created", index=settings.opensearch_index)
    except Exception as exc:
        log.error("opensearch_ensure_index_failed", error=str(exc))
        raise UpstreamError("OpenSearch is not reachable") from exc


def _filters(tenant_id: str, filters: dict[str, Any] | None) -> list[dict]:
    clauses: list[dict] = [{"term": {"tenant_id": tenant_id}}]
    f = filters or {}
    if f.get("only_active", True):
        clauses.append({"term": {"is_active": True}})
    for field in ("doc_class", "ci_name", "category"):
        if f.get(field):
            clauses.append({"terms": {field: f[field]}})
    if f.get("acl_groups"):
        clauses.append(
            {
                "bool": {
                    "should": [
                        {"terms": {"acl": f["acl_groups"]}},
                        {"bool": {"must_not": {"exists": {"field": "acl"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if f.get("created_after"):
        clauses.append({"range": {"created_at": {"gte": f["created_after"]}}})
    return clauses


async def bm25_search(
    tenant_id: str, query: str, top_k: int, filters: dict | None = None
) -> list[dict[str, Any]]:
    body = {
        "size": top_k,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["title^3", "heading_path^2", "content"],
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                        }
                    }
                ],
                "filter": _filters(tenant_id, filters),
            }
        },
        "highlight": {"fields": {"content": {"fragment_size": 240, "number_of_fragments": 1}}},
        "_source": {"excludes": ["embedding"]},
    }
    return await _search(body, "bm25")


async def knn_search(
    tenant_id: str, vector: list[float], top_k: int, filters: dict | None = None
) -> list[dict[str, Any]]:
    body = {
        "size": top_k,
        "query": {
            "bool": {
                "must": [
                    {"knn": {"embedding": {"vector": vector, "k": top_k * 2}}}
                ],
                "filter": _filters(tenant_id, filters),
            }
        },
        "_source": {"excludes": ["embedding"]},
    }
    return await _search(body, "knn")


async def _search(body: dict, kind: str) -> list[dict[str, Any]]:
    started = time.perf_counter()
    try:
        response = await get_client().search(index=settings.opensearch_index, body=body)
    except NotFoundError:
        await ensure_index()
        return []
    except Exception as exc:
        log.error("opensearch_search_failed", kind=kind, error=str(exc))
        raise UpstreamError("Search backend failed") from exc

    hits = []
    for h in response["hits"]["hits"]:
        source = h["_source"]
        source["_score"] = h["_score"]
        source["_kind"] = kind
        if "highlight" in h:
            source["_highlight"] = " … ".join(h["highlight"].get("content", []))
        hits.append(source)
    log.debug("opensearch_search", kind=kind, hits=len(hits),
              ms=round((time.perf_counter() - started) * 1000, 1))
    return hits


async def bulk_index(docs: list[dict[str, Any]]) -> int:
    """Index chunk documents. `_id` is the chunk id so re-runs are idempotent."""
    if not docs:
        return 0
    payload: list[dict] = []
    for doc in docs:
        payload.append({"index": {"_index": settings.opensearch_index, "_id": doc["chunk_id"]}})
        payload.append(doc)
    response = await get_client().bulk(body=payload, refresh=False)
    if response.get("errors"):
        failed = [i for i in response["items"] if i.get("index", {}).get("error")]
        log.error("opensearch_bulk_partial_failure", failed=len(failed),
                  sample=failed[:2])
        raise UpstreamError(f"{len(failed)} chunks failed to index")
    return len(docs)


async def delete_by_document(tenant_id: str, document_id: str) -> int:
    response = await get_client().delete_by_query(
        index=settings.opensearch_index,
        body={"query": {"bool": {"filter": [
            {"term": {"tenant_id": tenant_id}}, {"term": {"document_id": document_id}}
        ]}}},
        refresh=True,
    )
    return int(response.get("deleted", 0))


async def health() -> dict[str, Any]:
    try:
        info = await get_client().cluster.health(index=settings.opensearch_index)
        return {"status": info.get("status", "unknown"), "reachable": True}
    except Exception as exc:
        return {"status": "unavailable", "reachable": False, "error": str(exc)}
