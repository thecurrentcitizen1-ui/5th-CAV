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
    clerk_sync_key: str
    discord_invite_url: str
    discord_client_id: str
    discord_client_secret: str
    discord_oauth_redirect_uri: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            secret_key=(os.getenv("SECRET_KEY") or os.getenv("WEB_SECRET_KEY") or "change-this-secret-key"),
            admin_username=os.getenv("ADMIN_USERNAME", "commander").strip() or "commander",
            admin_password=os.getenv("ADMIN_PASSWORD", "change-me-now"),
            port=int(os.getenv("PORT", "8080") or 8080),
            clerk_sync_key=os.getenv("CLERK_SYNC_KEY", "").strip(),
            discord_invite_url=os.getenv("DISCORD_INVITE_URL", "").strip(),
            discord_client_id=(os.getenv("DISCORD_CLIENT_ID") or os.getenv("DISCORD_APPLICATION_ID") or "").strip(),
            discord_client_secret=(os.getenv("DISCORD_CLIENT_SECRET") or os.getenv("DISCORD_OAUTH_CLIENT_SECRET") or "").strip(),
            discord_oauth_redirect_uri=(os.getenv("DISCORD_OAUTH_REDIRECT_URI") or os.getenv("DISCORD_REDIRECT_URI") or "").strip(),
        )

CONFIG = Config.from_env()
