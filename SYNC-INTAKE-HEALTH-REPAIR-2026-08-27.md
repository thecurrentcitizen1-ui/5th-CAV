# Discord Intake Presence Reconciliation — 2026-08-27

- When an applicant is physically present in the Discord guild, Battalion Clerk now files joined=true with the website for the matched Recruiting Case.
- This clears historical OAuth/auto-join failures after the person joins manually.
- Applied to future on_member_join handling and /accessions-backfill retroactive processing.
- Existing website-authoritative personnel and recruiting workflows remain unchanged.
