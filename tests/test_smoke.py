import os
import re
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="trainer-test-"))
os.environ["TRAINER_DATA"] = str(_tmp / "data")
os.environ["TRAINER_QUESTIONS"] = str(_tmp / "questions")
shutil.copytree(ROOT / "questions" / "demo", _tmp / "questions" / "demo")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.render import md
from app.schema import parse_text


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_demo_file_is_valid():
    res = parse_text((ROOT / "questions" / "demo" / "demo.yaml").read_text(encoding="utf-8"), "demo")
    assert not res.errors, res.errors
    assert {q.type for q in res.questions} == {"mcq", "multi", "card", "open", "code"}
    new = parse_text((ROOT / "questions" / "demo" / "demo_new_types.yaml").read_text(encoding="utf-8"), "demo2")
    assert not new.errors, new.errors
    assert {q.type for q in new.questions} == {"match", "order"}


def test_prompt_example_is_valid():
    """Пример из промпта для чата-автора должен проходить нашу же валидацию."""
    text = (ROOT / "docs" / "PROMPT_FOR_QUESTION_CHAT.md").read_text(encoding="utf-8")
    found = re.findall(r"^[ \t]*```yaml\n(.*?)\n[ \t]*```[ \t]*$", text, re.S | re.M)
    blocks = [b for b in found if b.lstrip().startswith("- id:")]
    assert len(blocks) == 1
    res = parse_text(blocks[0], "prompt")
    assert not res.errors, res.errors
    assert {q.type for q in res.questions} == {"mcq", "multi", "card", "open", "code", "match", "order"}


def test_schema_rejects_bad_questions():
    bad = """
- id: x1
  topic: Linalg
  type: mcq
  question: q
  options:
    - {text: a, correct: true}
    - {text: b, correct: true}
- id: ok-001
  topic: a/b
  type: open
  question: q
  answer: a
  rubric: [only one]
- id: ok-002
  topic: a/b
  type: card
  question: q
  answer: a
  unknown_field: 1
"""
    res = parse_text(bad, "t")
    assert len(res.errors) == 3 and not res.questions
    assert "x1" in res.bad_ids


def test_yaml_error_and_code_fence():
    res = parse_text("- id: a\n  question: [unclosed", "t")
    assert res.fatal and "строка" in res.errors[0]
    fenced = (
        "Вот вопросы:\n```yaml\n- id: card-001\n  topic: a/b\n  type: card\n  question: q\n  answer: a\n```\nГотово."
    )
    assert len(parse_text(fenced, "t").questions) == 1

    # пример кода внутри поля (с отступом) не должен ломать разбор внешней ```yaml ограды
    nested = (
        "```yaml\n"
        "- id: card-002\n"
        "  topic: a/b\n"
        "  type: card\n"
        "  question: |-\n"
        "    ```python\n"
        "    x = 1\n"
        "    ```\n"
        "    Что выведет код?\n"
        "  answer: '1'\n"
        "```"
    )
    res2 = parse_text(nested, "t")
    assert not res2.errors and len(res2.questions) == 1
    assert res2.questions[0].question.startswith("```python")


def test_latex_in_double_quotes_is_caught():
    # \c — YAML сам ругается на неизвестный escape; \theta и \frac — молча портятся (TAB, FF), их ловит валидатор
    loud = r"""- {id: a-001, topic: a, type: card, question: q, answer: "$\cos x$"}"""
    silent = r"""- {id: a-002, topic: a, type: card, question: "Угол $\theta$ и $\frac{1}{2}$", answer: a}"""
    ok = r"""- {id: a-003, topic: a, type: card, question: "Угол $\\theta$", answer: 'x $\theta$'}"""
    r1, r2, r3 = parse_text(loud, "t"), parse_text(silent, "t"), parse_text(ok, "t")
    assert r1.errors and "одинарных" in r1.errors[0]
    assert r2.errors and "a-002" in r2.errors[0] and not r2.questions
    assert not r3.errors and r3.questions[0].question == r"Угол $\theta$" and r3.questions[0].answer == r"x $\theta$"


def test_math_render():
    html = str(md(r"Пусть $x_1^2 < y$ и \(a_i\)" + "\n\n$$\\sum_i a_i$$"))
    assert 'class="math inline"' in html and 'class="math block"' in html
    assert "x_1^2 &lt; y" in html and html.count("math inline") == 2


