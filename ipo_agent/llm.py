"""Free-LLM fallback chain: Gemini -> NVIDIA -> Groq -> OpenRouter.

- Every provider accepts several API keys (comma separated). A key that hits its limit is skipped for a while.
- Free models get retired often. Each provider has a list of models to try, and if all of them are gone the
  provider is asked for its live model list and the best chat models are tried instead.
- Error messages are kept short and name the quota that was hit, so a failure is easy to read in Telegram.
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

# Gemini free tier is rate limited per key, so space calls out per key.
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
    searched: bool  # True only if the answer used live search


# ---- key handling ----------------------------------------------------------
_cool = {}       # key -> (resume_time, is_daily)
_last_call = {}  # key -> time of the last call


def _keys(*env_names):
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


def _short(r):
    """One readable line from an error response, including which quota was hit."""
    msg, quota = "", ""
    try:
        j = r.json()
        err = j.get("error", j) if isinstance(j, dict) else {}
        msg = err.get("message") or err.get("detail") or err.get("title") or ""
    except ValueError:
        pass
    msg = (msg or r.text or "").replace("\n", " ").strip()
    m = re.search(r'"quotaId":\s*"([^"]+)"', r.text) or re.search(r'"quotaMetric":\s*"([^"/]*/?([^"]+))"', r.text)
    if m:
        quota = " [" + m.group(m.lastindex) + "]"
    return f"{r.status_code} {msg[:110]}{quota}"


def _check(r):
    if r.status_code in (429, 503):
        raise RateLimit(_short(r))
    if r.status_code >= 400:
        raise LLMError(_short(r))


# ---- Gemini ----------------------------------------------------------------
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
                    last = LLMError(f"{model}: {_short(r)}")
                    break
                if use_search and r.status_code in (400, 403, 429) and re.search(r"search|ground", r.text, re.I):
                    _search_ok[model] = False  # free keys cannot use live search: retry without it
                    continue
                if r.status_code == 429:  # free quotas are per model, so the next model may still have room
                    last = RateLimit(f"{model}: {_short(r)}")
                    break
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


# ---- OpenAI-compatible providers (NVIDIA, Groq, OpenRouter) -------------------
_NOT_CHAT = re.compile(r"embed|rerank|vision|-vl|guard|safety|reward|retriev|clip|parse|tts|asr|whisper|image|flux|"
                       r"diffusion|bge|ocr|speech|audio|coder|code|translate|moderation|orpheus|playai|omni", re.I)
_PREFER = ["gpt-oss", "nemotron", "llama-3.3", "llama-3.1-70", "llama", "mistral", "qwen", "deepseek", "gemma"]
_good = {}   # provider -> model that last worked
_gone = {}   # provider -> models that are retired or not allowed for this key


def _discover(base, key, free_only):
    """Ask the provider which models it serves now and return the best chat models first."""
    r = requests.get(base + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    ids = [m.get("id", "") for m in r.json().get("data", []) if isinstance(m, dict)]
    if free_only:
        ids = [i for i in ids if i.endswith(":free")]
    ids = [i for i in ids if i and not _NOT_CHAT.search(i)]
    ids.sort(key=lambda i: next((n for n, p in enumerate(_PREFER) if p in i.lower()), 99))
    return ids


def _openai_compat(name, base, env_names, models_env, defaults, free_only=False):
    configured = [m.strip() for m in os.getenv(models_env, defaults).split(",") if m.strip()]

    def call(prompt, system, search):
        def one(key):
            tried, last = set(), None
            order = ([_good[name]] if name in _good else []) + configured
            for phase in (0, 1):
                for model in order:
                    if model in tried or model in _gone.get(name, set()):
                        continue
                    tried.add(model)
                    r = requests.post(
                        base + "/chat/completions",
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
                    gone = r.status_code in (404, 410, 403) or (
                        r.status_code == 400 and re.search(r"model", r.text, re.I))
                    if gone:  # retired, or this key may not use it: remember and try another
                        _gone.setdefault(name, set()).add(model)
                        last = LLMError(f"{model}: {_short(r)}")
                        continue
                    _check(r)
                    try:
                        text = r.json()["choices"][0]["message"]["content"].strip()
                    except (KeyError, IndexError, TypeError, AttributeError):
                        raise LLMError(f"{model}: empty reply")
                    if not text:
                        raise LLMError(f"{model}: empty reply")
                    _good[name] = model
                    return text
                if phase == 0:  # every listed model is gone: use the provider's live list
                    try:
                        order = _discover(base, key, free_only)[:4]
                    except (requests.RequestException, ValueError) as e:
                        raise last or LLMError(f"model list failed: {e}")
            raise last or LLMError("no working model")

        return _rotate(_keys(*env_names), one)

    return call


PROVIDERS = {
    "gemini": _gemini,
    # NVIDIA's free hosted models (build.nvidia.com). OpenAI compatible, no live web search.
    "nvidia": _openai_compat(
        "nvidia", "https://integrate.api.nvidia.com/v1", ("NVIDIA_API_KEYS", "NVIDIA_API_KEY"), "NVIDIA_MODELS",
        "nvidia/nemotron-3-super-120b-a12b,openai/gpt-oss-20b,nvidia/llama-3.1-nemotron-70b-instruct"),
    "groq": _openai_compat(
        "groq", "https://api.groq.com/openai/v1", ("GROQ_API_KEYS", "GROQ_API_KEY"), "GROQ_MODELS",
        "llama-3.3-70b-versatile,openai/gpt-oss-120b,llama-3.1-8b-instant"),
    "openrouter": _openai_compat(
        "openrouter", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
        "OPENROUTER_MODELS", "meta-llama/llama-3.3-70b-instruct:free,openai/gpt-oss-20b:free", free_only=True),
}


def ask(prompt, system, search=False, order=("gemini", "nvidia", "groq", "openrouter")):
    """Try providers in order. Only Gemini can do live search; the others answer from training data."""
    errors = []
    for name in order:
        try:
            res = PROVIDERS[name](prompt, system, search)
        except (LLMError, requests.RequestException) as e:  # includes RateLimit: just move to the next provider
            errors.append(f"{name}: {e}"[:200])
            continue
        text, used_search = res if isinstance(res, tuple) else (res, False)
        return Reply(text=text, provider=name, searched=bool(used_search))
    raise LLMError("All providers failed: " + " | ".join(errors))
