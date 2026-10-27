"""Web search through Tavily (free key from tavily.com). Used so the free LLMs get real, current facts.

Set TAVILY_API_KEY (or several, comma separated, in TAVILY_API_KEYS). Without a key enabled() is False.
Searches can be limited to trusted sites. If a limited search finds nothing, it is repeated on the open web once.
Results are cached for the run so the same query never costs a second credit.
"""
import os
from urllib.parse import urlparse

import requests

_cache = {}
_dead = set()  # keys that ran out of credits during this run


def _keys():
    out = []
    for name in ("TAVILY_API_KEYS", "TAVILY_API_KEY"):
        for k in os.getenv(name, "").split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def enabled():
    return bool(_keys())


def site(url):
    """Domain of a URL without www, for showing where information came from."""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def search(query, max_results=5, news=False, domains=None):
    """Return a list of {title, url, content}. Empty list if search is unavailable or finds nothing."""
    ck = (query, news, tuple(domains or ()))
    if ck in _cache:
        return _cache[ck]
    results = []
    for key in _keys():
        if key in _dead:
            continue
        body = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}
        if news:
            body["topic"] = "news"
        if domains:
            body["include_domains"] = list(domains)
        try:
            r = requests.post("https://api.tavily.com/search",
                              headers={"Authorization": f"Bearer {key}"}, json=body, timeout=40)
        except requests.RequestException as e:
            print(f"[search] request failed: {e}")
            continue
        if r.status_code in (429, 432, 433):  # out of credits or rate limited: try the next key
            _dead.add(key)
            continue
        if r.status_code >= 400:
            print(f"[search] tavily {r.status_code}: {r.text[:150]}")
            continue
        try:
            for x in r.json().get("results", []):
                results.append({"title": x.get("title", ""), "url": x.get("url", ""),
                                "content": (x.get("content") or "")[:600]})
        except ValueError:
            pass
        break
    if not results and domains:  # trusted sites had nothing: try the open web once
        results = search(query, max_results, news, None)
    _cache[ck] = results
    return results


def gather(queries, news=False, domains=None):
    """Run several queries. Returns (text for the LLM, number of results, [{title, url}] sources)."""
    seen, lines, sources = set(), [], []
    for q in queries:
        for x in search(q, news=news, domains=domains):
            if x["url"] in seen:
                continue
            seen.add(x["url"])
            lines.append(f"- {x['title']} ({x['url']})\n  {x['content']}")
            sources.append({"title": x["title"] or site(x["url"]), "url": x["url"]})
    return "\n".join(lines), len(lines), sources