def test_full_flow(client):
    r = client.get("/")
    assert r.status_code == 200 and "к повторению" in r.text

    r = client.get("/start?mode=daily", follow_redirects=False)
    assert r.status_code == 303
    sid = int(re.search(r"/s/(\d+)", r.headers["location"]).group(1))

    answered = 0
    while True:
        page = client.get(f"/s/{sid}", follow_redirects=False)
        if page.status_code == 303:
            break
        assert page.status_code == 200
        qid = re.search(r'name="qid" value="([^"]+)"', page.text).group(1)
        if 'name="choice"' in page.text:
            # всегда выбираем первый вариант (для mcq он может быть неверным — проверяем и Again-повтор)
            val = re.search(r'name="choice" value="(\d+)"', page.text).group(1)
            data = {"qid": qid, "choice": val, "time_ms": "1234"}
        elif re.search(r'name="m0"', page.text):  # match: выбираем верный вариант для каждой пары
            n = len(re.findall(r'<select name="m\d+"', page.text))
            data = {"qid": qid, "time_ms": "900", **{f"m{i}": str(i) for i in range(n)}}
        elif 'id="order-list"' in page.text:  # order: порядок по возрастанию индексов — верный
            n = len(re.findall(r'<li data-i="\d+"', page.text))
            data = {"qid": qid, "time_ms": "900", "order": [str(i) for i in range(n)]}
        else:
            data = {"qid": qid, "rating": "3", "time_ms": "800", "text": "мой ответ"}
        resp = client.post(f"/s/{sid}/answer", data=data, follow_redirects=False)
        assert resp.status_code == 303
        fb = client.get(resp.headers["location"])
        assert fb.status_code == 200
        answered += 1
        assert answered < 30

    assert answered >= 8
    done = client.get(f"/s/{sid}/done")
    assert done.status_code == 200 and "Сессия завершена" in done.text

    # после сессии новые карточки получили расписание → due=0 сразу после ответа
    home = client.get("/")
    assert "demo" in home.text


