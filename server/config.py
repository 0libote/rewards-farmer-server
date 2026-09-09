import json
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

DATA_DIR = Path(os.getenv("DATA_PATH", "./data"))
CONFIG_FILE = DATA_DIR / "config.json"
UPSTREAM_DIR = Path(os.getenv("UPSTREAM_PATH", "./upstream"))
PROFILES_DIR = DATA_DIR / "data-dir"
VISUAL_SEARCH_IMAGE = DATA_DIR / "visual_search.jpg"
NOUNS_FILE = DATA_DIR / "nouns.txt"

# Account/profile names become directory names and are interpolated into the
# dashboard, so keep them strict: letters, digits, dashes, underscores.
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_ACCOUNTS = 20

# "HH:MM" 24-hour strings used by the custom_times schedule mode.
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def is_valid_account_name(name: object) -> bool:
    return isinstance(name, str) and bool(ACCOUNT_NAME_RE.match(name))


class ScheduleConfig(BaseModel):
    enabled: bool = True
    mode: str = "daily"  # "daily", "interval", "custom_times"
    interval_hours: int = 6  # For mode == "interval"
    custom_times: List[str] = Field(default_factory=lambda: ["03:00", "15:00"])  # For mode == "custom_times"
    cron_hour: int = 3  # For mode == "daily"
    cron_minute: int = 0
    run_on_startup: bool = False
    # Skip scheduled (not manual) runs when the last run recently finished
    # with quota complete and 0 earned - the daily cap is hit, another run
    # right now can only earn 0 and just adds ban-surface. Manual runs always
    # proceed. Disable if you prefer the old every-tick behaviour.
    skip_empty_runs: bool = True
    empty_skip_hours: int = 12

    @field_validator("mode")
    @classmethod
    def _coerce_mode(cls, v: object) -> str:
        # Coerce (instead of rejecting) so one bad value can't nuke the
        # whole stored config back to defaults on next load.
        return v if v in ("daily", "interval", "custom_times") else "daily"

    @field_validator("interval_hours")
    @classmethod
    def _clamp_interval(cls, v: object) -> int:
        try:
            iv = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 6
        return min(max(iv, 1), 168)

    @field_validator("custom_times")
    @classmethod
    def _filter_times(cls, v: object) -> List[str]:
        if not isinstance(v, list):
            return ["03:00"]
        cleaned = [t.strip() for t in v if isinstance(t, str) and TIME_RE.match(t.strip())]
        return cleaned or ["03:00"]

    @field_validator("cron_hour")
    @classmethod
    def _clamp_hour(cls, v: object) -> int:
        try:
            hv = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 3
        return min(max(hv, 0), 23)

    @field_validator("cron_minute")
    @classmethod
    def _clamp_minute(cls, v: object) -> int:
        try:
            mv = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return min(max(mv, 0), 59)

    @field_validator("empty_skip_hours")
    @classmethod
    def _clamp_skip_hours(cls, v: object) -> int:
        try:
            hv = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 12
        return min(max(hv, 1), 72)


class AppConfig(BaseModel):
    accounts: List[str] = Field(default_factory=lambda: ["default"])
    query_source: str = "trends"  # "trends" or "llm"
    ollama_host: Optional[str] = None
    log_level: str = "INFO"  # "INFO" or "DEBUG"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    webhook_url: Optional[str] = None
    auto_update_upstream: bool = True

    @field_validator("accounts")
    @classmethod
    def _filter_accounts(cls, v: object) -> List[str]:
        if not isinstance(v, list):
            return ["default"]
        cleaned = [a.strip() for a in v if isinstance(a, str) and is_valid_account_name(a.strip())]
        return (cleaned or ["default"])[:MAX_ACCOUNTS]

    @field_validator("query_source")
    @classmethod
    def _coerce_query_source(cls, v: object) -> str:
        return v if v in ("trends", "llm") else "trends"

    @field_validator("log_level")
    @classmethod
    def _coerce_log_level(cls, v: object) -> str:
        return v if v in ("INFO", "DEBUG") else "INFO"


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
    # Atomic write so a concurrent reader never sees a truncated config.
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix="config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config.model_dump(), f, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
