import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL")
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "").rstrip("/")
CLERK_SYNC_KEY = os.getenv("CLERK_SYNC_KEY", "")
BATTALION_TIMEZONE = os.getenv("BATTALION_TIMEZONE", "America/New_York")
VOICE_FLUSH_SECONDS = int(os.getenv("VOICE_FLUSH_SECONDS", "300") or 300)
