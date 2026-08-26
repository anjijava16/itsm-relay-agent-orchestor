from app.retrieval.chunking import chunk_markdown, estimate_tokens

RUNBOOK = """# VPN Troubleshooting

Intro paragraph about the VPN service.

## Error 812

1. Check the certificate has not expired.
2. Re-enrol the device.
3. Restart the client.

## Error 691

Credentials are wrong or the account is locked.

### Locked accounts

Unlock through the self-service portal.
"""


def test_splits_on_headings():
    chunks = chunk_markdown(RUNBOOK, chunk_size_tokens=40, overlap_tokens=5)
    assert len(chunks) >= 3
    assert any("Error 812" in c.heading_path for c in chunks if c.heading_path)


def test_ordinals_are_contiguous():
    chunks = chunk_markdown(RUNBOOK, chunk_size_tokens=30, overlap_tokens=5)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_oversized_section_is_windowed():
    big = "# Huge\n\n" + ("sentence about outages. " * 2000)
    chunks = chunk_markdown(big, chunk_size_tokens=200, overlap_tokens=20)
    assert len(chunks) > 1
    assert all(c.token_count <= 400 for c in chunks)


def test_heading_path_nests():
    chunks = chunk_markdown(RUNBOOK, chunk_size_tokens=25, overlap_tokens=0)
    paths = [c.heading_path for c in chunks if c.heading_path]
    assert any("›" in p for p in paths)


def test_estimate_tokens_positive():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100
