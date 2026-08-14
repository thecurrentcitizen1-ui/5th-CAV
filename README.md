# Battalion Clerk v1

A lightweight Discord data collector for the 2d Battalion, 327th Infantry HLL: Vietnam community.

## What v1 collects

- Discord member joins
- Discord member leaves
- Voice channel joins
- Voice channel leaves
- Voice channel moves
- Discord user IDs, usernames, display names, guild/channel IDs, and timestamps associated with those events

Battalion Clerk deliberately does **not** manage ranks, awards, personnel files, training, promotions, or other community administration. Those systems belong on the website.

## Data flow

`Discord -> Battalion Clerk -> Website API`

Every event is also written to a local SQLite buffer first. This prevents attendance/activity data from disappearing if the website is temporarily offline.

## Discord Developer Portal setup

1. Create a new application named **Battalion Clerk**.
2. Add a bot user.
3. In **Bot > Privileged Gateway Intents**, enable **Server Members Intent**. Member join/leave events require the members intent in discord.py.
4. Invite the bot to the Vietnam Discord server.
5. The bot only needs basic visibility/connectivity for collection. It does not need Administrator or Manage Roles for v1.

## Install

Python 3.11+ recommended.

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your bot token and guild ID.

## Run

```bash
python bot.py
```

## Event format sent to the future website

```json
{
  "source": "battalion-clerk",
  "event_type": "voice_join",
  "created_at": "2026-08-14T22:00:00+00:00",
  "payload": {
    "guild_id": "123",
    "discord_user_id": "456",
    "username": "example",
    "display_name": "PFC Example",
    "from_channel_id": null,
    "from_channel_name": null,
    "to_channel_id": "789",
    "to_channel_name": "Operation Voice",
    "timestamp": "2026-08-14T22:00:00+00:00"
  }
}
```

## Next planned layer

When the Vietnam website exists, add an authenticated `/api/integrations/discord/events` endpoint. Battalion Clerk will POST these raw events there. The website can then calculate official operation attendance, time in channel, streaks, activity history, and other personnel metrics.

Do **not** put the Discord bot token in the website source code or GitHub repository.
