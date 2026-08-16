# 5th Cavalry Battalion Clerk — Phase 9 Event Orders Automation

## Commands
- `/duty-channel` — assign permanent Training, Operation, Meeting voice channels
- `/duty-channel-status` — review duty voice-channel assignments
- `/orders-channel` — assign the text channel for official battalion event notices
- `/orders-channel-status` — review the orders channel
- `/schedule` — file an official duty period
- `/duty-status` — inspect current attendance progress
- `/close-duty` — close a duty period and file final credit

## Event notice lifecycle
When `/schedule` is used:
1. Initial Operations Notice / Training Circular / Battalion Notice is posted.
2. 15 minutes before step-off, Battalion Clerk posts a warning.
3. At start time, Battalion Clerk posts a commencement notice.
4. When `/close-duty` is used, Battalion Clerk posts a conclusion notice with tracked/credited totals.

## Railway variables
- DISCORD_TOKEN
- GUILD_ID
- TEST_GUILD_ID
- DATABASE_URL
- WEBSITE_BASE_URL
- CLERK_SYNC_KEY
- BATTALION_TIMEZONE
- VOICE_FLUSH_SECONDS

No additional Railway variable is required for the orders channel; the channel assignment is stored in PostgreSQL.
