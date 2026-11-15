# -*- coding: utf-8 -*-
"""Хранилище: Postgres (если есть DATABASE_URL) или SQLite-файл."""
import os
import sqlite3
import threading
import time

from foods import FIELDS

DATABASE_URL = os.environ.get("DATABASE_URL")
_lock = threading.Lock()

# имена колонок без конфликта регистра (c / C)
COL = {f: ("v_" + f if f.islower() else "m_" + f.lower()) for f in FIELDS}
COLS = ", ".join(COL[f] for f in FIELDS)
NUM_COLS = ", ".join(f"{COL[f]} REAL DEFAULT 0" for f in FIELDS)

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS entries (
        id {{serial}},
        user_id BIGINT NOT NULL,
        day TEXT NOT NULL,
        name TEXT NOT NULL,
        grams REAL NOT NULL,
        created_at BIGINT NOT NULL,
        {NUM_COLS})""",
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        kcal_goal REAL DEFAULT 2200,
        report_hour INTEGER DEFAULT 22,
        last_report TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS jarvis_links (
        user_id BIGINT PRIMARY KEY,
        key_hash TEXT NOT NULL,
        created_at BIGINT NOT NULL)""",
    f"""CREATE TABLE IF NOT EXISTS custom_foods (
        id {{serial}},
        user_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        {NUM_COLS})""",
]


class DB:
    def __init__(self):
        if DATABASE_URL:
            import psycopg2
            self.pg = True
            self._connect = lambda: psycopg2.connect(DATABASE_URL)
            self.ph = "%s"
            serial = "SERIAL PRIMARY KEY"
        else:
            self.pg = False
            path = os.environ.get("SQLITE_PATH", "data.db")
            self._connect = lambda: sqlite3.connect(path, check_same_thread=False)
            self.ph = "?"
            serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        self.conn = None
        for stmt in SCHEMA:
            self.execute(stmt.replace("{serial}", serial))

    def _get_conn(self):
        if self.conn is None:
            self.conn = self._connect()
        return self.conn

    def execute(self, sql, params=(), fetch=False):
        sql = sql.replace("?", self.ph)
        with _lock:
            for attempt in range(3):
                try:
                    conn = self._get_conn()
                    cur = conn.cursor()
                    cur.execute(sql, params)
                    rows = cur.fetchall() if fetch else None
                    conn.commit()
                    cur.close()
                    return rows
                except Exception:
                    # соединение с Postgres могло отвалиться — переподключаемся
                    try:
                        if self.conn is not None:
                            self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
                    if attempt == 2:
                        raise
                    time.sleep(1)

    # ---------- users ----------
    def ensure_user(self, user_id, chat_id):
        rows = self.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,), fetch=True)
        if not rows:
            self.execute("INSERT INTO users (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
        else:
            self.execute("UPDATE users SET chat_id=? WHERE user_id=?", (chat_id, user_id))

    def get_user(self, user_id):
        rows = self.execute(
            "SELECT user_id, chat_id, kcal_goal, report_hour, last_report FROM users WHERE user_id=?",
            (user_id,), fetch=True)
        if not rows:
            return None
        r = rows[0]
        return {"user_id": r[0], "chat_id": r[1], "kcal_goal": r[2], "report_hour": r[3], "last_report": r[4]}

    def all_users(self):
        rows = self.execute("SELECT user_id, chat_id, kcal_goal, report_hour, last_report FROM users", fetch=True)
        return [{"user_id": r[0], "chat_id": r[1], "kcal_goal": r[2], "report_hour": r[3], "last_report": r[4]} for r in rows]

    def set_user(self, user_id, **kw):
        for k, v in kw.items():
            assert k in ("kcal_goal", "report_hour", "last_report")
            self.execute(f"UPDATE users SET {k}=? WHERE user_id=?", (v, user_id))

    # ---------- связь с JARVIS (в базе только хэш кода) ----------
    def set_jarvis_key(self, user_id, key_hash):
        self.execute("DELETE FROM jarvis_links WHERE user_id=?", (user_id,))
        if key_hash:
            self.execute("INSERT INTO jarvis_links (user_id, key_hash, created_at) VALUES (?, ?, ?)",
                         (user_id, key_hash, int(time.time())))

    def user_by_jarvis_key(self, key_hash):
        rows = self.execute("SELECT user_id FROM jarvis_links WHERE key_hash=?", (key_hash,), fetch=True)
        return rows[0][0] if rows else None

    # ---------- entries ----------
    def add_entry(self, user_id, day, name, grams, nutr):
        cols = COLS
        qs = ", ".join("?" for _ in FIELDS)
        self.execute(
            f"INSERT INTO entries (user_id, day, name, grams, created_at, {cols}) VALUES (?, ?, ?, ?, ?, {qs})",
            (user_id, day, name, grams, int(time.time())) + tuple(float(nutr[f]) for f in FIELDS))

    def day_entries(self, user_id, day):
        cols = COLS
        rows = self.execute(
            f"SELECT id, name, grams, created_at, {cols} FROM entries WHERE user_id=? AND day=? ORDER BY id",
            (user_id, day), fetch=True)
        out = []
        for r in rows:
            e = {"id": r[0], "name": r[1], "grams": r[2], "created_at": r[3]}
            e.update(dict(zip(FIELDS, r[4:])))
            out.append(e)
        return out

    def delete_entry(self, user_id, entry_id):
        self.execute("DELETE FROM entries WHERE user_id=? AND id=?", (user_id, entry_id))

    def last_entry(self, user_id, day):
        rows = self.execute("SELECT id, name, grams FROM entries WHERE user_id=? AND day=? ORDER BY id DESC LIMIT 1",
                            (user_id, day), fetch=True)
        return rows[0] if rows else None

    def days_totals(self, user_id, days):
        out = {}
        for d in days:
            rows = self.execute("SELECT COALESCE(SUM(v_kcal),0), COALESCE(SUM(v_p),0), COALESCE(SUM(v_f),0), "
                                "COALESCE(SUM(v_c),0), COUNT(*) FROM entries WHERE user_id=? AND day=?",
                                (user_id, d), fetch=True)
            out[d] = rows[0]
        return out

    # ---------- custom foods ----------
    def add_custom(self, user_id, name, per100):
        cols = COLS
        qs = ", ".join("?" for _ in FIELDS)
        self.execute(f"INSERT INTO custom_foods (user_id, name, {cols}) VALUES (?, ?, {qs})",
                     (user_id, name) + tuple(float(per100.get(f, 0)) for f in FIELDS))

    def customs(self, user_id):
        cols = COLS
        rows = self.execute(f"SELECT id, name, {cols} FROM custom_foods WHERE user_id=? ORDER BY id",
                            (user_id,), fetch=True)
        return [{"id": f"u{r[0]}", "name": r[1], "per100": dict(zip(FIELDS, r[2:])),
                 "aliases": [r[1].lower()], "portion": None, "cat": "custom"} for r in rows]

    def delete_custom(self, user_id, cid):
        self.execute("DELETE FROM custom_foods WHERE user_id=? AND id=?", (user_id, cid))
