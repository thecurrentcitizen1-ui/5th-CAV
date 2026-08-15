# 1/5 Cavalry Battalion Clerk — Duty Automation

Discord companion service for the website duty-credit bridge.

## Commands
- `/duty-channel duty_type channel` — permanently assigns Training, Operation, or Meeting voice duty station.
- `/duty-channel-status` — shows configured duty stations.
- `/schedule` — files an official duty period. Only voice time overlapping this window can earn credit.
- `/duty-status` — flushes current voice presence and shows minutes toward the 45-minute requirement.
- `/close-duty` — files final live presence, closes the duty period, and reports credited strength.

## Credit rules
- Exactly three categories: Training, Operation, Meeting.
- 45 minutes (2700 seconds) of cumulative qualifying presence earns one credit.
- Members may leave and return; qualifying chunks accumulate.
- Time outside the scheduled event window does not count.
- Presence in non-assigned channels does not count.
- Duplicate voice chunks are rejected by the website bridge.
- Operation duty can be linked to an existing website operation UUID and will file participation in the Soldier's combat operations record.

## Railway
Deploy this folder as a separate worker service. Configure the variables in `.env.example` in Railway. The website and bot MUST share the same `CLERK_SYNC_KEY` value.

Discord Developer Portal intents required: **Server Members Intent** and **Voice States** (voice states are part of standard gateway intents; member intent must be enabled for member resolution).


## Guild configuration
`TEST_GUILD_ID` is preferred for immediate slash-command synchronization. If it is not set, the bot will use `GUILD_ID`. If neither is configured, commands are synchronized globally and may take longer to appear. It is safe for both Railway variables to exist; when both are present, `TEST_GUILD_ID` takes precedence.

## Startup verification
On boot the worker validates `DISCORD_TOKEN`, `WEBSITE_BASE_URL`, and `CLERK_SYNC_KEY`, prints the selected guild synchronization source, timezone, flush interval, and website bridge URL, then starts the Discord client.
