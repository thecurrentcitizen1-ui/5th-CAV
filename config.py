import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL")
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "").rstrip("/")
CLERK_SYNC_KEY = os.getenv("CLERK_SYNC_KEY", "")
BATTALION_TIMEZONE = os.getenv("BATTALION_TIMEZONE", "America/New_York")
VOICE_FLUSH_SECONDS = int(os.getenv("VOICE_FLUSH_SECONDS", "300") or 300)

# Hell Let Loose: Vietnam RCON telemetry
HLL_RCON_ENABLED = os.getenv("HLL_RCON_ENABLED", "false").strip().lower() in {"1","true","yes","on"}
HLL_RCON_HOST = os.getenv("HLL_RCON_HOST", "").strip()
HLL_RCON_PORT = int(os.getenv("HLL_RCON_PORT", "7779") or 7779)
HLL_RCON_POLL_SECONDS = max(3, int(os.getenv("HLL_RCON_POLL_SECONDS", "5") or 5))