def test_double_submit_is_ignored(client):
    sid = int(
        re.search(r"/s/(\d+)", client.get("/start?mode=random&n=3", follow_redirects=False).headers["location"]).group(
            1
        )
    )
    page = client.get(f"/s/{sid}")
    qid = re.search(r'name="qid" value="([^"]+)"', page.text).group(1)
    data = {"qid": qid, "choice": "0", "rating": "3", "time_ms": "1"}
    first = client.post(f"/s/{sid}/answer", data=data, follow_redirects=False)
    assert first.headers["location"].startswith("/r/")
    # тот же qid второй раз: очередь уже ушла вперёд → запись не создаётся, просто возвращаем на текущий вопрос
    again = client.post(f"/s/{sid}/answer", data=data, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == f"/s/{sid}"


def test_paste_import_and_flag(client):
    batch = r"""```yaml
- id: paste-001
  topic: demo/paste
  type: mcq
  question: |
    Чему равна производная $\sin x$?
  options:
    - {text: '$\cos x$', correct: true}
    - {text: '$-\cos x$'}
- id: paste-bad
  topic: demo/paste
  type: mcq
  question: q
  options:
    - {text: a}
    - {text: b}
```"""
    r = client.post("/import", data={"text": batch})
    assert r.status_code == 200
    assert "новых <b>1</b>" in r.text and "paste-bad" in r.text
    saved = list((_tmp / "questions" / "inbox").glob("batch-*.yaml"))
    assert len(saved) == 1 and "paste-001" in saved[0].read_text(encoding="utf-8")

    # повторная вставка того же батча ничего не дублирует
    r2 = client.post("/import", data={"text": batch})
    assert "без изменений <b>1</b>" in r2.text
    assert len(list((_tmp / "questions" / "inbox").glob("batch-*.yaml"))) == 1

    # флаг убирает вопрос из выдачи; правка YAML — снимает
    client.post("/q/paste-001/flag", data={"note": "проверь знак"}, follow_redirects=False)
    assert "проверь знак" in client.get("/flagged").text
    fixed = batch.replace(r"- {text: '$-\cos x$'}", r"- {text: '$-\sin x$'}")
    assert fixed != batch
    r3 = client.post("/import", data={"text": fixed})
    assert "обновлено <b>1</b>" in r3.text
    assert "Флагов нет" in client.get("/flagged").text


def test_rescan_reactivates_and_deactivates(client):
    shutil.rmtree(_tmp / "questions" / "demo")
    r = client.post("/import/rescan")
    assert int(re.search(r"скрыто (\d+)", r.text).group(1)) >= 8
    assert client.get("/questions?topic=demo/paste").status_code == 200


def test_srs_modes_cap_intervals_and_previews(client):
    from datetime import timedelta

    from app import settings, srs
    from app.db import db, iso, utcnow

    qid = "demo-la-eig-001"
    with db() as conn:
        p = settings.srs_params(conn)
        assert p["mode"] == "normal" and p["cap"] == 14 and p["retention"] == 0.93
        # серия «Легко» упирается в потолок режима
        t = utcnow()
        for _ in range(8):
            due = srs.apply_review(conn, qid, 4, t)
            assert due - t <= timedelta(days=p["cap"], hours=1)
            t = due
        assert due - utcnow() > timedelta(days=10)  # а потолок 14 дней действительно достигается

        # интенсив: потолок 3 дня; уже назначенные сроки подтягиваются под него, повторный вызов ничего не меняет
        settings.set_srs(conn, 0.95, 3)
        assert settings.srs_params(conn)["mode"] == "intensive"
        assert srs.clamp_existing_due(conn) >= 1
        row = conn.execute("SELECT due FROM cards WHERE question_id=?", (qid,)).fetchone()
        assert row["due"] <= iso(utcnow() + timedelta(days=3, minutes=1))
        assert srs.clamp_existing_due(conn) == 0
        pv = srs.preview_intervals(conn, qid, utcnow())
        assert set(pv) == {1, 2, 3, 4} and all(v.endswith("дн.") for v in pv.values())

        # произвольные значения → custom, границы ограничиваются
        settings.set_srs(conn, 0.5, 9999)
        cur = settings.srs_params(conn)
        assert cur["mode"] == "custom" and cur["retention"] == 0.70 and cur["cap"] == 365
        settings.set_srs(conn, 0.93, 14)  # вернуть режим по умолчанию
        assert settings.srs_params(conn)["mode"] == "normal"

    r = client.post("/settings/srs", data={"retention": "0,90", "cap": "30"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=srs" in r.headers["location"]
    with db() as conn:
        assert settings.srs_params(conn)["mode"] == "support"
    assert "Поддержка" in client.get("/settings").text
    bad = client.post("/settings/srs", data={"retention": "abc", "cap": "x"}, follow_redirects=False)
    assert "srs_bad" in bad.headers["location"]
    client.post("/settings/srs", data={"retention": "0.93", "cap": "14"})


def test_paused_topics_are_excluded_from_daily(client):
    import json

    from app import settings
    from app.db import db

    batch = (
        "- {id: zz-001, topic: zzz/a, type: card, question: q, answer: a}\n"
        "- {id: zz-002, topic: zzz/b, type: card, question: q, answer: a}\n"
        "- {id: yy-001, topic: yyy/a, type: card, question: q, answer: a}\n"
        "- {id: yy-002, topic: yyy/a, type: card, question: q, answer: a}\n"
    )
    assert "новых <b>4</b>" in client.post("/import", data={"text": batch}).text

    def daily_ids():
        r = client.get("/start?mode=daily", follow_redirects=False)
        if "empty" in r.headers["location"]:
            return []
        sid = int(re.search(r"/s/(\d+)", r.headers["location"]).group(1))
        with db() as conn:
            items = json.loads(conn.execute("SELECT items FROM sessions WHERE id=?", (sid,)).fetchone()["items"])
        return [qid for qid, _ in items]

    assert {"zz-001", "zz-002"} <= set(daily_ids())  # без паузы новые вопросы темы zzz попадают в подборку

    # ставим zzz на паузу: галочки только у остальных тем
    with db() as conn:
        known = {r[0].split("/")[0] for r in conn.execute("SELECT topic FROM questions WHERE active=1")}
    r = client.post("/settings", data={"enabled": sorted(known - {"zzz"})}, follow_redirects=False)
    assert r.status_code == 303
    with db() as conn:
        assert settings.paused_topics(conn) == ["zzz"]

    ids = daily_ids()
    assert ids and not any(i.startswith("zz-") for i in ids)
    assert "zzz" in client.get("/").text and "На паузе" in client.get("/").text

    # явный выбор темы паузу обходит
    r = client.get("/start?mode=daily&topic=zzz", follow_redirects=False)
    assert "empty" not in r.headers["location"]

    # снимаем паузу
    client.post("/settings", data={"enabled": sorted(known)}, follow_redirects=False)
    with db() as conn:
        assert settings.paused_topics(conn) == []


# ---------------------------------------------------------------------------------------------------------------
# match / order
# ---------------------------------------------------------------------------------------------------------------


def _start(client, url):
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 303 and "empty" not in r.headers["location"], r.headers["location"]
    return int(re.search(r"/s/(\d+)", r.headers["location"]).group(1))


def _review(rid_location):
    from app.db import db

    rid = int(rid_location.rsplit("/", 1)[1])
    with db() as conn:
        row = conn.execute("SELECT rating, score FROM reviews WHERE id=?", (rid,)).fetchone()
    return row["rating"], row["score"]


def test_match_and_order_scoring(client):
    batch = r"""
- id: mo-match-001
  topic: mo/m
  type: match
  question: Сопоставь
  pairs:
    - {left: 'L1', right: 'Лаплас'}
    - {left: 'L2', right: 'Гаусс'}
    - {left: 'L0', right: 'Дельта'}
  extra_right: ['Равномерное']
- id: mo-order-001
  topic: mo/o
  type: order
  question: Расставь
  items: ['one', 'two', 'three', 'four']
"""
    assert "новых <b>2</b>" in client.post("/import", data={"text": batch}).text

    def answer(url, data):
        sid = _start(client, url)
        page = client.get(f"/s/{sid}")
        qid = re.search(r'name="qid" value="([^"]+)"', page.text).group(1)
        resp = client.post(f"/s/{sid}/answer", data={"qid": qid, "time_ms": "5", **data}, follow_redirects=False)
        assert resp.status_code == 303
        return resp.headers["location"], page, sid

    # match: страница показывает варианты (включая лишний) и по одному полю выбора на пункт
    loc, page, _ = answer("/start?mode=topic&topic=mo/m", {"m0": "0", "m1": "1", "m2": "2"})
    assert "Равномерное" in page.text and page.text.count("<select") == 3
    assert _review(loc) == (3, 1.0)
    loc, _, _ = answer("/start?mode=topic&topic=mo/m", {"m0": "0", "m1": "1", "m2": "3"})  # 2 из 3 → «Трудно»
    rating, score = _review(loc)
    assert rating == 2 and abs(score - 2 / 3) < 1e-9
    assert "Почти" in client.get(loc).text and "67%" in client.get(loc).text
    loc, _, _ = answer("/start?mode=topic&topic=mo/m", {"m0": "1", "m1": "0", "m2": "3"})  # 0 из 3
    assert _review(loc) == (1, 0.0)

    # неполный или некорректный ответ не записывается
    sid = _start(client, "/start?mode=topic&topic=mo/m")
    qid = re.search(r'name="qid" value="([^"]+)"', client.get(f"/s/{sid}").text).group(1)
    for bad in ({"m0": "0"}, {"m0": "0", "m1": "1", "m2": "99"}, {"m0": "a", "m1": "b", "m2": "c"}):
        r = client.post(f"/s/{sid}/answer", data={"qid": qid, **bad}, follow_redirects=False)
        assert r.headers["location"] == f"/s/{sid}"

    # order: верный порядок / одна перестановка соседних / полностью наоборот
    loc, page, _ = answer("/start?mode=topic&topic=mo/o", {"order": ["0", "1", "2", "3"]})
    assert 'id="order-list"' in page.text and page.text.count("<li data-i=") == 4
    assert _review(loc) == (3, 1.0)
    loc, _, _ = answer("/start?mode=topic&topic=mo/o", {"order": ["1", "0", "2", "3"]})
    rating, score = _review(loc)
    assert rating == 2 and abs(score - 5 / 6) < 1e-9
    loc, _, _ = answer("/start?mode=topic&topic=mo/o", {"order": ["3", "2", "1", "0"]})
    assert _review(loc) == (1, 0.0)
    sid = _start(client, "/start?mode=topic&topic=mo/o")
    qid = re.search(r'name="qid" value="([^"]+)"', client.get(f"/s/{sid}").text).group(1)
    for bad in ({"order": ["0", "1"]}, {"order": ["0", "0", "1", "2"]}, {"order": ["x", "1", "2", "3"]}):
        r = client.post(f"/s/{sid}/answer", data={"qid": qid, **bad}, follow_redirects=False)
        assert r.headers["location"] == f"/s/{sid}"

    # страница просмотра вопроса показывает верные пары и порядок
    assert "Лаплас" in client.get("/q/mo-match-001").text
    assert "Верный порядок" in client.get("/q/mo-order-001").text


# ---------------------------------------------------------------------------------------------------------------
# голос и проверка моделью (OpenRouter подменён)
# ---------------------------------------------------------------------------------------------------------------

import json  # noqa: E402
from datetime import UTC

GOOD_GRADE = (
    '{"points": [{"status": "hit", "comment": "названо"}, {"status": "hit", "comment": "названо"}, '
    '{"status": "partial", "comment": "неполно"}], "mistakes": [], "major_error": false, "feedback": "Повтори пороги."}'
)


def _fake_openrouter(state):
    import httpx

    def handler(request):
        body = json.loads(request.content)
        state["calls"].append((request.url.path, body))
        if request.url.path.endswith("/audio/transcriptions"):
            if state.get("reject_keyterms") and "keyterms" in body:
                return httpx.Response(400, json={"error": {"message": "keyterms are not supported"}})
            return httpx.Response(
                200, json={"text": "Accuracy бессмысленна из-за дисбаланса", "usage": {"cost": 0.0018, "seconds": 59.0}}
            )
        if request.url.path.endswith("/chat/completions"):
            if state.get("reject_reasoning") and "reasoning" in body:
                return httpx.Response(400, json={"error": {"message": "reasoning is not supported"}})
            replies = state["replies"]
            content = replies.pop(0) if len(replies) > 1 else replies[0]
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"cost": 0.0004, "prompt_tokens": 900, "completion_tokens": 250},
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return httpx.MockTransport(handler)


AI_BATCH = """
- id: ai-open-001
  topic: aitest/a
  type: open
  question: Что делать с дисбалансом классов?
  answer: Взвешивание классов или сэмплирование, метрики PR-AUC, порог по стоимости ошибок.
  rubric:
    - 'Веса классов или сэмплирование'
    - 'Метрики precision/recall и PR-AUC'
    - 'Порог по стоимости ошибок'
- id: ai-open-002
  topic: aitest/a
  type: open
  question: Как бороться с утечкой данных?
  answer: Делить данные до препроцессинга, использовать пайплайны.
  rubric:
    - 'Делить до препроцессинга'
    - 'Пайплайны'
    - 'Следить за временной утечкой'
"""


def test_llm_voice_check_and_override(client, monkeypatch):
    from app import llm
    from app.db import db

    assert "новых <b>2</b>" in client.post("/import", data={"text": AI_BATCH}).text
    state = {"calls": [], "replies": [GOOD_GRADE]}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state))
    llm.reset_glossary_cache()

    # --- без ключа: ручной режим, голосовых кнопок нет, API отвечает понятной ошибкой ---
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    sid = _start(client, "/start?mode=topic&topic=aitest")
    page = client.get(f"/s/{sid}")
    assert 'id="mic"' not in page.text and 'id="check-btn"' not in page.text and 'id="reveal"' in page.text
    r = client.post(f"/api/transcribe?sid={sid}&qid=x", content=llm._silent_wav(2.0))
    assert r.status_code == 502 and "OPENROUTER_API_KEY" in r.json()["error"]
    assert not state["calls"]

    # --- с ключом: появляются кнопки ---
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    page = client.get(f"/s/{sid}")
    assert 'id="mic"' in page.text and 'id="check-btn"' in page.text
    qid = re.search(r'name="qid" value="([^"]+)"', page.text).group(1)

    # распознавание: словарь терминов из базы уходит как keyterms
    r = client.post(f"/api/transcribe?sid={sid}&qid={qid}", content=llm._silent_wav(2.0))
    data = r.json()
    assert r.status_code == 200 and data["ok"] and "дисбаланса" in data["text"]
    path, body = state["calls"][-1]
    assert path.endswith("/audio/transcriptions") and body["model"] == "elevenlabs/scribe-v2"
    assert body["language"] == "ru" and body["input_audio"]["format"] == "wav"
    assert "PR-AUC" in body["keyterms"]
    assert "Взвешивание классов" not in json.dumps(body, ensure_ascii=False)  # эталона распознавание не видит

    # слишком короткая запись и пустой текст отклоняются до обращения к API
    n_calls = len(state["calls"])
    assert client.post(f"/api/transcribe?sid={sid}&qid={qid}", content=b"RIFF").status_code == 502
    assert client.post(f"/s/{sid}/check", data={"qid": qid, "text": "ну"}).status_code == 400
    assert client.post(f"/s/{sid}/check", data={"qid": "другой", "text": "длинный ответ"}).status_code == 409
    assert len(state["calls"]) == n_calls

    # --- проверка ---
    r = client.post(f"/s/{sid}/check", data={"qid": qid, "text": "веса классов, метрики и порог"})
    res = r.json()
    assert r.status_code == 200 and res["ok"] and res["suggested"] == 3 and abs(res["frac"] - 5 / 6) < 1e-3
    assert "Разбор по пунктам · 83%" in res["html"] and "Модель предлагает: «Хорошо»" in res["html"]
    assert 'class="rate"' in res["html"] and "suggested" in res["html"]
    path, body = state["calls"][-1]
    assert path.endswith("/chat/completions") and body["model"] == "anthropic/claude-haiku-5.5"
    user_msg = body["messages"][1]["content"]
    assert "<rubric>" in user_msg and "<user_answer>" in user_msg and "веса классов" in user_msg

    # принять предложенную оценку → в статистику идёт реальная доля, в журнале — результат проверки
    resp = client.post(
        f"/s/{sid}/answer",
        follow_redirects=False,
        data={"qid": qid, "rating": "3", "check_id": str(res["check_id"]), "text": "веса классов, метрики и порог"},
    )
    rating, score = _review(resp.headers["location"])
    assert rating == 3 and abs(score - 5 / 6) < 1e-3
    fb = client.get(resp.headers["location"]).text
    assert "Разбор по пунктам" in fb and "оценки совпали" in fb

    # --- второй вопрос: оценку меняют вручную → score берётся из выбранной оценки ---
    page = client.get(f"/s/{sid}")
    qid2 = re.search(r'name="qid" value="([^"]+)"', page.text).group(1)
    assert qid2 != qid
    res2 = client.post(f"/s/{sid}/check", data={"qid": qid2, "text": "делить до препроцессинга"}).json()
    resp = client.post(
        f"/s/{sid}/answer",
        follow_redirects=False,
        data={"qid": qid2, "rating": "2", "check_id": str(res2["check_id"]), "text": "x"},
    )
    assert _review(resp.headers["location"]) == (2, 0.5)
    assert "итоговая оценка" in client.get(resp.headers["location"]).text

    # учёт расходов: распознавание 0,0018 + две проверки по 0,0004
    with db() as conn:
        spent = llm.month_spend(conn)
    assert abs(spent - (0.0018 + 0.0004 * 2)) < 1e-9
    assert "$0.0026" in client.get("/settings").text


