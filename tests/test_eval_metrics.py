from app.evals.metrics import (
    citation_coverage,
    citation_validity,
    context_precision,
    deflection_rate,
)


def test_citation_coverage():
    answer = "Restart the client to clear the session [1]. Then re-enrol the certificate [2]."
    assert citation_coverage(answer, 2) == 1.0


def test_citation_validity_catches_hallucinated_marker():
    assert citation_validity("Do the thing [7].", 3) == 0.0


def test_context_precision():
    hits = [{"_rerank_score": 8}, {"_rerank_score": 2}, {"_rerank_score": 6}]
    assert context_precision(hits) == 0.667


def test_deflection_rate_handles_zero():
    assert deflection_rate(0, 0) == 0.0
    assert deflection_rate(30, 120) == 0.25
