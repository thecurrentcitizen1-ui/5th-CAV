from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    database_url: str
    secret_key: str
    admin_username: str
    admin_password: str
    port: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            secret_key=os.getenv("SECRET_KEY", "change-this-secret-key"),
            admin_username=os.getenv("ADMIN_USERNAME", "commander").strip() or "commander",
            admin_password=os.getenv("ADMIN_PASSWORD", "change-me-now"),
            port=int(os.getenv("PORT", "8080") or 8080),
        )

CONFIG = Config.from_env()
