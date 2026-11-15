# -*- coding: utf-8 -*-
"""Распознавание еды по фото и описанию через Google Gemini (бесплатный тариф)."""
import base64
import json
import os
import re
import urllib.request
import urllib.error

from foods import FIELDS

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"), "gemini-2.5-flash-lite"] if m]

PROMPT = """Ты — опытный диетолог. Определи всю еду и напитки {where}.
Для каждого продукта оцени вес порции в граммах (для напитков — мл) по размеру тарелки, приборов, упаковки,
и посчитай пищевую ценность ИМЕННО ЭТОЙ ПОРЦИИ (не на 100 г).
{hint}
Верни ТОЛЬКО JSON такого вида:
{{"items": [{{"name": "название по-русски", "grams": 0, "kcal": 0, "p": 0, "f": 0, "c": 0, "fib": 0,
"A": 0, "C": 0, "D": 0, "B12": 0, "Ca": 0, "Fe": 0, "K": 0, "Mg": 0}}],
"comment": "короткий комментарий по-русски: насколько блюдо калорийное и полезное (1-2 предложения)"}}
Единицы: p,f,c,fib — граммы; A,D,B12 — мкг; C,Ca,Fe,K,Mg — мг.
Если еды нет — верни {{"items": [], "comment": "На фото не видно еды"}}.
Не пиши ничего кроме JSON."""


class AIError(Exception):
    pass


def enabled():
    return bool(GEMINI_KEY)


def _call(parts):
    last_err = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _parse(text)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:300]
            print("Gemini error", model, e.code, msg, flush=True)
            last_err = AIError("limit" if e.code == 429 else f"http {e.code}")
            continue  # пробуем запасную модель
        except Exception as e:
            print("Gemini error", model, repr(e), flush=True)
            last_err = AIError(str(e))
    raise last_err or AIError("unknown")


def _num(x):
    try:
        return max(0.0, float(str(x).replace(",", ".")))
    except (TypeError, ValueError):
        return 0.0


def _parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError("bad json")
    data = json.loads(m.group(0))
    items = []
    for it in data.get("items", [])[:12]:
        name = str(it.get("name", "")).strip()[:40]
        grams = _num(it.get("grams"))
        if not name or grams <= 0:
            continue
        item = {"name": name, "grams": grams}
        for f in FIELDS:
            item[f] = _num(it.get(f))
        items.append(item)
    return items, str(data.get("comment", ""))[:300]


def analyze_photo(image_bytes, caption=""):
    hint = f"Пользователь подписал фото: «{caption}». Учти это." if caption else ""
    parts = [{"text": PROMPT.format(where="на фото", hint=hint)},
             {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}}]
    return _call(parts)


def analyze_text(description):
    parts = [{"text": PROMPT.format(where=f"в описании: «{description}»",
                                    hint="Если вес не указан — возьми обычную порцию.")}]
    return _call(parts)
