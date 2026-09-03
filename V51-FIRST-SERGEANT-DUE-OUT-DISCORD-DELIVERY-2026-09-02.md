# V51 — First Sergeant Due-Out Discord Delivery

- Adds Battalion Clerk delivery for website-authoritative Company 1SG due-outs.
- New assignments are DM'd to the linked NCO Discord account within the one-minute watch cycle.
- The DM includes priority, due date, instructions, and a direct NCO Dashboard link.
- Incomplete due-outs receive a low-noise reminder when within 24 hours of suspense or overdue, at most once per 24 hours.
- Completion/status remains website-authoritative; Discord cannot close or alter a due-out.
- Delivery timestamps persist in PostgreSQL so bot restarts do not duplicate new-task messages.
