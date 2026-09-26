import os
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

from .models import Network
from .scoring import Params


@dataclass(frozen=True, slots=True)
class Settings:
    networks: tuple[Network, ...]
    db_path: str
    cooldown_hours: float
    params: Params
    telegram_token: str | None
    telegram_chat_id: str | None
    ollama_url: str
    ollama_model: str | None


def _cast(raw: str, default: object) -> object:
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return type(default)(raw)


def load_params(env: dict[str, str]) -> Params:
    """Переопределение порогов через PARAM_<ИМЯ_ПОЛЯ>, например PARAM_MAX_TAX=0.03."""
    defaults = Params()
    overrides = {
        f.name: _cast(env[key], getattr(defaults, f.name))
        for f in fields(Params)
        if (key := f"PARAM_{f.name.upper()}") in env
    }
    return Params(**overrides)


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    env = dict(os.environ)
    networks = tuple(
        Network(n.strip()) for n in env.get("NETWORKS", "bsc,base,robinhood").split(",") if n.strip()
    )
    return Settings(
        networks=networks,
        db_path=env.get("DB_PATH", "candidates.db"),
        cooldown_hours=float(env.get("COOLDOWN_HOURS", "24")),
        params=load_params(env),
        telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID") or None,
        ollama_url=env.get("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=env.get("OLLAMA_MODEL") or None,
    )
