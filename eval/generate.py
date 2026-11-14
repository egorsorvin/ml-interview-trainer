"""Пробная генерация оценочного набора: карточки сценариев → ответы → прогон проверяющей модели v0.

    python eval/generate.py cards              # карточки сценариев (детерминированно, без вызовов API)
    python eval/generate.py answers --limit 2  # сгенерировать ответы для ещё не сделанных карточек
    python eval/generate.py judge              # прогнать проверяющую модель (текущий промпт приложения)

Повторный запуск продолжает с места остановки. Всё пишется в eval/trial/*.jsonl, включая промпты и сырые ответы моделей.
"""

import argparse
import asyncio
import json
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm  # noqa: E402  (импорт подхватывает .env)
from app.schema import parse_text  # noqa: E402

OUT = ROOT / "eval" / "trial"
CARDS, ANSWERS, JUDGE = OUT / "cards.jsonl", OUT / "answers.jsonl", OUT / "judge_v0.jsonl"

SEED = 20261114
BUDGET_USD = 1.0  # общий предел на пробную генерацию: генерация + проверка
QUESTION_IDS = [
    "la-mcoll-005",
    "mlm-pr-003",
    "mlv-diag-015",
    "mc-chain-005",
    "mll-imb-015",
    "stprob-binom-015",
    "sthyp-errcost-012",
    "stan-conflict-014",
    "dlo-warm-007",
    "np-debug-014",
]
GENERATORS = ["openai/gpt-6.1-terra", "google/gemini-4-hydrogen"]
JUDGE_MODEL = llm.config.DEFAULT_GRADER_MODEL

SCENARIOS = {
    1: "сильный: все тезисы раскрыты",
    2: "слабый: общие слова, тезисы не раскрыты",
    3: "смешанный",
    4: "смешанный",
    5: "смешанный",
    6: "смешанный + одна фактическая ошибка",
}
STATUS_RU = {"hit": "раскрыт", "partial": "раскрыт частично", "miss": "не раскрыт"}


# ---------- данные ----------


def load_questions() -> dict:
    bank = {}
    for p in sorted((ROOT / "questions").rglob("*.yaml")):
        if "demo" in p.parts or p.name.startswith(("_", ".")):
            continue
        res = parse_text(p.read_text(encoding="utf-8"), p.name)
        bank.update({q.id: q for q in res.questions})
    return bank


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def spent() -> float:
    return sum(r.get("cost", 0.0) for path in (ANSWERS, JUDGE) for r in read_jsonl(path))


# ---------- карточки ----------


ONE_OF = re.compile(r"хотя бы (по )?од(ин|ну|но|ним|ному)\b", re.I)


def mixed_statuses(rubric: list[str], rng: random.Random) -> list[str]:
    """Случайные статусы: хотя бы один «частично» и хотя бы один полный или отсутствующий тезис.

    У тезиса с мерой «хотя бы один» нет частичного раскрытия: назвать вариант из списка — уже раскрыть тезис,
    а расплывчатое «направление» читается как угодно. Такие тезисы получают только «раскрыт» или «не раскрыт».
    """
    while True:
        # «частично» — самый трудный класс, и в сильном и слабом сценариях его нет: в смешанных даём ему больший вес
        st = [
            rng.choice(["hit", "miss"]) if ONE_OF.search(t) else rng.choices(["hit", "partial", "miss"], [1, 2, 1])[0]
            for t in rubric
        ]
        if "partial" in st and ({"hit", "miss"} & set(st)):
            return st


