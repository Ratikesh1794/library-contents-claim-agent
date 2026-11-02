"""What the voice agent's tools do to the app, and its instructions. The voice engine itself is
backend/live/voice_pipecat.py.

The agent only talks and calls tools (design D2). It has no tool that writes a title, price or measurement.
"""
from __future__ import annotations

import asyncio
import re

from backend.core import locale
from backend.prompts import agent as prompts, fill


_ORDINALS = {"first": 1, "top": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}


def normalize_region(text: str) -> str:
    """Spoken shelf references -> the Coach's region ids: 'shelf2', 'second shelf', 'shelf row 2', 'top shelf' -> 'row2'.
    Anything else is kept as said."""
    t = text.strip().lower()
    num = re.search(r"\d+", t)
    if num:
        return f"row{int(num.group())}"
    word = next((n for w, n in _ORDINALS.items() if re.search(rf"\b{w}\b", t)), None)
    return f"row{word}" if word else text.strip()


def execute_tool(hooks, name: str, a: dict) -> dict:
    """What every voice tool does to the app (called by the Pipecat voice engine). Writes USER FACTS only.
    Half-filled calls (seen with background chatter) are rejected so the model asks instead of guessing."""
    missing = [k for k in {"record_user_fact": ("fact",), "mark_region": ("region", "action"),
                           "set_locale": ("country", "currency")}.get(name, ())
               if not str(a.get(k) or "").strip()]
    if missing:
        return {"ok": False, "error": f"missing {', '.join(missing)}; ask the user which one they mean"}
    if name == "record_user_fact":
        hint = a.get("target_hint")
        if hint and re.search(r"\b(shelf|row)\b", hint.lower()):   # "second shelf" -> "row2"; titles stay as said
            hint = normalize_region(hint)
        hooks.on_fact(hint, a["fact"], None)
    elif name == "mark_region":
        hooks.on_skip(normalize_region(a["region"]), a["action"].strip().lower())
    elif name == "set_locale":
        country, currency = locale.clean(a["country"], a["currency"])
        if not (country and currency):
            return {"ok": False, "error": "need a two-letter country code and a three-letter currency code; ask the user again"}
        hooks.on_locale(country, currency)
        return {"ok": True, "country": country, "currency": currency}
    elif name == "finish_sweep":
        asyncio.create_task(hooks.on_finish())
    else:
        return {"ok": False, "error": "unknown tool"}
    return {"ok": True}


def instructions(meta: dict) -> str:
    """The agent's rules (backend/prompts/agent.py: SYSTEM), filled with this sweep's country and currency."""
    return fill(prompts.SYSTEM, country=meta.get("country") or "unknown", currency=meta.get("currency") or "unknown")
