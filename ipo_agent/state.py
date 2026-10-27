"""Tiny JSON state file, committed back to the repo by the workflow."""
import json
import pathlib

PATH = pathlib.Path(__file__).resolve().parent.parent / "state.json"
DEFAULT = {"offset": 0, "last_morning": "", "interested": [], "last_report": []}


def load():
    try:
        data = json.loads(PATH.read_text())
    except Exception:
        data = {}
    return {**DEFAULT, **data}


def save(state):
    PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))
