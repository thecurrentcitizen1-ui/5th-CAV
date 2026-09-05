# Battalion Clerk V68 — Recruiting Interaction Privacy Repair

## Fix
- Part 1 completion response is now ephemeral/private to the applicant.
- Part 2 completion response is now ephemeral/private to the applicant.
- Audited the Discord-first recruiting flow: validation errors, resume prompts, recruiter selection, final submit status, and application-linking responses already use the recruiting ephemeral helper.
- Intended official recruiting/command notifications remain unchanged.

## Result
Intermediate application buttons and progress messages no longer clutter the channel where the recruiting interaction was opened.
