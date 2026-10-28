import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions(
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    type TEXT NOT NULL,
    difficulty INTEGER NOT NULL DEFAULT 2,
    tags TEXT NOT NULL DEFAULT '[]',
    data TEXT NOT NULL,
    hash TEXT NOT NULL,
    source TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    flagged INTEGER NOT NULL DEFAULT 0,
    flag_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_questions_topic ON questions(topic);

CREATE TABLE IF NOT EXISTS cards(
    question_id TEXT PRIMARY KEY REFERENCES questions(id),
    fsrs TEXT NOT NULL,
    due TEXT NOT NULL,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_review TEXT
);
CREATE INDEX IF NOT EXISTS ix_cards_due ON cards(due);

CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT NOT NULL,
    session_id INTEGER,
    ts TEXT NOT NULL,
    rating INTEGER NOT NULL,          -- 1 Again, 2 Hard, 3 Good, 4 Easy
    score REAL NOT NULL,              -- 0 / 0.5 / 1 для статистики
    scheduled INTEGER NOT NULL,       -- 1 = обновил расписание FSRS
    next_due TEXT,
    time_ms INTEGER,
    answer TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_q ON reviews(question_id);
CREATE INDEX IF NOT EXISTS ix_reviews_ts ON reviews(ts);

-- обращения к платным API (распознавание речи и проверка ответов): учёт расходов + сохранённые результаты проверки
CREATE TABLE IF NOT EXISTS api_calls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,               -- 'stt' | 'grade'
    model TEXT NOT NULL,
    cost REAL NOT NULL DEFAULT 0,     -- USD
    estimated INTEGER NOT NULL DEFAULT 0,
    session_id INTEGER,
    question_id TEXT,
    detail TEXT                       -- JSON: для 'grade' результат проверки, для 'stt' длительность и текст
);
CREATE INDEX IF NOT EXISTS ix_api_calls_ts ON api_calls(ts);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    topic TEXT,
    tag TEXT,
    items TEXT NOT NULL,              -- JSON: [[question_id, is_retry], ...]
    idx INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Соединение живёт один запрос, но FastAPI открывает его в пуле потоков, а async-обработчик работает в другом.
    # autocommit (isolation_level=None): зависимость с yield закрывается уже ПОСЛЕ отправки ответа, и браузер
    # успел бы пойти по редиректу до коммита. Многошаговые записи делаем в явной транзакции — см. tx().
    conn = sqlite3.connect(config.DB_PATH, timeout=15, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Атомарная группа записей."""
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_db():
    """FastAPI-зависимость."""
    with db() as conn:
        yield conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# --- время: в БД всё в UTC, формат сортируется как строка ---


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_month_start() -> str:
    """Начало текущего календарного месяца (локального) в UTC-строке."""
    now = datetime.now().astimezone()
    return iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def local_day_start(offset_days: int = 0) -> str:
    """Начало локального дня (с учётом сдвига) в UTC-строке."""
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    return iso(start)