def test_llm_budget_limit_and_manual_fallback(client, monkeypatch):
    from app import llm, settings
    from app.db import db

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    state = {"calls": [], "replies": [GOOD_GRADE]}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state))
    cfg = {"stt_model": "elevenlabs/scribe-v2", "grader_model": "anthropic/claude-haiku-5.5"}

    sid = _start(client, "/start?mode=topic&topic=aitest")
    qid = re.search(r'name="qid" value="([^"]+)"', client.get(f"/s/{sid}").text).group(1)

    # месячный лимит исчерпан → 402 с понятным текстом, обращений к API нет
    client.post("/settings/llm", data={**cfg, "monthly_limit": "0.001"})
    n = len(state["calls"])
    r = client.post(f"/api/transcribe?sid={sid}&qid={qid}", content=llm._silent_wav(2.0))
    assert r.status_code == 402 and "лимит" in r.json()["error"]
    r = client.post(f"/s/{sid}/check", data={"qid": qid, "text": "достаточно длинный ответ"})
    assert r.status_code == 402 and len(state["calls"]) == n

    # ручной режим продолжает работать: отмечаем 2 из 3 пунктов → реальная доля 0,667 → «Хорошо»;
    # несуществующий check_id игнорируется
    resp = client.post(
        f"/s/{sid}/answer",
        follow_redirects=False,
        data={"qid": qid, "rating": "3", "rubric": ["0", "2"], "check_id": "999999", "text": "ответ"},
    )
    rating, score = _review(resp.headers["location"])
    assert rating == 3 and abs(score - 2 / 3) < 1e-9

    # лимит 0 = без лимита
    client.post("/settings/llm", data={**cfg, "monthly_limit": "0"})
    with db() as conn:
        assert settings.llm_cfg(conn)["monthly_limit"] == 0.0
        llm.ensure_budget(conn)
    client.post("/settings/llm", data={**cfg, "monthly_limit": "5"})


