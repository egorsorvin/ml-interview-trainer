import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный разбор .env (KEY=VALUE). Уже заданные переменные окружения не перезаписываются."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("TRAINER_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "trainer.db"
QUESTIONS_DIR = Path(os.environ.get("TRAINER_QUESTIONS", BASE_DIR / "questions"))

NEW_PER_DAY = int(os.environ.get("TRAINER_NEW_PER_DAY", 15))
DUE_LIMIT = 80  # максимум повторений в одной ежедневной сессии
DEFAULT_SESSION_N = 10
TOPIC_SESSION_N = 20
WEAK_THRESHOLD = 0.7  # средняя доля правильного за последние 5 ответов ниже этого значения → «слабое место»

# --- расписание повторений (FSRS) ---
# retention — целевая вероятность вспомнить в момент повтора; cap — потолок интервала в днях
SRS_PRESETS = {
    "intensive": {"label": "Интенсив", "retention": 0.95, "cap": 3},
    "normal": {"label": "Обычный", "retention": 0.93, "cap": 14},
    "support": {"label": "Поддержка", "retention": 0.90, "cap": 30},
}
DEFAULT_SRS_MODE = "normal"

# --- ограничение роста интервала за один ответ ---
# FSRS после долгой паузы щедро награждает любой успешный ответ (даже «Трудно») и отправляет вопрос к потолку.
# Поэтому следующий интервал не может вырасти больше чем в N раз от запланированного. «Трудно» = вспомнил,
# но очень слабо: рост минимальный (×1,2, не меньше +1 день), а прочность подгоняется под этот интервал.
GROWTH_LIMIT = {2: 1.2, 3: 2.5, 4: 3.5}  # оценка → максимальный множитель интервала
HARD_MIN_STEP_DAYS = 1                   # «Трудно» всегда может прибавить хотя бы столько дней
FIRST_INTERVAL_LIMIT = {2: 1, 3: 2, 4: 4}  # потолок первого интервала у новой карточки, дней

# --- оценка открытых ответов: доля правильного → оценка ---
RATING_HARD_FROM = 0.35  # меньше → «Не вспомнил»
RATING_GOOD_FROM = 0.60  # от этого значения → «Хорошо»

# --- проверка ответов моделью (OpenRouter) ---
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
DEFAULT_STT_MODEL = os.environ.get("TRAINER_STT_MODEL", "elevenlabs/scribe-v2")
DEFAULT_GRADER_MODEL = os.environ.get("TRAINER_GRADER_MODEL", "anthropic/claude-haiku-5.5")
DEFAULT_LLM_MONTHLY_LIMIT = float(os.environ.get("TRAINER_LLM_MONTHLY_LIMIT", 5.0))  # USD
STT_FALLBACK_PRICE_PER_SEC = 0.0000306  # если OpenRouter не вернул стоимость (Scribe v2)
MAX_AUDIO_BYTES = 24 * 1024 * 1024
STT_LANGUAGE = os.environ.get("TRAINER_STT_LANGUAGE", "ru")


def openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()
