"""Сборка очередей сессий."""
from __future__ import annotations

import json
import random
import sqlite3

from . import config, settings
from .db import iso, local_day_start, utcnow

MODES = {
    "daily": "Ежедневная сессия",
    "topic": "Тренировка по теме",
    "weak": "Слабые места",
    "random": "Мини-собес",
}


def _where(topic: str | None, tag: str | None) -> tuple[str, list]:
    where = ["q.active=1", "q.flagged=0"]
    params: list = []
    if topic:
        where.append("(q.topic=? OR substr(q.topic,1,?)=?)")
        params += [topic, len(topic) + 1, topic + "/"]
    if tag:
        where.append("q.tags LIKE ?")
        params.append(f'%"{tag}"%')
    return " AND ".join(where), params


def new_today(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards WHERE created_at>=?", (local_day_start(),)).fetchone()[0]


def new_budget(conn: sqlite3.Connection) -> int:
    return max(0, config.NEW_PER_DAY - new_today(conn))


def build_items(conn: sqlite3.Connection, mode: str, topic: str | None, tag: str | None, n: int | None) -> list[list]:
    where, params = _where(topic, tag)
    now = iso(utcnow())
    ids: list[str]

    if mode == "daily" and not topic:
        # темы на паузе в ежедневную подборку не попадают; явный выбор темы (ссылка «повторить») паузу обходит
        extra, extra_params = settings.exclude_sql(conn)
        if extra:
            where += f" AND {extra}"
            params += extra_params

    if mode == "daily":
        due = [r[0] for r in conn.execute(
            f"SELECT q.id FROM questions q JOIN cards c ON c.question_id=q.id WHERE {where} AND c.due<=?"
            " ORDER BY c.due LIMIT ?", [*params, now, config.DUE_LIMIT])]
        new = [r[0] for r in conn.execute(
            f"SELECT q.id FROM questions q LEFT JOIN cards c ON c.question_id=q.id WHERE {where}"
            " AND c.question_id IS NULL ORDER BY RANDOM() LIMIT ?", [*params, new_budget(conn)])]
        ids = due + new
        random.shuffle(ids)  # перемешиваем, чтобы темы чередовались
    elif mode in ("topic", "random"):
        default = config.TOPIC_SESSION_N if mode == "topic" else config.DEFAULT_SESSION_N
        ids = [r[0] for r in conn.execute(
            f"SELECT q.id FROM questions q WHERE {where} ORDER BY RANDOM() LIMIT ?", [*params, n or default])]
    elif mode == "weak":
        # средний результат по последним 5 ответам на вопрос; берём те, где ниже config.WEAK_THRESHOLD
        ids = [r[0] for r in conn.execute(
            "WITH r AS (SELECT question_id, score, ROW_NUMBER() OVER (PARTITION BY question_id ORDER BY id DESC) rn"
            " FROM reviews)"
            f" SELECT q.id FROM r JOIN questions q ON q.id=r.question_id WHERE r.rn<=5 AND {where}"
            " GROUP BY q.id HAVING AVG(r.score) < ? ORDER BY AVG(r.score) ASC, RANDOM() LIMIT ?",
            [*params, config.WEAK_THRESHOLD, n or config.TOPIC_SESSION_N])]
    else:
        raise ValueError(f"unknown mode {mode}")
    return [[qid, 0] for qid in ids]


def create_session(conn: sqlite3.Connection, mode: str, topic: str | None, tag: str | None, n: int | None) -> int | None:
    items = build_items(conn, mode, topic, tag, n)
    if not items:
        return None
    cur = conn.execute(
        "INSERT INTO sessions(mode, topic, tag, items, created_at) VALUES(?,?,?,?,?)",
        (mode, topic, tag, json.dumps(items), iso(utcnow())),
    )
    return cur.lastrowid
