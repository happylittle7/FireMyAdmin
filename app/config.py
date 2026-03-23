from dataclasses import dataclass
import os


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AppSettings:
    firebase_credentials: str = os.getenv("FIREBASE_CREDENTIALS", "").strip()
    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "").strip()
    read_only: bool = _is_truthy(os.getenv("APP_READ_ONLY", "false"))


settings = AppSettings()
