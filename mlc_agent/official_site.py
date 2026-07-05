from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from mlc_agent.exceptions import DataSourceError


ABOUT_TERMS = (
    "关于我们",
    "公司简介",
    "企业概况",
    "公司介绍",
    "about us",
    "company profile",
    "corporate profile",
)


@dataclass(frozen=True)
class OfficialPage:
    url: str
    text: str


def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DataSourceError(f"官网 URL 无效: {url}")


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    blocks = [
        " ".join(node.get_text(" ", strip=True).split())
        for node in soup.select("main p, article p, .content p, .about p, p")
    ]
    blocks = [block for block in blocks if len(block) >= 20]
    return "\n".join(blocks)


def fetch_official_profile(client: httpx.Client, homepage_url: str) -> OfficialPage:
    _validate_public_http_url(homepage_url)
    response = client.get(homepage_url)
    response.raise_for_status()
    homepage_text = _extract_text(response.text)
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []
    for link in soup.find_all("a", href=True):
        label = " ".join(link.get_text(" ", strip=True).lower().split())
        if any(term in label for term in ABOUT_TERMS):
            candidate = urljoin(str(response.url), str(link["href"]))
            if urlparse(candidate).hostname == urlparse(str(response.url)).hostname:
                candidates.append(candidate)

    for candidate in dict.fromkeys(candidates):
        try:
            about_response = client.get(candidate)
            about_response.raise_for_status()
            text = _extract_text(about_response.text)
            if len(text) >= 120:
                return OfficialPage(url=str(about_response.url), text=text[:12_000])
        except httpx.HTTPError:
            continue

    if len(homepage_text) >= 120:
        return OfficialPage(url=str(response.url), text=homepage_text[:12_000])
    raise DataSourceError("官网未找到可用的公司介绍正文。")

