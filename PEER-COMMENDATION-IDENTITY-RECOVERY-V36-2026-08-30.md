# Peer Commendation Identity Recovery V36
- /commend now stores recipient_discord_user_id and giver_discord_user_id with every entry.
- Existing commendation rows are identity-backfilled from surviving website member links and recruiting cases.
- Added indexes for Discord identity lookups.
- Existing personnel UUID verification and anti-spam rules remain intact.
