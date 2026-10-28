"""Формат вопросов и разбор YAML-батчей.

Один вопрос — одна запись YAML. Описание формата для людей: docs/FORMAT.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
# Только ограда БЕЗ отступа (^```) — так это настоящая обёртка ```yaml ... ``` вокруг всего ответа чата,
# а не пример кода внутри поля вопроса (question/explanation/...), который всегда идёт с отступом как часть
# YAML block-scalar. Без этого якоря такой вложенный пример резался бы как отдельная граница блока.
FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\n(.*?)\n^```[ \t]*$", re.S | re.M)

QType = Literal["mcq", "multi", "card", "open", "code", "match", "order"]


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    correct: bool = False
    why: str | None = None

    @field_validator("text", "why", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return v if v is None or isinstance(v, str) else str(v)


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: str
    right: str

    @field_validator("left", "right", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return v if isinstance(v, str) else str(v)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    topic: str
    type: QType
    question: str
    difficulty: int = Field(2, ge=1, le=3)
    tags: list[str] = []
    # mcq / multi
    options: list[Option] | None = None
    # card / open: эталонный ответ. open: ещё и rubric
    answer: str | None = None
    rubric: list[str] | None = None
    explanation: str | None = None
    # match: пары «левое — правое», extra_right — лишние правые варианты. order: items в ВЕРНОМ порядке
    pairs: list[Pair] | None = None
    extra_right: list[str] | None = None
    items: list[str] | None = None
    # code (пока самопроверка по эталону; автотесты — позже)
    language: str | None = None
    starter: str | None = None
    solution: str | None = None
    tests: str | None = None

    @field_validator(
        "id", "topic", "question", "answer", "explanation", "language", "starter", "solution", "tests",
        mode="before",
    )
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return v if v is None or isinstance(v, str) else str(v)

    @field_validator("tags", "rubric", "extra_right", "items", mode="before")
    @classmethod
    def _list_of_str(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [x if isinstance(x, str) else str(x) for x in v]
        return v

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip()
        if not ID_RE.match(v):
            raise ValueError("id: латиница в нижнем регистре, цифры, . _ - ; длина 3–64 (пример: la-eig-001)")
        return v

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not TOPIC_RE.match(v):
            raise ValueError("topic: путь из сегментов a-z0-9_- через / (пример: linalg/eigen)")
        return v

    @field_validator("question", "answer", "explanation", "starter", "solution", "tests")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip("\n").rstrip()
        return v or None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().lower()
            if t and t not in out:
                out.append(t)
        return out

    @model_validator(mode="after")
    def _check_type(self) -> "Question":
        if not self.question:
            raise ValueError("question: пусто")
        t = self.type
        if t in ("mcq", "multi"):
            opts = self.options or []
            lo, hi = (2, 8) if t == "mcq" else (3, 8)
            if not lo <= len(opts) <= hi:
                raise ValueError(f"{t}: нужно {lo}–{hi} вариантов в options, сейчас {len(opts)}")
            n_ok = sum(o.correct for o in opts)
            if t == "mcq" and n_ok != 1:
                raise ValueError(f"mcq: ровно один вариант с correct: true, сейчас {n_ok}")
            if t == "multi" and not 1 <= n_ok <= len(opts) - 1:
                raise ValueError(f"multi: правильных вариантов должно быть от 1 до {len(opts) - 1}, сейчас {n_ok}")
        elif t == "card":
            if not self.answer:
                raise ValueError("card: нужен answer")
        elif t == "open":
            if not self.answer:
                raise ValueError("open: нужен answer (эталонный ответ)")
            if not self.rubric or not 2 <= len(self.rubric) <= 10:
                raise ValueError("open: rubric — список из 2–10 ключевых пунктов ответа")
        elif t == "code":
            if not self.solution:
                raise ValueError("code: нужен solution (эталонное решение)")
            if not self.language:
                self.language = "python"
        elif t == "match":
            pairs = self.pairs or []
            if not 3 <= len(pairs) <= 8:
                raise ValueError(f"match: нужно 3–8 пар в pairs, сейчас {len(pairs)}")
            extra = [x.strip() for x in (self.extra_right or [])]
            if len(extra) > 4:
                raise ValueError("match: extra_right — не больше 4 лишних вариантов")
            lefts = [p.left.strip() for p in pairs]
            rights = [p.right.strip() for p in pairs] + extra
            if any(not x for x in lefts + rights):
                raise ValueError("match: пустой текст в pairs/extra_right")
            if len({x.casefold() for x in lefts}) != len(lefts):
                raise ValueError("match: левые части пар должны быть уникальны")
            if len({x.casefold() for x in rights}) != len(rights):
                raise ValueError("match: правые части (включая extra_right) должны быть уникальны, иначе пары неоднозначны")
            self.pairs = [Pair(left=a, right=b) for a, b in zip(lefts, rights)]
            self.extra_right = extra or None
        elif t == "order":
            items = [x.strip() for x in (self.items or [])]
            if not 3 <= len(items) <= 8:
                raise ValueError(f"order: нужно 3–8 элементов в items (в верном порядке), сейчас {len(items)}")
            if any(not x for x in items) or len({x.casefold() for x in items}) != len(items):
                raise ValueError("order: элементы items должны быть непустыми и уникальными")
            self.items = items
        return self


@dataclass
class ParseResult:
    questions: list[Question] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bad_ids: set[str] = field(default_factory=set)  # id записей, которые не прошли валидацию
    fatal: bool = False  # YAML не разобрался целиком


def _fmt_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc and not msg.startswith(loc.split(".")[0] + ":") else msg)
    return "; ".join(parts)


ESCAPE_HINT = (
    "В двойных кавычках YAML обратный слэш служебный. Формулы (\\theta, \\frac, \\cos…) пиши в одинарных "
    "кавычках '…' или в блоке | — тогда слэши остаются как есть."
)


def _escape_traps(block: str) -> dict[int, list[int]]:
    """№ записи → строки, где в "двойных кавычках" стоит \\n, \\t, \\f, \\b, \\a, \\r, \\v.

    YAML молча превращает их в управляющие символы: "\\theta" → TAB+"heta", "\\frac" → FF+"rac".
    """
    try:
        root = yaml.compose(block)
    except yaml.YAMLError:
        return {}
    if isinstance(root, yaml.MappingNode):
        for k, v in root.value:
            if isinstance(k, yaml.ScalarNode) and k.value == "questions":
                root = v
                break
    if not isinstance(root, yaml.SequenceNode):
        return {}
    out: dict[int, list[int]] = {}
    for i, item in enumerate(root.value, 1):
        lines: set[int] = set()
        stack = [item]
        while stack:
            n = stack.pop()
            if isinstance(n, yaml.ScalarNode):
                if n.style == '"':
                    raw = block[n.start_mark.index:n.end_mark.index]
                    if any(m.group(1) in "abfnrtv" for m in re.finditer(r"\\(.)", raw, re.S)):
                        lines.add(n.start_mark.line + 1)
            elif isinstance(n, yaml.SequenceNode):
                stack.extend(n.value)
            elif isinstance(n, yaml.MappingNode):
                for k, v in n.value:
                    stack += [k, v]
        if lines:
            out[i] = sorted(lines)
    return out


def _yaml_blocks(text: str) -> list[str]:
    """Если LLM обернул ответ в ```yaml ...```, берём содержимое всех таких блоков."""
    blocks = FENCE_RE.findall(text)
    return blocks if blocks else [text]


def parse_text(text: str, label: str = "текст") -> ParseResult:
    res = ParseResult()
    for bi, block in enumerate(_yaml_blocks(text), 1):
        where = label if bi == 1 and len(_yaml_blocks(text)) == 1 else f"{label} (блок {bi})"
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            pos = f" (строка {mark.line + 1}, столбец {mark.column + 1})" if mark else ""
            problem = getattr(e, "problem", None) or str(e)
            hint = f" {ESCAPE_HINT}" if "escape" in problem else ""
            res.errors.append(f"{where}: ошибка YAML{pos}: {problem}.{hint}")
            res.fatal = True
            continue
        if isinstance(data, dict) and "questions" in data:
            data = data["questions"]
        if data is None:
            continue
        if not isinstance(data, list):
            res.errors.append(f"{where}: на верхнем уровне должен быть список вопросов (или ключ questions:)")
            res.fatal = True
            continue
        traps = _escape_traps(block)
        for i, item in enumerate(data, 1):
            if not isinstance(item, dict):
                res.errors.append(f"{where}, запись №{i}: ожидался объект с полями, а не {type(item).__name__}")
                continue
            qid = item.get("id")
            name = f"{where}, запись №{i}" + (f" [{qid}]" if qid else "")
            if i in traps:
                res.errors.append(
                    f"{name}: в строках {traps[i]} в двойных кавычках есть \\n, \\t, \\f, \\b… — YAML превратит их "
                    f"в управляющие символы и испортит формулу. {ESCAPE_HINT}")
                if qid:
                    res.bad_ids.add(str(qid))
                continue
            try:
                res.questions.append(Question.model_validate(item))
            except ValidationError as e:
                res.errors.append(f"{name}: {_fmt_validation_error(e)}")
                if qid:
                    res.bad_ids.add(str(qid))
    return res
