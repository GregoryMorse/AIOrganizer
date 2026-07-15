from __future__ import annotations

from ai_organizer.application.update_research import PublicWebResearchClient, _SearchParser


def test_search_parser_decodes_https_results_without_dns_lookup() -> None:
    parser = _SearchParser()
    parser.feed(
        '<a class="result__a" href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fvendor.example%2Freleases">Official releases</a>'
    )

    assert parser.results == [
        {"title": "Official releases", "url": "https://vendor.example/releases"}
    ]


def test_fetch_returns_only_bounded_visible_page_text() -> None:
    client = PublicWebResearchClient()
    client._read = lambda _url: (  # type: ignore[method-assign]
        b"<html><title>Releases</title><script>secret()</script>"
        b"<h1>Version 4.2.1</h1></html>",
        "https://vendor.example/releases",
        "text/html",
    )

    result = client.fetch("https://vendor.example/releases")

    assert result["title"] == "Releases"
    assert "Version 4.2.1" in result["text"]
    assert "secret" not in result["text"]
