from app.retrieval.pipeline import reciprocal_rank_fusion


def make(cid, score, kind):
    return {"chunk_id": cid, "_score": score, "_kind": kind}


def test_documents_in_both_rankings_win():
    bm25 = [make("a", 9.0, "bm25"), make("b", 4.0, "bm25")]
    knn = [make("b", 0.9, "knn"), make("c", 0.8, "knn")]
    fused = reciprocal_rank_fusion([bm25, knn])
    assert fused[0]["chunk_id"] == "b"


def test_all_documents_survive():
    fused = reciprocal_rank_fusion([[make("a", 1, "bm25")], [make("b", 1, "knn")]])
    assert {d["chunk_id"] for d in fused} == {"a", "b"}


def test_empty_rankings():
    assert reciprocal_rank_fusion([[], []]) == []


def test_scores_are_monotonic():
    ranking = [make(str(i), 1.0, "bm25") for i in range(5)]
    fused = reciprocal_rank_fusion([ranking])
    scores = [d["_fused_score"] for d in fused]
    assert scores == sorted(scores, reverse=True)
