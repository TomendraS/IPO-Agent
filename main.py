"""Entry point.  python main.py morning | poll"""
import datetime as dt
import sys
import zoneinfo

from ipo_agent import state as st
from ipo_agent import team
from ipo_agent import telegram_api as tg
from ipo_agent import llm
from ipo_agent import search
from ipo_agent.live import live

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
CLOSE_HOUR = 17  # IPO bidding closes at 5 PM IST on the closing day
DISCLAIMER = "Research support only, not financial advice. GMP and listing gains can change quickly."


def _close_dt(item):
    try:
        d = dt.date.fromisoformat(item["close_date"])
    except (KeyError, ValueError, TypeError):
        return None
    return dt.datetime.combine(d, dt.time(CLOSE_HOUR, 0), IST)


def _interested_lines(state, now):
    lines = []
    for it in state["interested"]:
        close = _close_dt(it)
        if close and close > now:
            days = (close.date() - now.date()).days
            when = "closes TODAY at 5 PM" if days == 0 else f"closes in {days} day(s) ({it['close_date']})"
            lines.append(f"⭐ {it['name']} {when}")
    return lines


def morning():
    state = st.load()
    now = dt.datetime.now(IST)
    today = now.date().isoformat()
    if state["last_morning"] == today:
        return
    parts = ["Good morning! Search for IPOs today?"]
    watch = _interested_lines(state, now)
    if watch:
        parts.append("Your watchlist:\n" + "\n".join(watch))
    tg.send("\n\n".join(parts), [[
        {"text": "✅ Yes, research", "callback_data": "run_yes"},
        {"text": "❌ Not today", "callback_data": "run_no"},
    ]])
    state["last_morning"] = today
    st.save(state)


def run_report(state):
    tg.send("🔎 Research started. Specialist agents are working, this takes a few minutes.")
    live.run_start()
    try:
        _run_report(state)
    except Exception as e:  # noqa: BLE001 - show the failure in the office, then let the job fail
        live.run_end(False, f"The run stopped: {str(e)[:150]}")
        raise


def _run_report(state):
    try:
        ipos, searched = team.discover()
    except llm.LLMError as e:
        tg.send(f"Could not fetch the IPO list: {str(e)[:900]}")
        live.run_end(False, "Could not fetch the IPO list, all free models were busy")
        return
    if not searched:
        if search.enabled():
            tg.send("The web search returned nothing (Tavily may be out of credits or down), so I cannot list "
                    "real IPOs without guessing. I stopped instead of inventing any. Check your Tavily dashboard.")
        else:
            tg.send("No web search is set up, so I cannot list real IPOs without guessing. I stopped instead "
                    "of inventing any. Add a TAVILY_API_KEY secret in GitHub.")
        live.run_end(False, "Stopped: no live web search available, so no real IPO list")
        return
    if not ipos:
        tg.send("No open or upcoming IPOs found for the next 7 days.")
        live.run_end(True, "No open or upcoming IPOs in the next 7 days")
        return
    live.ipos_found(ipos)
    # Each report gets an id so a tap on an old message can never save the wrong IPO.
    rid = dt.datetime.now(IST).strftime("%d%H%M")
    state["report_id"] = rid
    state["last_report"] = []
    for idx, ipo in enumerate(ipos):
        live.ipo_start(ipo, idx, len(ipos))
        res = team.analyse(ipo)
        state["last_report"].append({
            "name": ipo.get("name"),
            "close_date": ipo.get("close_date", ""),
            "verdict": str(res["verdict"].get("verdict", "NEUTRAL")),
        })
        live.ipo_done({
            "name": ipo.get("name", ""),
            "open_date": ipo.get("open_date", ""),
            "close_date": ipo.get("close_date", ""),
            "price_band": ipo.get("price_band", ""),
            "exchange": ipo.get("exchange", ""),
            "verdict": res["verdict"],
            "signals": res["signals"],
            "all_searched": res["all_searched"],
        })
        tg.send(team.format_ipo(ipo, res),
                [[{"text": "⭐ I'm interested", "callback_data": f"int:{rid}:{idx}"}]])
        st.save(state)
    tg.send(DISCLAIMER)
    live.run_end(True, f"Research complete, {len(ipos)} IPO(s) analysed")


def add_interested(state, rid, idx):
    if rid != state.get("report_id"):
        return "That report is outdated. Send /ipo for a fresh one."
    try:
        item = state["last_report"][idx]
    except IndexError:
        return "That report is no longer available."
    if any(i["name"] == item["name"] for i in state["interested"]):
        return f"{item['name']} is already on your watchlist."
    if _close_dt(item) is None:
        tg.send(f"{item['name']} saved, but I could not find its closing date, so no reminder is set.")
    state["interested"].append({"name": item["name"], "close_date": item["close_date"], "reminded": False})
    return f"Saved {item['name']}. I'll remind you 1 hour before it closes."


def reminders(state):
    now = dt.datetime.now(IST)
    keep = []
    for it in state["interested"]:
        close = _close_dt(it)
        if close and not it.get("reminded") and close - dt.timedelta(hours=1) <= now < close:
            tg.send(f"⏰ {it['name']} closes at 5 PM today. Place your bid in the next hour if you still want it.")
            it["reminded"] = True
        if close is None or close + dt.timedelta(days=1) > now:
            keep.append(it)
    state["interested"] = keep


def poll():
    state = st.load()
    chat = tg.chat_id()
    run_requested = False

    for u in tg.get_updates(state["offset"]):
        state["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        msg = u.get("message")
        if cb:
            if str(cb["message"]["chat"]["id"]) != chat:
                continue
            data = cb.get("data", "")
            if data == "run_yes":
                tg.answer_callback(cb["id"], "Starting research")
                run_requested = True
            elif data == "run_no":
                tg.answer_callback(cb["id"], "Okay, skipping today")
                tg.send("Okay, skipping IPO research today.")
            elif data.startswith("int:"):
                tg.answer_callback(cb["id"], "Saved")
                try:
                    _, rid, idx = data.split(":")
                    tg.send(add_interested(state, rid, int(idx)))
                except ValueError:
                    tg.send("That button is outdated. Send /ipo for a fresh report.")
        elif msg and str(msg["chat"]["id"]) == chat:
            text = (msg.get("text") or "").strip().lower()
            if text in ("/ipo", "ipo", "ipo check"):
                run_requested = True
            elif text in ("/list", "list"):
                lines = _interested_lines(state, dt.datetime.now(IST))
                tg.send("\n".join(lines) or "Your watchlist is empty.")

    st.save(state)  # save the offset first so a long run is never double-triggered
    if run_requested:
        run_report(state)
    reminders(state)
    live.set_watchlist(state["interested"])
    st.save(state)


def research():
    """Started from the web UI (workflow_dispatch): run the full research right now."""
    state = st.load()
    run_report(state)
    reminders(state)
    live.set_watchlist(state["interested"])
    st.save(state)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    {"morning": morning, "poll": poll, "research": research}[mode]()
