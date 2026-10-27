"""Minimal Telegram Bot API helpers (plain text, inline buttons)."""
import os

import requests


def _url(method):
    return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"


def chat_id():
    return str(os.environ["TELEGRAM_CHAT_ID"])


def send(text, buttons=None):
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or ["(empty)"]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id(), "text": chunk, "disable_web_page_preview": True}
        if buttons and i == len(chunks) - 1:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        requests.post(_url("sendMessage"), json=payload, timeout=30).raise_for_status()


def get_updates(offset):
    r = requests.get(
        _url("getUpdates"),
        params={"offset": offset, "timeout": 0, "allowed_updates": '["message","callback_query"]'},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def answer_callback(callback_id, text=""):
    try:
        requests.post(
            _url("answerCallbackQuery"),
            json={"callback_query_id": callback_id, "text": text},
            timeout=30,
        )
    except requests.RequestException:
        pass  # old callbacks expire; not important