def test_llm_retries_and_provider_fallbacks(client, monkeypatch):
    import asyncio

    import httpx

    from app import llm
    from app.db import db

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    q = {"id": "ai-open-001", "question": "Q", "answer": "A", "rubric": ["a", "b", "c"]}

    # модель вернула мусор, затем корректный JSON в ограде → один повтор, стоимость суммируется
    state = {"calls": [], "replies": ["извини, не могу", "```json\n" + GOOD_GRADE + "\n```"]}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state))
    with db() as conn:
        out = asyncio.run(llm.grade(conn, q, "мой ответ"))
    assert out["suggested"] == 3 and abs(out["cost"] - 0.0008) < 1e-9
    assert sum(1 for p, _ in state["calls"] if p.endswith("/chat/completions")) == 2
    assert [p["text"] for p in out["points"]] == ["a", "b", "c"]  # тексты пунктов берутся из рубрики

    # неверное число пунктов дважды → понятная ошибка, а не падение
    state2 = {"calls": [], "replies": ['{"points": [{"status": "hit"}], "feedback": ""}']}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state2))
    with db() as conn, pytest.raises(llm.LLMError, match="неверном формате"):
        asyncio.run(llm.grade(conn, q, "мой ответ"))

    # существенная ошибка ограничивает предложенную оценку «Трудно»
    major = GOOD_GRADE.replace('"major_error": false', '"major_error": true').replace(
        '"mistakes": []', '"mistakes": ["перепутал L1 и L2"]'
    )
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter({"calls": [], "replies": [major]}))
    with db() as conn:
        out = asyncio.run(llm.grade(conn, q, "мой ответ"))
    assert out["frac"] > 0.8 and out["suggested"] == 2 and out["major_error"]

    # модель не принимает reasoning → повтор без него; провайдер не принимает keyterms → повтор без них
    state4 = {"calls": [], "replies": [GOOD_GRADE], "reject_reasoning": True, "reject_keyterms": True}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state4))
    llm.reset_glossary_cache()
    with db() as conn:
        asyncio.run(llm.grade(conn, q, "мой ответ"))
        r = asyncio.run(llm.transcribe(conn, llm._silent_wav(2.0)))
    chat = [b for p, b in state4["calls"] if p.endswith("/chat/completions")]
    stt = [b for p, b in state4["calls"] if p.endswith("/audio/transcriptions")]
    assert len(chat) == 2 and "reasoning" in chat[0] and "reasoning" not in chat[1]
    assert len(stt) == 2 and "keyterms" in stt[0] and "keyterms" not in stt[1] and r["keyterms"] is False

    # ошибки провайдера превращаются в понятные сообщения
    monkeypatch.setattr(
        llm,
        "_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"message": "bad key"}})),
    )
    with db() as conn, pytest.raises(llm.LLMError, match="не принял ключ"):
        asyncio.run(llm.grade(conn, q, "мой ответ"))


