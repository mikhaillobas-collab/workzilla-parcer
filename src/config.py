import os
from pathlib import Path
from typing import List, Optional
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Корневая директория проекта
BASE_DIR = Path(__file__).resolve().parent.parent

# Загружаем переменные из .env
load_dotenv(BASE_DIR / ".env")

class GeminiConfig(BaseModel):
    model: str = "gemini-2.0-flash"
    temperature: float = 0.3
    max_tokens: int = 600
    timeout_seconds: int = 30

class FiltersConfig(BaseModel):
    min_price: int = 150
    max_proposal_chars: int = 320
    hide_rejected_on_site: bool = True
    min_confidence: float = 0.75

class DelaysConfig(BaseModel):
    min_apply_delay_sec: int = 15
    max_apply_delay_sec: int = 45
    poll_interval_sec: int = 10

class DatabaseConfig(BaseModel):
    path: str = "data/workzilla.db"

class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: int = 0
    order_timeout_minutes: int = 15

class AppConfig(BaseModel):
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    delays: DelaysConfig = Field(default_factory=DelaysConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    # Переменные из окружения (.env / BotHost)
    gemini_api_key: str = ""
    proxy_url: Optional[str] = None
    browser_mode: str = "cdp"
    cdp_url: str = "http://127.0.0.1:9222"
    headless: bool = True
    workzilla_cookies: Optional[str] = None
    dry_run: bool = True

    # Список стоп-слов
    stop_words: List[str] = Field(default_factory=list)

def load_config() -> AppConfig:
    """Загрузка конфигурации из YAML, .env и списка стоп-слов."""
    yaml_path = BASE_DIR / "config" / "config.yaml"
    data = {}
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig(**data)

    # Применение переменных из окружения
    if os.getenv("GEMINI_MODEL"):
        config.gemini.model = os.getenv("GEMINI_MODEL").strip()
    config.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    config.proxy_url = os.getenv("PROXY_URL", None)
    if config.proxy_url:
        config.proxy_url = config.proxy_url.strip()
    config.browser_mode = os.getenv("BROWSER_MODE", "cdp").strip().lower()
    config.cdp_url = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip()
    
    headless_str = os.getenv("HEADLESS", "true").strip().lower()
    config.headless = headless_str in ("1", "true", "yes", "y")
    
    config.workzilla_cookies = os.getenv("WORKZILLA_COOKIES", "").strip() or None

    dry_run_str = os.getenv("DRY_RUN", "true").strip().lower()
    config.dry_run = dry_run_str in ("1", "true", "yes", "y")

    # Telegram интеграция
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config.telegram.bot_token = os.getenv("TELEGRAM_BOT_TOKEN").strip()
    if os.getenv("TELEGRAM_CHAT_ID"):
        try:
            config.telegram.chat_id = int(os.getenv("TELEGRAM_CHAT_ID").strip())
        except ValueError:
            pass
    if os.getenv("ORDER_TIMEOUT_MINUTES"):
        try:
            config.telegram.order_timeout_minutes = int(os.getenv("ORDER_TIMEOUT_MINUTES").strip())
        except ValueError:
            pass

    # Переопределения фильтров из окружения (для хостинга)
    if os.getenv("MIN_PRICE"):
        try:
            config.filters.min_price = int(os.getenv("MIN_PRICE"))
        except ValueError:
            pass
    if os.getenv("MAX_PROPOSAL_CHARS"):
        try:
            config.filters.max_proposal_chars = int(os.getenv("MAX_PROPOSAL_CHARS"))
        except ValueError:
            pass

    # Чтение стоп-слов
    stop_words_file = BASE_DIR / "config" / "stop_words.txt"
    if stop_words_file.exists():
        with open(stop_words_file, "r", encoding="utf-8") as f:
            words = []
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line.lower())
            config.stop_words = words

    # Гарантируем абсолютный путь к БД
    if not os.path.isabs(config.database.path):
        config.database.path = str(BASE_DIR / config.database.path)

    return config
