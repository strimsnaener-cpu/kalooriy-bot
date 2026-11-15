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


def _call(parts, parser=None):
    last_err = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"}}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return (parser or _parse)(text)
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


RECIPE_PROMPT = """Ты — повар и нутрициолог. На фото продукты (холодильник, стол, пакеты из магазина).
{hint}
1) Перечисли, какие продукты видишь.
2) Предложи 3 простых, сытных и полезных блюда из этих продуктов (можно добавить соль, специи, воду, немного масла).
   Блюда должны подходить подростку, который хочет питаться здоровее: побольше белка и овощей,
   поменьше сахара и жареного. Без экстремальных диет.
3) Для каждого блюда: ингредиенты с граммами на ОДНУ порцию, 3–6 коротких шагов приготовления,
   и пищевая ценность ОДНОЙ порции.
Верни ТОЛЬКО JSON:
{{"products": ["..."],
 "recipes": [{{"title": "название", "time_min": 0,
   "ingredients": [{{"name": "...", "grams": 0}}],
   "steps": ["..."],
   "grams": 0, "kcal": 0, "p": 0, "f": 0, "c": 0, "fib": 0,
   "A": 0, "C": 0, "D": 0, "B12": 0, "Ca": 0, "Fe": 0, "K": 0, "Mg": 0}}]}}
Единицы: p,f,c,fib — г; A,D,B12 — мкг; C,Ca,Fe,K,Mg — мг. Всё по-русски."""


def _parse_recipes(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError("bad json")
    data = json.loads(m.group(0))
    recipes = []
    for r in data.get("recipes", [])[:3]:
        title = str(r.get("title", "")).strip()[:60]
        if not title:
            continue
        rec = {"title": title, "time_min": int(_num(r.get("time_min"))),
               "ingredients": [(str(i.get("name", ""))[:40], _num(i.get("grams"))) for i in r.get("ingredients", [])[:12]],
               "steps": [str(s)[:200] for s in r.get("steps", [])[:8]],
               "grams": _num(r.get("grams"))}
        for f in FIELDS:
            rec[f] = _num(r.get(f))
        recipes.append(rec)
    products = [str(p)[:30] for p in data.get("products", [])[:20]]
    return products, recipes


def recipes_from_photo(image_bytes, caption=""):
    hint = f"Пользователь подписал: «{caption}»." if caption else ""
    parts = [{"text": RECIPE_PROMPT.format(hint=hint)},
             {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}}]
    return _call(parts, parser=_parse_recipes)
