import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    database_url: str


def load_settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return Settings(database_url=url)


def load_watchlist(path: Path | None = None) -> list[dict]:
    path = path or REPO_ROOT / "config" / "watchlist.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("companies", [])
