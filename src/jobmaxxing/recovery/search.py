"""Free DuckDuckGo HTML search -> candidate result URLs (find-elsewhere recovery)."""

import re
import urllib.parse

_RESULT = re.compile(r'class="result__a"[^>]+href="([^"]+)"')


def build_query(company: str | None, title: str | None) -> str:
    return " ".join(p for p in (company, title) if p).strip()


def ddg_search(query: str, *, fetch_text, max_results: int = 6) -> list[str]:
    """Query DuckDuckGo's HTML endpoint and return candidate result URLs, unwrapping the
    `uddg=` redirector and excluding Workday hosts (the gated source we're routing around)."""
    body = fetch_text("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    urls: list[str] = []
    for href in _RESULT.findall(body):
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        if "myworkdayjobs.com" in url:
            continue
        urls.append(url)
        if len(urls) >= max_results:
            break
    return urls
