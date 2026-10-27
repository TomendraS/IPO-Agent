"""The IPO research team: specialist agents -> skeptic -> manager."""
import datetime as dt
import json
import os
import re
import zoneinfo

from . import llm
from . import search
from .live import live

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
MARKET = os.getenv("IPO_MARKET", "India (NSE/BSE)")
MAX_IPOS = int(os.getenv("MAX_IPOS", "4"))

COMMON = (
    "You are a specialist analyst on an IPO research team covering {market}. Today is {today}. "
    "Use live web search for current facts and numbers. If you cannot find a figure, write 'not found' - "
    "never invent or guess data. Name your sources briefly. Keep the report under 350 words in short bullets. "
    "Finish with exactly two lines:\nSIGNAL: POSITIVE or NEUTRAL or NEGATIVE (for listing-day price and for the first few months)\n"
    "CONFIDENCE: LOW or MEDIUM or HIGH"
)

SPECIALISTS = {
    "fundamentals": (
        "Fundamentals analyst",
        "Revenue, EBITDA and PAT for the last 3 years plus latest period, margins, debt, ROE/ROCE, cash flow quality, "
        "issue size, fresh issue vs offer for sale, use of proceeds, and valuation (P/E, P/B, EV/EBITDA) against listed peers.",
    ),
    "market_demand": (
        "Market demand and subscription analyst",
        "Grey market premium (GMP) and its trend, day-wise subscription (QIB, NII, retail), anchor investors, "
        "price band, lot size, allotment odds, and how IPOs listed in the last 30 days actually performed.",
    ),
    "sector": (
        "Sector and competition analyst",
        "Sector outlook and growth drivers, competitive position and market share, regulation risks, "
        "and how the listed peers' shares have behaved recently.",
    ),
    "geopolitics_macro": (
        "Geopolitics and macro analyst",
        "Geopolitical events, global market mood, crude oil, USD/INR, FII/DII flows, Nifty/Sensex trend, "
        "interest-rate news, and anything likely to move market sentiment around the listing date.",
    ),
    "governance": (
        "Promoter and governance analyst",
        "Promoter background and holding after IPO, pledges, litigation, SEBI/regulatory orders, auditor remarks, "
        "related-party transactions, key risk factors from the RHP/DRHP, and the lead managers' recent IPO track record.",
    ),
    "news_sentiment": (
        "News and sentiment analyst",
        "Recent news, brokerage and analyst views (subscribe / avoid), media and social sentiment, controversies.",
    ),
}

SKEPTIC_SYSTEM = (
    "You are the team skeptic. You get the specialists' reports on one IPO. Find contradictions, weak or missing data, "
    "over-optimism, hidden risks and anything the specialists skipped. Be specific and concise (under 250 words). "
    "Do not make the final call."
)

MANAGER_SYSTEM = (
    "You are the manager of an IPO research team ({market}). Today is {today}. You receive reports from specialist "
    "analysts and a skeptic. Weigh the evidence, say where analysts disagree, and treat missing data as lower confidence. "
    "Never claim certainty about listing gains. Reply with ONLY a JSON object, no other text:\n"
    '{{"verdict": "APPLY" or "AVOID" or "NEUTRAL", "confidence": "LOW" or "MEDIUM" or "HIGH", '
    '"expected_listing_gain": "short range like 5-15% or unknown", '
    '"best_for": "listing gain" or "long term" or "both" or "neither", '
    '"key_reasons": ["max 4 short strings"], "key_risks": ["max 4 short strings"], '
    '"data_gaps": ["max 3 short strings"], "summary": "2 sentences"}}'
)


ACTIVITY = {
    "fundamentals": "Reading financials and peer valuation",
    "market_demand": "Checking GMP and subscription",
    "sector": "Studying the sector and competitors",
    "geopolitics_macro": "Scanning global news and markets",
    "governance": "Checking promoters and red flags",
    "news_sentiment": "Reading news and analyst views",
}


def _first_point(text, limit=150):
    """First bullet of a report, short enough for a speech bubble."""
    for line in str(text).splitlines():
        line = line.strip().lstrip("-•*# ").strip()
        if line and not line.upper().startswith(("SIGNAL", "CONFIDENCE")):
            return line[:limit]
    return str(text)[:limit]


def _today():
    return dt.datetime.now(IST).strftime("%A, %d %B %Y")