def test_settings_connection_test_and_glossary(client, monkeypatch):
    from app import llm
    from app.db import db

    state = {"calls": [], "replies": ["ok"]}
    monkeypatch.setattr(llm, "_TRANSPORT", _fake_openrouter(state))

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert "OPENROUTER_API_KEY" in client.post("/settings/llm/test").json()["error"]
    assert "Ключ не задан" in client.get("/settings").text

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    out = client.post("/settings/llm/test").json()
    assert out["stt"]["ok"] and out["grader"]["ok"] and out["grader"]["reply"] == "ok"
    assert "Ключ OpenRouter найден" in client.get("/settings").text

    # словарь терминов: латинские термины без формул, самые частые первыми
    llm.reset_glossary_cache()
    with db() as conn:
        terms = llm.glossary(conn)
    assert "PR-AUC" in terms and "L1" not in terms  # формулы вырезаны, слишком короткие токены отброшены
    assert all(len(t) <= 40 for t in terms)


# ---------------------------------------------------------------------------------------------------------------
# ограничение роста интервала: «Трудно» = вспомнил, но очень слабо
# ---------------------------------------------------------------------------------------------------------------


def _flat_scheduler(retention=0.93, cap=14, fuzz=False):
    from fsrs import Scheduler

    return Scheduler(
        desired_retention=retention, learning_steps=(), relearning_steps=(), maximum_interval=cap, enable_fuzzing=fuzz
    )


