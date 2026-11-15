# -*- coding: utf-8 -*-
"""Телеграм-бот «Дневник питания»: считает калории, БЖУ, витамины и минералы.
Без сторонних библиотек для Telegram — только стандартный Python (+psycopg2 для Postgres).
"""
import difflib
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from foods import FOODS, FIELDS, NORMS, MICRO_NAMES, MICRO_TIPS, CATEGORIES, by_category
from db import DB
import ai
import workouts as W

TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Kyiv"))
API = f"https://api.telegram.org/bot{TOKEN}/"

db = None
STATE = {}  # user_id -> {"await": ..., ...}  ожидание ввода текста
LAST_PHOTO = {}  # user_id -> (photos, caption)
RECIPES = {}  # user_id -> [recipe, ...]
AI_PENDING = {}  # user_id -> {"items": [...], "comment": str}  результат ИИ до подтверждения

BTN_ADD = "➕ Добавить еду"
BTN_TODAY = "📊 Сегодня"
BTN_REPORT = "📋 Итог дня"
BTN_UNDO = "↩️ Отменить последнее"
BTN_WEEK = "📈 Неделя"
BTN_GOALS = "🎯 Цели"
BTN_MY = "⭐ Мои продукты"
BTN_HELP = "❓ Помощь"
BTN_WORKOUT = "🏋️ Тренировка"
BTN_COOK = "🧑‍🍳 Что приготовить"