def _json_array(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _json_obj(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


QUERIES = {
    "fundamentals": ["{n} IPO financials revenue profit debt RHP", "{n} IPO valuation P/E compared to peers"],
    "market_demand": ["{n} IPO GMP today", "{n} IPO subscription status QIB NII retail"],
    "sector": ["{n} IPO sector outlook competitors market share"],
    "geopolitics_macro": ["Sensex Nifty today FII DII flows crude oil rupee", "global markets geopolitical risk India stocks this week"],
    "governance": ["{n} IPO promoters holding litigation risks RHP"],
    "news_sentiment": ["{n} IPO review subscribe or avoid brokerage"],
}
NEWS_KEYS = {"market_demand", "geopolitics_macro", "news_sentiment"}


def _sites(env, default):
    return [s.strip() for s in os.getenv(env, default).split(",") if s.strip()]


# Searches look here first. If these sites have nothing, the open web is used once. Change with IPO_SITES / NEWS_SITES.
IPO_SITES = _sites("IPO_SITES", "chittorgarh.com,ipowatch.in,ipocentral.in,investorgain.com,nseindia.com,bseindia.com,"
                   "sebi.gov.in,moneycontrol.com,economictimes.indiatimes.com,business-standard.com,livemint.com,"
                   "screener.in,trendlyne.com,groww.in")
NEWS_SITES = _sites("NEWS_SITES", "reuters.com,economictimes.indiatimes.com,moneycontrol.com,livemint.com,"
                    "business-standard.com,thehindubusinessline.com,ndtvprofit.com")
discovery_sites = []  # sites the IPO list was taken from, shown to the user after discovery


def discover():
    """Find IPOs that are open now or open within 7 days. Returns (ipos, searched)."""
    if search.enabled():
        ctx, n, srcs = search.gather([
            f"IPO open this week India mainboard subscription dates price band {_today()}",
            "upcoming IPO calendar India next week opening date closing date price band",
        ], domains=IPO_SITES)
        discovery_sites[:] = list(dict.fromkeys(search.site(s["url"]) for s in srcs if s["url"]))
        if n == 0:
            return [], False
        prompt = (
            f"Today is {_today()}. Below are web search results. Using ONLY these results, list IPOs in {MARKET} "
            "that are open for subscription now or open within the next 7 days, with a closing date of today or later. "
            "Mainboard IPOs only, unless there are fewer than 3, then add SME IPOs. Do NOT add any IPO that is not "
            "named in the results, and leave a field empty if the results do not state it. Reply with ONLY a JSON "
            'array, no other text: [{"name": "", "open_date": "YYYY-MM-DD", "close_date": "YYYY-MM-DD", '
            '"price_band": "", "exchange": ""}]. Use [] if there are none.\n\nSEARCH RESULTS:\n' + ctx
        )
        reply = llm.ask(prompt, "You are a precise market data assistant. Never invent data.")
        ipos = [i for i in _json_array(reply.text) if isinstance(i, dict) and i.get("name")]
        return ipos[:MAX_IPOS], True
    prompt = (
        f"Today is {_today()}. List IPOs in {MARKET} that are open for subscription right now or open within the next "
        "7 days. Mainboard IPOs only, unless there are fewer than 3, then add SME IPOs. Only IPOs whose closing date "
        "is today or later. Reply with ONLY a JSON array, no other text: "
        '[{"name": "", "open_date": "YYYY-MM-DD", "close_date": "YYYY-MM-DD", "price_band": "", "exchange": ""}]. '
        "Use [] if there are none."
    )
    reply = llm.ask(prompt, "You are a precise market data assistant. Use live web search.", search=True)
    ipos = [i for i in _json_array(reply.text) if isinstance(i, dict) and i.get("name")]
    return ipos[:MAX_IPOS], reply.searched


def _ipo_line(ipo):
    return (
        f"IPO: {ipo.get('name')} | exchange: {ipo.get('exchange', '?')} | price band: {ipo.get('price_band', '?')} | "
        f"opens: {ipo.get('open_date', '?')} | closes: {ipo.get('close_date', '?')}"
    )


def _specialist(key, ipo):
    title, focus = SPECIALISTS[key]
    system = f"{COMMON.format(market=MARKET, today=_today())}\nYour role: {title}."
    prompt = f"{_ipo_line(ipo)}\n\nYour focus: {focus}"
    srcs = []
    if search.enabled():
        ctx, n, srcs = search.gather([q.format(n=ipo.get("name", "")) for q in QUERIES[key]], news=key in NEWS_KEYS,
                                     domains=NEWS_SITES if key == "geopolitics_macro" else IPO_SITES)
        if n:
            prompt += ("\n\nSEARCH RESULTS (current web data). Base your numbers on these, name the source, "
                       "and write 'not found' for anything they do not show:\n" + ctx)
            reply = llm.ask(prompt, system)
            reply.searched = True
        else:
            reply = llm.ask(prompt + "\n\nNo web results were found. Say 'not found' rather than guessing.", system)
    else:
        reply = llm.ask(prompt, system, search=True)
    sig = re.search(r"SIGNAL:\s*(POSITIVE|NEUTRAL|NEGATIVE)", reply.text, re.I)
    return {
        "key": key,
        "title": title,
        "report": reply.text,
        "signal": sig.group(1).upper() if sig else "?",
        "searched": reply.searched,
        "provider": reply.provider,
        "sources": srcs[:4],
    }


def analyse(ipo):
    """Run the full team on one IPO and return the manager's verdict plus the specialist signals."""
    results = []
    for n, key in enumerate(SPECIALISTS, 1):
        live.agent(key, "working", ACTIVITY[key])
        try:
            r = _specialist(key, ipo)
            results.append(r)
            live.agent(key, "done", _first_point(r["report"]), signal=r["signal"],
                       provider=r["provider"], searched=r["searched"])
        except llm.LLMError as e:
            results.append({"key": key, "title": SPECIALISTS[key][0], "report": f"(failed: {e})",
                            "signal": "?", "searched": False, "provider": "none"})
            live.agent(key, "error", "Could not finish, all free models were busy",
                       signal="?", provider="none", searched=False)
        live.manager_progress(n, len(SPECIALISTS))

    bundle = "\n\n".join(f"### {r['title']}\n{r['report'][:2500]}" for r in results)

    live.agent("skeptic", "working", "Looking for holes in the reports")
    try:
        sk = llm.ask(f"{_ipo_line(ipo)}\n\n{bundle}", SKEPTIC_SYSTEM, order=("nvidia", "groq", "openrouter", "gemini"))
        skeptic = sk.text
        live.agent("skeptic", "done", _first_point(skeptic), provider=sk.provider, searched=sk.searched)
    except llm.LLMError as e:
        skeptic = f"(skeptic unavailable: {e})"
        live.agent("skeptic", "error", "Could not review, all free models were busy", provider="none")

    prompt = f"{_ipo_line(ipo)}\n\n{bundle}\n\n### Skeptic\n{skeptic[:2000]}"
    live.agent("manager", "working", "Weighing every report and writing the verdict")
    try:
        mgr = llm.ask(prompt, MANAGER_SYSTEM.format(market=MARKET, today=_today()))
        verdict = _json_obj(mgr.text) or {"verdict": "NEUTRAL", "confidence": "LOW", "summary": mgr.text[:500]}
        live.agent("manager", "done", str(verdict.get("summary", ""))[:200],
                   verdict=str(verdict.get("verdict", "NEUTRAL")).upper(),
                   confidence=str(verdict.get("confidence", "?")).upper(), provider=mgr.provider)
    except llm.LLMError as e:
        verdict = {"verdict": "NEUTRAL", "confidence": "LOW", "summary": f"Manager failed: {e}"}
        live.agent("manager", "error", "Could not write the verdict, all free models were busy",
                   verdict="NEUTRAL", confidence="LOW", provider="none")

    seen, sources = set(), []
    for r in results:
        for s in r.get("sources", []):
            if s["url"] not in seen:
                seen.add(s["url"])
                sources.append(s)
    return {
        "verdict": verdict,
        "signals": {r["key"]: r["signal"] for r in results},
        "all_searched": all(r["searched"] for r in results),
        "sources": sources[:8],
    }


EMOJI = {"APPLY": "✅", "AVOID": "❌", "NEUTRAL": "⚖️"}
SIGN = {"POSITIVE": "+", "NEUTRAL": "=", "NEGATIVE": "-", "?": "?"}


def _bullets(items):
    return "\n".join(f"  • {x}" for x in (items or [])) or "  • none"


def format_ipo(ipo, res):
    v = res["verdict"]
    verdict = str(v.get("verdict", "NEUTRAL")).upper()
    signals = " ".join(f"{k.split('_')[0]}{SIGN.get(s, '?')}" for k, s in res["signals"].items())
    lines = [
        f"📊 {ipo.get('name')}  ({ipo.get('open_date', '?')} → {ipo.get('close_date', '?')})",
        f"Price band: {ipo.get('price_band', '?')}",
        f"Verdict: {verdict} {EMOJI.get(verdict, '')}  | confidence: {v.get('confidence', '?')}",
        f"Expected listing gain: {v.get('expected_listing_gain', 'unknown')}  | best for: {v.get('best_for', '?')}",
        f"Summary: {v.get('summary', '')}",
        "Why:\n" + _bullets(v.get("key_reasons")),
        "Risks:\n" + _bullets(v.get("key_risks")),
        "Data gaps:\n" + _bullets(v.get("data_gaps")),
        f"Team signals: {signals}",
    ]
    if res.get("sources"):
        lines.append("Sources:\n" + "\n".join(
            f"  • {search.site(s['url'])}: {s['url']}" for s in res["sources"][:6]))
    if not res["all_searched"]:
        lines.append("⚠️ Some agents had no web results, so their data may be missing or stale.")
    return "\n".join(lines)
