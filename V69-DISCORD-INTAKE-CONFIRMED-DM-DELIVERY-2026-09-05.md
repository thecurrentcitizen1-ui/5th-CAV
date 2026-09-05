# V69 — Discord Intake Confirmed DM Delivery

- Hardened the Website `SEND DISCORD INTAKE` delivery path in Battalion Clerk.
- The Website queue still remains authoritative for Command-requested intake delivery.
- Battalion Clerk first resolves the recruit as a guild Member, then falls back to the verified Discord user ID with `fetch_user()` when member cache/fetch resolution fails.
- The recruit receives the existing Replacement Interview DM with the persistent **BEGIN / RESUME INTAKE** button.
- Battalion Clerk does not report `sent=true` back to the Website until Discord returns a real message object/message ID for the DM.
- If Discord DMs are blocked, the user cannot be resolved, or Discord rejects the send, the Website remains unsent and receives a delivery error for Command visibility.
- This preserves V68 recruiting-interaction privacy behavior.
