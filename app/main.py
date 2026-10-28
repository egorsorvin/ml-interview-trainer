from __future__ import annotations

import json
import random
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, importer, llm, settings, srs, stats
from .db import db, get_db, init_db, iso, tx, utcnow
from .planner import MODES, create_session
from .render import md, md_inline

APP_DIR = config.BASE_DIR / "app"
last_sync: importer.Report | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global last_sync
    init_db()
    with db() as conn:
        last_sync = importer.sync_files(conn)
        srs.clamp_existing_due(conn)
    yield


app = FastAPI(title="ML Trainer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["md"] = md
templates.env.filters["md_inline"] = md_inline
templates.env.filters["from_json"] = json.loads
templates.env.globals["MODES"] = MODES
# версия статики для ?v= в ссылках: после обновления кода браузер не использует закэшированный старый app.js / style.css
templates.env.globals["ASSET_V"] = str(
    int(max((p.stat().st_mtime for p in (APP_DIR / "static").glob("*.*")), default=0))
)

TYPE_LABELS = {
    "mcq": "Тест",
    "multi": "Несколько верных",
    "card": "Карточка",
    "open": "Кейс",
    "code": "Код",
    "match": "Сопоставь",
    "order": "Порядок",
}
RATING_LABELS = {1: "Не вспомнил", 2: "Трудно", 3: "Хорошо", 4: "Легко"}
templates.env.globals["TYPE_LABELS"] = TYPE_LABELS
templates.env.globals["RATING_LABELS"] = RATING_LABELS


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- helpers ----------


def load_question(conn: sqlite3.Connection, qid: str) -> dict:
    row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Вопрос {qid} не найден")
    q = json.loads(row["data"])
    q.update(flagged=bool(row["flagged"]), flag_note=row["flag_note"], active=bool(row["active"]))
    return q


def load_session(conn: sqlite3.Connection, sid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise HTTPException(404, "Сессия не найдена")
    return row


# ---------- главная ----------


@app.get("/")
def index(request: Request, empty: int = 0, conn: sqlite3.Connection = Depends(get_db)):
    return render(request, "index.html", d=stats.dashboard(conn), empty=bool(empty), sync=last_sync)


@app.get("/start")
def start(mode: str = "daily", topic: str = "", tag: str = "", n: int = 0, conn: sqlite3.Connection = Depends(get_db)):
    if mode not in MODES:
        raise HTTPException(400, "Неизвестный режим")
    sid = create_session(conn, mode, topic or None, tag or None, n or None)
    if sid is None:
        return redirect("/?empty=1")
    return redirect(f"/s/{sid}")


# ---------- сессия ----------


def rating_previews(conn: sqlite3.Connection, qid: str, retry: bool) -> dict | None:
    """Интервалы под кнопками оценки; None, если ответ не повлияет на расписание (повтор ошибки или вопрос ещё не подошёл)."""
    now = utcnow()
    if retry or not srs.is_scheduled(conn, qid, iso(now)):
        return None
    return srs.preview_intervals(conn, qid, now)


@app.get("/s/{sid}")
def session_page(request: Request, sid: int, conn: sqlite3.Connection = Depends(get_db)):
    s = load_session(conn, sid)
    items = json.loads(s["items"])
    if s["idx"] >= len(items):
        return redirect(f"/s/{sid}/done")
    qid, retry = items[s["idx"]]
    q = load_question(conn, qid)
    options = match_choices = order_items = None
    if q["type"] in ("mcq", "multi"):
        options = list(enumerate(q["options"]))
        random.shuffle(options)
    elif q["type"] == "match":
        n = len(q["pairs"])
        match_choices = [(i, p["right"]) for i, p in enumerate(q["pairs"])] + [
            (n + j, t) for j, t in enumerate(q.get("extra_right") or [])
        ]
        random.shuffle(match_choices)
    elif q["type"] == "order":
        order_items = list(enumerate(q["items"]))
        while True:  # показываем заведомо не в верном порядке
            random.shuffle(order_items)
            if [i for i, _ in order_items] != sorted(i for i, _ in order_items):
                break
    previews = rating_previews(conn, qid, bool(retry)) if q["type"] in ("card", "open", "code") else None
    return render(
        request,
        "question.html",
        q=q,
        options=options,
        match_choices=match_choices,
        order_items=order_items,
        s=s,
        retry=bool(retry),
        pos=s["idx"] + 1,
        total=len(items),
        llm_on=llm.enabled() and q["type"] in ("open", "card"),
        previews=previews,
    )


@app.post("/s/{sid}/answer")
async def answer(request: Request, sid: int, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    s = load_session(conn, sid)
    items = json.loads(s["items"])
    if s["idx"] >= len(items):
        return redirect(f"/s/{sid}/done")
    qid, retry = items[s["idx"]]
    if form.get("qid") != qid:  # повторная отправка формы / устаревшая вкладка
        return redirect(f"/s/{sid}")
    q = load_question(conn, qid)
    t = q["type"]

    score = None  # None → берётся SCORE[rating]
    if t in ("mcq", "multi"):
        selected = sorted({int(x) for x in form.getlist("choice") if x.isdigit()})
        if not selected:
            return redirect(f"/s/{sid}")
        correct = {i for i, o in enumerate(q["options"]) if o.get("correct")}
        chosen = set(selected)
        if chosen == correct:
            rating = 3
        elif t == "multi" and not chosen - correct:
            rating = 2  # ничего лишнего, но не всё
        else:
            rating = 1
        payload = {"selected": selected}
    elif t == "match":
        n = len(q["pairs"])
        n_choices = n + len(q.get("extra_right") or [])
        try:
            chosen_list = [int(form.get(f"m{i}")) for i in range(n)]
        except (TypeError, ValueError):
            return redirect(f"/s/{sid}")
        if any(not 0 <= c < n_choices for c in chosen_list):
            return redirect(f"/s/{sid}")
        score = sum(c == i for i, c in enumerate(chosen_list)) / n
        rating = 3 if score == 1 else 2 if score >= 0.6 else 1
        payload = {"match": chosen_list}
    elif t == "order":
        n = len(q["items"])
        try:
            order = [int(x) for x in form.getlist("order")]
        except ValueError:
            return redirect(f"/s/{sid}")
        if sorted(order) != list(range(n)):
            return redirect(f"/s/{sid}")
        place = {item: k for k, item in enumerate(order)}
        ok_pairs = sum(1 for i in range(n) for j in range(i + 1, n) if place[i] < place[j])
        score = ok_pairs / (n * (n - 1) / 2)  # доля пар элементов в верном взаимном порядке
        rating = 3 if score == 1 else 2 if score >= 0.75 else 1
        payload = {"order": order}
    else:
        try:
            rating = int(form.get("rating", 0))
        except ValueError:
            rating = 0
        if rating not in (1, 2, 3, 4):
            raise HTTPException(400, "Нужна оценка 1–4")
        rubric_hit = sorted(int(x) for x in form.getlist("rubric") if x.isdigit())
        payload = {"text": str(form.get("text", ""))[:20000], "rubric": rubric_hit}
        frac = suggested = None
        cid = str(form.get("check_id") or "")
        check = llm.load_check(conn, int(cid), sid, qid) if cid.isdigit() else None
        if check:  # ответ проверен моделью
            payload["llm"], payload["check_id"] = check, int(cid)
            frac, suggested = check["frac"], check["suggested"]
        elif q.get("rubric"):  # ручная отметка пунктов рубрики
            frac = len(rubric_hit) / len(q["rubric"])
            suggested = srs.rating_from_fraction(frac)
        if frac is not None:
            payload["suggested"] = suggested
            if suggested == rating:  # оценку не меняли вручную → в статистику идёт реальная доля
                score = frac

    payload["retry"] = bool(retry)
    try:
        time_ms = max(0, min(int(form.get("time_ms") or 0), 3_600_000))
    except ValueError:
        time_ms = 0

    now = utcnow()
    if rating == 1 and not retry:
        items.append([qid, 1])  # ошибку ещё раз спросим в конце сессии (на расписание не влияет)
    with tx(conn):
        scheduled = not retry and srs.is_scheduled(conn, qid, iso(now))
        next_due = iso(srs.apply_review(conn, qid, rating, now)) if scheduled else None
        cur = conn.execute(
            "INSERT INTO reviews(question_id, session_id, ts, rating, score, scheduled, next_due, time_ms, answer)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                qid,
                sid,
                iso(now),
                rating,
                srs.SCORE[rating] if score is None else score,
                int(scheduled),
                next_due,
                time_ms,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.execute("UPDATE sessions SET items=?, idx=idx+1 WHERE id=?", (json.dumps(items), sid))
    return redirect(f"/r/{cur.lastrowid}")


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _llm_status(e: llm.LLMError) -> int:
    return 402 if isinstance(e, llm.BudgetExceeded) else 502


@app.post("/api/transcribe")
async def api_transcribe(request: Request, sid: int = 0, qid: str = "", conn: sqlite3.Connection = Depends(get_db)):
    """Принимает WAV (16 кГц, моно) из браузера, возвращает текст. Тело запроса — сами байты записи."""
    wav = await request.body()
    try:
        r = await llm.transcribe(conn, wav, session_id=sid or None, question_id=qid or None)
    except llm.LLMError as e:
        return _json_error(str(e), _llm_status(e))
    return {"ok": True, "text": r["text"], "seconds": r["seconds"], "cost": r["cost"]}


@app.post("/s/{sid}/check")
async def check_answer(request: Request, sid: int, conn: sqlite3.Connection = Depends(get_db)):
    """Проверка ответа моделью. Возвращает готовый HTML разбора и оценку, которую модель предлагает."""
    form = await request.form()
    s = load_session(conn, sid)
    items = json.loads(s["items"])
    if s["idx"] >= len(items):
        return _json_error("Сессия уже завершена.", 409)
    qid, retry = items[s["idx"]]
    if form.get("qid") != qid:
        return _json_error("Вопрос уже сменился: обнови страницу.", 409)
    q = load_question(conn, qid)
    if q["type"] not in ("open", "card"):
        return _json_error("Для этого типа вопроса проверка моделью не используется.")
    text = str(form.get("text", "")).strip()
    if len(text) < 3:
        return _json_error("Сначала запиши ответ голосом или напиши его в поле.")
    try:
        res = await llm.grade(conn, q, text[:20000], session_id=sid)
    except llm.LLMError as e:
        return _json_error(str(e), _llm_status(e))
    html = templates.get_template("_check_result.html").render(
        q=q, res=res, previews=rating_previews(conn, qid, bool(retry))
    )
    return {
        "ok": True,
        "html": html,
        "check_id": res["check_id"],
        "suggested": res["suggested"],
        "frac": res["frac"],
        "cost": res["cost"],
    }


@app.get("/r/{rid}")
def feedback(request: Request, rid: int, conn: sqlite3.Connection = Depends(get_db)):
    r = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise HTTPException(404)
    s = load_session(conn, r["session_id"])
    q = load_question(conn, r["question_id"])
    items = json.loads(s["items"])
    next_due = None
    if r["next_due"]:
        next_due = srs.humanize_delta(datetime.fromisoformat(r["next_due"].replace("Z", "+00:00")), utcnow())
    return render(
        request,
        "feedback.html",
        q=q,
        r=r,
        a=json.loads(r["answer"]),
        s=s,
        is_last=s["idx"] >= len(items),
        left=len(items) - s["idx"],
        next_due=next_due,
    )


@app.get("/s/{sid}/done")
def done(request: Request, sid: int, conn: sqlite3.Connection = Depends(get_db)):
    s = load_session(conn, sid)
    rows = conn.execute("SELECT * FROM reviews WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    first = [x for x in rows if not json.loads(x["answer"]).get("retry")]
    missed = list(dict.fromkeys(x["question_id"] for x in rows if x["rating"] == 1))
    counts = {k: sum(1 for x in first if x["rating"] == k) for k in (1, 2, 3, 4)}
    total_ms = sum(x["time_ms"] or 0 for x in rows)
    avg = round(100 * sum(x["score"] for x in first) / len(first)) if first else None
    return render(
        request,
        "done.html",
        s=s,
        n=len(first),
        counts=counts,
        missed=missed,
        avg=avg,
        minutes=round(total_ms / 60000, 1),
    )


# ---------- вопросы: каталог, просмотр, флаги ----------


@app.get("/questions")
def questions_page(request: Request, q: str = "", topic: str = "", conn: sqlite3.Connection = Depends(get_db)):
    where, params = ["x.active=1"], []
    if q:
        where.append("x.data LIKE ?")
        params.append(f"%{q}%")
    if topic:
        where.append("(x.topic=? OR substr(x.topic,1,?)=?)")
        params += [topic, len(topic) + 1, topic + "/"]
    rows = conn.execute(
        "SELECT x.id, x.topic, x.type, x.difficulty, x.flagged, x.data, c.due, c.reps,"
        " (SELECT AVG(score) FROM reviews r WHERE r.question_id=x.id) acc"
        f" FROM questions x LEFT JOIN cards c ON c.question_id=x.id WHERE {' AND '.join(where)}"
        " ORDER BY x.topic, x.id LIMIT 300",
        params,
    ).fetchall()
    items = [
        {
            "id": r["id"],
            "topic": r["topic"],
            "type": r["type"],
            "difficulty": r["difficulty"],
            "flagged": r["flagged"],
            "reps": r["reps"] or 0,
            "due": r["due"],
            "acc": None if r["acc"] is None else round(100 * r["acc"]),
            "title": json.loads(r["data"])["question"],
        }
        for r in rows
    ]
    return render(request, "questions.html", items=items, q=q, topic=topic, now=iso(utcnow()))


@app.get("/q/{qid}")
def question_view(request: Request, qid: str, conn: sqlite3.Connection = Depends(get_db)):
    q = load_question(conn, qid)
    card = conn.execute("SELECT * FROM cards WHERE question_id=?", (qid,)).fetchone()
    history = conn.execute("SELECT * FROM reviews WHERE question_id=? ORDER BY id DESC LIMIT 15", (qid,)).fetchall()
    return render(request, "question_view.html", q=q, card=card, history=history, now=iso(utcnow()))


@app.post("/q/{qid}/flag")
async def flag(request: Request, qid: str, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    load_question(conn, qid)
    conn.execute(
        "UPDATE questions SET flagged=1, flag_note=? WHERE id=?", (str(form.get("note", "")).strip()[:2000], qid)
    )
    return redirect(str(form.get("next") or f"/q/{quote(qid)}"))


@app.post("/q/{qid}/unflag")
async def unflag(request: Request, qid: str, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    conn.execute("UPDATE questions SET flagged=0, flag_note=NULL WHERE id=?", (qid,))
    return redirect(str(form.get("next") or "/flagged"))


@app.get("/flagged")
def flagged_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT id, topic, data, flag_note FROM questions WHERE flagged=1 AND active=1 ORDER BY topic, id"
    ).fetchall()
    items = [{"id": r["id"], "topic": r["topic"], "note": r["flag_note"], "q": json.loads(r["data"])} for r in rows]
    return render(request, "flagged.html", items=items)


# ---------- импорт ----------


@app.get("/import")
def import_page(request: Request):
    return render(request, "import.html", report=last_sync, text="", source="scan")


@app.post("/import")
async def import_post(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    text = str(form.get("text", ""))
    report = (
        importer.import_text(conn, text) if text.strip() else importer.Report(errors=["Пусто: вставь YAML с вопросами"])
    )
    # при ошибках оставляем текст в поле, чтобы можно было поправить
    return render(request, "import.html", report=report, text=text if report.errors else "", source="paste")


@app.post("/import/rescan")
def import_rescan(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    global last_sync
    last_sync = importer.sync_files(conn)
    return render(request, "import.html", report=last_sync, text="", source="scan")


# ---------- настройки ----------


@app.get("/settings")
def settings_page(request: Request, saved: str = "", conn: sqlite3.Connection = Depends(get_db)):
    return render(
        request,
        "settings.html",
        topics=stats.topics(conn),
        paused=set(settings.paused_topics(conn)),
        saved=saved,
        new_per_day=config.NEW_PER_DAY,
        srs=settings.srs_params(conn),
        presets=config.SRS_PRESETS,
        llm_cfg=settings.llm_cfg(conn),
        key_set=llm.enabled(),
        spend=llm.month_spend(conn),
    )


@app.post("/settings")
async def settings_save(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    enabled = {str(x) for x in form.getlist("enabled")}
    known = {g["name"] for g in stats.topics(conn)}
    old = set(settings.paused_topics(conn))
    # на паузе: все известные темы без галочки + ранее отключённые, которых сейчас нет в базе (чтобы пауза не терялась)
    settings.set_paused_topics(conn, (known - enabled) | (old - known))
    return redirect("/settings?saved=topics")


@app.post("/settings/srs")
async def settings_srs(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    try:
        retention = float(str(form.get("retention", "")).replace(",", "."))
        cap = int(form.get("cap", ""))
    except ValueError:
        return redirect("/settings?saved=srs_bad")
    settings.set_srs(conn, retention, cap)
    srs.clamp_existing_due(conn)
    return redirect("/settings?saved=srs")


@app.post("/settings/llm")
async def settings_llm(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    try:
        limit = float(str(form.get("monthly_limit", "")).replace(",", "."))
    except ValueError:
        limit = None
    settings.set_llm_cfg(
        conn,
        stt_model=str(form.get("stt_model", "")),
        grader_model=str(form.get("grader_model", "")),
        monthly_limit=limit,
        deny_collection=form.get("deny_collection") == "on",
    )
    return redirect("/settings?saved=llm")


@app.post("/settings/llm/test")
async def settings_llm_test(conn: sqlite3.Connection = Depends(get_db)):
    return await llm.ping(conn)
