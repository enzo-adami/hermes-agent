import pytest

from plugins.web.local import provider


@pytest.mark.parametrize("format", ["markdown", "html"])
def test_real_trafilatura_extracts_article_and_metadata(monkeypatch, format):
    pytest.importorskip("trafilatura")
    html = b"""<!doctype html><html><head><title>Local extraction contract</title>
    <meta name="author" content="Test Author"></head><body><article>
    <h1>Local extraction contract</h1>
    <p>This article verifies the actual readability library against a deterministic
    document. The transport is tested separately and no network access is required.</p>
    <p>Readable content and metadata must survive extraction in both supported output
    formats. This paragraph is intentionally ordinary prose rather than navigation.</p>
    </article></body></html>"""
    monkeypatch.setattr(provider, "_fetch_readable", lambda url: {
        "final_url": url, "content": html, "content_type": "text/html", "encoding": "utf-8",
    })
    assert provider.LocalWebSearchProvider().is_available()
    item = provider._extract_one("https://example.com/article", format)
    assert "error" not in item
    assert "actual readability library" in item["content"]
    assert item["content"] == item["raw_content"]
    assert item["metadata"]["sourceURL"] == "https://example.com/article"
    assert item["title"] == "Local extraction contract"
