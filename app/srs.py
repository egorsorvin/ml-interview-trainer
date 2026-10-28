"""Расписание повторений на FSRS (библиотека py-fsrs).

learning_steps пустые: у нас нет «повторить через 10 минут», ошибочная карточка
возвращается на следующий день, а внутри сессии ошибка ставится в хвост очереди
(см. main.answer) без влияния на расписание.

Целевая вероятность вспомнить (retention) и потолок интервала задаются режимом в настройках
(см. settings.srs_params), поэтому планировщик собирается на каждый вызов.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fsrs import Card, Rating, Scheduler

from . import config, settings
from .db import iso, utcnow

SCORE = {1: 0.0, 2: 0.5, 3: 1.0, 4: 1.0}


def get_scheduler(conn: sqlite3.Connection) -> Scheduler:
    p = settings.srs_params(conn)
    return Scheduler(
        desired_retention=p["retention"],
        learning_steps=(),
        relearning_steps=(),
        maximum_interval=p["cap"],
    )


def rating_from_fraction(f: float) -> int:
    """Доля правильного (0..1) → оценка 1..3. «Легко» (4) модель не предлагает: это решение отвечающего."""
    if f >= config.RATING_GOOD_FROM:
        return 3
    if f >= config.RATING_HARD_FROM:
        return 2
    return 1


def _uncapped_days(scheduler: Scheduler, stability: float) -> float:
    """Интервал, который FSRS назначил бы по этой прочности без потолка режима (та же формула, что в библиотеке)."""
    decay = -scheduler.parameters[20]
    factor = 0.9 ** (1 / decay) - 1
    return stability / factor * (scheduler.desired_retention ** (1 / decay) - 1)


def growth_limit_days(before: Card, rating: int) -> int | None:
    """Максимальный следующий интервал (дней) после этого ответа, либо None для «Не вспомнил»."""
    if rating == 1:
        return None
    if before.last_review is None:  # новая карточка
        return config.FIRST_INTERVAL_LIMIT[rating]
    prev = max(1.0, (before.due - before.last_review).total_seconds() / 86400)  # запланированный интервал
    grown = int(prev * config.GROWTH_LIMIT[rating] + 0.5)
    return max(grown, int(prev + 0.5) + config.HARD_MIN_STEP_DAYS) if rating == 2 else max(1, grown)


def review(scheduler: Scheduler, before: Card, rating: int, now: datetime) -> Card:
    """Один ответ: FSRS плюс ограничение роста. Прочность подгоняется под фактический интервал, чтобы не копился
    скрытый запас (раньше из-за него «Трудно» после паузы отправляло вопрос к потолку)."""
    card, _ = scheduler.review_card(Card.from_json(before.to_json()), Rating(rating), now)
    limit = growth_limit_days(before, rating)
    if limit is None:
        return card
    if (card.due - now).total_seconds() / 86400 > limit + 1e-6:  # фактическая дата, включая случайный разброс FSRS
        uncapped = _uncapped_days(scheduler, card.stability)
        if uncapped > limit:
            card.stability *= limit / uncapped
        card.due = now + timedelta(days=limit)
    return card


def apply_review(conn: sqlite3.Connection, qid: str, rating: int, now: datetime) -> datetime:
    """Записывает повторение в карточку (создаёт её при первом разе). Возвращает новую дату due."""
    row = conn.execute("SELECT fsrs, reps, lapses FROM cards WHERE question_id=?", (qid,)).fetchone()
    card = review(get_scheduler(conn), Card.from_json(row["fsrs"]) if row else Card(), rating, now)
    if row is None:
        conn.execute(
            "INSERT INTO cards(question_id, fsrs, due, reps, lapses, created_at, last_review) VALUES(?,?,?,?,?,?,?)",
            (qid, card.to_json(), iso(card.due), 1, 0, iso(now), iso(now)),
        )
    else:
        lapses = row["lapses"] + (1 if rating == 1 else 0)
        conn.execute(
            "UPDATE cards SET fsrs=?, due=?, reps=?, lapses=?, last_review=? WHERE question_id=?",
            (card.to_json(), iso(card.due), row["reps"] + 1, lapses, iso(now), qid),
        )
    return card.due


def preview_intervals(conn: sqlite3.Connection, qid: str, now: datetime) -> dict[int, str]:
    """Через сколько вернётся вопрос при каждой из оценок (приблизительно: FSRS добавляет небольшой разброс)."""
    row = conn.execute("SELECT fsrs FROM cards WHERE question_id=?", (qid,)).fetchone()
    scheduler = get_scheduler(conn)
    before = Card.from_json(row["fsrs"]) if row else Card()
    return {r: short_interval(review(scheduler, before, r, now).due - now) for r in (1, 2, 3, 4)}


def clamp_existing_due(conn: sqlite3.Connection) -> int:
    """Подтягивает уже назначенные сроки под потолок текущего режима (идемпотентно, при старте).

    Если потолок увеличили, ничего не меняется: сроки сами вырастут при следующих повторениях.
    """
    cap = settings.srs_params(conn)["cap"]
    limit = utcnow() + timedelta(days=cap)
    rows = conn.execute("SELECT question_id, fsrs FROM cards WHERE due>?", (iso(limit),)).fetchall()
    for r in rows:
        card = Card.from_json(r["fsrs"])
        card.due = limit
        conn.execute("UPDATE cards SET due=?, fsrs=? WHERE question_id=?", (iso(limit), card.to_json(), r["question_id"]))
    return len(rows)


def is_scheduled(conn: sqlite3.Connection, qid: str, now_iso: str) -> bool:
    """Влияет ли этот ответ на расписание: только для новых и наступивших карточек."""
    row = conn.execute("SELECT due FROM cards WHERE question_id=?", (qid,)).fetchone()
    return row is None or row["due"] <= now_iso


def short_interval(delta: timedelta) -> str:
    days = delta.total_seconds() / 86400
    if days < 1.5:
        return "1 дн."
    if days < 60:
        return f"{round(days)} дн."
    if days < 365:
        return f"{round(days / 30)} мес."
    return f"{days / 365:.1f} г."


def humanize_delta(due: datetime, now: datetime) -> str:
    days = (due - now).total_seconds() / 86400
    if days < 1.5:
        return "завтра"
    if days < 30:
        return f"через {round(days)} дн."
    if days < 365:
        return f"через {round(days / 30)} мес."
    return f"через {days / 365:.1f} г."
