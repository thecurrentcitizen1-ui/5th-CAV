# Replacement Interview — Live Test Checklist

## Test A — Bot / Website Queue Health
1. In Discord, run `/recruit-intake-health` from an account with Manage Server.
2. Expected: `REPLACEMENT INTERVIEW PIPELINE — ONLINE`.
3. The command should show whether any manual interviews are pending.

## Test B — Direct Discord DM
1. Run `/test-recruit-intake member:@TestUser`.
2. Expected: TestUser receives the full Replacement Interview DM immediately.
3. This bypasses the Website queue. If this fails, the problem is Discord permissions/member resolution rather than the Website queue.

## Test C — Real Adopted Recruiting Case
1. Adopt a Discord newcomer into a Recruiting Case on the Website.
2. Open that case and click `SEND DISCORD INTAKE` or `RESEND DISCORD INTAKE`.
3. Within one 5-second poll cycle, refresh the case.
4. Expected Website state: `SENT — AWAITING RECRUIT` with a delivery-attempt timestamp.
5. Expected Discord state: recruit receives the Replacement Interview DM and `BEGIN / RESUME INTAKE` button.
6. Recruit clicks the button and completes Parts 1–3.
7. Expected Website state: same Recruiting Case is populated with the Discord intake answers and moves to Command review.

## Failure Isolation
- `/recruit-intake-health` fails: check Website URL / CLERK_SYNC_KEY / Railway connectivity.
- Health succeeds but direct DM fails: Discord DMs, member resolution, or bot permissions are the issue.
- Direct DM succeeds but real case remains queued: inspect the Recruiting Case delivery error/attempt fields and bot Railway logs.
- DM sends but button does not resume adopted case: verify Website V142 and Battalion Clerk V59 are both deployed together.
