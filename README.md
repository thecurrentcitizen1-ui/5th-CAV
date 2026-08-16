# 5th Cavalry Battalion Clerk — Rank-Role Roster Reset

UPLOAD TO:
5th-CAV Discord bot GitHub repository

NEW BEHAVIOR
- New 201 Files are driven by recognized Discord RANK roles.
- Voice-channel presence does not create personnel.
- On startup/member-role update, Battalion Clerk sends the member's role list to the website.
- The website creates a Soldier only when it recognizes a rank role.

ONE-TIME COMMAND
/reset-roster confirmation:RESET ROSTER

The command:
1. Clears the current website personnel roster.
2. Preserves staff/admin accounts, unit structure, catalogs, events, channel assignments, and M16 serial-number inventory.
3. Immediately scans the Discord server.
4. Recreates 201 Files only for current rank-role holders.
5. Issues Battle Roster credentials and M16s to those newly created records.

This is a destructive personnel reset. Use it once for the clean restart.
