"""Пользовательские настройки (таблица settings, ключ → JSON).

- paused_topics: темы верхнего уровня, исключённые из ежедневной сессии. Хранится список ОТКЛЮЧЁННЫХ,
  чтобы новые темы из импорта попадали в подборку по умолчанию.
- srs: режим повторений (retention + потолок интервала).
- llm: модели и месячный лимит расходов на проверку ответов. Сам ключ API хранится только в .env.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config

KEY_PAUSED = "paused_topics"
KEY_SRS = "srs"
KEY_LLM = "llm"


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


# ---------- темы на паузе ----------

def paused_topics(conn: sqlite3.Connection) -> list[str]:
    return sorted(get(conn, KEY_PAUSED, []))


def set_paused_topics(conn: sqlite3.Connection, topics: set[str] | list[str]) -> None:
    put(conn, KEY_PAUSED, sorted(set(topics)))


def exclude_sql(conn: sqlite3.Connection, alias: str = "q") -> tuple[str, list]:
    """Условие «тема не на паузе» для запросов по таблице вопросов. Пустая строка, если пауз нет."""
    paused = paused_topics(conn)
    if not paused:
        return "", []
    clauses = " OR ".join(f"({alias}.topic=? OR substr({alias}.topic,1,?)=?)" for _ in paused)
    params: list = []
    for t in paused:
        params += [t, len(t) + 1, t + "/"]
    return f"NOT ({clauses})", params


# ---------- режим повторений ----------

def srs_params(conn: sqlite3.Connection) -> dict:
    """{'mode': 'normal'|'intensive'|'support'|'custom', 'retention': float, 'cap': int}"""
    preset = config.SRS_PRESETS[config.DEFAULT_SRS_MODE]
    saved = get(conn, KEY_SRS, {}) or {}
    return {
        "mode": saved.get("mode", config.DEFAULT_SRS_MODE),
        "retention": float(saved.get("retention", preset["retention"])),
        "cap": int(saved.get("cap", preset["cap"])),
    }


def set_srs(conn: sqlite3.Connection, retention: float, cap: int) -> dict:
    """Сохраняет параметры; режим определяется по совпадению с пресетом, иначе custom."""
    retention = round(min(max(retention, 0.70), 0.99), 3)
    cap = int(min(max(cap, 1), 365))
    mode = "custom"
    for name, p in config.SRS_PRESETS.items():
        if abs(p["retention"] - retention) < 1e-9 and p["cap"] == cap:
            mode = name
    put(conn, KEY_SRS, {"mode": mode, "retention": retention, "cap": cap})
    return srs_params(conn)


# ---------- проверка ответов моделью ----------

def llm_cfg(conn: sqlite3.Connection) -> dict:
    saved = get(conn, KEY_LLM, {}) or {}
    return {
        "stt_model": (saved.get("stt_model") or config.DEFAULT_STT_MODEL).strip(),
        "grader_model": (saved.get("grader_model") or config.DEFAULT_GRADER_MODEL).strip(),
        "monthly_limit": float(saved.get("monthly_limit", config.DEFAULT_LLM_MONTHLY_LIMIT)),
        "deny_collection": bool(saved.get("deny_collection", False)),
    }


def set_llm_cfg(conn: sqlite3.Connection, **kw: Any) -> dict:
    cur = llm_cfg(conn)
    for k in ("stt_model", "grader_model"):
        if kw.get(k):
            cur[k] = str(kw[k]).strip()[:120]
    if kw.get("monthly_limit") is not None:
        cur["monthly_limit"] = max(0.0, float(kw["monthly_limit"]))
    if kw.get("deny_collection") is not None:
        cur["deny_collection"] = bool(kw["deny_collection"])
    put(conn, KEY_LLM, cur)
    return cur