def build_cards() -> None:
    bank = load_questions()
    rng = random.Random(SEED)
    cards = []
    for qi, qid in enumerate(QUESTION_IDS):
        q = bank[qid]
        n = len(q.rubric)
        # генератор: у чётных вопросов первому достаются сценарии 1, 3, 5, у нечётных — 2, 4, 6,
        # чтобы тип сценария не смешивался с генератором
        gen_of = {s: GENERATORS[(s + qi + 1) % 2] for s in SCENARIOS}
        for s in SCENARIOS:
            statuses = ["hit"] * n if s == 1 else ["miss"] * n if s == 2 else mixed_statuses(q.rubric, rng)
            cards.append(
                {
                    "card_id": f"{qid}#s{s}",
                    "question_id": qid,
                    "scenario": s,
                    "scenario_name": SCENARIOS[s],
                    "generator": gen_of[s],
                    "factual_error": s == 6,
                    "statuses": statuses,
                }
            )
    OUT.mkdir(parents=True, exist_ok=True)
    CARDS.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cards), encoding="utf-8")
    print(f"карточек: {len(cards)} → {CARDS.relative_to(ROOT)}")


# ---------- генерация ответов ----------

GEN_SYSTEM = """Ты помогаешь собрать оценочный набор для системы, которая проверяет устные ответы на собеседованиях \
по машинному обучению. Тебе дают вопрос, чек-лист тезисов и карточку сценария: для каждого тезиса указано, должен ли \
он быть в ответе раскрыт, раскрыт частично или не раскрыт. Напиши ответ кандидата, который в точности соответствует \
карточке.

Что значит каждый статус:
- раскрыт — мысль тезиса названа по существу и верно;
- раскрыт частично — если тезис из нескольких частей («назвал X и объяснил Y»), назови одну часть и пропусти \
остальные; если в тезисе мера от двух («хотя бы два из…»), назови меньше, чем требуется, но не ноль; если тезис \
простой — задень мысль верно, но без сути: назови термин без объяснения;
- не раскрыт — мысли тезиса в ответе нет совсем. Для тезиса с мерой «хотя бы один» не называй ни одного варианта \
из списка и не намекай на них.

Требования к ответу:
1. Это ответ, сказанный вслух: от первого лица, связный рассказ без списков и разметки. Длина 60–250 слов: \
сильный ответ длиннее, слабый короче.
2. Раскрытые тезисы раскрывай верно по существу, своими словами; не повторяй формулировки тезисов дословно.
3. Не добавляй мыслей из нераскрытых тезисов даже намёком. Посторонние рассуждения, не относящиеся к чек-листу, \
допустимы.
4. Если в карточке указана фактическая ошибка: вставь одно уверенное неверное утверждение по теме вопроса, \
не относящееся к раскрытым тезисам (например, неверный знак, перепутанное понятие).

Верни только JSON без пояснений и без markdown-ограждения:
{"answer": "текст ответа", "theses": [{"n": 1, "status": "hit|partial|miss", "said": "что именно сказано \
по тезису, а для частичного — что пропущено"}], "factual_error": "неверное утверждение или null"}"""


def gen_user_message(q, card: dict) -> str:
    rubric = "\n".join(
        f"{i}. {t} — {STATUS_RU[s]}" for i, (t, s) in enumerate(zip(q.rubric, card["statuses"], strict=True), 1)
    )
    flags = [
        f"Сценарий: {card['scenario_name']}.",
        "Фактическая ошибка: да, одна." if card["factual_error"] else "Фактическая ошибка: нет.",
    ]
    return "\n\n".join(
        [
            f"<question>\n{q.question}\n</question>",
            f"<card>\n{rubric}\n\n" + "\n".join(flags) + "\n</card>",
        ]
    )


def check_generation(out: dict, card: dict) -> str | None:
    if not isinstance(out.get("answer"), str) or len(out["answer"].split()) < 20:
        return "нет текста ответа или он короче 20 слов"
    statuses = [t.get("status") for t in out.get("theses") or []]
    if statuses != card["statuses"]:
        return f"статусы в theses {statuses} не совпадают с карточкой {card['statuses']}"
    return None


