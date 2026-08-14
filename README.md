# Battalion Clerk v1.1

Lightweight Discord data collector for the **1st Battalion, 5th Cavalry Regiment** Hell Let Loose: Vietnam community.

Battalion Clerk is intentionally **not** the administrative brain of the community. The future website/database owns personnel records, ranks, awards, qualifications, operations, readiness, equipment, and promotions. Battalion Clerk only collects Discord activity and forwards it.

## v1.1 collects

- Member joins and leaves
- Voice channel joins and leaves
- Voice channel moves
- Completed voice-session duration in seconds and `HH:MM:SS`
- Discord user ID, username, display name, guild/channel IDs, and timestamps
- Current voice users are re-seeded when the bot restarts so their next leave/move still closes a usable session
- Clear Railway console logs for testing

Example Railway logs:

```text
[VOICE JOIN] Garretson (123456789) -> #Operations Room
[VOICE LEAVE] Garretson (123456789) <- #Operations Room | session=00:42:17
[VOICE MOVE] Garretson (123456789) #Ready Room -> #Operations Room | prior_session=00:05:31
```

## Data flow

```text
Discord -> Battalion Clerk -> Website API -> Website Database
```

Every event is first written to a local SQLite safety buffer. The SQLite buffer is **not intended to be the permanent source of truth**.

### Railway persistence

Railway container files can be replaced during deploys. Until the website API/database exists, you can attach a Railway Volume and set:

```text
LOCAL_DB_PATH=/data/battalion_clerk.db
```

Mount the Railway Volume at `/data`.

Once the website API is live, the website database should become the authoritative long-term store.

## Railway variables

Required:

```text
DISCORD_TOKEN=your_secret_bot_token
GUILD_ID=your_discord_server_id
```

Optional now / used later:

```text
LOCAL_DB_PATH=data/battalion_clerk.db
WEBSITE_API_URL=
WEBSITE_API_KEY=
```

Never commit the real `DISCORD_TOKEN` or `WEBSITE_API_KEY` to GitHub.

## Railway start command

```text
python bot.py
```

## Discord Developer Portal

Enable **Server Members Intent**. Battalion Clerk also requests Discord's normal guild and voice-state intents in code. It does not need Administrator, Manage Roles, or Message Content access for this collector.

## Voice warning

You may see a warning that PyNaCl is not installed and the bot cannot *join* Discord voice. That is harmless for Battalion Clerk v1.1. The collector does not connect to voice audio; it only watches voice-state membership changes.

## Website event example

A completed voice session is emitted as:

```json
{
  "source": "battalion-clerk",
  "event_type": "voice_session",
  "created_at": "2026-08-14T23:00:00+00:00",
  "payload": {
    "guild_id": "123",
    "discord_user_id": "456",
    "username": "example",
    "display_name": "PFC Example",
    "channel_id": "789",
    "channel_name": "Operations Room",
    "started_at": "2026-08-14T22:15:00+00:00",
    "ended_at": "2026-08-14T23:00:00+00:00",
    "duration_seconds": 2700,
    "duration_hms": "00:45:00",
    "close_reason": "voice_leave",
    "recovered_after_restart": false
  }
}
```

## Next layer

The website should expose an authenticated endpoint such as:

```text
/api/integrations/discord/events
```

Battalion Clerk can then POST raw events and completed sessions there. The website can decide what counts as an official operation, minimum attendance, credited service time, and other personnel metrics.
