# Battalion Clerk v1.2

Persistent Discord data collector for the **1st Battalion, 5th Cavalry Regiment** Hell Let Loose: Vietnam community.

The design rule remains the same: **the website is the brain; Battalion Clerk is a collector.** The bot does not own ranks, awards, promotions, DEROS, readiness, equipment, or personnel decisions.

## What v1.2 adds

- Railway PostgreSQL support through `DATABASE_URL`
- Automatically creates its database tables at startup
- Syncs current Discord member identity on startup
- Tracks username/display-name changes
- Stores member joins/leaves persistently
- Stores every raw collector event in `discord_events`
- Stores completed voice sessions in a dedicated `voice_sessions` table
- Keeps a local SQLite safety buffer as a fallback
- Includes a reserved `website_member_links` table for connecting Discord IDs to future website personnel IDs

## Permanent data model

### `discord_members`
Source identity only:
- Discord user ID
- Guild/server ID
- Username
- Display name
- Discord join date
- Leave date
- Last-seen timestamp

### `discord_events`
Raw audit/event stream:
- member join/leave
- username/display-name changes
- voice join/leave/move
- completed voice-session envelopes
- collector ready events

### `voice_sessions`
Website-friendly completed attendance sessions:
- member
- channel
- start/end timestamps
- total duration in seconds
- close reason
- whether the session was recovered after a bot restart

### `website_member_links`
Reserved bridge for later:
- Discord member ID -> website personnel ID

Battalion Clerk does **not** automatically create personnel records. The future website controls linking and personnel administration.

## Recommended Railway architecture

```text
Discord
   |
   v
Battalion Clerk (Railway service)
   |
   v
Railway PostgreSQL  <---- future 1/5 Cavalry website
```

The bot and website can therefore read/write the same persistent database without making Discord the source of truth.

## Railway setup

Keep your existing variables:

```text
DISCORD_TOKEN=...
GUILD_ID=...
```

Add a PostgreSQL database to the same Railway project, then add this variable to the **5th-CAV / Battalion Clerk service**:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Railway may give the database service a different name. Use the reference variable Railway offers for that database's `DATABASE_URL`.

The start command remains:

```text
python bot.py
```

After deployment, the logs should include:

```text
[POSTGRES READY] PostgreSQL ...
[COLLECTOR READY] postgres=configured | website_api=not configured
[GUILD SYNC] guild=... members=... recovered_voice_sessions=...
```

## Local safety buffer

SQLite remains enabled as a safety buffer. It is not the authoritative store once PostgreSQL is active.

If you later attach a Railway Volume at `/data`, set:

```text
LOCAL_DB_PATH=/data/battalion_clerk.db
```

This is optional once PostgreSQL is working, but it gives you an additional on-service buffer.

## Discord permissions/intents

Keep **Server Members Intent** enabled in the Discord Developer Portal. Battalion Clerk also uses normal guild and voice-state intents. It does not need Administrator, Manage Roles, or Message Content permission.

## Next website step

When the website is created, it can use the same PostgreSQL database and calculate things such as:

- qualifying event attendance
- total unit activity
- operation attendance
- training attendance
- last activity
- Discord account linkage

Those calculated results belong to the website, not Battalion Clerk.