MAIN_KB = {
    "keyboard": [[BTN_ADD, BTN_TODAY], [BTN_REPORT, BTN_UNDO], [BTN_WORKOUT, BTN_COOK], [BTN_WEEK, BTN_GOALS], [BTN_MY, BTN_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
}


# ======================= Telegram API =======================
def api(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if "message is not modified" not in body:
            print("API error", method, e.code, body[:300], flush=True)
        return {"ok": False, "error_code": e.code, "description": body}


def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb is not None:
        p["reply_markup"] = kb
    return api("sendMessage", **p)


def edit(chat_id, msg_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if kb is not None:
        p["reply_markup"] = kb
    r = api("editMessageText", **p)
    if not r.get("ok") and "not modified" not in r.get("description", ""):
        send(chat_id, text, kb)


def ikb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


# ======================= Утилиты =======================
def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def fmt(x, nd=0):
    if nd == 0:
        return f"{round(x):,}".replace(",", " ")
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
    return s


def bar(val, goal, width=10):
    if goal <= 0:
        return ""
    ratio = val / goal
    filled = min(width, int(round(ratio * width)))
    return "▓" * filled + "░" * (width - filled) + f" {round(ratio * 100)}%"


def goals_for(kcal):
    # Белки 25%, жиры 30%, углеводы 45% от калорий
    return {"kcal": kcal, "p": kcal * 0.25 / 4, "f": kcal * 0.30 / 9, "c": kcal * 0.45 / 4}


def calc(per100, grams):
    k = grams / 100.0
    return {f: per100.get(f, 0) * k for f in FIELDS}


def sum_entries(entries):
    tot = {f: 0.0 for f in FIELDS}
    for e in entries:
        for f in FIELDS:
            tot[f] += e[f] or 0
    return tot


def all_foods(user_id):
    d = dict(FOODS)
    for cf in db.customs(user_id):
        d[cf["id"]] = cf
    return d


def get_food(user_id, fid):
    if fid.startswith("u"):
        for cf in db.customs(user_id):
            if cf["id"] == fid:
                return cf
        return None
    return FOODS.get(fid)


def _norm(t):
    return re.sub(r"[^a-zа-я0-9 ]", " ", t.lower().replace("ё", "е")).split()


def _stem(w):
    if len(w) > 6:
        return w[:-3]
    if len(w) > 4:
        return w[:-2]
    if len(w) > 3:
        return w[:-1]
    return w


def _wmatch(a, b):
    sa, sb = _stem(a), _stem(b)
    return a == b or sa == sb or (len(sa) >= 4 and b.startswith(sa)) or (len(sb) >= 4 and a.startswith(sb))


def _score(qw, alias):
    aw = _norm(alias)
    if not aw:
        return 0
    if qw == aw:
        return 100
    q_in_a = all(any(_wmatch(q, a) for a in aw) for q in qw)
    a_in_q = all(any(_wmatch(a, q) for q in qw) for a in aw)
    if q_in_a and a_in_q:
        return 90
    if q_in_a:
        return 70 - 3 * (len(aw) - len(qw))
    if a_in_q:
        return 60
    r = difflib.SequenceMatcher(None, " ".join(qw), " ".join(aw)).ratio()
    return r * 50 if r > 0.72 else 0


def search_foods(user_id, query, limit=8, with_scores=False):
    qw = _norm(query)
    if not qw:
        return []
    scored = []
    for f in all_foods(user_id).values():
        sc = max(_score(qw, a) for a in f["aliases"])
        if f["cat"] == "custom" and sc:
            sc += 5  # свои продукты в приоритете
        if sc > 0:
            scored.append((sc, f))
    scored.sort(key=lambda x: -x[0])
    scored = scored[:limit]
    return scored if with_scores else [f for _, f in scored]


UNIT_RE = r"(кг|г|гр|грамм|граммов|грамма|мл|л|шт|штук|штуки|штука|ломтик|ломтика|ломтиков|ст\.?\s?л\.?|ч\.?\s?л\.?|стакан|стакана|чашка|чашки|тарелка|тарелки|кусок|куска|кусков)?"


def parse_item(text):
    """'гречка 200', '200 г гречки', '2 яйца', 'банан' -> (name, amount, unit)"""
    t = text.strip().lower().replace(",", ".")
    m = re.match(rf"^(\d+(?:\.\d+)?)\s*{UNIT_RE}\s+(.+)$", t)
    if m:
        return m.group(3).strip(), float(m.group(1)), (m.group(2) or "")
    m = re.match(rf"^(.+?)\s+(\d+(?:\.\d+)?)\s*{UNIT_RE}$", t)
    if m:
        return m.group(1).strip(), float(m.group(2)), (m.group(3) or "")
    return t, None, ""


def to_grams(food, amount, unit):
    unit = (unit or "").replace(" ", "")
    portion = food.get("portion")
    if amount is None:
        return portion[1] if portion else 100
    if unit == "кг" or unit == "л":
        return amount * 1000
    if unit in ("г", "гр", "грамм", "граммов", "грамма", "мл"):
        return amount
    if unit.startswith("ст") and "л" in unit and unit not in ("стакан", "стакана"):
        return amount * 15 if not portion or portion[0] != "ст.л." else amount * portion[1]
    if unit.startswith("ч") and "л" in unit:
        return amount * 5 if not portion or portion[0] != "ч.л." else amount * portion[1]
    if unit in ("стакан", "стакана"):
        return amount * 250
    if unit:  # шт, ломтик, чашка, тарелка, кусок
        return amount * (portion[1] if portion else 100)
    # без единицы: маленькое число + есть порция -> штуки, иначе граммы
    if portion and amount <= 20:
        return amount * portion[1]
    return amount


# ======================= Тексты =======================
def entry_line(e):
    return f"• {e['name']} — {fmt(e['grams'])} г · <b>{fmt(e['kcal'])}</b> ккал"


def today_text(user_id):
    u = db.get_user(user_id)
    g = goals_for(u["kcal_goal"])
    entries = db.day_entries(user_id, today())
    if not entries:
        return "📊 <b>Сегодня</b>\n\nПока ничего не записано. Жми «➕ Добавить еду» или просто напиши, например: <i>гречка 200</i>"
    t = sum_entries(entries)
    left = g["kcal"] - t["kcal"]
    lines = ["📊 <b>Сегодня</b>\n"]
    lines += [entry_line(e) for e in entries]
    lines.append("")
    lines.append(f"🔥 <b>{fmt(t['kcal'])}</b> / {fmt(g['kcal'])} ккал\n{bar(t['kcal'], g['kcal'])}")
    lines.append(f"🥩 Белки: <b>{fmt(t['p'])}</b> / {fmt(g['p'])} г")
    lines.append(f"🧈 Жиры: <b>{fmt(t['f'])}</b> / {fmt(g['f'])} г")
    lines.append(f"🍞 Углеводы: <b>{fmt(t['c'])}</b> / {fmt(g['c'])} г")
    lines.append("")
    lines.append(f"Осталось: <b>{fmt(left)}</b> ккал" if left >= 0 else f"Перебор: <b>{fmt(-left)}</b> ккал")
    return "\n".join(lines)


def report_text(user_id, day=None):
    day = day or today()
    u = db.get_user(user_id)
    g = goals_for(u["kcal_goal"])
    entries = db.day_entries(user_id, day)
    nice_day = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    if not entries:
        return f"📋 <b>Итог дня {nice_day}</b>\n\nЗа день ничего не записано."
    t = sum_entries(entries)
    L = [f"📋 <b>Итог дня {nice_day}</b>\n", "<b>Что ты съел:</b>"]
    L += [entry_line(e) for e in entries]
    L.append("")
    L.append(f"🔥 <b>Калории: {fmt(t['kcal'])}</b> из {fmt(g['kcal'])}\n{bar(t['kcal'], g['kcal'])}")
    L.append("")
    L.append("<b>БЖУ:</b>")
    L.append(f"🥩 Белки — {fmt(t['p'])} г из {fmt(g['p'])} ({round(t['p'] / g['p'] * 100)}%)")
    L.append(f"🧈 Жиры — {fmt(t['f'])} г из {fmt(g['f'])} ({round(t['f'] / g['f'] * 100)}%)")
    L.append(f"🍞 Углеводы — {fmt(t['c'])} г из {fmt(g['c'])} ({round(t['c'] / g['c'] * 100)}%)")
    L.append("")
    L.append("<b>Витамины и минералы (% от нормы):</b>")
    low = []
    for key, norm in NORMS.items():
        name, unit = MICRO_NAMES[key]
        pct = t[key] / norm * 100
        mark = "✅" if pct >= 80 else ("🟡" if pct >= 40 else "🔴")
        nd = 1 if norm < 50 else 0
        L.append(f"{mark} {name}: {fmt(t[key], nd)} {unit} — {round(pct)}%")
        if pct < 40:
            low.append(key)
    L.append("")
    # Выводы
    tips = []
    if t["kcal"] < g["kcal"] * 0.8:
        tips.append(f"Калорий маловато — недобор {fmt(g['kcal'] - t['kcal'])} ккал.")
    elif t["kcal"] > g["kcal"] * 1.1:
        tips.append(f"Перебор по калориям на {fmt(t['kcal'] - g['kcal'])} ккал.")
    else:
        tips.append("По калориям — точно в цель 👌")
    if t["p"] < g["p"] * 0.8:
        tips.append("Белка не хватает — добавь курицу, творог, яйца или рыбу.")
    for key in low[:4]:
        tips.append(f"Мало: {MICRO_NAMES[key][0].lower()} → {MICRO_TIPS[key]}.")
    L.append("💡 <b>Выводы:</b>")
    L += [f"— {x}" for x in tips]
    L.append("\n<i>Значения примерные (±10–15%), витамины считаются только из еды.</i>")
    return "\n".join(L)


HELP = (
    "🍽 <b>Дневник питания</b>\n\n"
    "<b>Как записывать еду:</b>\n"
    "1️⃣ Кнопка «➕ Добавить еду» → категория → продукт → граммы.\n"
    "2️⃣ Или просто напиши текстом:\n"
    "   <i>гречка 200</i>\n   <i>2 яйца</i>\n   <i>банан</i> (возьмёт 1 шт)\n"
    "   <i>курица 150, рис 200, огурец</i> — можно сразу несколько через запятую или с новой строки.\n"
    "3️⃣ 📷 Или просто <b>пришли фото еды</b> — ИИ сам поймёт, что на тарелке, прикинет порцию и посчитает калории. "
    "Можно добавить подпись, например «бутерброд с сыром и кофе с сахаром».\n\n"
    "<b>Кнопки:</b>\n"
    "📊 Сегодня — что съел и сколько осталось\n"
    "📋 Итог дня — полный отчёт: калории, БЖУ, витамины, минералы и советы\n"
    "↩️ Отменить последнее — удалить последнюю запись\n"
    "📈 Неделя — калории по дням за 7 дней\n"
    "🎯 Цели — норма калорий и время вечернего отчёта\n"
    "⭐ Мои продукты — добавить свой продукт со своим КБЖУ\n"
    "🏋️ Тренировка — план на сегодня (гантели + турник), отметка «сделал», утреннее напоминание\n"
    "🧑‍🍳 Что приготовить — сфоткай продукты, получишь 3 рецепта с КБЖУ\n\n"
    "🎙 <b>Голосовой помощник JARVIS:</b> команда /jarvis даст код для связи. Тогда можно сказать на ПК "
    "«Джарвис, запиши: гречка 200» или «сколько я сегодня съел?».\n\n"
    "Каждый вечер в выбранное время бот сам пришлёт итог дня 🌙"
)


# ======================= Клавиатуры =======================
def kb_categories():
    rows, row = [], []
    for cid, title in CATEGORIES:
        row.append((title, f"cat:{cid}:0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⭐ Мои продукты", "cat:custom:0"), ("🔍 Поиск", "search")])
    return ikb(rows)


PAGE = 12


def kb_foods(user_id, cat, page):
    items = db.customs(user_id) if cat == "custom" else by_category(cat)
    chunk = items[page * PAGE:(page + 1) * PAGE]
    rows, row = [], []
    for f in chunk:
        row.append((f["name"], f"food:{f['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(("⬅️", f"cat:{cat}:{page - 1}"))
    if (page + 1) * PAGE < len(items):
        nav.append(("➡️", f"cat:{cat}:{page + 1}"))
    if nav:
        rows.append(nav)
    if cat == "custom":
        rows.append([("➕ Создать продукт", "newcustom")])
    rows.append([("🔙 Категории", "cats")])
    return ikb(rows)


def kb_amount(food):
    fid = food["id"]
    rows = []
    p = food.get("portion")
    if p:
        unit, gr = p
        rows.append([(f"1 {unit} ({fmt(gr)} г)", f"add:{fid}:{gr}"),
                     (f"2 {unit}", f"add:{fid}:{gr * 2}"),
                     (f"3 {unit}", f"add:{fid}:{gr * 3}")])
    rows.append([(f"{g} г", f"add:{fid}:{g}") for g in (50, 100, 150)])
    rows.append([(f"{g} г", f"add:{fid}:{g}") for g in (200, 250, 300)])
    rows.append([("✏️ Своё количество", f"custamt:{fid}")])
    back = "cat:custom:0" if fid.startswith("u") else f"cat:{food['cat']}:0"
    rows.append([("🔙 Назад", back)])
    return ikb(rows)


def kb_after_add():
    return ikb([[("➕ Ещё еду", "cats"), ("📊 Сегодня", "today")],
                [("↩️ Отменить", "undo")]])


def kb_goals(u):
    return ikb([
        [(f"{k}{' ✅' if int(u['kcal_goal']) == k else ''}", f"goal:{k}") for k in (2000, 2200, 2400)],
        [(f"{k}{' ✅' if int(u['kcal_goal']) == k else ''}", f"goal:{k}") for k in (2600, 2800, 3000)],
        [("✏️ Своя (от врача)", "goal:custom")],
        [(f"🌙 {h}:00{' ✅' if u['report_hour'] == h else ''}", f"hour:{h}") for h in (20, 21, 22, 23)],
        [(f"🔕 Не присылать{' ✅' if u['report_hour'] == -1 else ''}", "hour:-1")],
    ])


def goals_text(u):
    g = goals_for(u["kcal_goal"])
    hour = "выключен" if u["report_hour"] == -1 else f"в {u['report_hour']}:00"
    return (f"🎯 <b>Твои цели</b>\n\n"
            f"🔥 Калории: <b>{fmt(g['kcal'])}</b> ккал\n"
            f"🥩 Белки: {fmt(g['p'])} г · 🧈 Жиры: {fmt(g['f'])} г · 🍞 Углеводы: {fmt(g['c'])} г\n"
            f"🌙 Вечерний отчёт: <b>{hour}</b>\n\n"
            f"ℹ️ Подросткам нельзя резко урезать еду — организм ещё растёт. Лучшую норму подскажет педиатр "
            f"или эндокринолог: впиши её через «Своя». Главное — не голодать, есть больше белка и овощей, "
            f"меньше сладкого и газировки, и больше двигаться.\n\n"
            f"Выбери норму калорий и время отчёта:")


# ======================= Логика =======================
def add_food(user_id, chat_id, food, grams, msg_id=None):
    nutr = calc(food["per100"], grams)
    db.add_entry(user_id, today(), food["name"], grams, nutr)
    u = db.get_user(user_id)
    t = sum_entries(db.day_entries(user_id, today()))
    text = (f"✅ Записал: <b>{food['name']}</b>, {fmt(grams)} г\n"
            f"{fmt(nutr['kcal'])} ккал · Б {fmt(nutr['p'], 1)} · Ж {fmt(nutr['f'], 1)} · У {fmt(nutr['c'], 1)}\n\n"
            f"За сегодня: <b>{fmt(t['kcal'])}</b> / {fmt(u['kcal_goal'])} ккал\n{bar(t['kcal'], u['kcal_goal'])}")
    if msg_id:
        edit(chat_id, msg_id, text, kb_after_add())
    else:
        send(chat_id, text, kb_after_add())


def parse_and_add(user_id, text, pick_best=False):
    """Разбирает «гречка 200, 2 яйца» и сразу записывает то, что точно узнал.
    pick_best=True (запрос от JARVIS) — при сомнении берёт самый похожий продукт, а не спрашивает."""
    parts = [p for p in re.split(r"[,\n;]+|\s+и\s+", text) if p.strip()]
    added, unknown, ambiguous = [], [], []
    for part in parts:
        name, amount, unit = parse_item(part)
        scored = search_foods(user_id, name, with_scores=True)
        if not scored:
            unknown.append(part.strip())
            continue
        found = [f for _, f in scored]
        best = scored[0][0]
        second = scored[1][0] if len(scored) > 1 else 0
        if best >= 85 and best > second or len(scored) == 1 and best >= 60 or pick_best and best >= 60:
            food = found[0]
            grams = to_grams(food, amount, unit)
            nutr = calc(food["per100"], grams)
            db.add_entry(user_id, today(), food["name"], grams, nutr)
            added.append((food, grams, nutr))
        else:
            ambiguous.append((part.strip(), found, amount, unit))
    return added, ambiguous, unknown


def handle_free_text(user_id, chat_id, text):
    added, ambiguous, unknown = parse_and_add(user_id, text)
    if added:
        u = db.get_user(user_id)
        t = sum_entries(db.day_entries(user_id, today()))
        lines = ["✅ <b>Записал:</b>"]
        for food, grams, nutr in added:
            lines.append(f"• {food['name']} — {fmt(grams)} г · {fmt(nutr['kcal'])} ккал")
        lines.append(f"\nЗа сегодня: <b>{fmt(t['kcal'])}</b> / {fmt(u['kcal_goal'])} ккал\n{bar(t['kcal'], u['kcal_goal'])}")
        send(chat_id, "\n".join(lines), kb_after_add())

    for part, found, amount, unit in ambiguous:
        grams_hint = ""
        if amount is not None:
            STATE.setdefault(user_id, {})["pending_amount"] = (amount, unit)
            grams_hint = f" ({fmt(amount, 1)}{' ' + unit if unit else ''})"
        rows = [[(f["name"], f"pick:{f['id']}")] for f in found[:8]]
        send(chat_id, f"🤔 «{part}» — уточни, что именно{grams_hint}:", ikb(rows))

    if unknown and ai.enabled():
        desc = ", ".join(unknown)
        run_ai(user_id, chat_id, lambda: ai.analyze_text(desc), f"🤖 «{desc}» нет в базе — считаю с помощью ИИ…")
        return
    for part in unknown:
        rows = [[("⭐ Добавить свой продукт", "newcustom")], [("📂 Выбрать из списка", "cats")]]
        send(chat_id, f"❓ Не нашёл «{part}» в базе.\nМожешь выбрать похожее из категорий или добавить свой продукт с КБЖУ.", ikb(rows))


def parse_custom_product(text):
    # "Название; ккал; б; ж; у"  или  "Название 250 10 5 30"
    t = text.strip()
    if ";" in t:
        parts = [p.strip() for p in t.split(";")]
        name, nums = parts[0], parts[1:]
    else:
        m = re.match(r"^(.+?)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$", t)
        if not m:
            return None
        name, nums = m.group(1), list(m.groups()[1:])
    try:
        nums = [float(x.replace(",", ".")) for x in nums[:4]]
    except ValueError:
        return None
    if len(nums) < 4 or not name:
        return None
    return name[:40], {"kcal": nums[0], "p": nums[1], "f": nums[2], "c": nums[3]}


def on_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    db.ensure_user(user_id, chat_id)
    if msg.get("photo"):
        cook = STATE.get(user_id, {}).get("await") == "cook_photo" or \
            re.search(r"приготов|рецепт|что сделать|что можно", (msg.get("caption") or "").lower())
        STATE.pop(user_id, None)
        LAST_PHOTO[user_id] = (msg["photo"], (msg.get("caption") or "").strip())
        if cook:
            handle_cook_photo(user_id, chat_id)
        else:
            handle_photo(msg)
        return
    if not text:
        send(chat_id, "Пришли текстом, что съел — например: <i>гречка 200</i>", MAIN_KB)
        return

    st = STATE.get(user_id, {})
    if text.startswith("/start"):
        STATE.pop(user_id, None)
        name = msg["from"].get("first_name", "")
        send(chat_id, f"Привет, {name}! 👋\n\n" + HELP, MAIN_KB)
        return
    if text.startswith("/jarvis_off"):
        db.set_jarvis_key(user_id, None)
        send(chat_id, "🔌 JARVIS отключён: старый код больше не работает. Новый код — команда /jarvis.", MAIN_KB)
        return
    if text.startswith("/jarvis"):
        key = "nb_" + secrets.token_urlsafe(24)
        db.set_jarvis_key(user_id, key_hash(key))
        send(chat_id, JARVIS_TEXT.format(key=key), MAIN_KB)
        return
    if text.startswith("/help") or text == BTN_HELP:
        STATE.pop(user_id, None)
        send(chat_id, HELP, MAIN_KB)
        return

    menu = {BTN_ADD, BTN_TODAY, BTN_REPORT, BTN_UNDO, BTN_WEEK, BTN_GOALS, BTN_MY, BTN_WORKOUT, BTN_COOK}
    if text in menu or text.startswith("/"):
        STATE.pop(user_id, None)

    if text == BTN_ADD or text == "/add":
        send(chat_id, "Выбери категорию 👇\n<i>Или просто напиши: «курица 150»</i>", kb_categories())
    elif text == BTN_TODAY or text == "/today":
        send(chat_id, today_text(user_id), ikb([[("🗑 Удалить запись", "dellist"), ("📋 Итог дня", "report")]]))
    elif text == BTN_REPORT or text == "/report":
        send(chat_id, report_text(user_id))
    elif text == BTN_UNDO or text == "/undo":
        do_undo(user_id, chat_id)
    elif text == BTN_WEEK or text == "/week":
        send(chat_id, week_text(user_id))
    elif text == BTN_GOALS or text == "/goals":
        u = db.get_user(user_id)
        send(chat_id, goals_text(u), kb_goals(u))
    elif text == BTN_MY:
        my_products(user_id, chat_id)
    elif text == BTN_WORKOUT or text == "/workout":
        text_, kb = workout_screen(user_id)
        send(chat_id, text_, kb)
    elif text == BTN_COOK or text == "/cook":
        STATE[user_id] = {"await": "cook_photo"}
        send(chat_id, "🧑‍🍳 Сфоткай продукты, которые у тебя есть (холодильник, стол, пакет из магазина) — "
                      "я предложу 3 блюда с рецептом и посчитаю калории, белки, жиры и углеводы на порцию.")
    elif st.get("await") == "amount":
        m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", text)
        food = get_food(user_id, st["fid"])
        if not m or not food:
            send(chat_id, "Напиши число граммов, например: <b>180</b>")
            return
        grams = float(m.group(1).replace(",", "."))
        if not 0 < grams <= 5000:
            send(chat_id, "Странное количество 🙂 Напиши от 1 до 5000 г.")
            return
        STATE.pop(user_id, None)
        add_food(user_id, chat_id, food, grams)
    elif st.get("await") == "custom":
        res = parse_custom_product(text)
        if not res:
            send(chat_id, "Не понял формат. Пример:\n<code>Протеиновый батончик; 350; 30; 10; 35</code>\n(название; ккал; белки; жиры; углеводы — на 100 г)")
            return
        name, per100 = res
        db.add_custom(user_id, name, per100)
        STATE.pop(user_id, None)
        send(chat_id, f"⭐ Продукт «{name}» сохранён! Теперь его можно писать текстом или найти в «⭐ Мои продукты».", MAIN_KB)
    elif st.get("await") == "goal":
        m = re.match(r"^\s*(\d{3,5})", text)
        if not m or not 1800 <= int(m.group(1)) <= 4500:
            send(chat_id, "Напиши число калорий от 1800 до 4500, например <b>2400</b>. Меньше 1800 в твоём возрасте — опасно мало.")
            return
        STATE.pop(user_id, None)
        db.set_user(user_id, kcal_goal=int(m.group(1)))
        u = db.get_user(user_id)
        send(chat_id, goals_text(u), kb_goals(u))
    elif st.get("await") == "search":
        STATE.pop(user_id, None)
        found = search_foods(user_id, text, limit=10)
        if not found:
            send(chat_id, f"Ничего не нашёл по «{text}».", ikb([[("⭐ Добавить свой продукт", "newcustom")], [("📂 Категории", "cats")]]))
        else:
            send(chat_id, f"🔍 Нашёл по «{text}»:", ikb([[(f["name"], f"food:{f['id']}")] for f in found]))
    else:
        handle_free_text(user_id, chat_id, text)


def do_undo(user_id, chat_id, msg_id=None):
    last = db.last_entry(user_id, today())
    if not last:
        text = "Сегодня ещё нечего отменять."
    else:
        db.delete_entry(user_id, last[0])
        text = f"↩️ Удалил: {last[1]}, {fmt(last[2])} г"
    if msg_id:
        edit(chat_id, msg_id, text)
    else:
        send(chat_id, text, MAIN_KB)


def week_text(user_id):
    now = datetime.now(TZ)
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    tot = db.days_totals(user_id, days)
    u = db.get_user(user_id)
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    L = ["📈 <b>Последние 7 дней</b>\n"]
    filled = []
    for d in days:
        kcal, p, f, c, n = tot[d]
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = f"{names[dt.weekday()]} {dt.strftime('%d.%m')}"
        if n:
            filled.append((kcal, p))
            L.append(f"{label}: <b>{fmt(kcal)}</b> ккал · Б {fmt(p)} г  {bar(kcal, u['kcal_goal'], 6)}")
        else:
            L.append(f"{label}: —")
    L.append(f"\n🏋️ Тренировок за 7 дней: <b>{db.workouts_count(user_id, days[0])}</b>")
    if filled:
        avg = sum(k for k, _ in filled) / len(filled)
        avg_p = sum(p for _, p in filled) / len(filled)
        L.append(f"\nВ среднем: <b>{fmt(avg)}</b> ккал/день, белок {fmt(avg_p)} г (по {len(filled)} дн. с записями)")
    return "\n".join(L)


def my_products(user_id, chat_id):
    items = db.customs(user_id)
    lines = ["⭐ <b>Мои продукты</b>\n"]
    if items:
        for f in items:
            p = f["per100"]
            lines.append(f"• {f['name']} — {fmt(p['kcal'])} ккал, Б{fmt(p['p'], 1)} Ж{fmt(p['f'], 1)} У{fmt(p['c'], 1)} /100 г")
    else:
        lines.append("Пока пусто. Добавь продукт, которого нет в базе (например, любимый батончик или домашнее блюдо).")
    rows = [[("➕ Создать продукт", "newcustom")]]
    if items:
        rows.append([("🗑 Удалить продукт", "delcustom")])
    send(chat_id, "\n".join(lines), ikb(rows))


def on_callback(cq):
    data = cq.get("data", "")
    user_id = cq["from"]["id"]
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id", user_id)
    msg_id = msg.get("message_id")
    db.ensure_user(user_id, chat_id)
    answer = ""

    if data.startswith("ai_"):
        answer = ai_callback(user_id, chat_id, msg_id, data)
    elif data == "cook_last":
        AI_PENDING.pop(user_id, None)
        edit(chat_id, msg_id, "Ок, это не еда на тарелке, а продукты 👍")
        handle_cook_photo(user_id, chat_id)
    elif data.startswith("rcp:"):
        recs = RECIPES.get(user_id) or []
        n = int(data[4:])
        if 0 <= n < len(recs):
            r = recs[n]
            db.add_entry(user_id, today(), "🍳 " + r["title"], r["grams"] or 300, r)
            u = db.get_user(user_id)
            t = sum_entries(db.day_entries(user_id, today()))
            send(chat_id, f"✅ Записал: {r['title']} — {fmt(r['kcal'])} ккал\n\nЗа сегодня: <b>{fmt(t['kcal'])}</b> / "
                          f"{fmt(u['kcal_goal'])} ккал\n{bar(t['kcal'], u['kcal_goal'])}", kb_after_add())
            answer = "Записано ✅"
        else:
            answer = "Рецепт устарел, пришли фото ещё раз"
    elif data == "wk_done":
        db.mark_workout(user_id, today())
        answer = "Красавчик! 💪"
        t, kb = workout_screen(user_id)
        edit(chat_id, msg_id, t, kb)
    elif data == "wk_week":
        u = db.get_user(user_id)
        send(chat_id, W.week_plan_text(training_week(u)))
    elif data == "wk_today":
        t, kb = workout_screen(user_id)
        edit(chat_id, msg_id, t, kb)
    elif data.startswith("wkh:"):
        db.set_user(user_id, workout_hour=int(data[4:]))
        answer = "Сохранено"
        t, kb = workout_screen(user_id)
        edit(chat_id, msg_id, t, kb)
    elif data == "cats":
        edit(chat_id, msg_id, "Выбери категорию 👇", kb_categories())
    elif data.startswith("cat:"):
        _, cat, page = data.split(":")
        title = dict(CATEGORIES).get(cat, "⭐ Мои продукты")
        if cat == "custom" and not db.customs(user_id):
            edit(chat_id, msg_id, "⭐ У тебя пока нет своих продуктов.", ikb([[("➕ Создать продукт", "newcustom")], [("🔙 Категории", "cats")]]))
        else:
            edit(chat_id, msg_id, f"{title} — выбери продукт:", kb_foods(user_id, cat, int(page)))
    elif data.startswith("food:"):
        food = get_food(user_id, data[5:])
        if food:
            p = food["per100"]
            edit(chat_id, msg_id,
                 f"<b>{food['name']}</b>\nна 100 г: {fmt(p['kcal'])} ккал · Б {fmt(p['p'], 1)} · Ж {fmt(p['f'], 1)} · У {fmt(p['c'], 1)}\n\nСколько съел?",
                 kb_amount(food))
    elif data.startswith("pick:"):
        food = get_food(user_id, data[5:])
        pend = STATE.get(user_id, {}).pop("pending_amount", None)
        if food and pend:
            add_food(user_id, chat_id, food, to_grams(food, pend[0], pend[1]), msg_id)
        elif food:
            p = food["per100"]
            edit(chat_id, msg_id, f"<b>{food['name']}</b>\nна 100 г: {fmt(p['kcal'])} ккал\n\nСколько съел?", kb_amount(food))
    elif data.startswith("add:"):
        _, fid, grams = data.split(":")
        food = get_food(user_id, fid)
        if food:
            add_food(user_id, chat_id, food, float(grams), msg_id)
            answer = "Записано ✅"
    elif data.startswith("custamt:"):
        food = get_food(user_id, data[8:])
        STATE[user_id] = {"await": "amount", "fid": data[8:]}
        edit(chat_id, msg_id, f"✏️ Напиши, сколько грамм <b>{food['name'] if food else ''}</b> ты съел (просто число):")
    elif data == "today":
        send(chat_id, today_text(user_id), ikb([[("🗑 Удалить запись", "dellist"), ("📋 Итог дня", "report")]]))
    elif data == "report":
        send(chat_id, report_text(user_id))
    elif data == "undo":
        do_undo(user_id, chat_id, msg_id)
    elif data == "dellist":
        entries = db.day_entries(user_id, today())
        if not entries:
            answer = "Нечего удалять"
        else:
            rows = [[(f"❌ {e['name']} {fmt(e['grams'])} г", f"del:{e['id']}")] for e in entries[-15:]]
            rows.append([("✔️ Готово", "today_edit")])
            edit(chat_id, msg_id, "Нажми на запись, чтобы удалить:", ikb(rows))
    elif data.startswith("del:"):
        db.delete_entry(user_id, int(data[4:]))
        answer = "Удалено"
        entries = db.day_entries(user_id, today())
        rows = [[(f"❌ {e['name']} {fmt(e['grams'])} г", f"del:{e['id']}")] for e in entries[-15:]]
        rows.append([("✔️ Готово", "today_edit")])
        edit(chat_id, msg_id, "Нажми на запись, чтобы удалить:" if entries else "Записей за сегодня больше нет.", ikb(rows))
    elif data == "today_edit":
        edit(chat_id, msg_id, today_text(user_id), ikb([[("🗑 Удалить запись", "dellist"), ("📋 Итог дня", "report")]]))
    elif data.startswith("goal:"):
        v = data[5:]
        if v == "custom":
            STATE[user_id] = {"await": "goal"}
            send(chat_id, "✏️ Напиши свою норму калорий в день (например, <b>2350</b>):")
        else:
            db.set_user(user_id, kcal_goal=int(v))
            u = db.get_user(user_id)
            edit(chat_id, msg_id, goals_text(u), kb_goals(u))
            answer = f"Норма: {v} ккал"
    elif data.startswith("hour:"):
        db.set_user(user_id, report_hour=int(data[5:]))
        u = db.get_user(user_id)
        edit(chat_id, msg_id, goals_text(u), kb_goals(u))
        answer = "Сохранено"
    elif data == "newcustom":
        STATE[user_id] = {"await": "custom"}
        send(chat_id, "⭐ <b>Новый продукт</b>\n\nНапиши одной строкой: название; ккал; белки; жиры; углеводы (на 100 г).\n\nПример:\n<code>Сырок глазированный; 410; 8; 27; 32</code>\n\n<i>КБЖУ обычно написано на упаковке.</i>")
    elif data == "delcustom":
        items = db.customs(user_id)
        rows = [[(f"❌ {f['name']}", f"dc:{f['id'][1:]}")] for f in items]
        edit(chat_id, msg_id, "Какой продукт удалить?", ikb(rows) if rows else None)
    elif data.startswith("dc:"):
        db.delete_custom(user_id, int(data[3:]))
        answer = "Продукт удалён"
        edit(chat_id, msg_id, "🗑 Продукт удалён.")
    elif data == "search":
        STATE[user_id] = {"await": "search"}
        send(chat_id, "🔍 Напиши название продукта:")

    api("answerCallbackQuery", callback_query_id=cq["id"], text=answer)


# ======================= ИИ: фото и описание =======================
def download_photo(photos):
    best = sorted(photos, key=lambda p: p.get("file_size", 0) or p.get("width", 0))[-1]
    r = api("getFile", file_id=best["file_id"])
    path = r.get("result", {}).get("file_path")
    if not path:
        raise ai.AIError("no file")
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60) as resp:
        return resp.read()


def ai_card(user_id):
    pend = AI_PENDING.get(user_id)
    if not pend or not pend["items"]:
        return "Список пуст — ничего не записываю.", None
    tot = {f: sum(i[f] for i in pend["items"]) for f in FIELDS}
    L = ["🤖 <b>Вот что я вижу:</b>\n"]
    for i in pend["items"]:
        L.append(f"• {i['name']} ~{fmt(i['grams'])} г — <b>{fmt(i['kcal'])}</b> ккал "
                 f"(Б {fmt(i['p'])} · Ж {fmt(i['f'])} · У {fmt(i['c'])})")
    L.append(f"\n🔥 Всего: <b>{fmt(tot['kcal'])}</b> ккал · Б {fmt(tot['p'])} · Ж {fmt(tot['f'])} · У {fmt(tot['c'])}")
    if pend.get("comment"):
        L.append(f"\n💬 {pend['comment']}")
    L.append("\n<i>Это оценка на глаз (±20–30%). Поправь порцию, если ешь больше или меньше.</i>")
    rows = [[("✅ Записать", "ai_ok"), ("🚫 Отмена", "ai_no")],
            [("➖ Порция меньше", "ai_x:0.75"), ("➕ Порция больше", "ai_x:1.25")]]
    for n, i in enumerate(pend["items"][:8]):
        rows.append([(f"❌ Убрать: {i['name'][:25]}", f"ai_rm:{n}")])
    if LAST_PHOTO.get(user_id):
        rows.append([("🧑‍🍳 Это продукты — что приготовить?", "cook_last")])
    return "\n".join(L), ikb(rows)


def run_ai(user_id, chat_id, job, wait_text):
    """Запускает ИИ в отдельном потоке, чтобы бот не подвисал."""
    r = send(chat_id, wait_text)
    wait_id = (r.get("result") or {}).get("message_id")

    def worker():
        try:
            items, comment = job()
            if not items:
                text = "🤷 " + (comment or "Не смог распознать еду. Попробуй фото поближе или напиши текстом.")
                edit(chat_id, wait_id, text) if wait_id else send(chat_id, text)
                return
            AI_PENDING[user_id] = {"items": items, "comment": comment}
            text, kb = ai_card(user_id)
            edit(chat_id, wait_id, text, kb) if wait_id else send(chat_id, text, kb)
        except ai.AIError as e:
            text = ("⏳ Лимит бесплатного ИИ на сейчас исчерпан — попробуй через минуту или запиши текстом."
                    if str(e) == "limit" else "😕 ИИ сейчас не ответил. Попробуй ещё раз или запиши текстом.")
            edit(chat_id, wait_id, text) if wait_id else send(chat_id, text)
        except Exception:
            traceback.print_exc()
            send(chat_id, "😕 Что-то пошло не так с распознаванием. Попробуй ещё раз.")

    threading.Thread(target=worker, daemon=True).start()


def handle_photo(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    if not ai.enabled():
        send(chat_id, "📷 Распознавание по фото пока не подключено. Запиши текстом, например: <i>бутерброд с сыром, кофе</i>")
        return
    caption = (msg.get("caption") or "").strip()
    photos = msg["photo"]
    run_ai(user_id, chat_id, lambda: ai.analyze_photo(download_photo(photos), caption),
           "📷 Смотрю на фото и считаю калории… (5–15 сек)")


def ai_callback(user_id, chat_id, msg_id, data):
    pend = AI_PENDING.get(user_id)
    if not pend:
        edit(chat_id, msg_id, "Этот результат уже неактуален. Пришли фото ещё раз.")
        return ""
    if data == "ai_no":
        AI_PENDING.pop(user_id, None)
        edit(chat_id, msg_id, "🚫 Отменено, ничего не записал.")
        return ""
    if data == "ai_ok":
        for i in pend["items"]:
            db.add_entry(user_id, today(), "🤖 " + i["name"], i["grams"], i)
        AI_PENDING.pop(user_id, None)
        u = db.get_user(user_id)
        t = sum_entries(db.day_entries(user_id, today()))
        edit(chat_id, msg_id, f"✅ Записал {len(pend['items'])} поз.\n\nЗа сегодня: <b>{fmt(t['kcal'])}</b> / "
                              f"{fmt(u['kcal_goal'])} ккал\n{bar(t['kcal'], u['kcal_goal'])}", kb_after_add())
        return "Записано ✅"
    if data.startswith("ai_x:"):
        k = float(data[5:])
        for i in pend["items"]:
            i["grams"] *= k
            for f in FIELDS:
                i[f] *= k
    elif data.startswith("ai_rm:"):
        n = int(data[6:])
        if 0 <= n < len(pend["items"]):
            pend["items"].pop(n)
    text, kb = ai_card(user_id)
    edit(chat_id, msg_id, text, kb)
    return ""


# ======================= Тренировки =======================
def training_week(u):
    start = u.get("workout_start") or ""
    if not start:
        start = today()
        db.set_user(u["user_id"], workout_start=start)
    days = (datetime.now(TZ).date() - datetime.strptime(start, "%Y-%m-%d").date()).days
    return max(0, days) // 7 + 1


def workout_screen(user_id):
    u = db.get_user(user_id)
    week = training_week(u)
    wd = datetime.now(TZ).weekday()
    done = db.workout_done(user_id, today())
    since = (datetime.now(TZ) - timedelta(days=6)).strftime("%Y-%m-%d")
    cnt = db.workouts_count(user_id, since)
    text = W.workout_text(wd, week)
    text += f"\n\n{'✅ Сегодня уже сделано!' if done else '⬜ Сегодня ещё не отмечено'} · за 7 дней: {cnt} трен."
    hour = u["workout_hour"]
    text += f"\n⏰ Утреннее напоминание: <b>{'выключено' if hour < 0 else f'в {hour}:00'}</b>"
    rows = []
    if not done:
        rows.append([("✅ Сделал тренировку", "wk_done")])
    rows.append([("📅 План на неделю", "wk_week")])
    rows.append([(f"⏰ {h}:00{' ✅' if hour == h else ''}", f"wkh:{h}") for h in (6, 7, 8, 9)])
    rows.append([(f"⏰ 10:00{' ✅' if hour == 10 else ''}", "wkh:10"), (f"🔕 Выкл{' ✅' if hour < 0 else ''}", "wkh:-1")])
    return text, ikb(rows)


# ======================= Что приготовить =======================
def recipe_text(n, r):
    L = [f"🍳 <b>{n + 1}. {r['title']}</b>" + (f" · ~{r['time_min']} мин" if r["time_min"] else "")]
    L.append("\n<b>На 1 порцию:</b>")
    for name, g in r["ingredients"]:
        L.append(f"• {name}" + (f" — {fmt(g)} г" if g else ""))
    L.append("\n<b>Как готовить:</b>")
    for i, st in enumerate(r["steps"], 1):
        L.append(f"{i}. {st}")
    L.append(f"\n🔥 <b>{fmt(r['kcal'])} ккал</b> · Б {fmt(r['p'])} · Ж {fmt(r['f'])} · У {fmt(r['c'])} г")
    micro = []
    for key in ("C", "Fe", "Ca", "K"):
        pct = r[key] / NORMS[key] * 100
        if pct >= 15:
            micro.append(f"{MICRO_NAMES[key][0].lower()} {round(pct)}%")
    if micro:
        L.append("💊 Хороший источник: " + ", ".join(micro))
    return "\n".join(L)


def handle_cook_photo(user_id, chat_id):
    if not ai.enabled():
        send(chat_id, "ИИ пока не подключён.")
        return
    photos, caption = LAST_PHOTO.get(user_id, (None, ""))
    if not photos:
        send(chat_id, "Пришли фото продуктов 📷")
        return
    r = send(chat_id, "🧑‍🍳 Смотрю, что у тебя есть, и придумываю блюда… (10–20 сек)")
    wait_id = (r.get("result") or {}).get("message_id")

    def worker():
        try:
            products, recipes = ai.recipes_from_photo(download_photo(photos), caption)
            if not recipes:
                edit(chat_id, wait_id, "🤷 Не получилось придумать блюда по этому фото. Сфоткай продукты поближе.")
                return
            RECIPES[user_id] = recipes
            head = "🧺 <b>Вижу:</b> " + (", ".join(products) if products else "продукты") + "\n\nВот что можно приготовить 👇"
            edit(chat_id, wait_id, head)
            for n, rec in enumerate(recipes):
                send(chat_id, recipe_text(n, rec), ikb([[("✅ Съел это — записать", f"rcp:{n}")]]))
        except ai.AIError as e:
            edit(chat_id, wait_id, "⏳ Лимит бесплатного ИИ исчерпан, попробуй через минуту." if str(e) == "limit"
                 else "😕 ИИ сейчас не ответил. Попробуй ещё раз.")
        except Exception:
            traceback.print_exc()
            send(chat_id, "😕 Что-то пошло не так. Попробуй ещё раз.")

    threading.Thread(target=worker, daemon=True).start()


# ======================= Фоновые задачи =======================
def scheduler():
    """Раз в минуту проверяет, кому пора прислать вечерний отчёт."""
    while True:
        try:
            now = datetime.now(TZ)
            d = now.strftime("%Y-%m-%d")
            for u in db.all_users():
                wh = u["workout_hour"]
                if wh is not None and wh >= 0 and now.hour == wh and u["last_workout"] != d:
                    db.set_user(u["user_id"], last_workout=d)
                    t, kb = workout_screen(u["user_id"])
                    send(u["chat_id"], "☀️ Доброе утро! Вот тренировка на сегодня:\n\n" + t, kb)
                if u["report_hour"] is None or u["report_hour"] < 0:
                    continue
                if now.hour == u["report_hour"] and u["last_report"] != d:
                    db.set_user(u["user_id"], last_report=d)
                    if db.day_entries(u["user_id"], d):
                        send(u["chat_id"], "🌙 Вечерний отчёт\n\n" + report_text(u["user_id"], d), MAIN_KB)
                    else:
                        send(u["chat_id"], "🌙 Сегодня в дневнике пусто. Если ел — запиши сейчас, и нажми «📋 Итог дня».", MAIN_KB)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def keep_alive():
    """Бесплатный Render засыпает через 15 минут без запросов — пингуем сами себя."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=20).read()
        except Exception as e:
            print("keep-alive error:", e, flush=True)


# ======================= Связь с JARVIS =======================
JARVIS_TEXT = (
    "🎙 <b>Связь с JARVIS</b>\n\n"
    "JARVIS — голосовой помощник на твоём ПК. После связи он умеет:\n"
    "• записывать еду голосом: «Джарвис, запиши: гречка 200 и 2 яйца»\n"
    "• отвечать, сколько ты съел сегодня и сколько осталось\n"
    "• зачитывать итог дня и статистику за неделю\n\n"
    "Твой секретный код (нажми, чтобы скопировать):\n<code>{key}</code>\n\n"
    "Вставь его в JARVIS → ⚙️ Настройки → вкладка «Питание».\n"
    "⚠️ Никому не показывай этот код. Новый код отменяет старый; отключить связь — /jarvis_off"
)


def key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def plain(text):
    """HTML-текст бота → обычный текст для JARVIS."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def today_json(user_id):
    u = db.get_user(user_id)
    g = goals_for(u["kcal_goal"])
    entries = db.day_entries(user_id, today())
    t = sum_entries(entries) if entries else {"kcal": 0, "p": 0, "f": 0, "c": 0}
    return {"date": today(), "kcal": round(t["kcal"]), "goal_kcal": round(g["kcal"]),
            "left_kcal": round(g["kcal"] - t["kcal"]),
            "protein": round(t["p"]), "fat": round(t["f"]), "carbs": round(t["c"]),
            "goal_protein": round(g["p"]), "goal_fat": round(g["f"]), "goal_carbs": round(g["c"]),
            "items": [{"name": e["name"], "grams": round(e["grams"]), "kcal": round(e["kcal"])} for e in entries]}


def jarvis_add(user_id, text):
    """Запись еды по голосовой команде из JARVIS: без вопросов; непонятное считает ИИ."""
    added, ambiguous, unknown = parse_and_add(user_id, text, pick_best=True)
    lines = [f"{f['name']} {fmt(g)} г — {fmt(n['kcal'])} ккал" for f, g, n in added]
    unknown = unknown + [part for part, *_ in ambiguous]
    unclear = []
    if unknown and ai.enabled():
        try:
            items, _ = ai.analyze_text(", ".join(unknown))
            for i in items:
                db.add_entry(user_id, today(), "🤖 " + i["name"], i["grams"], i)
                lines.append(f"{i['name']} ~{fmt(i['grams'])} г — {fmt(i['kcal'])} ккал (оценка ИИ)")
            if not items:
                unclear = unknown
        except Exception:
            traceback.print_exc()
            unclear = unknown
    else:
        unclear = unknown
    u = db.get_user(user_id)
    if lines:
        t = sum_entries(db.day_entries(user_id, today()))
        send(u["chat_id"], "🎙 <b>Записал через JARVIS:</b>\n" + "\n".join("• " + x for x in lines) +
             f"\n\nЗа сегодня: <b>{fmt(t['kcal'])}</b> / {fmt(u['kcal_goal'])} ккал\n{bar(t['kcal'], u['kcal_goal'])}",
             kb_after_add())
    return {"added": lines, "unclear": unclear, "today": today_json(user_id)}


def jarvis_undo(user_id):
    e = db.last_entry(user_id, today())
    if not e:
        return {"removed": None, "today": today_json(user_id)}
    entry_id, name = e[0], e[1]
    db.delete_entry(user_id, entry_id)
    send(db.get_user(user_id)["chat_id"], f"↩️ JARVIS удалил последнюю запись: {name}", MAIN_KB)
    return {"removed": name, "today": today_json(user_id)}


class Health(BaseHTTPRequestHandler):
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _jarvis_user(self):
        key = (self.headers.get("X-Jarvis-Key") or "").strip()
        if not key.startswith("nb_") or len(key) > 100:
            return None
        return db.user_by_jarvis_key(key_hash(key))

    def _api(self, method):
        path = self.path.split("?")[0].rstrip("/")
        user_id = self._jarvis_user()
        if user_id is None or db.get_user(user_id) is None:
            time.sleep(1)  # замедляем подбор кода
            return self._json(401, {"error": "Неверный код JARVIS. Получи новый командой /jarvis в боте."})
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4000:
                return self._json(413, {"error": "Слишком длинный запрос"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "Плохой JSON"})
        try:
            if method == "GET" and path == "/api/jarvis/today":
                return self._json(200, {"today": today_json(user_id), "text": plain(today_text(user_id))})
            if method == "GET" and path == "/api/jarvis/report":
                return self._json(200, {"text": plain(report_text(user_id))})
            if method == "GET" and path == "/api/jarvis/week":
                return self._json(200, {"text": plain(week_text(user_id))})
            if method == "POST" and path == "/api/jarvis/add":
                text = str(body.get("text", "")).strip()[:500]
                if not text:
                    return self._json(400, {"error": "Пустой список еды"})
                return self._json(200, jarvis_add(user_id, text))
            if method == "POST" and path == "/api/jarvis/undo":
                return self._json(200, jarvis_undo(user_id))
        except Exception:
            traceback.print_exc()
            return self._json(500, {"error": "Ошибка в боте"})
        return self._json(404, {"error": "Нет такой команды"})

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api("GET")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Бот дневника питания работает ✅".encode("utf-8"))

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._api("POST")
        self._json(404, {"error": "not found"})

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def run_http():
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever()


def polling():
    api("deleteWebhook", drop_pending_updates=False)
    api("setMyCommands", commands=[
        {"command": "start", "description": "Главное меню"},
        {"command": "add", "description": "Добавить еду"},
        {"command": "today", "description": "Что съел сегодня"},
        {"command": "report", "description": "Итог дня"},
        {"command": "week", "description": "Статистика за неделю"},
        {"command": "goals", "description": "Цели и время отчёта"},
        {"command": "undo", "description": "Отменить последнюю запись"},
        {"command": "workout", "description": "Тренировка на сегодня"},
        {"command": "cook", "description": "Что приготовить из продуктов"},
        {"command": "jarvis", "description": "Связать с голосовым помощником JARVIS"},
    ])
    offset = 0
    print("Bot started", flush=True)
    while True:
        r = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(3 if r.get("error_code") == 409 else 5)
            continue
        for upd in r["result"]:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    on_message(upd["message"])
                elif "callback_query" in upd:
                    on_callback(upd["callback_query"])
            except Exception:
                traceback.print_exc()


def main():
    global db
    if not TOKEN:
        raise SystemExit("Нет BOT_TOKEN в переменных окружения")
    db = DB()
    threading.Thread(target=run_http, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    polling()


if __name__ == "__main__":
    main()
