"""Free-LLM fallback chain: Gemini (live search, one or more keys) -> NVIDIA -> Groq -> OpenRouter.

Every provider accepts several API keys (comma separated). When one key hits its limit the next key is used
straight away, and a key that is out of quota is skipped until it recovers.
"""
import os
import re
import time
from dataclasses import dataclass

import requests

# Tried in order. A model Google has retired for new keys answers 404, so the next one is used.
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
if os.getenv("GEMINI_MODEL"):
    GEMINI_MODELS.insert(0, os.getenv("GEMINI_MODEL").strip())
_search_ok = {}  # model -> False once Google refuses live search on this key (free tier)
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# Gemini free tier is rate limited (~10 requests/min per key), so space calls out per key.
GEMINI_GAP_SECONDS = float(os.getenv("GEMINI_GAP_SECONDS", "7"))
MINUTE_COOLDOWN = 30        # a per-minute limit usually clears within a minute
DAILY_COOLDOWN = 6 * 3600   # a daily limit will not clear during this run


class LLMError(Exception):
    pass


class RateLimit(LLMError):
    pass


@dataclass
class Reply:
    text: str
    provider: str
    searched: bool  # True only if the answer used live Google Search


# ---- key handling ----------------------------------------------------------
_cool = {}  # key -> (resume_time, is_daily)
_last_call = {}  # key -> time of the last call


def _keys(*env_names):
    """Collect keys from comma separated env vars, in order, without duplicates."""
    out = []
    for name in env_names:
        for k in os.getenv(name, "").split(","):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def _bench(key, body):
    daily = bool(re.search(r"per ?day|daily", body, re.I))
    _cool[key] = (time.time() + (DAILY_COOLDOWN if daily else MINUTE_COOLDOWN), daily)


def _rotate(keys, call):
    """Run call(key) on the least recently used key that is not cooling down; move on if it is rate limited."""
    if not keys:
        raise LLMError("no API key set")
    now = time.time()
    usable = [k for k in keys if _cool.get(k, (0, False))[0] <= now]
    if not usable:
        if all(_cool[k][1] for k in keys):
            raise LLMError("daily quota used up on every key")
        raise RateLimit("every key is cooling down")
    usable.sort(key=lambda k: _last_call.get(k, 0))
    last = None
    for key in usable:
        try:
            return call(key)
        except RateLimit as e:
            _bench(key, str(e))
            last = e
    raise last


def _check(r):
    if r.status_code in (429, 503):
        raise RateLimit(f"{r.status_code} {r.text[:300]}")
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code} {r.text[:200]}")


# ---- providers -------------------------------------------------------------
def _gemini(prompt, system, search):
    def one(key):
        wait = GEMINI_GAP_SECONDS - (time.time() - _last_call.get(key, 0))
        if wait > 0:
            time.sleep(wait)
        last = None
        for model in GEMINI_MODELS:
            for use_search in ([True, False] if search else [False]):
                if use_search and _search_ok.get(model) is False:
                    continue
                body = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3},
                }
                if use_search:
                    body["tools"] = [{"google_search": {}}]
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=body,
                    timeout=180,
                )
                _last_call[key] = time.time()
                if r.status_code == 404:  # model retired or not offered to this key: try the next model
                    last = LLMError(f"model {model} unavailable: {r.text[:120]}")
                    break
                if use_search and r.status_code in (400, 403, 429) and re.search(r"search|ground", r.text, re.I):
                    _search_ok[model] = False  # free keys cannot use live search: retry without it
                    continue
                _check(r)
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                except (KeyError, IndexError):
                    raise LLMError("Gemini returned no candidates")
                text = "".join(p.get("text", "") for p in parts).strip()
                if not text:
                    raise LLMError("Gemini returned empty text")
                return text, use_search
        raise last or LLMError("no Gemini model worked")

    return _rotate(_keys("GEMINI_API_KEYS", "GEMINI_API_KEY"), one)


def _openai_compat(url, env_names, model):
    def call(prompt, system, search):
        def one(key):
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=180,
            )
            _last_call[key] = time.time()
            _check(r)
            try:
                text = r.json()["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError, AttributeError):
                raise LLMError("empty reply")
            if not text:
                raise LLMError("empty reply")
            return text

        return _rotate(_keys(*env_names), one)

    return call


PROVIDERS = {
    "gemini": _gemini,
    # NVIDIA's free hosted models (build.nvidia.com). OpenAI compatible, no live web search.
    "nvidia": _openai_compat("https://integrate.api.nvidia.com/v1/chat/completions",
                             ("NVIDIA_API_KEYS", "NVIDIA_API_KEY"), NVIDIA_MODEL),
    "groq": _openai_compat("https://api.groq.com/openai/v1/chat/completions",
                           ("GROQ_API_KEYS", "GROQ_API_KEY"), GROQ_MODEL),
    "openrouter": _openai_compat("https://openrouter.ai/api/v1/chat/completions",
                                 ("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"), OPENROUTER_MODEL),
}


def ask(prompt, system, search=False, order=("gemini", "nvidia", "groq", "openrouter")):
    """Try providers in order. Only Gemini can do live search; the others answer from training data."""
    errors = []
    for name in order:
        for attempt in range(2):
            try:
                res = PROVIDERS[name](prompt, system, search)
                text, used_search = res if isinstance(res, tuple) else (res, False)
                return Reply(text=text, provider=name, searched=bool(used_search))
            except RateLimit as e:
                errors.append(f"{name}: {e}"[:160])
                if attempt == 0:
                    time.sleep(20)
                    continue
                break
            except (LLMError, requests.RequestException) as e:
                errors.append(f"{name}: {e}"[:160])
                break
    raise LLMError("All providers failed: " + " | ".join(errors))
