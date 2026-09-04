# V60 — New Arrival Seven-Day Discord Role

- Adds a temporary **NEW ARRIVAL** Discord role to non-bot members on initial guild join.
- Preserves the original join clock through the existing `discord_members.joined_at` ledger, so leaving/rejoining does not restart the seven-day window when the original record remains.
- Automatically removes the role after 7 days.
- Runs a deployment/startup backfill that adds the role to all current non-bot members whose preserved Discord join date is less than 7 days old.
- Startup backfill is idempotent; members who already have the role are not duplicated.
- The recurring expiry watcher only removes expired roles and does not continually force the role back onto members after startup.
- Adds optional `NEW_ARRIVAL_ROLE_NAME` and `NEW_ARRIVAL_DAYS` environment settings; defaults are `NEW ARRIVAL` and `7`.
- The role is presentation-only and is separate from Replacement, Member, rank, formation, NCO, and staff roles.
