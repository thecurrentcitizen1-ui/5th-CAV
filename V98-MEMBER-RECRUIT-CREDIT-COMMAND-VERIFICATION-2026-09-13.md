# Battalion Clerk V98 — Member Recruit Credit / Command Verification

Date: 2026-09-13
Baseline: V97
Website dependency: V301

## New member command
`/claim-recruit member:<Discord member> note:<optional>`

Purpose: allow an active 1/5 Cavalry member to report that they personally recruited a Discord member without allowing self-awarded credit.

## Behavior
- Must be run inside the battalion Discord.
- Target must be a real Discord member, not the claimant and not a bot.
- Website V301 verifies that the claimant's Discord identity maps to an active Soldier record.
- Battalion Clerk files the claim to the Website Command Desk.
- The command does not write recruiter ribbon credit directly and cannot approve its own claim.
- Command verifies or denies the claim on the Website.
- If verified before the recruit runs `/apply`, the claim waits for the recruit's Recruiting Case and attaches automatically.
- Credit becomes official only after the recruit reaches **ENLISTED** status, preserving the existing verified recruiter/ribbon rules.

## Recruiting language cleanup
The active Discord recruiting experience now uses **Discord Intake** and **Recruiting Case** language instead of directing members to a website application. `/apply` remains the intake command and `/link-game` remains the game-identity step.

Legacy command names such as `/application-status` and `/application-system-check` are retained for compatibility, but their visible responses now refer to the Discord intake / Recruiting Case system.

## V97 preserved
- `/schedule-nco-meeting`
- `/cancel-training`
- `/schedule-operation`

## Validation
- `bot.py`, `collector.py`, `config.py`, `database.py`, and `hllv_rcon.py`: Python compile PASS.
- `/claim-recruit`, `/schedule-nco-meeting`, `/cancel-training`, and `/schedule-operation`: present.
- Direct `@bot.tree.command` registrations in this build: 95, below Discord's 100 top-level command limit.
- `/claim-recruit` posts to the Website authority endpoint and contains no direct recruiter-credit database write.
- Self-credit protection: PASS.
- Command-verification messaging: PASS.

Deploy Website V301 before this bot package.
