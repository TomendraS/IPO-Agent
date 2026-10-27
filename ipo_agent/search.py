"""Web search through Tavily (free key from tavily.com). Used so the free LLMs get real, current facts.

Set TAVILY_API_KEY (or several, comma separated, in TAVILY_API_KEYS). Without a key enabled() is False.
Results are cached for the run so the same query never costs a second credit.
"""
import os

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


def search(query, max_results=5, news=False):
    """Return a list of {title, url, content}. Empty list if search is unavailable or finds nothing."""
    ck = (query, news)
    if ck in _cache:
        return _cache[ck]
    results = []
    for key in _keys():
        if key in _dead:
            continue
        body = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}
        if news:
            body["topic"] = "news"
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
    _cache[ck] = results
    return results


def gather(queries, news=False):
    """Run several queries and return one block of text for the LLM, plus how many results it holds."""
    seen, lines = set(), []
    for q in queries:
        for x in search(q, news=news):
            if x["url"] in seen:
                continue
            seen.add(x["url"])
            lines.append(f"- {x['title']} ({x['url']})\n  {x['content']}")
    return "\n".join(lines), len(lines)