async def generate_one(q, card: dict) -> dict:
    messages = [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": gen_user_message(q, card)}]
    total, estimated, raw, err, out = 0.0, False, [], None, None
    for _attempt in range(2):
        content, cost, est = await llm._chat(messages, card["generator"], max_tokens=4000)
        total += cost
        estimated |= est
        raw.append(content)
        try:
            out = llm._extract_json(content)
            err = check_generation(out, card)
        except (ValueError, json.JSONDecodeError) as e:
            err = str(e)
        if err is None:
            break
        messages += [
            {"role": "assistant", "content": content[:6000]},
            {"role": "user", "content": f"Ответ не подходит: {err}. Исправь и верни только JSON по схеме."},
        ]
    return {
        "card_id": card["card_id"],
        "question_id": card["question_id"],
        "generator": card["generator"],
        "ok": err is None,
        "error": err,
        "answer": (out or {}).get("answer"),
        "theses": (out or {}).get("theses"),
        "factual_error": (out or {}).get("factual_error"),
        "cost": total,
        "cost_estimated": estimated,
        "prompt": {"system": GEN_SYSTEM, "user": messages[1]["content"]},
        "raw": raw,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def run_answers(limit: int | None, only: list[str] | None = None) -> None:
    bank = load_questions()
    done = {r["card_id"] for r in read_jsonl(ANSWERS) if r["ok"]}
    todo = [c for c in read_jsonl(CARDS) if c["card_id"] not in done and (not only or c["card_id"] in only)][:limit]
    for card in todo:
        if spent() >= BUDGET_USD:
            print(f"достигнут предел ${BUDGET_USD}, остановка")
            return
        row = await generate_one(bank[card["question_id"]], card)
        append_jsonl(ANSWERS, row)
        if row["cost_estimated"]:
            # запасной расчёт в app.llm — по ценам проверяющей модели, для генераторов он занижен; бюджет не проконтролировать
            sys.exit("OpenRouter не вернул стоимость вызова — остановка, бюджет нельзя проконтролировать")
        mark = "ok" if row["ok"] else f"ОШИБКА: {row['error']}"
        print(f"{card['card_id']:24s} {card['generator']:26s} ${row['cost']:.4f}  {mark}")
    print(f"потрачено всего: ${spent():.4f}")


# ---------- проверяющая модель v0 ----------


async def run_judge(limit: int | None) -> None:
    bank = load_questions()
    done = {r["card_id"] for r in read_jsonl(JUDGE)}
    todo = [a for a in read_jsonl(ANSWERS) if a["ok"] and a["card_id"] not in done][:limit]
    for a in todo:
        if spent() >= BUDGET_USD:
            print(f"достигнут предел ${BUDGET_USD}, остановка")
            return
        q = bank[a["question_id"]]
        qd = {"id": q.id, "question": q.question, "answer": q.answer, "rubric": q.rubric}
        messages = [
            {"role": "system", "content": llm.SYSTEM_PROMPT},
            {"role": "user", "content": llm._build_user_message(qd, a["answer"])},
        ]
        content, cost, est = await llm._chat(messages, JUDGE_MODEL)
        try:
            out = llm._parse_grade(content, qd)
            row = {
                "ok": True,
                "statuses": [p.status for p in out.points],
                "points": [p.model_dump() for p in out.points],
            }
            row |= {"mistakes": out.mistakes, "major_error": out.major_error}
        except Exception as e:  # noqa: BLE001 — сохраняем сбой формата как результат
            row = {"ok": False, "error": str(e)[:300]}
        row |= {"card_id": a["card_id"], "model": JUDGE_MODEL, "cost": cost, "cost_estimated": est, "raw": content}
        append_jsonl(JUDGE, row)
        print(f"{a['card_id']:24s} ${cost:.4f}  {row.get('statuses', row.get('error'))}")
    print(f"потрачено всего: ${spent():.4f}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["cards", "answers", "judge"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="+", help="card_id, например la-mcoll-005#s3")
    args = ap.parse_args()
    if args.step == "cards":
        build_cards()
    elif args.step == "answers":
        asyncio.run(run_answers(args.limit, args.only))
    else:
        asyncio.run(run_judge(args.limit))


if __name__ == "__main__":
    main()