def test_growth_limit_chains():
    from datetime import datetime

    from fsrs import Card

    from app import srs

    s = _flat_scheduler()

    def chain(pattern):
        card, t, out = Card(), datetime(2026, 10, 1, tzinfo=UTC), []
        for rating in pattern:
            card = srs.review(s, card, rating, t)
            out.append(round((card.due - t).total_seconds() / 86400))
            t = card.due
        return out

    assert chain([3] * 6) == [1, 3, 8, 14, 14, 14]  # раньше 1, 4, 14
    assert chain([2] * 6) == [1, 2, 3, 4, 5, 6]  # «Трудно» растёт на день-два, а не удваивается
    assert chain([4] * 3) == [4, 14, 14]
    assert chain([3, 3, 1, 3, 3]) == [1, 3, 1, 2, 4]  # ошибка возвращает на следующий день


def test_hard_after_a_long_pause_does_not_jump_to_the_cap():
    """Вопрос 204: запланирован на 3 дня, отвечен через 16. Раньше «Трудно» давало 14 дней."""
    from datetime import datetime, timedelta

    from fsrs import Card, Rating

    from app import srs

    t0 = datetime(2026, 9, 22, 11, 55, tzinfo=UTC)
    before, _ = _flat_scheduler(0.90, 3).review_card(Card(), Rating.Easy, t0)
    assert (before.due - t0).days == 3 and 8 < before.stability < 8.5
    now = t0 + timedelta(days=16)
    s = _flat_scheduler()

    days = {r: round((srs.review(s, before, r, now).due - now).total_seconds() / 86400) for r in (1, 2, 3, 4)}
    assert days == {1: 1, 2: 4, 3: 8, 4: 11}
    # без ограничения FSRS отправил бы «Трудно» к потолку
    plain, _ = s.review_card(Card.from_json(before.to_json()), Rating.Hard, now)
    assert round((plain.due - now).total_seconds() / 86400) == 14
    # «Трудно» не повышает оценку прочности памяти, «Не вспомнил» резко снижает, «Хорошо» и «Легко» повышают
    st = {r: srs.review(s, before, r, now).stability for r in (1, 2, 3, 4)}
    assert st[1] < st[2] < before.stability < st[3] < st[4]


