"""Данные для главной страницы и статистики."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import config, settings
from .db import iso, local_day_start, utcnow
from .planner import new_budget

ACTIVE = "q.active=1 AND q.flagged=0"


def _streak(conn: sqlite3.Connection) -> int:
    days = {date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT date(ts,'localtime') FROM reviews WHERE ts>=?", (local_day_start(-400),))}
    d = date.today()
    if d not in days:
        d -= timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= timedelta(days=1)
    return n


def _activity(conn: sqlite3.Connection, days: int = 14) -> list[dict]:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT date(ts,'localtime'), COUNT(*) FROM reviews WHERE ts>=? GROUP BY 1", (local_day_start(-(days - 1)),))}
    out = []
    for i in range(days - 1, -1, -1):
        d = date.today() - timedelta(days=i)
        out.append({"date": d, "label": d.strftime("%d.%m"), "n": counts.get(d.isoformat(), 0)})
    peak = max((x["n"] for x in out), default=0) or 1
    for x in out:
        x["h"] = round(100 * x["n"] / peak)
    return out


def topics(conn: sqlite3.Connection) -> list[dict]:
    """Дерево тем: верхний уровень → подтемы, с числом вопросов, due и точностью за 30 дней."""
    now = iso(utcnow())
    rows = conn.execute(
        "SELECT q.topic, COUNT(*) total, COUNT(c.question_id) learned,"
        " COALESCE(SUM(c.due<=?),0) due"
        " FROM questions q LEFT JOIN cards c ON c.question_id=q.id"
        f" WHERE {ACTIVE} GROUP BY q.topic ORDER BY q.topic", (now,)).fetchall()
    acc = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT q.topic, AVG(r.score), COUNT(*) FROM reviews r JOIN questions q ON q.id=r.question_id"
        " WHERE r.ts>=? GROUP BY q.topic", (local_day_start(-30),))}

    groups: dict[str, dict] = {}
    for r in rows:
        top = r["topic"].split("/")[0]
        g = groups.setdefault(top, {"name": top, "total": 0, "learned": 0, "due": 0, "sum": 0.0, "n": 0, "children": []})
        a_avg, a_n = acc.get(r["topic"], (None, 0))
        g["total"] += r["total"]
        g["learned"] += r["learned"]
        g["due"] += r["due"]
        if a_n:
            g["sum"] += a_avg * a_n
            g["n"] += a_n
        if r["topic"] != top:
            g["children"].append({
                "name": r["topic"], "total": r["total"], "learned": r["learned"], "due": r["due"],
                "acc": None if not a_n else round(100 * a_avg), "n": a_n,
            })
    out = []
    for g in groups.values():
        g["acc"] = round(100 * g["sum"] / g["n"]) if g["n"] else None
        out.append(g)
    return out


def dashboard(conn: sqlite3.Connection) -> dict:
    now = iso(utcnow())
    total = conn.execute(f"SELECT COUNT(*) FROM questions q WHERE {ACTIVE}").fetchone()[0]
    learned = conn.execute(
        f"SELECT COUNT(*) FROM cards c JOIN questions q ON q.id=c.question_id WHERE {ACTIVE}").fetchone()[0]
    # «к повторению» и «новых» считаем ровно по тем же правилам, что и ежедневная сессия (без тем на паузе)
    extra, extra_params = settings.exclude_sql(conn)
    extra = f" AND {extra}" if extra else ""
    due = conn.execute(
        f"SELECT COUNT(*) FROM cards c JOIN questions q ON q.id=c.question_id WHERE {ACTIVE} AND c.due<=?{extra}",
        (now, *extra_params)).fetchone()[0]
    unlearned = conn.execute(
        f"SELECT COUNT(*) FROM questions q LEFT JOIN cards c ON c.question_id=q.id"
        f" WHERE {ACTIVE} AND c.question_id IS NULL{extra}", extra_params).fetchone()[0]
    today = conn.execute("SELECT COUNT(*), COALESCE(AVG(score),0) FROM reviews WHERE ts>=?",
                         (local_day_start(),)).fetchone()
    weak = conn.execute(
        "WITH r AS (SELECT question_id, score, ROW_NUMBER() OVER (PARTITION BY question_id ORDER BY id DESC) rn"
        " FROM reviews) SELECT COUNT(*) FROM (SELECT r.question_id FROM r JOIN questions q ON q.id=r.question_id"
        f" WHERE r.rn<=5 AND {ACTIVE} GROUP BY r.question_id HAVING AVG(r.score)<?)",
        (config.WEAK_THRESHOLD,)).fetchone()[0]
    flagged = conn.execute("SELECT COUNT(*) FROM questions WHERE active=1 AND flagged=1").fetchone()[0]
    return {
        "total": total,
        "learned": learned,
        "due": due,
        "new_available": min(new_budget(conn), unlearned),
        "paused": settings.paused_topics(conn),
        "today_n": today[0],
        "today_acc": round(100 * today[1]) if today[0] else None,
        "streak": _streak(conn),
        "activity": _activity(conn),
        "topics": topics(conn),
        "weak": weak,
        "flagged": flagged,
        "new_per_day": config.NEW_PER_DAY,
    }
