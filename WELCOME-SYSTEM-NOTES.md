# Battalion Clerk — Welcome System

## New behavior
When a non-bot member joins the battalion Discord, Battalion Clerk posts the approved Replacement Personnel reporting notice in the configured welcome channel and mentions the new arrival.

The public welcome is separate from recruiting-case/OAuth DMs and does not replace recruiting status processing.

## Commands
- `/welcome-channel channel:#channel` — sets the public welcome channel.
- `/welcome-channel-status` — shows the active welcome channel.
- `/welcome-channel-clear` — clears the explicit setting. If `#welcome-to-the-1-5` exists, Battalion Clerk automatically uses it as the fallback.

## Approved message
The message ends with `BATTALION CLERK / 1/5 CAV`. No `GARRYOWEN` line is included.
