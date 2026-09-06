import json
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

DATA_DIR = Path(os.getenv("DATA_PATH", "./data"))
CONFIG_FILE = DATA_DIR / "config.json"
UPSTREAM_DIR = Path(os.getenv("UPSTREAM_PATH", "./upstream"))
PROFILES_DIR = DATA_DIR / "data-dir"
VISUAL_SEARCH_IMAGE = DATA_DIR / "visual_search.jpg"
NOUNS_FILE = DATA_DIR / "nouns.txt"


class ScheduleConfig(BaseModel):
    enabled: bool = True
    mode: str = "daily"  # "daily", "interval", "custom_times"
    interval_hours: int = 6  # For mode == "interval"
    custom_times: List[str] = Field(default_factory=lambda: ["03:00", "15:00"])  # For mode == "custom_times"
    cron_hour: int = 3  # For mode == "daily"
    cron_minute: int = 0
    run_on_startup: bool = False


class AppConfig(BaseModel):
    accounts: List[str] = Field(default_factory=lambda: ["default"])
    query_source: str = "trends"  # "trends" or "llm"
    ollama_host: Optional[str] = None
    log_level: str = "INFO"  # "INFO" or "DEBUG"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    webhook_url: Optional[str] = None
    auto_update_upstream: bool = True


def get_config() -> AppConfig:
    if not CONFIG_FILE.exists():
        config = AppConfig()
        save_config(config)
        return config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return AppConfig(**data)
    except Exception as e:
        print(f"Error loading config.json, using defaults: {e}")
        return AppConfig()


def save_config(config: AppConfig) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config.model_dump(), f, indent=2)
