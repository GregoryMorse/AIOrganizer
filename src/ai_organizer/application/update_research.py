from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

MAX_PAGE_BYTES = 1_500_000
MAX_PAGE_TEXT = 80_000


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._ignored = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title = f"{self.title} {value}".strip()
        self.parts.append(value)

    def text(self) -> str:
        return "\n".join(
            line.strip()
            for line in " ".join(self.parts).splitlines()
            if line.strip()
        )[:MAX_PAGE_TEXT]


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._capture = False
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = str(values.get("class", ""))
        if tag == "a" and "result__a" in classes:
            self._href = str(values.get("href", ""))
            self._capture = True
            self._title = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._capture:
            return
        self._capture = False
        url = _decode_search_url(self._href)
        title = " ".join("".join(self._title).split())
        if url and title:
            self.results.append({"title": title[:500], "url": url})


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicWebResearchClient:
    """Bounded public-web search/fetch tools; no scripts, downloads, or private-network access."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())

    def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        value = " ".join(query.split())[:500]
        if not value:
            raise ValueError("Search query is empty")
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": value})
        body, final_url, _content_type = self._read(url)
        parser = _SearchParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        bounded = max(1, min(10, limit))
        return {
            "query": value,
            "results": parser.results[:bounded],
            "search_page": final_url,
        }

    def fetch(self, url: str) -> dict[str, Any]:
        body, final_url, content_type = self._read(url)
        text = body.decode("utf-8", errors="replace")
        if "html" in content_type:
            parser = _PageParser()
            parser.feed(text)
            title, visible = parser.title[:500], parser.text()
        else:
            title, visible = "", text[:MAX_PAGE_TEXT]
        return {
            "url": final_url,
            "title": title,
            "content_type": content_type,
            "text": visible,
            "truncated": len(body) >= MAX_PAGE_BYTES or len(visible) >= MAX_PAGE_TEXT,
        }

    def _read(self, url: str) -> tuple[bytes, str, str]:
        _validate_public_https(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AIOrganizer/0.1 update-research (+local user initiated)",
                "Accept": "text/html,text/plain,application/json,application/xml;q=0.9",
            },
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            final_url = str(response.geturl())
            _validate_public_https(final_url)
            content_type = str(response.headers.get_content_type()).casefold()
            allowed = (
                content_type.startswith("text/")
                or content_type in {"application/json", "application/xml", "application/rss+xml"}
            )
            if not allowed:
                raise ValueError(f"Update research cannot parse content type {content_type}")
            return response.read(MAX_PAGE_BYTES), final_url, content_type


def _validate_public_https(url: str) -> None:
    parsed = _validate_https_shape(url)
    assert parsed.hostname is not None
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as error:
        raise ValueError("Update research hostname could not be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Update research blocks private or non-global network addresses")


def _validate_https_shape(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Update research accepts public HTTPS URLs only")
    return parsed


def _decode_search_url(value: str) -> str:
    url = urllib.parse.urljoin("https://html.duckduckgo.com", value)
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query:
        url = query["uddg"][0]
    try:
        # Search results are only fetched later, where DNS and public-IP checks run.
        # Avoid one DNS lookup per result while merely parsing the result page.
        _validate_https_shape(url)
    except ValueError:
        return ""
    return url


def compact_target_json(targets: list[dict[str, Any]]) -> str:
    return json.dumps(targets, ensure_ascii=False, separators=(",", ":"), default=str)
