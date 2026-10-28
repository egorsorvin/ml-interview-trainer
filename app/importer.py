"""Синхронизация вопросов: папка questions/*.yaml  →  SQLite.

Источник правды — файлы. Прогресс (карточки, история) хранится в БД по id вопроса,
поэтому правка текста вопроса в YAML прогресс не сбрасывает.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from . import config
from .db import iso, tx, utcnow
from .schema import Question, parse_text


@dataclass
class Report:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    files: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    saved_file: str | None = None


def _rel(path) -> str:
    try:
        return path.relative_to(config.QUESTIONS_DIR).as_posix()
    except ValueError:
        return str(path)


def _files():
    root = config.QUESTIONS_DIR
    if not root.exists():
        return []
    found = [
        p for p in list(root.rglob("*.yaml")) + list(root.rglob("*.yml"))
        if not p.name.startswith(("_", "."))
    ]
    return sorted(found, key=lambda p: (p.stat().st_mtime, str(p)))  # при дубликатах id побеждает более свежий файл


def _upsert(conn: sqlite3.Connection, q: Question, source: str, update_source: bool = True) -> str:
    data = q.model_dump(exclude_none=True)
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    now = iso(utcnow())
    row = conn.execute("SELECT hash FROM questions WHERE id=?", (q.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO questions(id, topic, type, difficulty, tags, data, hash, source, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (q.id, q.topic, q.type, q.difficulty, json.dumps(q.tags, ensure_ascii=False),
             json.dumps(data, ensure_ascii=False), digest, source, now, now),
        )
        return "added"
    if row["hash"] != digest:
        # содержимое изменилось → снимаем флаг «неверный вопрос»
        conn.execute(
            "UPDATE questions SET topic=?, type=?, difficulty=?, tags=?, data=?, hash=?, source=?,"
            " active=1, flagged=0, flag_note=NULL, updated_at=? WHERE id=?",
            (q.topic, q.type, q.difficulty, json.dumps(q.tags, ensure_ascii=False),
             json.dumps(data, ensure_ascii=False), digest, source, now, q.id),
        )
        return "updated"
    if update_source:
        conn.execute("UPDATE questions SET source=?, active=1 WHERE id=?", (source, q.id))
    else:
        conn.execute("UPDATE questions SET active=1 WHERE id=?", (q.id,))
    return "unchanged"


def sync_files(conn: sqlite3.Connection) -> Report:
    report = Report()
    seen: dict[str, tuple[Question, str]] = {}
    keep_ids: set[str] = set()
    bad_sources: set[str] = set()

    for path in _files():
        rel = _rel(path)
        report.files += 1
        res = parse_text(path.read_text(encoding="utf-8"), rel)
        report.errors += res.errors
        keep_ids |= res.bad_ids
        if res.fatal:
            bad_sources.add(rel)
        for q in res.questions:
            if q.id in seen:
                report.warnings.append(f"id {q.id} есть в {seen[q.id][1]} и в {rel} — использован {rel} (он свежее)")
            seen[q.id] = (q, rel)

    with tx(conn):
        for q, rel in seen.values():
            status = _upsert(conn, q, rel)
            setattr(report, status, getattr(report, status) + 1)

        # вопросы, которых больше нет в файлах, прячем (история остаётся)
        for row in conn.execute("SELECT id, source FROM questions WHERE active=1").fetchall():
            if row["id"] in seen or row["id"] in keep_ids or row["source"] in bad_sources:
                continue
            conn.execute("UPDATE questions SET active=0 WHERE id=?", (row["id"],))
            report.deactivated += 1
    return report


# --- вставка батча из текста (из UI) ---

class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, s: str):
    style = "|" if "\n" in s else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)


_Dumper.add_representer(str, _str_representer)


def dump_questions(questions: list[Question]) -> str:
    items = [q.model_dump(exclude_none=True, exclude_defaults=False) for q in questions]
    return yaml.dump(items, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


def import_text(conn: sqlite3.Connection, text: str) -> Report:
    report = Report()
    res = parse_text(text, "вставленный текст")
    report.errors = res.errors

    unique: dict[str, Question] = {}
    for q in res.questions:
        if q.id in unique:
            report.warnings.append(f"id {q.id} повторяется в батче — взят последний")
        unique[q.id] = q
    if not unique:
        return report

    rel = f"inbox/batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.yaml"
    to_save: list[Question] = []
    with tx(conn):
        for q in unique.values():
            status = _upsert(conn, q, rel, update_source=False)
            setattr(report, status, getattr(report, status) + 1)
            if status in ("added", "updated"):
                to_save.append(q)

        if to_save:
            path = config.QUESTIONS_DIR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_questions(to_save), encoding="utf-8")
            report.saved_file = rel
            conn.executemany("UPDATE questions SET source=? WHERE id=?", [(rel, q.id) for q in to_save])
    return report
