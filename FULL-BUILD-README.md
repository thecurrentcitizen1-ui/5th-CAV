# Battalion Clerk — FULL LATEST BUILD

This is the complete Discord bot package, not a patch.

Included cumulative edits:
- Discord/Railway bot startup configuration
- PostgreSQL collector
- database schema compatibility fixes
- Discord ID BIGINT compatibility fix
- voice-state tracking and recovery
- permanent duty-channel mappings
- /duty-channel
- /duty-channel-status
- /schedule
- /duty-status
- /close-duty
- /orders-channel
- /orders-channel-status
- event notification lifecycle
- Training / Operation / Meeting duty tracking
- 45-minute service-credit framework
- automatic internal Discord identity synchronization
- rank-role-driven 201 File creation
- no automatic PVT creation from voice presence
- /reset-roster clean rebuild command

Repository:
Upload this project to the 5th-CAV Discord bot GitHub repository.

Railway variables remain:
DISCORD_TOKEN
GUILD_ID
TEST_GUILD_ID
DATABASE_URL
WEBSITE_BASE_URL
CLERK_SYNC_KEY
BATTALION_TIMEZONE
VOICE_FLUSH_SECONDS

## Personnel Orders Discord Routing
Commands requiring Manage Server:
- `/personnel-orders-channel order_type channel` — route one order type to a channel.
- `/personnel-orders-status` — display current routes.
- `/personnel-orders-clear order_type` — disable one route.
Supported: ALL, REPLACEMENT, ASSIGNMENT, PROMOTION, AWARD, APPOINTMENT, LEAVE, RETURN.
The existing `/orders-channel` remains for scheduled Operations/Training/Meeting notices.

FULL FLOW / PERSONNEL PROCESSING PASS
- 30-second Discord role settle remains authoritative and restarts whenever roles change.
- Added validation holds for duplicate rank, MOS, company, platoon, or squad roles before a new 201 File can be created.
- Added /personnel-status, /personnel-reprocess, /personnel-health, and /reissue-login.
- Added secure website-backed Field Code rotation and private DM delivery for reissued Soldier Record access.
- Member departure from Discord now creates an S-1 disposition action instead of silently deleting history.
- Personnel order routing expanded to Separation, Tour Extension, Training, and Qualification documents.

## Deep Battalion Flow Commands
- `/operation-duty-channel` — assign the Discord channel for S-3 pre-operation duty rosters.
- `/operation-duty-status` — show the assigned duty-roster channel.
- `/publish-operation-duty` — immediately publish pending S-3 duty rosters.

Battalion Clerk mirrors the authoritative website personnel record after intake. The website personnel row owns active rank/MOS/assignment state; Discord roles are synchronized to it rather than becoming a second source of truth.

## Discord structure automation pass
Added owner/manage-server setup commands:
- `/setup-roles confirm:True` — creates/repairs divider roles, ranks, appointments, battlefield MOS roles, unit assignment roles, qualifications, and staff access roles.
- `/setup-channels confirm:True` — creates/repairs categories, text channels, voice channels, and category access scopes. Ensures roles first.
- `/battalion-setup confirm:True` — one-command complete server construction.
- `/structure-status` — read-only report of missing expected roles/categories/channels.
- `/structure-repair confirm:True` — idempotent repair; does not intentionally duplicate existing named items.

Blueprint includes Replacement Detachment, Battalion HQ, S-1, S-3, S-4, Battalion Command, and A/B/C Company areas. Divider roles are visual-only and personnel sync ignores them.

The bot must have Manage Roles + Manage Channels and its bot role must be above every role it needs to manage.
