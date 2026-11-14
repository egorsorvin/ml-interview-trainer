"""Страница ручной проверки пробных ответов: python eval/review.py → http://127.0.0.1:8010

Проверка вслепую: карточка сценария (задуманные статусы), генератор и номер сценария не показываются, пока разметка
ответа не сохранена — иначе разметка подстраивается под задуманное. Ответы идут в перемешанном порядке.
Разметка дописывается в eval/trial/review.jsonl; при повторной разметке ответа действует последняя запись.
"""

import html
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from eval.generate import ANSWERS, CARDS, OUT, STATUS_RU, load_questions, read_jsonl  # noqa: E402

REVIEW = OUT / "review.jsonl"
app = FastAPI()
BANK = load_questions()

CSS = """
body{font:16px/1.5 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
.box{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:12px 0}
.answer{white-space:pre-wrap}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eee;padding:6px;text-align:left;vertical-align:top}
label{margin-right:12px;white-space:nowrap}.muted{color:#777;font-size:14px}
.ok{color:#2a7a2a}.bad{color:#b02a2a;font-weight:600}button{font-size:16px;padding:6px 18px}
textarea{width:100%;min-height:50px}a{color:#2a5db0}
"""


def items() -> list[dict]:
    cards = {c["card_id"]: c for c in read_jsonl(CARDS)}
    ans = {a["card_id"]: a for a in read_jsonl(ANSWERS) if a["ok"]}  # последняя удачная генерация
    rows = [{"card": cards[k], "answer": a} for k, a in ans.items() if k in cards]
    rows.sort(key=lambda r: r["card"]["card_id"])
    random.Random(7).shuffle(rows)  # порядок не выдаёт генератор и сценарий
    return rows


def reviews() -> dict:
    return {r["card_id"]: r for r in read_jsonl(REVIEW)}


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{title}</title><style>{CSS}</style>{body}")


@app.get("/")
def index():
    rows, rev = items(), reviews()
    done = sum(r["card"]["card_id"] in rev for r in rows)
    nxt = next((i for i, r in enumerate(rows) if r["card"]["card_id"] not in rev), None)
    links = "".join(
        f'<a href="/item/{i}">{i + 1}</a>{"✓" if r["card"]["card_id"] in rev else ""} ' for i, r in enumerate(rows)
    )
    start = (
        f'<p><a href="/item/{nxt}">Продолжить с №{nxt + 1} →</a></p>' if nxt is not None else "<p>Всё размечено.</p>"
    )
    return page(
        "Проверка ответов",
        f"<h1>Проверка пробных ответов</h1><p>Размечено {done} из {len(rows)}.</p>{start}<p>{links}</p>",
    )


@app.get("/item/{i}")
def item(i: int):
    rows = items()
    r = rows[i]
    q, a = BANK[r["card"]["question_id"]], r["answer"]
    prev = reviews().get(r["card"]["card_id"])
    lines = []
    for n, t in enumerate(q.rubric, 1):
        cur = prev["statuses"][n - 1] if prev else None
        radios = "".join(
            f'<label><input type=radio name="t{n}" value="{s}" required {"checked" if cur == s else ""}> {ru}</label>'
            for s, ru in STATUS_RU.items()
        )
        lines.append(f"<tr><td>{n}. {html.escape(t)}</td><td>{radios}</td></tr>")
    err_yes = "checked" if prev and prev["factual_error"] else ""
    unnat = "checked" if prev and prev["unnatural"] else ""
    note = html.escape(prev["note"]) if prev else ""
    body = f"""
<p class=muted><a href="/">← к списку</a> · ответ {i + 1} из {len(rows)}</p>
<div class=box><b>Вопрос.</b> {html.escape(q.question)}</div>
<div class=box><b>Ответ кандидата</b><div class=answer>{html.escape(a["answer"])}</div></div>
<form method=post class=box>
<p class=muted>Раскрыт — мысль названа по существу и верно; частично — названа часть или поверхностно;
не раскрыт — мысли нет или она неверна.</p>
<table>{"".join(lines)}</table>
<p><label><input type=checkbox name=factual_error {err_yes}> в ответе есть фактическая ошибка</label>
<label><input type=checkbox name=unnatural {unnat}> ответ звучит неестественно</label></p>
<p>Комментарий (необязательно):<br><textarea name=note>{note}</textarea></p>
<button>Сохранить и показать карточку</button></form>
<details class=box><summary>Эталонный ответ</summary><div class=answer>{html.escape(q.answer)}</div></details>"""
    return page(f"Ответ {i + 1}", body)


@app.post("/item/{i}")
async def save(
    i: int,
    request: Request,
    factual_error: str | None = Form(None),
    unnatural: str | None = Form(None),
    note: str = Form(""),
):
    r = items()[i]
    form = await request.form()
    n = len(BANK[r["card"]["question_id"]].rubric)
    row = {
        "card_id": r["card"]["card_id"],
        "statuses": [form[f"t{k}"] for k in range(1, n + 1)],
        "factual_error": factual_error is not None,
        "unnatural": unnatural is not None,
        "note": note.strip(),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    with REVIEW.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return RedirectResponse(f"/item/{i}/card", status_code=303)


@app.get("/item/{i}/card")
def card(i: int):
    rows = items()
    r = rows[i]
    c, a, q = r["card"], r["answer"], BANK[r["card"]["question_id"]]
    rev = reviews()[c["card_id"]]
    trs = []
    for n, (t, want, got) in enumerate(zip(q.rubric, c["statuses"], rev["statuses"], strict=True), 1):
        said = next((x.get("said", "") for x in a.get("theses") or [] if x.get("n") == n), "")
        cls = "ok" if want == got else "bad"
        trs.append(
            f"<tr><td>{n}. {html.escape(t)}<div class=muted>генератор: {html.escape(str(said))}</div></td>"
            f"<td>{STATUS_RU[want]}</td><td class={cls}>{STATUS_RU[got]}</td></tr>"
        )
    err = html.escape(str(a.get("factual_error"))) if c["factual_error"] else "не задумана"
    nxt = f'<a href="/item/{i + 1}">Следующий →</a>' if i + 1 < len(rows) else '<a href="/">К списку</a>'
    body = f"""
<p class=muted><a href="/">← к списку</a> · ответ {i + 1} из {len(rows)}</p>
<div class=box><p>Сценарий: {c["scenario_name"]} · генератор: {c["generator"]}</p>
<table><tr><th>тезис</th><th>задумано</th><th>твоя разметка</th></tr>{"".join(trs)}</table>
<p>Фактическая ошибка — задумана: {err}; отмечена тобой: {"да" if rev["factual_error"] else "нет"}</p></div>
<p><a href="/item/{i}">← исправить разметку</a> · {nxt}</p>"""
    return page("Карточка", body)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8010)
