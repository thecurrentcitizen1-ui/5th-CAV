# 5th Cavalry Battalion Clerk — Phase 9 Auto Personnel / Unranked Roster — FINAL

UPLOAD TO: 5th-CAV Discord bot GitHub repository

Behavior:
- Discord numeric ID is used internally to resolve a Soldier's 201 File.
- Members do not manually link Discord to the website.
- A Soldier may have a 201 File / Battle Roster Card with NO rank.
- Battalion Clerk does not create a default PVT rank.
- Rank is assigned only when the Soldier receives a recognized Discord rank role.
- Later role changes synchronize rank changes to the website.
- Duty-channel attendance automation remains enabled.
- Existing PostgreSQL schema/type compatibility fixes remain included.

Railway variables are unchanged.