def test_growth_limit_holds_with_random_answers_and_delays():
    import random
    from datetime import datetime, timedelta

    from fsrs import Card

    from app import srs

    rng = random.Random(7)
    for retention, cap in ((0.93, 14), (0.95, 3), (0.90, 30)):
        for _ in range(60):
            s = _flat_scheduler(retention, cap, fuzz=True)
            card, t = Card(), datetime(2026, 10, 1, tzinfo=UTC)
            for _ in range(10):
                rating = rng.choice([1, 2, 3, 3, 3, 4])
                limit = srs.growth_limit_days(card, rating)
                card = srs.review(s, card, rating, t)
                days = (card.due - t).total_seconds() / 86400
                assert 1 - 1e-6 <= days <= cap + 1e-6
                if limit is not None:
                    assert days <= limit + 1e-6
                t = card.due + timedelta(days=rng.choice([0, 0, 1, 5, 12]))  # иногда с просрочкой


def test_previews_use_the_same_limits(client):
    from app import srs
    from app.db import db, utcnow

    with db() as conn:
        pv = srs.preview_intervals(conn, "never-answered-question", utcnow())
    assert pv[1] == "1 дн." and pv[2] == "1 дн." and pv[4] == "4 дн."  # новая карточка: Легко не больше 4 дней
