"""Publishes live team status to Firebase Realtime Database so the web UI can show the office in real time.

Needs two env vars (GitHub secrets): FIREBASE_DB_URL and FIREBASE_SERVICE_ACCOUNT (the service-account JSON).
Without them every method is a silent no-op, so the agent still works exactly as before.
Publishing never raises: a Firebase problem must not stop the research.
"""
import datetime as dt
import json
import os
import threading
import zoneinfo

import requests

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
SPECIALIST_KEYS = ["fundamentals", "market_demand", "sector", "geopolitics_macro", "governance", "news_sentiment"]
AGENT_KEYS = ["manager"] + SPECIALIST_KEYS + ["skeptic"]
SCOPES = [
    "https://www.googleapis.com/auth/firebase.database",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _blank():
    return {
        "updated": "",
        "run": {"state": "idle", "message": "Waiting for the next run"},
        "ipo": {},
        "agents": {k: {"state": "idle", "task": ""} for k in AGENT_KEYS},
        "results": [],
        "watchlist": [],
        "log": [],
    }


class Live:
    def __init__(self):
        self.db = os.getenv("FIREBASE_DB_URL", "").strip().rstrip("/")
        self.sa = os.getenv("FIREBASE_SERVICE_ACCOUNT", "").strip()
        self.enabled = bool(self.db and self.sa)
        self.data = _blank()
        self._fresh = False
        self._creds = None
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self._auth_init()
                self._load()
            except Exception as e:  # noqa: BLE001 - never break the agent
                print(f"[live] disabled: {e}")
                self.enabled = False

    # ---- Firebase plumbing -------------------------------------------------
    def _auth_init(self):
        from google.oauth2 import service_account

        self._creds = service_account.Credentials.from_service_account_info(json.loads(self.sa), scopes=SCOPES)

    def _token(self):
        from google.auth.transport.requests import Request

        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    def _load(self):
        """Start from what is already published so results and watchlist survive between runs."""
        r = requests.get(f"{self.db}/status.json", params={"access_token": self._token()}, timeout=15)
        r.raise_for_status()
        old = r.json()
        if isinstance(old, dict):
            for key in ("results", "watchlist", "log"):
                if isinstance(old.get(key), list):
                    self.data[key] = old[key]
            if isinstance(old.get("ipo"), dict):
                self.data["ipo"] = old["ipo"]
            if isinstance(old.get("agents"), dict):
                for k, v in old["agents"].items():
                    if k in self.data["agents"] and isinstance(v, dict):
                        self.data["agents"][k] = v
            if isinstance(old.get("run"), dict):
                self.data["run"] = old["run"]

    def _now(self):
        return dt.datetime.now(IST)

    def _flush(self):
        if not self.enabled:
            return
        with self._lock:
            self.data["updated"] = self._now().isoformat(timespec="seconds")
            try:
                r = requests.put(
                    f"{self.db}/status.json",
                    params={"access_token": self._token()},
                    json=self.data,
                    timeout=15,
                )
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                print(f"[live] publish failed: {e}")

    def _log(self, agent, text):
        line = {"t": self._now().strftime("%H:%M:%S"), "agent": agent, "text": str(text)[:200]}
        self.data["log"] = ([line] + self.data["log"])[:60]

    # ---- events called by the agent code -----------------------------------
    def run_start(self):
        if not self.enabled:
            return
        self._fresh = True
        self.data["run"] = {
            "state": "running",
            "started": self._now().isoformat(timespec="seconds"),
            "message": "Scanning for open and upcoming IPOs",
        }
        self.data["ipo"] = {}
        self.data["agents"] = {k: {"state": "idle", "task": ""} for k in AGENT_KEYS}
        self.data["agents"]["manager"] = {"state": "working", "task": "Scanning for open and upcoming IPOs"}
        self._log("manager", "Research run started, scanning for open and upcoming IPOs")
        self._flush()

    def ipos_found(self, ipos):
        if not self.enabled:
            return
        names = ", ".join(str(i.get("name")) for i in ipos) or "none"
        self.data["run"]["total"] = len(ipos)
        self.data["run"]["message"] = f"Found {len(ipos)} IPO(s)"
        self._log("manager", f"Found {len(ipos)} IPO(s): {names}")
        self._flush()

    def ipo_start(self, ipo, idx, total):
        if not self.enabled:
            return
        self.data["ipo"] = {
            "name": ipo.get("name", ""),
            "open_date": ipo.get("open_date", ""),
            "close_date": ipo.get("close_date", ""),
            "price_band": ipo.get("price_band", ""),
            "exchange": ipo.get("exchange", ""),
            "index": idx + 1,
            "total": total,
        }
        self.data["run"]["message"] = f"Analysing {ipo.get('name')} ({idx + 1} of {total})"
        self.data["agents"] = {k: {"state": "idle", "task": ""} for k in AGENT_KEYS}
        self.data["agents"]["manager"] = {
            "state": "waiting",
            "task": f"Waiting for reports (0/{len(SPECIALIST_KEYS)})",
        }
        self._log("manager", f"Now analysing {ipo.get('name')} ({idx + 1} of {total})")
        self._flush()

    def agent(self, key, state, task="", **extra):
        if not self.enabled:
            return
        self.data["agents"][key] = {"state": state, "task": str(task)[:220], **extra}
        if state in ("working", "done", "error") and task:
            self._log(key, task)
        self._flush()

    def manager_progress(self, done, total):
        if not self.enabled:
            return
        self.data["agents"]["manager"] = {"state": "waiting", "task": f"Waiting for reports ({done}/{total})"}
        self._flush()

    def ipo_done(self, entry):
        if not self.enabled:
            return
        if self._fresh:
            self.data["results"] = []
            self._fresh = False
        entry = {**entry, "at": self._now().isoformat(timespec="seconds")}
        self.data["results"] = (self.data["results"] + [entry])[-8:]
        self._flush()

    def run_end(self, ok=True, message="Research complete"):
        if not self.enabled:
            return
        self.data["run"] = {
            **self.data["run"],
            "state": "done" if ok else "error",
            "ended": self._now().isoformat(timespec="seconds"),
            "message": message,
        }
        self._log("manager", message)
        self._flush()

    def set_watchlist(self, items):
        if not self.enabled:
            return
        clean = [{"name": i.get("name", ""), "close_date": i.get("close_date", "")} for i in items]
        if clean == self.data.get("watchlist"):
            return
        self.data["watchlist"] = clean
        self._flush()


live = Live()
