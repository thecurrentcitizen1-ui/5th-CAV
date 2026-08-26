# Recruit / Onboarding Reliability Audit — 2026-08-25

## Unified flow
JOIN → APPLY → COMMAND REVIEW → REPORT FOR DUTY → WELCOME PACKET → PERMANENT ASSIGNMENT → MEMBER DASHBOARD

## Reliability rules verified in code
- Discord join offers BEGIN APPLICATION and I ALREADY APPLIED.
- Existing website case linking requires case number + verification code; it never guesses by display name.
- Existing Soldier Discord accounts are blocked from opening a second recruiting intake.
- Active Discord Recruiting Cases are reused rather than duplicated.
- Discord application drafts remain persisted in website database intake storage after each modal section.
- Approval still uses the existing personnel provisioning path, 201 File, Replacement Detachment, Discord sync, credential issue, and HLL identity linking.
- First website login routes through Report for Duty; a real Platoon assignment hands the Soldier into the standard member dashboard.
- Welcome Packet is six self-certification tasks plus two automatic identity checks.
- Existing legacy Welcome Packet tasks are retired, not deleted, preserving historical completion timestamps.
- Staff Recruiting Control can see the same recruit journey stages and open the exact Soldier assignment/action screen.

## Static validation
- Python compilation passed for Website and Battalion Clerk.
- 80 Website templates parsed with zero Jinja syntax errors.
- All new URL targets are present.
- Discord persistent component custom IDs have no duplicates.
- Welcome Packet definition verified as 6 SELF + 2 SYSTEM tasks.

## Production verification after deployment
Live Discord DM delivery, PostgreSQL state, Discord permissions, and external HLL identity verification require the deployed Railway/Discord environment. Use a test applicant to validate one full production accession before announcing the new flow publicly.
