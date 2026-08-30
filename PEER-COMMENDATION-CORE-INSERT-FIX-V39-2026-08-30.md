# Peer Commendation Core Insert Fix V39

- Rebuilt `/commend` around one autocommitted INSERT using only the original V33 personnel_commendations columns.
- Removed service-history mirroring from the filing transaction. A secondary history/schema failure can no longer roll back a valid commendation.
- `/commend` no longer depends on the V36 Discord-identity columns to file or enforce cooldowns.
- Canonical Soldier lookup now uses website_member_links first and recruiting_cases only as fallback, avoiding unrelated optional organization schema.
- Optional Discord identity enrichment occurs only after the commendation is already committed.
- Startup schema setup isolates optional identity-column migration failures from the core table.
- Remaining failures report the exception class in Discord and full details in Railway logs.
